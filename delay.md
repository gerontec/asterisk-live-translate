# Delay-Messung — AudioSocket Translator

## Benchmark 2026-05-17 — NLLB-200-distilled-1.3B (Tesla P4, CUDA fp16)

Skript: `bench_nllb.py` — reines Translations-Timing, kein STT/TTS.

### Latenz nach Satzlänge (DE→IT, n=12–18 Läufe je Bucket)

| Bucket | Wortanzahl | Median | Min | Max |
|--------|-----------|--------|-----|-----|
| short  | 1–3 Wörter | 400 ms | 316 ms | 440 ms |
| medium | 4–7 Wörter | 441 ms | 400 ms | 565 ms |
| long   | 8–13 Wörter | 671 ms | 650 ms | 695 ms |

### Latenz nach Sprachpaar (medium-Sätze, 3 Läufe je Paar)

| Paar | Median | Min | Max | Beispiel |
|------|--------|-----|-----|---------|
| DE→IT | 525 ms | 400 ms | 565 ms | "Hai chiamato la nonna?" |
| DE→EN | 525 ms | 400 ms | 565 ms | "Did you call Grandma?" |
| DE→RU | 441 ms | 441 ms | 484 ms | "Ты позвонил бабушке?" |
| DE→ZH | 566 ms | 442 ms | 566 ms | "你打电话给了吗?" |
| IT→DE | 442 ms | 400 ms | 526 ms | "Hast du Oma angerufen?" |
| EN→RU | 524 ms | 485 ms | 524 ms | "Ты злишься на Ому?" |
| DE→KA | 567 ms | 441 ms | 606 ms | "ბებიას დაურეკე?" |
| DE→KK | 566 ms | 400 ms | 606 ms | "Сен әжеге қоңырау шалдың ба?" |

### Cold-start vs. warm

Kein relevanter Unterschied — CUDA-Modell bleibt geladen, keine Warmup-Phase nötig.
`torch.cuda.empty_cache()` zwischen Läufen: konstant **401 ms**.
10 aufeinanderfolgende Warm-Läufe: konstant **400–401 ms**.

### Systemwerte

| | Wert |
|-|-----|
| Modell-Ladezeit | 3.6 s |
| VRAM NLLB-1.3B | 2618 MB |
| GPU (Tesla P4) | fp16 |

### Gesamt-Pipeline mit NLLB vs. Argostranslate

| Komponente | Argostranslate (alt) | NLLB-1.3B (neu) |
|------------|---------------------|-----------------|
| VAD-Hangover | 300 ms | 300 ms |
| Whisper STT | 600 ms median | 600 ms median |
| Übersetzung | 30 ms (warm) | 400–670 ms |
| Piper TTS | 40 ms | 40 ms |
| **Gesamt** | **~1.0 s** | **~1.35–1.6 s** |

Mehraufwand NLLB: **+350–630 ms** pro Segment — direkte Übersetzung ohne Bridge,
erheblich bessere Qualität, alle 120 Sprachpaare ohne Umweg über Englisch.

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
