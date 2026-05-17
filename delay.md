# Latency Measurement — AudioSocket Translator

## Benchmark 2026-05-17 — NLLB-200-distilled-1.3B (Tesla P4, CUDA fp16)

Script: `bench_nllb.py` — pure translation timing, no STT/TTS.

### Latency by Sentence Length (DE→IT, n=12–18 runs per bucket)

| Bucket | Word count | Median | Min | Max |
|--------|-----------|--------|-----|-----|
| short  | 1–3 words | 400 ms | 316 ms | 440 ms |
| medium | 4–7 words | 441 ms | 400 ms | 565 ms |
| long   | 8–13 words | 671 ms | 650 ms | 695 ms |

### Latency by Language Pair (medium sentences, 3 runs per pair)

| Pair | Median | Min | Max | Example |
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

No relevant difference — the CUDA model stays loaded, no warm-up phase needed.
`torch.cuda.empty_cache()` between runs: constant **401 ms**.
10 consecutive warm runs: constant **400–401 ms**.

### System metrics

| | Value |
|-|-----|
| Model load time | 3.6 s |
| VRAM NLLB-1.3B | 2618 MB |
| GPU (Tesla P4) | fp16 |

### Full Pipeline: NLLB vs. Argostranslate

| Component | Argostranslate (old) | NLLB-1.3B (new) |
|------------|---------------------|-----------------|
| VAD hangover | 300 ms | 300 ms |
| Whisper STT | 600 ms median | 600 ms median |
| Translation | 30 ms (warm) | 400–670 ms |
| Piper TTS | 40 ms | 40 ms |
| **Total** | **~1.0 s** | **~1.35–1.6 s** |

NLLB overhead: **+350–630 ms** per segment — direct translation without a bridge language,
significantly better quality, all 120 language pairs without routing through English.

## Test 2026-05-15 22:56 (Piper TTS, SILENCE_FR=15)

Pipeline per segment: **VAD hangover → STT → TRL → TTS → first audio samples**

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
     Median (real segments 4–9)             300ms  0.60s  0.03s  0.04s  ~1.0s
```

⚠HAL = Whisper hallucination (never spoken)  
⚠STT = Whisper misrecognition (short/unclear word → 4× longer STT time)

## Component Breakdown

| Component | Median | Min | Max | Note |
|------------|--------|-----|-----|-----------|
| VAD hangover | 300 ms | 300 ms | 300 ms | SILENCE_FR=15 × 20 ms, fixed |
| Whisper STT | 600 ms | 570 ms | 770 ms | medium CUDA int8; outlier 2.45 s on short words |
| Argostranslate | 30 ms | 20 ms | 60 ms | DE→EN→IT two-stage, after warm-up |
| Piper TTS | 40 ms | 30 ms | 50 ms | local ONNX, no network |
| **Total** | **~1.0 s** | 0.93 s | 1.08 s | excluding hallucinations |

## edge-TTS vs. Piper comparison (same pipeline)

| | edge-TTS (before) | Piper (now) |
|-|-------------------|---------------|
| TTS latency normal | 400–500 ms | 30–50 ms |
| TTS latency spike | up to 11.45 s | no spike |
| Total delay | ~1.8 s | ~1.0 s |
