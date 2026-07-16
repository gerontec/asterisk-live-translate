# Telegram Live-Übersetzungstelefon (DE → EN Echo)

A new frontend for this project: instead of a SIP/DID call into Asterisk, the
caller dials the bot **on Telegram**. The caller speaks **German**; after every
utterance they hear the **English** translation echoed back into the same call.

It reuses the existing inference engine unchanged — the Telegram side only adds
call signalling, a voice transport, and VAD segmentation.

## Architecture

```
Telegram user (speaks German)
        │  MTProto private (1-to-1) voice call
        ▼
 telegram_translate_bot.py            (venv_tgcall · Pyrogram 2.x userbot)
   ├── tgvoip_pyrogram  → call signalling (phone.requestCall / acceptCall, DH key)
   ├── libtgvoip (self-built) → Opus/SRTP voice transport, 48 kHz s16 mono
   ├── resample 48 kHz ⇄ 16 kHz       (everything is processed at 16 kHz)
   └── webrtcvad(2), 20 ms frames, 15 silence-frames end an utterance
        │  HTTP  →  inference_server.py  (127.0.0.1:9095)
        ▼
   /stt?lang=de   →  German text   (Whisper medium)
   /translate     →  de → en       (NLLB-200)
   /tts   lang=en →  English WAV    (Piper en_GB-alan-medium)
        │
        ▼  16 kHz → 48 kHz, queued into the call's send-callback
 Telegram user hears the English echo
```

The heavy models (Whisper / NLLB / Piper) live in `venv_py311` and are shared
with the Asterisk path via the HTTP inference server. The Telegram process runs
in its **own** venv (`venv_tgcall`) that carries the self-built `_tgvoip`
extension, `tgvoip_pyrogram`, and the realtime-audio deps — kept apart from the
GPU/model venv; the two only talk over the 9095 HTTP API.

`tgvoip_pyrogram` was written for **Pyrogram 1.x**, but Telegram now rejects
1.x logins (`406 UPDATE_APP_TO_LOGIN`), so the bot runs on **Pyrogram 2.x** and
shims the one renamed call it needs (`Client.send` → `Client.invoke`).

## Why a self-built voice stack

Answering **private** (1-to-1) Telegram calls is not supported by any released
Python library — `pytgcalls` only exposes group voice-chats; private-call code
exists only in unreleased dev branches. The one library purpose-built for
*answer → play/stream audio → record* is **`pytgvoip`**, which needs the C++
`libtgvoip` compiled locally.

`libtgvoip` was built from the `telegramdesktop/libtgvoip` submodule with:

* `TGVOIP_USE_CALLBACK_AUDIO_IO` — Python per-frame PCM callbacks (no ALSA device)
* `TGVOIP_NO_DSP` — the WebRTC audio-processing/AEC is stubbed out; there is no
  mic/speaker loop here, so acoustic echo-cancellation is irrelevant. This drops
  the entire 600-file WebRTC dependency.
* `WITHOUT_ALSA` — no PulseAudio/ALSA backends
* built-in OpenSSL crypto (`voip_crypto.cpp`), compiling cleanly against the
  host's **OpenSSL 3.5** (only deprecation warnings for `AES_ige_encrypt`).
* modern pip `pybind11` instead of the vendored 2019 copy (Python 3.11 opaque
  `PyFrameObject`).
* a GIL-acquire patch (`build_tgvoip.sh` applies it): the audio callbacks fire on
  libtgvoip's native thread, which doesn't hold the Python GIL — old pybind11
  tolerated this, pybind11 ≥3 aborts, so a `py::gil_scoped_acquire` is injected
  around each callback.

See `build_tgvoip.sh` for the exact recipe; it produces
`native/_tgvoip*.so` + `native/libtgvoip.a`.

## Two modes

* **Echo (incoming)** — `telegram_translate_bot.py`: answers incoming calls; the
  caller speaks German and hears English echoed back. One party. Demo/announce use.
* **Interpreter (outgoing, bidirectional)** — `telegram_interpreter.py`: the bot
  **calls any Telegram target** and you are a live party at the device holding it:

  ```
  [dein Mikro DE] → STT(de)→de→en→TTS(en) → Telegram → Partner hört EN
  [Partner EN]    → Telegram → STT(en)→en→de→TTS(de) → [dein Lautsprecher DE]
  ```

  Single call. Your German voice comes from the **local device mic**, the
  translated German is played on the **local speaker** (`--audio sounddevice`);
  `--audio files` streams a WAV in / writes one out for server-side logic tests.
  The symmetric `Translator` (VAD→STT→MT→TTS) is reused for both directions; the
  inference server already supports `de↔en` (Whisper multilingual, Piper `de`+`en`).

  Run: `telegram_interpreter.py <@user|id|phone> [--audio sounddevice]`

## Files

| File | Role |
|------|------|
| `telegram_translate_bot.py` | Echo mode: accepts calls, VAD, inference server, plays the echo. |
| `telegram_interpreter.py` | Interpreter mode: outgoing call, bidirectional DE↔EN, local mic/speaker. |
| `build_tgvoip.sh`           | One-shot native build of `libtgvoip` + the `_tgvoip` extension. |
| `voip_crypto.cpp`           | OpenSSL crypto glue for `libtgvoip` (`tgvoip::VoIPController::crypto`). |
| `login.py`                  | One-time interactive Pyrogram login → `telegram_translate.session`. |
| `native/`                   | Build output (`_tgvoip*.so`, `libtgvoip.a`) + vendored `tgvoip` package. |

## Setup

```bash
# 1. Build the native voice stack (once)
bash build_tgvoip.sh

# 2. One-time Telegram login (creates telegram_translate.session)
venv_tgcall/bin/python login.py      # prompts for phone number + login code

# 3. Run the bot
venv_tgcall/bin/python telegram_translate_bot.py
```

Then place a Telegram voice call to the logged-in account and speak German.

## Configuration (top of `telegram_translate_bot.py`)

| Setting | Default | Meaning |
|---------|---------|---------|
| `SRC_LANG` / `DST_LANG` | `de` / `en` | caller language → echo language |
| `SR_WORK` | `16000` | inference sample-rate (everything runs at 16 kHz) |
| `VAD_AGGR` | `2` | webrtcvad aggressiveness |
| `SILENCE_FR` | `15` | silence frames (~300 ms) that end an utterance |
| `MIN_SPEECH_FR` | `8` | ignore utterances shorter than ~160 ms |
| `GREETING_EN` | … | English greeting synthesised at call start |

## Notes & limits

* **Turn-based**, not simultaneous: the caller speaks, pauses, then hears the
  translation. A new utterance is ignored while the previous one is still being
  synthesised (`_busy`).
* Direction is fixed DE→EN by design (the requested feature). Swap `SRC_LANG` /
  `DST_LANG` for other pairs supported by the inference server.
* The API credentials are a Telegram *app* registration (userbot / MTProto), not
  a bot token — bots cannot place or receive calls.
