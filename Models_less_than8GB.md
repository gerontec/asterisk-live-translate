# Übersetzungsmodelle < 8 GB VRAM (Tesla P4)

Kontext: Whisper medium (int8) belegt ~900 MB VRAM → verbleiben ~6,7 GB für das Übersetzungsmodell.

## Modell-Vergleich

| Modell | HuggingFace-ID | VRAM float16 | VRAM int8 | Sprachen | Qualität DE↔FR | Geschwindigkeit | Passt mit Whisper? |
|---|---|---|---|---|---|---|---|
| M2M-100-418M | `facebook/m2m100_418M` | ~0,8 GB | — | 100 | ★★☆☆☆ | sehr schnell | ja |
| NLLB-200-distilled-600M | `facebook/nllb-200-distilled-600M` | ~1,2 GB | — | 200 | ★★★☆☆ | sehr schnell | ja |
| M2M-100-1.2B | `facebook/m2m100_1.2B` | ~2,4 GB | — | 100 | ★★★☆☆ | schnell | ja |
| **NLLB-200-distilled-1.3B** ← aktuell | `facebook/nllb-200-distilled-1.3B` | **~2,6 GB** | — | 200 | **★★★★☆** | schnell | **ja** |
| NLLB-200-1.3B | `facebook/nllb-200-1.3B` | ~2,6 GB | — | 200 | ★★★★☆ | schnell | ja |
| Madlad-400-3B | `google/madlad400-3b-mt` | ~6,0 GB | ~3,0 GB | 400+ | ★★★★☆ | mittel | ja (int8) |
| NLLB-200-3.3B | `facebook/nllb-200-3.3B` | ~6,6 GB | ~3,3 GB | 200 | ★★★★★ | mittel | knapp (int8) |
| NLLB-200-3.3B float16 | `facebook/nllb-200-3.3B` | ~6,6 GB | — | 200 | ★★★★★ | mittel | nein (OOM) |

## Anmerkungen

**M2M-100-418M / 1.2B**
Nur 100 Sprachen (kein Kasachisch, Georgisch, Farsi). Qualität unter NLLB. Kein Vorteil gegenüber NLLB außer bei sehr kurzen Texten etwas schneller.

**NLLB-200-distilled-600M** (war produktiv bis Mai 2026)
Destillierte Version — 40 % kleiner als 1.3B, aber spürbar schwächere Grammatik bei langen Sätzen und seltenen Sprachen.

**NLLB-200-distilled-1.3B** ← derzeit aktiv
Guter Kompromiss: doppelte Kapazität gegenüber 600M, kaum Mehrlatenz, 200 Sprachen inklusive aller eingesetzten (DE, FR, IT, ES, RU, UK, PL, TR, ZH, HI, FA, KK, KA, EL). Empfohlen für Echtzeit-Telefonie.

**NLLB-200-1.3B** (nicht destilliert)
Gleiches VRAM-Budget wie distilled-1.3B, 5–8 % bessere BLEU-Scores bei seltenen Sprachpaaren. Lohnt bei Bedarf an hoher Genauigkeit für wenig gesprochene Sprachen.

**Madlad-400-3B** (Google)
400+ Sprachen, konkurriert mit NLLB-1.3B bei Qualität, schlägt es bei einigen Sprachen knapp. Benötigt int8-Quantisierung um neben Whisper zu passen (~3,0 GB + 0,9 GB = 3,9 GB).

**NLLB-200-3.3B** (Metas bestes NLLB)
Deutlich besser als 1.3B — vor allem bei formeller Sprache und langen Sätzen. Nur als int8 neben Whisper nutzbar (~3,3 + 0,9 = 4,2 GB). Float16 passt nicht (6,6 + 0,9 = 7,5 GB → OOM-Risiko bei Spitzen).

## Nächste Upgrade-Stufe

Wenn Qualität wichtiger als Latenz wird:

```python
# audiosocket_translator.py Zeile 73
NLLB_MODEL = "facebook/nllb-200-3.3B"

# und in load_models() int8 aktivieren:
_nllb_model = AutoModelForSeq2SeqLM.from_pretrained(
    NLLB_MODEL, cache_dir=NLLB_CACHE,
    load_in_8bit=True,   # statt torch_dtype=torch.float16
    device_map="cuda",
)
```

Voraussetzung: `pip install bitsandbytes`

## VRAM-Übersicht (Tesla P4, 7680 MB)

```
Whisper medium int8   ~  900 MB  (fix)
NLLB-1.3B float16    ~ 2618 MB  ← aktuell
─────────────────────────────────
Gesamt               ~ 3518 MB
Frei                 ~ 4162 MB
```
