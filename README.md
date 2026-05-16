# Asterisk Live Translator

Real-time speech translation via Asterisk 22 AudioSocket.

An incoming call is transcribed with **Whisper STT**, translated with **Argostranslate**, and played back via **Piper TTS** — bidirectionally, during the live conversation.

## Architecture

```
SIP client / dus.net DID (+4980424967)
        │
   Asterisk 22
        │  AudioSocket (TCP 9093)
        ▼
 audiosocket_translator.py
   ├── Whisper medium (CUDA) — STT
   ├── Argostranslate         — DE↔IT, DE↔RU
   └── Piper TTS (local)      — speech synthesis
```

## Supported languages

| Suffix | Language |
|--------|----------|
| `39`   | Italian  |
| `99`   | Russian  |

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

## Files

| File | Purpose |
|------|---------|
| `audiosocket_translator.py` | Main process: AudioSocket server, STT/TRL/TTS pipeline |
| `notifyuuid.py` | AGI script: registers UUID+extension before AudioSocket starts |
| `loopback_call.py` | Test script: outbound loopback call via AMI |
| `asterisk_config.sh` | Save/apply Asterisk config (see below) |
| `start_as.sh` | Start script with CUDA environment |

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

First live end-to-end test — 2026-05-16, bidirectional DE↔IT over dus.net DID:

| Utterance | STT | Translation | TTS | Total |
|-----------|-----|-------------|-----|-------|
| "Ende 234." | 0.66 s | 0.41 s | 0.05 s | 1.12 s |
| "Das ist ziemlich perfekt." | 0.64 s | 0.03 s | 0.03 s | 0.70 s |

GPU during inference: **0% utilization** (Whisper medium completes faster than the 60 s pynvml polling interval).  
VRAM: 1128 / 7680 MiB — Temperature: 68–69 °C — Power: 25–27 W.

## Requirements

- Python 3.11, CUDA-capable GPU
- `faster-whisper`, `piper-tts`, `argostranslate`, `webrtcvad`, `scipy`, `soundfile`
- Asterisk 22 with `app_audiosocket`, `res_pjsip`
- Piper models in `piper_models/` (`.onnx` + `.onnx.json`)
