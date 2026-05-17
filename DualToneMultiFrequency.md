# DTMF Fallback — Dialling by Keypad Instead of Voice

This document explains how to use the keypad (DTMF) input when the voice-driven
call setup does not produce a result.  Voice input is always attempted first;
DTMF is the fallback.

---

## Call Flow Overview

```
You call +4980424967
    │
    ▼
Announcement plays in your language
(detected from your caller ID prefix)
    │
    ▼
Up to 20 seconds to speak:
  "Call [name], number [digits], in [language]"
    │
    ├─ Voice recognised → call is placed automatically
    │
    └─ Voice NOT recognised (or you press any key to skip)
           │
           ▼
        BEEP — keypad input active
           │
           ▼
        Type: destination number + language suffix
        (max 15 digits, 10 second window, # to confirm)
           │
           ▼
        Ringback tone plays while remote phone rings
           │
           ▼
        Remote answers → bidirectional translation begins
```

---

## Skipping Straight to DTMF

You do **not** have to wait for the voice recognition to time out.
Press **any digit key** at any point during or after the announcement —
the recording stops immediately and the BEEP for keypad entry follows within
a second or two.

---

## What to Type After the Beep

The input format is:

```
{destination number in 00XX format}{language suffix}
```

There is **no separator** between the phone number and the language suffix —
they are entered as one continuous digit string.  Press `#` immediately after
the last digit to confirm early, or simply stop pressing keys and wait for the
1-second silence timeout.

### Why `00XX` and not `+XX`?

The `+` sign cannot be dialled on a telephone keypad.  Use the international
prefix `00` instead of `+`:

| E.164 | Keypad equivalent |
|-------|-------------------|
| `+39 347 123 456` | `0039347123456` |
| `+33 6 12 34 56 78` | `003361234567` (see note on length below) |
| `+7 495 123 45 67` | `007495123456` |

---

## Language Suffixes

Append the two- or three-digit country calling code of the **target language**
at the very end of the destination number.  The system strips this suffix to
determine the translation direction; the remaining digits are the number dialled.

| Suffix | Language | Example country |
|--------|----------|-----------------|
| `33` | French | France |
| `39` | Italian | Italy |
| `7` | Russian | Russia |
| `44` | English | United Kingdom |
| `1` | English | USA / Canada |
| `34` | Spanish | Spain |
| `30` | Greek | Greece |
| `48` | Polish | Poland |
| `55` | Portuguese | Brazil |
| `38` | Ukrainian | Ukraine |
| `77` | Kazakh | Kazakhstan |
| `86` | Chinese | China |
| `90` | Turkish | Turkey |
| `91` | Hindi | India |
| `98` | Persian | Iran |
| `995` | Georgian | Georgia |

The suffix is **always matched from the end** of the digit string, longest
match first.  `995` is therefore checked before `95` or `5`.

---

## Practical Examples

### Example 1 — German speaker calling an Italian mobile

Destination: `+39 347 123 456`  Target language: Italian (`39`)

```
Type:  003934712345639
       └──────────────┘└─┘
       0039347123456   39  ← Italian suffix
```

Dialled number: `+39347123456`  
Translation direction: DE → IT (you) / IT → DE (remote)

---

### Example 2 — German speaker calling a French mobile

Destination: `+33 6 12 34 56 78`  Target language: French (`33`)

```
Type:  003361234567833
       └─────────────┘└─┘
       0033612345678  33  ← French suffix
```

Dialled number: `+33612345678`  
Translation direction: DE → FR / FR → DE

---

### Example 3 — German speaker calling a Russian mobile

Destination: `+7 916 123 45 67`  Target language: Russian (`7`)

```
Type:  0079161234567 7
       └─────────────┘└┘
       007916123456   7  ← Russian suffix (single digit)
```

> **Tip:** The `7` suffix is only one digit.  A Russian mobile in `007` format
> already starts with `007`; Python recognises the trailing `7` as Russian after
> checking all two-digit suffixes first, so there is no ambiguity.

---

### Example 4 — Calling a Georgian number (3-digit suffix)

Destination: `+995 32 123 4567`  Target language: Georgian (`995`)

```
Type:  009953212345 995
       ────────────  ─────
       (only 12 digits of number, then 995 → total 15)
```

> **Note:** Georgian numbers are long; the 15-digit limit applies here.
> See the length warning below.

---

## Important Limitations

### 15-digit input limit

The DTMF reader accepts a maximum of **15 digits**.  Since the language suffix
consumes 1–3 digits, the destination number itself can be at most 12–14 digits.

| Suffix length | Max destination digits |
|:---:|:---:|
| 1 digit (`7`) | 14 |
| 2 digits (`33`, `39` …) | 13 |
| 3 digits (`995`) | 12 |

Long numbers (e.g. some mobile formats with full `00XX` prefix and 10-digit
local number) may exceed this limit.  In that case, use **voice input** or
an internal extension via a Fritz!Box phonebook entry (see below).

### Suffix collision

The suffix is matched from the **end** of the typed string.  If the destination
number itself happens to end in the same digits as a language code, the system
will misinterpret it.  For example, a number ending in `…77` would be treated
as Kazakh.  There is currently no escape character.  Use voice input for such
numbers.

### 10-second window

After the beep, you have **10 seconds** to finish typing.  For long numbers,
begin typing immediately.  Press `#` after the last digit to confirm without
waiting.

---

## Alternative: Fritz!Box Phonebook Entry

For numbers you call regularly, add a phonebook entry in the Fritz!Box with the
language suffix already embedded:

```
Name:   Mario (IT)
Number: 003934712345639
```

Dial this number from any internal handset.  The translator dialplan
(`[from-internal]`) detects the international format and prompts for the
language code via DTMF — or you can include it in the phonebook number using
pause characters:

```
+39347123456,,39
```

The two commas insert a pause; `39` is then sent as a post-dial DTMF string
and the language code is picked up automatically.

---

## After a Failed Translation Call

If the translation call ends abnormally (network error, timeout), the system
plays another **beep** and re-opens the DTMF input window so you can dial again
without hanging up and redialling the DID.  The same format applies.

---

## Summary

| Step | Action |
|------|--------|
| 1 | Dial `+4980424967` |
| 2 | Hear the announcement in your language |
| 3 | **Option A** — Speak: *"Number 0039 347 123 456, Italian"* |
| 3 | **Option B** — Press any key → wait for beep → type `003934712345639` + `#` |
| 4 | Hear ringback while remote phone rings |
| 5 | Remote answers → speak normally, translation is automatic |
