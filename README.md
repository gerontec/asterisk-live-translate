# Asterisk Live Translator

Real-time speech translation via Asterisk 22 AudioSocket.

An incoming call is transcribed with **Whisper STT**, translated with **NLLB-200**, and played back via **Piper TTS** — bidirectionally, during the live conversation.

## Architecture

```
SIP client / dus.net DID (+4980424967)
        │
   Asterisk 22
        │  AudioSocket (TCP 9093)
        ▼
 audiosocket_translator.py
   ├── Whisper medium (CUDA)              — STT
   ├── NLLB-200-distilled-1.3B (CUDA)    — direct multilingual translation
   └── Piper TTS (local)                  — speech synthesis
```

## Supported languages

Dial suffix corresponds to the E.164 country code of the target language:

| Suffix | Language   | Piper voice               |
|--------|------------|---------------------------|
| `1`    | English    | en_GB-alan-medium         |
| `7`    | Russian    | ru_RU-dmitri-medium       |
| `30`   | Greek      | el_GR-rapunzelina-medium  |
| `33`   | French     | fr_FR-siwis-medium        |
| `34`   | Spanish    | es_ES-davefx-medium       |
| `38`   | Ukrainian  | uk_UA-ukrainian_tts-medium|
| `39`   | Italian    | it_IT-paola-medium        |
| `44`   | English    | en_GB-alan-medium         |
| `48`   | Polish     | pl_PL-darkman-medium      |
| `55`   | Portuguese | pt_BR-faber-medium        |
| `77`   | Kazakh     | kk_KZ-issai-high          |
| `86`   | Chinese    | zh_CN-huayan-medium       |
| `90`   | Turkish    | tr_TR-dfki-medium         |
| `91`   | Hindi      | hi_IN-rohan-medium        |
| `98`   | Persian    | fa_IR-amir-medium         |
| `995`  | Georgian   | ka_GE-natia-medium        |

The German (DE) side always uses `de_DE-thorsten-medium`. Suffixes `1` and `44` both map to English.

### Direct language pair matrix

`facebook/nllb-200-distilled-1.3B` is a direct multilingual model — all 120 pairs below are translated in a single inference step, with no bridge language.

|              | DE | EN | FR | IT | RU | ES | EL | PL | PT | UK | KK | ZH | TR | HI | FA | KA |
|:-------------|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **DE** Deutsch    | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **EN** English    | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **FR** Français   | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **IT** Italiano   | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **RU** Русский    | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **ES** Español    | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **EL** Ελληνικά  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **PL** Polski     | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **PT** Português  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **UK** Українська | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **KK** Қазақша   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  | ✓  |
| **ZH** 中文       | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  | ✓  |
| **TR** Türkçe     | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  | ✓  |
| **HI** हिन्दी    | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  | ✓  |
| **FA** فارسی      | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  | ✓  |
| **KA** ქართული   | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | ✓  | —  |

## Call modes

### 1. Inbound via DID (primary mode)

Call `+4980424967`, wait for the beep, then dial the destination number followed by the language suffix and `#`:

```
+4980424967 → beep → 01762525787839# → translated call to 017625257878 (DE↔IT)
```

Format: `<national number><lang suffix>#`  — e.g. `01762525787839#` for Italian.

### 2. Loopback test (no outbound call)

Internal SIP extension (2-digit extension + language suffix):

```
5039  → speak German → hear Italian
5099  → speak German → hear Russian
```

### 3. Fritz!Box / internal SIP client (direct outbound)

Endpoint `523523` routes directly via `fritzbox-out` without translation.
For translated calls from an internal client, dial via the DID (mode 1).

## Documentation

| Document | Contents |
|----------|----------|
| `TranslateServicePaths.md` | Architecture, routing, echo extensions per language |
| `SipE2eTest.md` | End-to-end test bot and latency measurement |
| `AudioSocket16k.md` | **Patching Asterisk's AudioSocket to 16 kHz** — required, not upstream |
| `TelegramPhone.md` | Telegram frontend |

## Files

| File | Purpose |
|------|---------|
| `audiosocket_translator.py` | Main process: AudioSocket server, STT/TRL/TTS pipeline |
| `notifyuuid.py` | AGI script: registers UUID+extension before AudioSocket starts |
| `loopback_call.py` | Test script: outbound loopback call via AMI |
| `asterisk_config.sh` | Save/apply Asterisk config (see below) |
| `start_as.sh` | Start script with CUDA environment |
| `sip_e2e_test.py` | End-to-end latency test: real SIP client → echo extension → wagodb (see `SipE2eTest.md`) |

## Asterisk configuration

**`asterisk/extensions_translator.conf`** — Dialplan contexts:

| Context | Purpose |
|---------|---------|
| `from-dusnet` | Inbound from dus.net DID — reads full number+suffix via DTMF |
| `from-internal` | Internal SIP clients — waits for DTMF language code after answer |
| `from-fritzbox` | Fritz!Box endpoint — direct outbound, no translation |
| `audiosocket-out` | Outbound leg of translation bridge |
| `translator-interactive` | Interactive mode: dial `*39`/`*99`, then enter number |

**`asterisk/pjsip.conf`** — Trunks and endpoints:

| Name | Purpose |
|------|---------|
| `fritzbox` | Fritz!Box as SIP client (inbound) |
| `fritzbox-out` | Fritz!Box as outbound trunk |
| `dusnet-trunk` | dus.net DID +4980424967 (IPv6, `sip.dus.net`) |
| `523523` | Fritz!Box SIP PBX connection (IP trust, no auth) |
| `linphone` | Local softphone (test client) |

### Managing Asterisk configuration

```bash
# Save /etc/asterisk → asterisk/ (passwords replaced with CHANGEME)
./asterisk_config.sh save

# Apply asterisk/ → /etc/asterisk (passwords from .asterisk-secrets) + reload
./asterisk_config.sh apply
```

Copy `.asterisk-secrets.example` to `.asterisk-secrets` and fill in passwords before first `apply`.

## Service

```bash
systemctl status audiosocket-translator
journalctl -fu audiosocket-translator
systemctl restart audiosocket-translator
```

Auto-restarts on failure (max 5 attempts / 2 minutes).

## Performance (Tesla P4, Whisper medium, CUDA)

### Translation latency — NLLB-200-distilled-1.3B (benchmark 2026-05-17)

| Sentence length | Median | Min | Max |
|-----------------|--------|-----|-----|
| Short (1–3 words) | 400 ms | 316 ms | 440 ms |
| Medium (4–7 words) | 441 ms | 400 ms | 565 ms |
| Long (8–13 words) | 671 ms | 650 ms | 695 ms |

No cold-start effect — model stays resident in VRAM, latency is constant from the first call.

### End-to-end pipeline

| Component | Median | Note |
|-----------|--------|------|
| VAD hangover | 300 ms | SILENCE_FR=15 × 20 ms, fixed |
| Whisper STT | 600 ms | medium, CUDA int8 |
| NLLB-200 translation | 400–670 ms | depends on sentence length |
| Piper TTS | 40 ms | local ONNX |
| **Total** | **~1.35–1.6 s** | typical phone utterance |

VRAM: Whisper medium ~500 MB + NLLB-1.3B 2618 MB = ~3.1 GB / 7680 MB.  
GPU utilization: **0%** between inferences (polling interval 60 s).  
Temperature: 68–69 °C — Power: 25–27 W.

## Model installation

### Whisper (STT)

`faster-whisper` downloads the model automatically on first start. No manual step required.
The `medium` model is used; it is stored in `~/.cache/huggingface/hub/`.

### NLLB-200 (translation)

`facebook/nllb-200-distilled-1.3B` is downloaded automatically by `load_models()` on first start via Hugging Face Hub into `nllb_cache/`. No manual step required.

### Piper TTS (speech synthesis)

Piper models are **not** downloaded automatically. Place `.onnx` and `.onnx.json` files in `piper_models/`.

**Single model** — download from Hugging Face:

```bash
cd piper_models
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main"
VOICE="de_DE/thorsten/medium"
MODEL="de_DE-thorsten-medium"
curl -LO "$BASE/$VOICE/$MODEL.onnx"
curl -LO "$BASE/$VOICE/$MODEL.onnx.json"
```

**All models at once** — use the bulk download script below. It reads the official `voices.json` index
and skips models that are already present:

```python
#!/usr/bin/env python3
"""Download all Piper medium-quality voices (plus kk_KZ-issai-high)."""
import json, urllib.request, pathlib, sys

OUTDIR = pathlib.Path("piper_models")
OUTDIR.mkdir(exist_ok=True)

INDEX = "https://huggingface.co/rhasspy/piper-voices/resolve/main/voices.json"
BASE  = "https://huggingface.co/rhasspy/piper-voices/resolve/main"

with urllib.request.urlopen(INDEX) as r:
    voices = json.load(r)

ok = skip = err = 0
for name, info in voices.items():
    quality = info.get("quality", "")
    # Download medium quality for all languages; also grab kk_KZ high (no medium available)
    if quality not in ("medium",) and name != "kk_KZ-issai-high":
        print(f"[SKIP] {name}")
        skip += 1
        continue
    for fname, meta in info.get("files", {}).items():
        dest = OUTDIR / pathlib.Path(fname).name
        if dest.exists():
            continue
        url = f"{BASE}/{fname}"
        print(f"[DL]   {dest.name}")
        try:
            urllib.request.urlretrieve(url, dest)
            ok += 1
        except Exception as e:
            print(f"  ERROR {dest.name}: {e}", file=sys.stderr)
            err += 1

print(f"Done: {ok} downloaded, {skip} skipped, {err} errors")
```

Save as e.g. `download_piper_models.py` and run from the repo root:

```bash
python3 download_piper_models.py
```

The `piper_models/` directory is gitignored (models are large binary files).
Voice names used by this project are configured in `PIPER_VOICES` in `audiosocket_translator.py`.

## Requirements

- Python 3.11, CUDA-capable GPU
- `faster-whisper`, `piper-tts`, `transformers`, `webrtcvad`, `scipy`, `soundfile`
- Asterisk 22 with `app_audiosocket`, `res_pjsip`
- Piper models in `piper_models/` (`.onnx` + `.onnx.json`)
