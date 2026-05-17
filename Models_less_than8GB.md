# Translation Models < 8 GB VRAM (Tesla P4)

Context: Whisper medium (int8) uses ~900 MB VRAM → ~6.7 GB remaining for the translation model.

## Model Comparison

| Model | HuggingFace ID | VRAM float16 | VRAM int8 | Languages | Quality DE↔FR | Speed | Fits with Whisper? |
|---|---|---|---|---|---|---|---|
| M2M-100-418M | `facebook/m2m100_418M` | ~0.8 GB | — | 100 | ★★☆☆☆ | very fast | yes |
| NLLB-200-distilled-600M | `facebook/nllb-200-distilled-600M` | ~1.2 GB | — | 200 | ★★★☆☆ | very fast | yes |
| M2M-100-1.2B | `facebook/m2m100_1.2B` | ~2.4 GB | — | 100 | ★★★☆☆ | fast | yes |
| **NLLB-200-distilled-1.3B** ← current | `facebook/nllb-200-distilled-1.3B` | **~2.6 GB** | — | 200 | **★★★★☆** | fast | **yes** |
| NLLB-200-1.3B | `facebook/nllb-200-1.3B` | ~2.6 GB | — | 200 | ★★★★☆ | fast | yes |
| Madlad-400-3B | `google/madlad400-3b-mt` | ~6.0 GB | ~3.0 GB | 400+ | ★★★★☆ | medium | yes (int8) |
| NLLB-200-3.3B | `facebook/nllb-200-3.3B` | ~6.6 GB | ~3.3 GB | 200 | ★★★★★ | medium | tight (int8) |
| NLLB-200-3.3B float16 | `facebook/nllb-200-3.3B` | ~6.6 GB | — | 200 | ★★★★★ | medium | no (OOM) |

## Notes

**M2M-100-418M / 1.2B**  
Only 100 languages (no Kazakh, Georgian, Persian). Quality below NLLB. No advantage over NLLB except slightly faster on very short texts.

**NLLB-200-distilled-600M** (was in production until May 2026)  
Distilled version — 40% smaller than 1.3B, but noticeably weaker grammar on long sentences and rare languages.

**NLLB-200-distilled-1.3B** ← currently active  
Good compromise: double capacity over 600M, minimal extra latency, 200 languages including all used (DE, FR, IT, ES, RU, UK, PL, TR, ZH, HI, FA, KK, KA, EL). Recommended for real-time telephony.

**NLLB-200-1.3B** (non-distilled)  
Same VRAM budget as distilled-1.3B, 5–8% better BLEU scores on rare language pairs. Worth it when high accuracy is needed for low-resource languages.

**Madlad-400-3B** (Google)  
400+ languages, competitive with NLLB-1.3B in quality, narrowly better on some languages. Requires int8 quantization to fit alongside Whisper (~3.0 GB + 0.9 GB = 3.9 GB).

**NLLB-200-3.3B** (Meta's best NLLB)  
Significantly better than 1.3B — especially for formal language and long sentences. Only usable as int8 alongside Whisper (~3.3 + 0.9 = 4.2 GB). Float16 does not fit (6.6 + 0.9 = 7.5 GB → OOM risk at peak).

## Next Upgrade Step

If quality becomes more important than latency:

```python
# audiosocket_translator.py line 73
NLLB_MODEL = "facebook/nllb-200-3.3B"

# and enable int8 in load_models():
_nllb_model = AutoModelForSeq2SeqLM.from_pretrained(
    NLLB_MODEL, cache_dir=NLLB_CACHE,
    load_in_8bit=True,   # instead of torch_dtype=torch.float16
    device_map="cuda",
)
```

Prerequisite: `pip install bitsandbytes`

## VRAM Overview (Tesla P4, 7680 MB)

```
Whisper medium int8   ~  900 MB  (fixed)
NLLB-1.3B float16    ~ 2618 MB  ← current
─────────────────────────────────
Total                ~ 3518 MB
Free                 ~ 4162 MB
```
