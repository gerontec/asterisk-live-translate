# Delay-Messung — AudioSocket Translator

## Test 2026-05-15 22:56 (Piper TTS, SILENCE_FR=15)

Pipeline pro Segment: **VAD-Hangover → STT → TRL → TTS → erste Audio-Samples**

```
Seg  Text (DE)                              VAD   STT    TRL    TTS    Σ
──────────────────────────────────────────────────────────────────────────
 1   "Das war's für heute. Bis zum…"  ⚠HAL  300ms  0.77s  0.47s  0.07s  1.61s
 2   "Vielen Dank für's Zuschauen."   ⚠HAL  300ms  0.67s  0.03s  0.04s  1.04s
 3   "Entdeutsch."                    ⚠STT  300ms  2.45s  0.03s  0.03s  2.81s
 4   "Eins, zwei, drei, vier."              300ms  0.64s  0.03s  0.04s  1.01s
 5   "5678"                                 300ms  0.57s  0.02s  0.05s  0.94s
 6   "21, 22, 23"                           300ms  0.60s  0.03s  0.04s  0.97s
 7   "Auf Wiedersehen."                     300ms  0.58s  0.03s  0.03s  0.94s
 8   "Und wann kommst du morgen früh…"      300ms  0.67s  0.06s  0.05s  1.08s
 9   "Sehr gut."                            300ms  0.57s  0.03s  0.03s  0.93s
──────────────────────────────────────────────────────────────────────────
     Median (echte Segmente 4–9)            300ms  0.60s  0.03s  0.04s  ~1.0s
```

⚠HAL = Whisper-Halluzination (nie gesprochen)
⚠STT = Whisper-Fehlerkennung (kurzes/unklares Wort → 4× längere STT-Zeit)

## Komponenten-Breakdown

| Komponente | Median | Min | Max | Anmerkung |
|------------|--------|-----|-----|-----------|
| VAD-Hangover | 300ms | 300ms | 300ms | SILENCE_FR=15 × 20ms, fix |
| Whisper STT | 600ms | 570ms | 770ms | medium CUDA int8; Outlier 2.45s bei kurzen Wörtern |
| Argostranslate | 30ms | 20ms | 60ms | DE→EN→IT zweistufig, nach Warmup |
| Piper TTS | 40ms | 30ms | 50ms | lokal ONNX, kein Netzwerk |
| **Gesamt** | **~1.0s** | 0.93s | 1.08s | ohne Halluzinationen |

## Vergleich edge-TTS vs. Piper (selbe Pipeline)

| | edge-TTS (vorher) | Piper (jetzt) |
|-|-------------------|---------------|
| TTS-Latenz normal | 400–500ms | 30–50ms |
| TTS-Latenz Spike | bis 11.45s | kein Spike |
| Gesamt-Delay | ~1.8s | ~1.0s |
