# Release Notes

## 2026-05-16 — Voice NLU Call Setup (commit `990286f`)

### New feature: Voice-driven call setup for inbound calls

Inbound calls on the public DID (`+4980424967`, context `[from-dusnet]`) now use
automatic speech recognition to identify the destination number and target language,
with a transparent fallback to DTMF entry if recognition fails.

#### Call flow

```
Caller dials +4980424967
  → Asterisk answers
  → CallerID prefix matched to language  (+49 → de, +39 → it, +7 → ru, …)
  → Announcement played in caller's own language
       "Bitte Zielrufnummer und Sprache nennen."  (de)
       "Prego indicare il numero di destinazione e la lingua."  (it)
       "Please state the destination number and language."  (en)
       … (16 languages total)
  → Up to 7 s of speech recorded (ends on 2 s silence or any DTMF key)
  → Whisper transcribes using the caller's language model
  → NLU extracts destination digits + language suffix
       e.g. "Call Mario in Italy, number 0039 347 123 456"
            → digits: 0039347123456   suffix: 39  (→ Italian TTS)
  → AudioSocket bridge started → bidirectional translation call

  IF voice recognition yields no result:
  → Beep + DTMF prompt (existing behaviour, unchanged)
       e.g. type 01762525787839 + # for IT translation
```

#### Key Whisper detail

Whisper uses the caller's own language (detected from CallerID prefix) when
transcribing the NLU recording.  A German caller speaks German, an Italian caller
speaks Italian — matching the model language to the speaker dramatically improves
number and keyword recognition accuracy.

#### New files

| File | Purpose |
|------|---------|
| `caller_lang.py` | AGI script: maps `CALLERID(num)` to `PROMPT_LANG` via `/lang` endpoint |
| `voice_nlu.py` | AGI script: posts WAV path + lang to `/nlu`, sets `DIAL_NUMBER` + `LANG_SUFFIX` |

#### Changes to existing files

**`audiosocket_translator.py`**
- `callerid_to_lang(callerid)` — maps CallerID E.164 prefix to language code
- `extract_dial_info(text)` — extracts `(digits, suffix)` from transcribed speech using language keyword matching and country-code prefix detection
- `generate_nlu_prompts()` — synthesises announcement WAVs via Piper for all 16 supported languages at startup; writes to `/var/lib/asterisk/sounds/custom/nlu_prompt_{lang}.wav`
- `_read_wav_16k(path)` — reads any WAV file and resamples to 16 kHz mono float32 for Whisper
- HTTP server on port 9094 extended with two new endpoints:
  - `POST /lang` — `{"callerid": "+49…"}` → `{"lang": "de"}`
  - `POST /nlu`  — `{"path": "/tmp/…wav", "lang": "de"}` → `{"text": "…", "number": "…", "suffix": "…"}`
- `handle_register` refactored to `handle_http` with path-based routing

**`asterisk/extensions_translator.conf`**
- `[from-dusnet]` rewritten with voice path (AGI calls, Playback, Record) plus labelled DTMF fallback section; both paths converge at the shared `(voicedial)` label

#### Supported CallerID → language mapping

| Prefix | Language |
|--------|----------|
| +49 | German (de) |
| +39 | Italian (it) |
| +7 | Russian (ru) |
| +44 | English (en) |
| +33 | French (fr) |
| +34 | Spanish (es) |
| +30 | Greek (el) |
| +48 | Polish (pl) |
| +55 | Portuguese/Brazil (pt) |
| +380 | Ukrainian (uk) |
| +77 | Kazakh (kk) |
| +86 | Chinese (zh) |
| +90 | Turkish (tr) |
| +91 | Hindi (hi) |
| +98 | Persian (fa) |
| +995 | Georgian (ka) |
| +1 | English (en) |

---

## 2026-05-16 — Multi-language support (commit `cbdd9da`)

Extended from DE↔IT to 16 languages via E.164-based dial suffixes.  
Added model installation documentation and bulk Piper download script (commit `9f71f3b`).

---

## 2026-05-15 — Initial release (commit `d3b3bbb`)

Asterisk 22 AudioSocket live translation: German ↔ Italian bidirectional,  
Whisper STT (CUDA) + Argostranslate + Piper TTS, VAD-based segmentation,  
AMI Originate for outbound leg, per-session state machine with history logging.
