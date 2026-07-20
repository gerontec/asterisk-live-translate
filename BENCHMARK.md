# Latenz / Zeitverhalten der Inference-API

Aktuelle Messwerte des GPU-Inference-Servers (`inference_server.py`, `yt6.heissa.de:9095`).

- **GPU-Host:** dell-3660 = `yt6.heissa.de` · **NVIDIA Tesla P4** · Whisper *medium* (int8) · NLLB-200-distilled-600M · Piper
- **Aktive Optimierungen:** HTTP **Keep-Alive** (Client + Server) · `/nlu` mit `lang`-Hint (kein Auto-Detect-Vorpass)
- **Werkzeuge:** [`api_bench.py`](api_bench.py) (Endpunkt-RTT) · [`call_latency.py`](call_latency.py) (Latenz je realem Call)

## Endpunkt-RTT (lokal, `::1`, n=15)

| Endpoint | median | p95 |
|---|--:|--:|
| `/tts` | 79 ms | 83 ms |
| `/translate` | 192 ms | 194 ms |
| `/stt` (2,4 s Audio) | 663 ms | 664 ms |
| `/nlu` | 633 ms | 635 ms |

Streuung durchweg < 5 ms, 0 Fehler.

## Live-Segment (`/stt` + `/translate` + `/tts`)

| gemessen von | Segment-Latenz |
|---|--:|
| lokal auf dem GPU-Host | **935 ms** |
| über Netz (ipgate1 → yt6, Keep-Alive) | **1095 ms** |

`call_latency.py` bestätigt dies an realen Anrufen (PIPE ≈ 1,1 s inkl. Netz-Hop).

## Nutzung

```bash
python3 api_bench.py --host ::1 --n 15            # alle 4 Endpunkte, lokal
python3 api_bench.py --host yt6.heissa.de --no-nlu # HTTP-Endpunkte über Netz
python3 call_latency.py --report                  # Latenz realer Calls aus dem Log
python3 call_latency.py --probe                   # aktiver RTT-Test
```
