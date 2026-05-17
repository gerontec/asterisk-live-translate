# SIP Translator — Projektdokumentation

## Dienste (systemd)

| Service | Datei | Beschreibung |
|---|---|---|
| `audiosocket-translator.service` | `audiosocket_translator.py` | Live-Übersetzung via AudioSocket (DE↔IT) |
| `sip-translator.service` | `translator.py` | SIP Translation B2BUA |
| `asterisk.service` | — | Asterisk PBX |

Neustart nach Code-Änderung:
```bash
sudo systemctl restart audiosocket-translator
sudo systemctl restart sip-translator
```

## Bot-Call-Test (`test_bot_call.py`)

Ruft eine Nummer an, stellt 3 Fragen auf Deutsch (übersetzt aus IT), nimmt Antworten auf und liefert das Ergebnis als IT-TTS-MP3.

### Ablauf
1. IT-Fragen → DE-TTS-WAVs generieren (Piper)
2. FastAGI-Server starten (Port 4573)
3. AMI Originate → Asterisk verbindet sich per AGI
4. AGI steuert: Frage abspielen → `RECORD FILE … s=1` (1s Stille = Satzende)
5. Nach Anruf: Whisper NLU → Übersetzung → Piper TTS → MP3
6. Transkript + MP3 nach `/var/www/web1/`

### Wichtige Parameter
```python
SILENCE_SECS = 1   # Sekunden Stille bis Aufnahme stoppt
AGI_PORT     = 4573
```

### Latenz (Tesla P4, Whisper medium, NLLB 1.3B)
| Phase | Zeit |
|---|---|
| VAD Stille-Erkennung | 300ms |
| Whisper NLU (fix, unabhängig von Satzlänge) | ~1100ms |
| NLLB Übersetzung | ~700ms |
| Piper TTS | ~80ms |
| **Gesamt** | **~2200ms** |

## Live-Translator (`audiosocket_translator.py`)

### VAD-Parameter
```python
FRAME_MS   = 20    # ms pro VAD-Frame
SILENCE_FR = 15    # 15 × 20ms = 300ms Stille → Segment abschicken
SPEECH_MIN = 8     # mind. 160ms echte Sprache
```

### Optimierungen (2026-05-17)
- **Parallelisierung**: Bei Sätzen mit mehreren Chunks wird die Übersetzung von Chunk N+1
  als `asyncio.Task` gestartet während Chunk N abgespielt wird (`as_write_audio` ist
  Echtzeit-gepaced → GPU liegt idle → kostenlose Überlappung).
- **Closure-Fix**: `lambda ch=chunk:` statt `lambda:` im Loop.

### Whisper-Modell
```python
WhisperModel("medium", device="cuda", compute_type="int8")
```
Kleineres Modell (`small`) würde ~400ms sparen, Genauigkeit sinkt.

## Fachvokabular-Hinweis (NLLB)
Das NLLB-Modell kennt keine Seilbahn-Fachbegriffe ohne Kontext:
- `"Zugseil"` → falscht (`"Legno di trazione"`)
- `"Zugseil der Seilbahn"` → korrekt (`"Cavo di trazione di una funivia"`)
- Immer mit Kontext formulieren für Fachtermini.
