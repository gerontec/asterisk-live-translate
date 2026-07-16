# Portierung: Live-Übersetzungstelefon auf Pixel 8 (Tensor G3)

## Kernfrage vorweg: gleiche Latenz & Pausenerkennung wie jetzt?

**Pausenerkennung (VAD): ja, identisch.** `webrtcvad` ist ein winziger
Festkomma-CPU-Algorithmus (20‑ms‑Frames, ein paar µs pro Frame). Er läuft auf
jedem ARM-Kern des Tensor G3 trivial — die Segmentierung ist 1:1 dieselbe.

**Latenz: nicht automatisch.** Die aktuelle „perfekte" Latenz kommt von einer
**NVIDIA-GPU** (dell‑3660), die Whisper *medium* (int8) + NLLB‑600M in ~1–2 s
rechnet. Der Tensor G3 ist für **genau diese Modelle** ein bis zwei
Größenordnungen langsamer — seine TPU beschleunigt nur TFLite/LiteRT/NNAPI-Graphen,
nicht faster-whisper/CTranslate2/PyTorch-CUDA. Vergleichbare Latenz on-device gibt
es nur mit **mobil-optimierten Modellen** (Qualitäts-/Geschwindigkeitskompromiss).

Kurz: **VAD gleich, Latenz nur mit passendem Modell-Stack — oder wenn die Inferenz
am Server bleibt.**

## Zwei Architekturen

### A) Pixel = Client, Inferenz bleibt am Server  → *empfohlen, wenn Latenz Priorität*
```
Pixel 8 (Telegram-Call + VAD)  --WireGuard-->  dell-3660 :9095 (GPU-Inferenz)
```
* Latenz **und** Qualität = **identisch zu jetzt** (dieselbe GPU), plus ~30–80 ms
  Netz-RTT über VPN. Ihr habt die VPN-Infrastruktur (ipgate1 / WireGuard) schon.
* Braucht Konnektivität (5G/LTE/WLAN) zum Server.
* Aufwand **gering**: nur der Call-Transport muss auf Android laufen; `INFER`
  zeigt auf die VPN-Adresse des Servers.

### B) Vollständig on-device auf Tensor G3  → autark/offline, aber Kompromisse
Alles auf dem Pixel, kein Server. Modelle müssen mobil sein:

| Stufe | jetzt (Server/GPU) | on-device (Tensor G3) |
|-------|--------------------|-----------------------|
| STT   | Whisper medium int8, CUDA | whisper.cpp **small/base** (ggml, CPU/Vulkan) oder Google SODA/Live-Transcribe (TPU) |
| MT    | NLLB‑200‑600M, CUDA | **ML Kit Translation** DE↔EN (~30 MB, NNAPI/TPU, sehr schnell) oder NLLB-distilled int8/LiteRT |
| TTS   | Piper en_GB-alan   | **Piper** (onnxruntime, ARM) — läuft unverändert gut |

* Realistische Latenz mit whisper-small + ML Kit + Piper: **~1,5–3 s** pause‑to‑echo
  — nutzbar, aber Whisper-*medium*-Qualität wird nicht 1:1 erreicht.
* Der Tensor G3 hilft v. a. bei ML Kit / SODA (TPU); whisper.cpp bleibt CPU/Vulkan.

## Schnittstellen-Trick

`telegram_translate_bot.py` spricht die Inferenz nur über die HTTP-API
`/stt · /translate · /tts` an. **Diese API bleibt gleich** — egal ob dahinter der
GPU-Server (A) oder ein on-device-Backend (B) steckt. Der Bot-Code muss für die
Portierung praktisch **nicht geändert** werden; nur `INFER` wechselt zwischen
`WireGuard→dell-3660` und `localhost-on-device`.

## Call-Transport auf Android

1. **Termux-Weg (zuerst, schnell):** Termux + Python + Pyrogram 2.x + selbst
   gebautes `libtgvoip`/`_tgvoip`. libtgvoip hat Android-Audio-Backends (OpenSLES);
   Cross-Build für aarch64 mit NDK, OpenSSL-android + opus-android, `TGVOIP_NO_DSP`,
   OpenSLES statt ALSA. Wiederverwendet den vorhandenen Python-Code fast 1:1.
2. **Native App (später, sauber):** Kotlin-App mit `tgcalls` (die C++-lib der
   offiziellen Clients) + TDLib für Signalisierung. Mehr Aufwand, aber echte App
   (Hintergrunddienst, Akku-Management, Play-tauglich).

## Stufenplan

1. **Stufe 1 — „gleiche Latenz" beweisen:** libtgvoip+pytgvoip in Termux auf dem
   Pixel bauen, Bot on-device starten, `INFER = WireGuard → dell-3660`.
   → Call-Transport auf G3 verifiziert, Latenz/Qualität wie heute.
2. **Stufe 2 — TTS + MT lokal:** Piper + ML Kit Translate on-device; nur STT bleibt
   am Server. Reduziert Netzabhängigkeit deutlich.
3. **Stufe 3 — voll autark:** whisper.cpp small/base (ggml, NNAPI/Vulkan) on-device;
   benchmarken; Latenz/Qualität tunen (`SILENCE_FR`, beam_size, Chunk-Länge).

## Risiken / offene Punkte

* `libtgvoip`-NDK-Cross-Build (OpenSSL-android, opus-android) — machbar, etwas Arbeit.
* Termux-Python + native pybind11-Extension — geht; OpenSSL-Version auf Android beachten.
* Whisper-*medium* ist on-device nicht praktikabel → Qualitätskompromiss in Variante B.
* Akku/Wärme bei Dauer-Inferenz auf dem G3; für Telefonate im grünen Bereich.

## Empfehlung

Mit **Stufe 1 / Variante A** starten: schnell, beweist den Android-Call-Transport
auf dem Tensor G3 **und** liefert die *identische* Latenz/Pausenerkennung wie jetzt
(GPU bleibt Server-seitig). Danach schrittweise Module on-device ziehen (Stufe 2→3),
je nachdem wie viel Offline-Autonomie vs. Spitzen-Qualität gewünscht ist.
