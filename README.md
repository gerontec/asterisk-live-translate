# Asterisk Live Translator

Echtzeit-Sprachübersetzung über Asterisk 22 AudioSocket.

Ein Anruf wird per **Whisper STT** transkribiert, mit **Argostranslate** übersetzt und als **Piper TTS** zurückgespielt — bidirektional, im laufenden Gespräch.

## Architektur

```
SIP-Client / dus.net DID
        │
   Asterisk 22
        │  AudioSocket (TCP 9093)
        ▼
 audiosocket_translator.py
   ├── Whisper medium (CUDA) — STT
   ├── Argostranslate          — DE↔IT, DE↔RU
   └── Piper TTS (lokal)       — Sprachausgabe
```

## Unterstützte Sprachen

| Nachwahl | Sprache   |
|----------|-----------|
| `39`     | Italienisch |
| `99`     | Russisch    |

## Anrufmodi

### 1. Loopback (Übersetzungstest)

Anruf auf die eigene DID `+4980424967`, nach dem Beep Sprachcode drücken:

```
+4980424967 → Beep → DTMF 39 → Deutsch sprechen → Italienisch hören
```

Oder intern per SIP (zweistellige Nebenstelle + Sprachcode):

```
5039  → Deutsch sprechen → Italienisch hören
5099  → Deutsch sprechen → Russisch hören
```

### 2. Outbound-Brücke

Nummer mit angehängtem Sprachcode wählen:

```
+491762XXXXXX39  → Anruf auf +491762XXXXXX, bidirektionale DE↔IT Übersetzung
```

## Dateien

| Datei | Funktion |
|-------|----------|
| `audiosocket_translator.py` | Hauptprozess: AudioSocket-Server, STT/TRL/TTS |
| `notifyuuid.py` | AGI-Skript: registriert UUID+Exten vor AudioSocket-Start |
| `loopback_call.py` | Testskript: ausgehender Loopback-Anruf via AMI |
| `start_as.sh` | Startskript mit CUDA-Umgebung |

## Asterisk-Konfiguration

**`/etc/asterisk/extensions_translator.conf`** — Dialplan-Contexts:

- `[from-internal]` — interne SIP-Clients (`_+X.`, `_XX39`, `_XX99`)
- `[from-dusnet]` — eingehend von dus.net DID (DTMF-Sprachauswahl)
- `[audiosocket-out]` — Outbound-Leg der Übersetzungsbrücke

**`/etc/asterisk/pjsip.conf`** — Trunks:

- `fritzbox` — Fritz!Box als SIP-Client (eingehend)
- `fritzbox-out` — Fritz!Box als Ausgangs-Trunk
- `dusnet-trunk` — dus.net DID +4980424967 (IPv6, `sip.dus.net`)
- `linphone` — lokales Softphone (Testclient)

## Service

```bash
# Status
systemctl status audiosocket-translator

# Logs live
journalctl -fu audiosocket-translator

# Neustart
systemctl restart audiosocket-translator
```

Startet automatisch nach Absturz (max. 5 Versuche / 2 Minuten).

## Voraussetzungen

- Python 3.11, CUDA-fähige GPU
- `faster-whisper`, `piper-tts`, `argostranslate`, `webrtcvad`, `scipy`, `soundfile`
- Asterisk 22 mit `app_audiosocket`, `res_pjsip`
- Piper-Modelle in `piper_models/` (`.onnx` + `.onnx.json`)
