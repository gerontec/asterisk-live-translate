# AudioSocket Translator — Test Setup & Echo Delay

## Test Setup

```
Mobile phone (DE speaker)
  │  SIP/RTP
  ▼
Fritz!Box (SIP trunk)
  │  PJSIP fritzbox-out
  ▼
Asterisk PBX
  │  Dialplan: from-internal
  │  AGI: notifyuuid.py  → POST uuid+exten → Translator:9094/register
  │  AudioSocket(uuid, 127.0.0.1:9093)
  ▼
audiosocket_translator.py
  │
  ├─ SpeechBuffer (webrtcvad, VAD aggressiveness 2)
  │    SILENCE_FR = 25 frames × 20 ms = 500 ms silence → utterance complete
  │    SPEECH_MIN = 8 frames = 160 ms minimum length
  │
  ├─ faster-whisper (CUDA)
  │    language=de, beam_size=5, word_timestamps=True
  │    no_speech_threshold=0.6, log_prob_threshold=-1.0
  │
  ├─ Argostranslate (offline, two-stage DE→EN→IT)
  │
  └─ edge-TTS (cloud, de-DE-ConradNeural / it-IT-DiegoNeural)
       → as_write_audio(): paced 20 ms/frame (AudioSocket → Asterisk → Fritz!Box → phone)
```

**Loopback mode** (`TRANSLATOR_LOOPBACK=1`): no outbound dial, no outbound worker;
only the inbound worker translates DE→IT and plays TTS back on the same channel.

### Test start

```bash
# Restart translator (only if code changed):
kill $(pgrep -f audiosocket_translator) 2>/dev/null; sleep 1
TRANSLATOR_LOOPBACK=1 /home/gh/python/translator/start_as.sh \
  > /tmp/translator_loopN.log 2>&1 &
tail -f /tmp/translator_loopN.log   # wait until "AudioSocket-Translator lauscht"

# Trigger call:
/home/gh/python/venv_py311/bin/python3 \
  /home/gh/python/translator/loopback_call.py \
  > /tmp/loopbackN.log 2>&1 &
tail -f /tmp/loopbackN.log
```

---

## Delay Issue: Too Long a Pause Before the IT Echo

**Symptom:** User speaks German, hears the translated Italian with a noticeable delay.

### Measured Latencies (Test 2026-05-15 22:36, 7 segments)

| Phase | Duration |
|-------|-------|
| VAD silence hangover | **500 ms** (SILENCE_FR=25 × 20 ms) |
| Whisper STT (CUDA) | 0.60–0.76 s |
| Argostranslate | 0.02–0.35 s |
| edge-TTS (normal) | 0.40–0.52 s |
| edge-TTS (spike!) | **11.45 s** (seg 2: network outlier) |

**Current total latency** (end of speech → first IT samples): ~**1.0–1.2 s**
(300 ms VAD + 600 ms STT + 35 ms TRL + 40 ms TTS)

### Optimizations Applied (2026-05-15)

| Measure | Before | After |
|----------|--------|---------|
| `SILENCE_FR` 25 → 15 | 500 ms VAD hangover | 300 ms |
| Piper TTS (local) instead of edge-TTS | 400–500 ms, spikes up to 11.45 s | 30–50 ms, no spikes |
| Sentence-chunk streaming (`word_timestamps=True`) | Wait for full segment | First chunks immediately |

Piper models: `it_IT-paola-medium.onnx`, `de_DE-thorsten-medium.onnx` (61 MB each, local in `piper_models/`).

### Remaining Bottleneck: Whisper STT (~600 ms)

Whisper medium (CUDA, int8) cannot be reduced further without quality loss.
`beam_size=1` or switching to the `small` model would save ~200 ms — deliberately not applied,
as 1.2 s is considered acceptable and transcription quality takes priority.
