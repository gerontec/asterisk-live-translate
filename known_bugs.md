# AudioSocket Translator — Testaufbau & Echo-Delay

## Testaufbau

```
Handy (DE-Sprecher)
  │  SIP/RTP
  ▼
Fritz!Box (SIP-Trunk)
  │  PJSIP fritzbox-out
  ▼
Asterisk PBX
  │  Dialplan: from-internal
  │  AGI: notifyuuid.py  → POST uuid+exten → Translator:9094/register
  │  AudioSocket(uuid, 127.0.0.1:9093)
  ▼
audiosocket_translator.py
  │
  ├─ SpeechBuffer (webrtcvad, VAD-Aggressivität 2)
  │    SILENCE_FR = 25 Frames × 20ms = 500ms Stille → Utterance fertig
  │    SPEECH_MIN = 8 Frames = 160ms Mindestlänge
  │
  ├─ faster-whisper (CUDA)
  │    language=de, beam_size=5, word_timestamps=True
  │    no_speech_threshold=0.6, log_prob_threshold=-1.0
  │
  ├─ Argostranslate (offline, zweistufig DE→EN→IT)
  │
  └─ edge-TTS (cloud, de-DE-ConradNeural / it-IT-DiegoNeural)
       → as_write_audio(): paced 20ms/Frame (AudioSocket → Asterisk → Fritz!Box → Handy)
```

**Loopback-Modus** (`TRANSLATOR_LOOPBACK=1`): kein Outbound-Dial, kein Outbound-Worker;
nur der Inbound-Worker DE→IT übersetzt und spielt TTS auf denselben Kanal zurück.

### Teststart

```bash
# Translator neu starten (nur wenn Code geändert):
kill $(pgrep -f audiosocket_translator) 2>/dev/null; sleep 1
TRANSLATOR_LOOPBACK=1 /home/gh/python/translator/start_as.sh \
  > /tmp/translator_loopN.log 2>&1 &
tail -f /tmp/translator_loopN.log   # warten bis "AudioSocket-Translator lauscht"

# Anruf auslösen:
/home/gh/python/venv_py311/bin/python3 \
  /home/gh/python/translator/loopback_call.py \
  > /tmp/loopbackN.log 2>&1 &
tail -f /tmp/loopbackN.log
```

---

## Delay-Problem: Zu lange Pause vor dem IT-Echo

**Symptom:** User spricht Deutsch, hört das übersetzte Italienisch mit wahrnehmbarer Verzögerung.

### Gemessene Latenzen (Test 2026-05-15 22:36, 7 Segmente)

| Phase | Dauer |
|-------|-------|
| VAD Stille-Hangover | **500ms** (SILENCE_FR=25 × 20ms) |
| Whisper STT (CUDA) | 0.60–0.76s |
| Argostranslate | 0.02–0.35s |
| edge-TTS (normal) | 0.40–0.52s |
| edge-TTS (Spike!) | **11.45s** (Seg 2: Netzwerk-Ausreißer) |

**Aktuelle Gesamtlatenz** (Ende Sprechen → erste IT-Samples): ~**1.0–1.2s**
(300ms VAD + 600ms STT + 35ms TRL + 40ms TTS)

### Durchgeführte Optimierungen (2026-05-15)

| Maßnahme | Vorher | Nachher |
|----------|--------|---------|
| `SILENCE_FR` 25 → 15 | 500ms VAD-Hangover | 300ms |
| Piper TTS (lokal) statt edge-TTS | 400–500ms, Spikes bis 11.45s | 30–50ms, keine Spikes |
| Sentence-Chunk-Streaming (`word_timestamps=True`) | Warten auf ganzes Segment | Erste Chunks sofort |

Piper-Modelle: `it_IT-paola-medium.onnx`, `de_DE-thorsten-medium.onnx` (je 61MB, lokal in `piper_models/`).

### Verbleibendes Bottleneck: Whisper STT (~600ms)

Whisper medium (CUDA, int8) ist nicht weiter reduzierbar ohne Qualitätsverlust.
`beam_size=1` oder Modellwechsel auf `small` würden ~200ms sparen — bewusst nicht umgesetzt,
da 1.2s als akzeptabel gilt und Transkriptionsqualität wichtiger ist.
