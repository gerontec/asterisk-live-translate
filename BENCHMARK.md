# Latenz-/Zeitverhalten der Inference-API

Messungen des GPU-Inference-Servers (`inference_server.py`, `yt6.heissa.de:9095`) und der End-to-End-Call-Latenz des AudioSocket-Translators.

- **Datum:** 2026-07-20
- **GPU-Host:** dell-3660 = `yt6.heissa.de` · **NVIDIA Tesla P4** (7680 MiB, CUDA)
- **Modelle:** Whisper *medium* (int8) · NLLB-200-distilled-600M · Piper
- **Werkzeuge:** [`api_bench.py`](api_bench.py) (Endpunkt-RTT) · [`call_latency.py`](call_latency.py) (Latenz je realem Call)

---

## Methodik

`api_bench.py` misst pro Endpunkt **10 Messungen + 1 verworfenen Warmup**, sequentiell (der Server serialisiert GPU-Jobs in einer FIFO-Queue, parallele Messung würde nur anstauen). Gemessen wird die **Client-RTT** (Verbindungsaufbau → vollständige Antwort). Das Test-Audio erzeugt das Script selbst per `/tts` (→ PCM für `/stt`, WAV-Kopien für `/nlu`). Testtext: *„Guten Morgen, wie war Ihre Anreise hierher?"* (~2,4 s Audio).

Zwei Messpunkte:
1. **lokal** auf dem GPU-Host (`::1`) — reine Server-/GPU-Zeit, kein Netz.
2. **über Netz** von ipgate1 aus (`yt6.heissa.de`, öffentl. IPv6) — wie der Live-Dienst.

---

## Ergebnis 1 — lokal auf dem GPU-Host (`::1:9095`, n=10)

| Endpoint | min | median | avg | p95 | max | std | warmup |
|---|--:|--:|--:|--:|--:|--:|--:|
| `/tts` | 72,8 | 77,9 | 77,9 | 83,4 | 83,4 | 3,7 | 102,0 |
| `/translate` | 191,9 | 192,1 | 192,5 | 195,7 | 195,7 | 1,1 | 199,6 |
| `/stt` (2,5 s) | 664,9 | 665,5 | 666,4 | 670,8 | 670,8 | 2,2 | 672,7 |
| `/nlu` | 1078,1 | 1079,1 | 1079,6 | 1083,3 | 1083,3 | 1,6 | 1086,1 |

Alle Werte in **ms**, 0 Fehler. **Σ eines Segments** (`/stt`+`/translate`+`/tts`, Median) = **935 ms**.

## Ergebnis 2 — über Netz-Hop (ipgate1 → `yt6.heissa.de:9095`, n=10)

| Endpoint | min | median | avg | p95 | max | std |
|---|--:|--:|--:|--:|--:|--:|
| `/tts` | 293,4 | 310,2 | 307,9 | 318,9 | 318,9 | 7,7 |
| `/translate` | 349,1 | 352,4 | 353,4 | 360,5 | 360,5 | 3,3 |
| `/stt` (2,3 s) | 870,5 | 876,5 | 879,3 | 894,4 | 894,4 | 8,0 |

`/nlu` benötigt eine server-lokale Datei und wird über Netz nicht gemessen. **Σ eines Segments** (Median) = **1539 ms**.

---

## Analyse

| Endpoint | lokal (med) | Netz (med) | Netz-Aufschlag |
|---|--:|--:|--:|
| `/tts` | 78 ms | 310 ms | **+232 ms** |
| `/translate` | 192 ms | 352 ms | **+160 ms** |
| `/stt` | 665 ms | 876 ms | **+211 ms** |
| **Σ Segment** | **935 ms** | **1539 ms** | **+604 ms** |

- **`/nlu` ist der teuerste Call** (~1,08 s): Datei-Read + Whisper mit *Auto-Spracherkennung* + Nummer-/Sprach-Extraktion.
- **`/stt` dominiert** die Segment-Latenz (Whisper medium).
- **Netz-Hop kostet ~130–230 ms pro Call** — überwiegend der **TCP-Handshake je Request** (RTT ipgate1↔dell ~130 ms; jeder Infer-Call öffnet eine neue Verbindung). Ein Segment löst 3 Calls aus → grob 0,4–0,6 s reine Verbindungs-/Netz-Zeit. **HTTP-Keep-Alive** (Verbindungs-Wiederverwendung) im Translator würde das spürbar senken.
- Sehr **geringe Streuung** (std 1–8 ms) → stabiles, vorhersagbares Zeitverhalten.

## Cross-Check mit realen Calls

`call_latency.py` wertet den Translator-Log realer Anrufe aus. Über 10 Calls / 13 Segmente:

```
PIPE  min 1,35  avg 1,43  p95 1,46  max 1,48  s   (STT+TRL+TTS, inkl. Netz-Hop)
WALL  min 3,00  avg 3,27  p95 3,50  max 3,52  s   (inkl. GPU-Queue, Pacing, Audio-Ausgabe)
```

Die real gemessene **PIPE-Latenz 1,43 s** deckt sich mit der Benchmark-**Netz-Segment-Summe 1,54 s** — zwei unabhängige Methoden bestätigen sich. ✅

---

## HTTP Keep-Alive (eingebaut 2026-07-20)

Zuvor öffnete jeder Infer-Call eine neue TCP-Verbindung → pro Segment 3 Handshakes
(~130 ms RTT je Handshake). Nun halten **Client** (`audiosocket_translator.py`,
thread-lokale persistente Verbindung) und **Server** (`inference_server.py`,
`handle_http` bearbeitet mehrere Requests je Verbindung, Antworten mit
`Connection: keep-alive`) die Verbindung offen.

Messung eines vollständigen Segments (`/stt`+`/translate`+`/tts`) von ipgate1, 8 Läufe:

| Variante | avg | min | max |
|---|--:|--:|--:|
| **fresh** (3 Handshakes, alt) | 1467 ms | 1453 | 1496 |
| **keep-alive** (1 Verbindung, neu) | 1089 ms | 1066 | 1187 |
| **Ersparnis** | **≈378 ms/Segment (~26 %)** | | |

(Einzel-Call auf einer Keep-Alive-Verbindung: req1 308 ms inkl. Handshake, req2+ ~168 ms.)

## `/nlu`: Auto-Spracherkennung abgeschaltet (2026-07-20)

`/nlu` erhält im Body bereits einen `lang`-Hint, transkribierte aber mit
`language=None` → Whisper machte einen zusätzlichen **Auto-Detect-Vorpass**.
Der Hint wird nun direkt durchgereicht (`language=lang or None`, Fallback
Auto-Detect nur ohne Hint).

| `/nlu` (lokal, n=12) | median |
|---|--:|
| vorher (`language=None`) | 1062 ms |
| nachher (`language=lang`) | **617 ms** |
| **Ersparnis** | **≈445 ms (~42 %)** |

Ergebnis-Korrektheit unverändert (`{"number":"+49…","suffix":"44"}`).

## Nutzung

```bash
# Endpunkt-Benchmark (alle 4, lokal auf dem GPU-Host):
python3 api_bench.py --host ::1 --n 10 --json ergebnisse.json
# nur die 3 HTTP-Endpunkte über Netz:
python3 api_bench.py --host yt6.heissa.de --n 20 --no-nlu

# Latenz realer Calls (Live oder Report):
python3 call_latency.py --report
python3 call_latency.py --follow --csv /tmp/call_latency.csv
python3 call_latency.py --probe        # aktiver RTT-Test gegen den Server
```
