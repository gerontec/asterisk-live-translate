# Loopback Test — Delay OK (2026-05-15 23:03)

Configuration: Piper TTS, SILENCE_FR=15, vad_filter=True, no_speech_threshold=0.7
Result: 6 segments, 0 skips, 0 hallucinations, call ended after 60 s (dial timeout)

| # | German (spoken)                                   | Italian (translated)                                 | STT   | TRL   | TTS   | Σ      |
|---|---------------------------------------------------|------------------------------------------------------|-------|-------|-------|--------|
| 1 | "Ja, super, dass du anrufst."                     | "Sì, è stato un piacere."                            | 0.76s | 0.46s | 0.04s | 1.26s  |
| 2 | "Es ist einfach unglaublich, was heute gezeigt wurde." | "È incredibile quello che è stato mostrato oggi." | 0.75s | 0.05s | 0.06s | 1.16s  |
| 3 | "ist das Licht ausgeschalten."                    | "la luce è spenta."                                  | 0.65s | 0.03s | 0.03s | 1.01s  |
| 4 | "Hast du Oma angerufen?"                          | "Hai chiamato la nonna?"                             | 0.63s | 0.03s | 0.03s | 0.99s  |
| 5 | "Das hat die Oma gesagt."                         | "È quello che ha detto la nonna."                    | 0.63s | 0.04s | 0.04s | 1.01s  |
| 6 | "So, jetzt schalte ich den Fernseher aus."        | "Quindi ora spengo la TV."                           | 0.73s | 0.04s | 0.04s | 1.11s  |

**Median total delay: ~1.1 s**
