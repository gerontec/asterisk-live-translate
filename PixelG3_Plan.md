# Portierung: Live-Übersetzungstelefon auf Pixel 8 (Tensor G3) — **GrapheneOS, on-device only**

Ziel: das Live-Dolmetscher-Telefon **komplett offline auf dem Pixel 8 unter
GrapheneOS**. Die **Übersetzung läuft auf dem Tensor G3** selbst — kein Backend im
Normalbetrieb (Zielzustand null; dell-3660 nur, falls etwas partout nicht aufs
Gerät passt).

**Betriebsmodus = bidirektionaler Dolmetscher** (`telegram_interpreter.py`): du
hältst das Pixel, sprichst Deutsch ins Mikro, rufst einen beliebigen Partner an;
der Partner nutzt einen **ganz normalen Telegram-Client** (keine Software nötig,
z. B. der Poco als @GeorgHeiss) und hört Englisch, antwortet Englisch, du hörst
Deutsch. Mikro/Lautsprecher = das Pixel; STT/MT/TTS = on-device.

## Kernfrage: gleiche Latenz & Pausenerkennung?

* **Pausenerkennung (VAD): identisch.** `webrtcvad` ist ein Festkomma-CPU-Algorithmus
  (20‑ms‑Frames, µs-Rechenzeit) — läuft auf dem Tensor G3 exakt gleich.
* **Latenz: abhängig vom Modell-Stack.** Die heutige „perfekte" Latenz kommt von
  einer NVIDIA-GPU (Whisper *medium* + NLLB‑600M). On-device muss auf mobil-
  optimierte Modelle getauscht werden → realistisch **~1,5–3 s** pause‑to‑echo,
  nutzbar, aber nicht in *medium*-Qualität. Details unten.

## GrapheneOS-Randbedingung (entscheidend)

GrapheneOS hat **standardmäßig keine Google Play Services**. Damit sind Googles
On-Device-Bausteine **nicht garantiert verfügbar**:

* **ML Kit Translation**, **SODA / Live Transcribe** (Googles Offline-ASR) und die
  „Live Translate"-Funktion des Pixels hängen an Play Services / Google-Apps.
* Auf GrapheneOS ließe sich *sandboxed* Play Services nachinstallieren — dann gingen
  die Google-Modelle. Für eine saubere, herstellerunabhängige und dauerhaft
  offline-fähige Lösung setzen wir aber auf einen **voll quelloffenen Stack** ohne
  jede Google-Abhängigkeit. (Google „schafft das offline" — aber eben über Play
  Services; wir bauen es Google-frei nach.)

**Konsequenz:** fully-open on-device Stack, kein Play-Services-Zwang.

## Ziel-Architektur (GrapheneOS, offline)

```
Telegram-Anruf (Deutsch)
   │  MTProto private call
   ▼
Termux auf GrapheneOS  (aarch64)
   ├── Pyrogram 2.x + libtgvoip (NDK-Cross-Build, OpenSLES)   ← Call-Transport
   ├── webrtcvad, 16 kHz, Utterance-Segmentierung             ← unverändert
   └── lokaler Inferenz-Server (localhost:9095, gleiche API /stt /translate /tts)
         ├── STT : whisper.cpp (ggml, small/base, q5)         ← Google-frei
         ├── MT  : Bergamot (Mozilla, Marian de-en, ~15 MB)   ← Google-frei
         └── TTS : Piper (onnxruntime, en_GB-alan)            ← Google-frei
   ▼
Anrufer hört das englische Echo — alles auf dem Gerät
```

Der **Schnittstellen-Trick bleibt**: `telegram_translate_bot.py` spricht die
Inferenz nur über `/stt · /translate · /tts`. Auf dem Pixel zeigt `INFER` auf
`http://127.0.0.1:9095` eines **lokalen** Termux-Servers mit obigen Modellen — der
Bot-Code ist damit **unverändert** übernehmbar.

## Open-Source-Stack (kein Google)

| Stufe | Server heute (GPU) | Pixel 8 / GrapheneOS (offline) |
|-------|--------------------|--------------------------------|
| STT   | faster-whisper medium int8, CUDA | **whisper.cpp** ggml small/base q5 (CPU/NEON, optional Vulkan/Mali) |
| MT    | NLLB‑200‑600M, CUDA | **Bergamot** (bergamot-translator, Marian de↔en) — oder **Argos Translate** (CTranslate2) |
| TTS   | Piper en_GB-alan   | **Piper** (onnxruntime-android, unverändert) |
| VAD   | webrtcvad          | webrtcvad (unverändert) |
| Call  | libtgvoip (x86-64) | **libtgvoip** aarch64 (NDK, OpenSLES, `TGVOIP_NO_DSP`) |

Warum Bergamot statt NLLB: Bergamot ist explizit für **on-device/Browser** gebaut
(Firefox Translations), int8-Marian-Modelle ~15 MB pro Richtung, sehr schnell auf
ARM, keine GPU/kein Google nötig. Alternative Argos Translate, falls die
Sprachpaar-Qualität besser passt.

## Warum Termux statt nativer App (zuerst)

* Termux läuft auf GrapheneOS (F-Droid), ohne Play Services.
* **Wiederverwendung**: der gesamte vorhandene Python-Code (Bot, VAD, Inferenz-API)
  läuft nahezu 1:1; nur die nativen Teile (libtgvoip, whisper.cpp, piper, marian)
  werden aarch64 gebaut.
* Eine native Kotlin-App (TDLib + tgcalls + gebündelte native Libs) ist der spätere,
  „saubere" Schritt (Hintergrunddienst, Akku, Foreground-Service) — aber deutlich
  mehr Aufwand und für den Machbarkeitsnachweis nicht nötig.

## Build-Status (auf dell-3660 vorbereitet, Deploy später)

Toolchain vorhanden aus der osmcycle-APK-Arbeit:
`~/.buildozer/android/platform/` → **NDK r25b + r28c**, android-sdk, adb.

Cross-Build-Artefakte (arm64-v8a, minSdk 26) werden auf dem Server erzeugt und
später per Termux/adb aufs Gerät gebracht:

* [x] `opus` + `OpenSSL` (libcrypto/libssl) aarch64 — `android/deps/` (`build` inline)
* [x] `libtgvoip.a` aarch64 (callback-audio, `TGVOIP_NO_DSP`) — `build_tgvoip_android.sh`,
      verifiziert `elf64-littleaarch64`, 385 VoIPController-Symbole
* [x] `whisper.cpp` aarch64 (`libwhisper.so`, `whisper-cli`, `whisper-server`) — NDK r25b,
      verifiziert `ELF ARM aarch64, for Android 26`
* [x] Termux-Bootstrap (`termux_bootstrap.sh`) + On-Device-Server
      (`inference_server_ondevice.py`, gleiche `/stt /translate /tts`-API)
* [ ] `piper` + `argostranslate`: On-Device via `pip` im Termux-Bootstrap (erst am Gerät)
* [ ] `_tgvoip`-Extension: On-Device in Termux gegen die vorgebaute `libtgvoip.a` gelinkt
* [ ] Deploy + Modell-/Latenz-Tuning am Gerät

Die aarch64-Binärartefakte (`android/`, `models/`) liegen build-seitig auf
dell-3660 und sind aus dem Git ausgenommen; im Repo stehen nur die Build-/Bootstrap-
Skripte und der On-Device-Server.

## Stufenplan (Build zuerst, Deploy später)

1. **Native Bausteine cross-compilen** (dieser Schritt): libtgvoip + whisper.cpp
   für arm64-v8a mit dem NDK. Damit steht das Fundament, ohne Gerät.
2. **Termux-Bootstrap**: Skript, das auf dem Pixel Python, Pyrogram, webrtcvad,
   Piper, Bergamot/Argos installiert und die vorgebauten `.so` einbindet.
3. **Lokaler Inferenz-Server** (Termux): gleiche `/stt /translate /tts`-API mit
   whisper.cpp + Bergamot + Piper → `INFER=127.0.0.1:9095`.
4. **Bot on-device**: `telegram_translate_bot.py` unverändert, auf localhost.
5. **Deploy & Tuning** (später, mit Gerät): Modellgröße (small vs. base), `SILENCE_FR`,
   Chunk-Länge, ggf. Vulkan-Backend für whisper.cpp auf der Mali-G715.

## Risiken / offene Punkte

* `libtgvoip`-NDK-Build: OpenSSL-android + opus-android bereitstellen; OpenSLES-
  Audio-Backend statt ALSA/Pulse; `TGVOIP_NO_DSP` wie auf dem Server.
* whisper.cpp *medium* ist auf dem G3 zu langsam → small/base + Quantisierung;
  Qualität < Server. Vulkan-Backend (Mali-G715) kann helfen, ist aber frickelig.
* Bergamot vs. Argos: Qualität des de↔en-Paars gegentesten.
* Termux-Python + native pybind11-Extension: on-device bauen oder als vorgebautes
  Wheel/`.so` mitliefern; NDK-API-Level und OpenSSL-Version beachten.
* Akku/Wärme bei Dauer-Inferenz; Foreground-Service/Wakelock einplanen.
* Kein Play Services → keine Google-Modelle; bewusst so gewählt (GrapheneOS).

## Empfehlung

Voll on-device unter GrapheneOS ist mit dem **quelloffenen Stack machbar**; die
Pausenerkennung bleibt identisch, die Latenz landet realistisch bei ~1,5–3 s (statt
GPU-1–2 s) und die STT-Qualität etwas unter Whisper *medium*. Wer die
Spitzenqualität/-latenz *und* echtes Offline will, muss beim Modell einen
Kompromiss eingehen — oder Whisper *medium* über die Mali-GPU (Vulkan) evaluieren.
```
