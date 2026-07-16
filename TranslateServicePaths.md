# Zwei Übersetzungs-Pfade: Tesla P4 (GPU) & Tensor G3 (on-device)

Das SIP↔Telegram-Gateway kann die Live-Übersetzung eines Anrufs auf **zwei
verschiedenen Backends** rechnen — ausgewählt **pro SIP-Nebenstelle**:

| SIP-User | Backend | Wo | Modelle |
|----------|---------|----|---------|
| `pixel`  | **Tensor G3** | on-device auf dem Pixel 8 (`10.9.0.8:9095`) | whisper.cpp + Argos + Piper (aarch64) |
| alle anderen (`poco`, …) | **Tesla P4** | GPU auf dell-3660 (`[::1]:9095`) | Whisper medium + NLLB-200 + Piper (CUDA) |

Der **Tesla-P4-Pfad** läuft produktiv; der **Tensor-G3-Pfad** ist die
On-Device-Variante für das Pixel (autark rechnen statt GPU).

## Gesamtarchitektur

```
                          ┌──────────────────── ipgate1 (VPS) ────────────────────┐
                          │  WireGuard-Hub wg0 10.9.0.1  ·  public IPv4/IPv6       │
                          │  ipgate1.heissa.de:51820 (Endpoint, wechselfest)       │
                          └───────▲───────────────────────────────▲────────────────┘
        WireGuard (LTE/WiFi egal) │                               │ WireGuard
                                  │                               │
             ┌────────────────────┴─────┐              ┌──────────┴──────────────────┐
             │ Pixel 8  10.9.0.8         │              │ dell-3660  10.9.0.6          │
             │  Linphone (SIP-User pixel)│              │  Asterisk 22 (192.168.5.23)  │
             │  + On-Device-Inferenz     │              │  + telegram_sip_gateway.py   │
             │    (Tensor G3) :9095      │              │  + Tesla-P4-Inferenz :9095   │
             └───────────────────────────┘              │  + Schorsch (Telegram-Userbot)│
                                                        └──────────────────────────────┘

Anruf-Fluss (beide Pfade identisch bis auf INFER):
  SIP-Client (DE) ──REGISTER/INVITE──► Asterisk (dell, über VPN 10.9.0.6)
     dialplan  ──AGI notifyuuid_gw──► Gateway (uuid → Zielnummer + backend)
     Asterisk ──AudioSocket 16 kHz──► telegram_sip_gateway.py
        Gateway: STT(de)→de→en→TTS(en)  über INFER  → Schorsch ruft Ziel auf Telegram an → Partner hört EN
                 Partner EN → STT(en)→en→de→TTS(de) → AudioSocket → SIP-Client hört DE
```

## Backend-Auswahl (Routing)

Die Auswahl erfolgt über den **Dialplan-Kontext** der Nebenstelle:

* Endpoint `pixel`  → context **`pixel-out`**      → Gateway nutzt `INFER = http://[10.9.0.8]:9095` (Tensor G3)
* Endpoint `poco`/… → context **`telegram-gateway`** → Gateway nutzt `INFER = http://[::1]:9095` (Tesla P4)

Der AGI `notifyuuid_gw.py` registriert pro Anruf `uuid → (Zielnummer, backend)`
beim Gateway (HTTP `:9097`); das Gateway wählt daraufhin den Inferenz-Endpunkt.
Beide Kontexte reichen die Audio per `AudioSocket(…,127.0.0.1:9096)` ins Gateway.

## Warum das VPN (WireGuard über ipgate1)

Beide Pfade brauchen stabile, überall erreichbare Adressen — besonders der
G3-Pfad, weil **dell → Pixel** eingehend verbinden muss (Mobilfunk firewallt
Inbound). WireGuard über ipgate1 löst auf einen Schlag:

* **LTE-Inbound-Firewall** — Pixel ist als `10.9.0.8` immer erreichbar
* **dynamischer IPv6-Präfix** zuhause — VPN-IP bleibt konstant
* **Fritzbox-DNS-Rebind** — VPN-IP ist nicht „lokal", kein Block
* **WiFi vs. LTE** — eine Config für überall

Der WG-Endpoint ist der **Hostname `ipgate1.heissa.de`** (→ statische VPS-IPv6),
nicht die per DHCP wechselnde IPv4.

## Komponenten & Orte

| Datei / Dienst | Ort | Aufgabe |
|----------------|-----|---------|
| `telegram_sip_gateway.py` | dell | AudioSocket-Server (9096) + Register (9097) + Schorsch-Userbot; Bridge SIP↔Telegram mit Übersetzung |
| `notifyuuid_gw.py` (AGI) | dell `/usr/share/asterisk/agi-bin` | registriert `uuid→Zielnummer` (+backend) beim Gateway |
| `inference_server_ondevice.py` | Pixel (Termux) | Tensor-G3-Inferenz (`/stt /translate /tts`, 16 kHz) — whisper.cpp + Argos + Piper |
| `inference_server.py` | dell (`venv_py311`, systemd) | Tesla-P4-Inferenz (Whisper medium + NLLB + Piper, CUDA) |
| Asterisk pjsip `[pixel]`, `[poco]` | dell `/etc/asterisk/pjsip.conf` | SIP-Nebenstellen (Auth/AOR/Endpoint), Registrierung über VPN |
| Dialplan `[pixel-out]`, `[telegram-gateway]` | dell `extensions_translator.conf` | Routing pro User zum jeweiligen Backend |
| WireGuard `wg0` | ipgate1 | VPN-Hub; Peers dell `10.9.0.6`, Pixel `10.9.0.8` |

> Secrets (SIP-Passwörter, netcup/WireGuard-Keys, `tg_credentials.py`,
> Provisioning-XMLs) liegen deployment-seitig und sind aus dem Git ausgenommen.

## Audio-Format

Durchgängig **16 kHz SLIN** — AudioSocket (Asterisk↔Gateway) und die Inferenz
laufen bei 16 kHz; nur die Telegram-Strecke (Schorsch↔Partner) wird auf 48 kHz
resampled. Wideband bis zum Handset über G722/Opus.

## Status

* ✅ Tesla-P4-Pfad produktiv (GPU-Inferenz, Gateway, Schorsch).
* ✅ SIP-Nebenstellen `poco` & `pixel` registriert über VPN (stabil, drinnen+draußen).
* ✅ WireGuard Pixel `10.9.0.8` ⇄ dell `10.9.0.6` (Hub ipgate1).
* ⏳ Tensor-G3-Pfad: On-Device-Inferenz am Pixel deployen (Termux, cross-gebaute
  aarch64-Artefakte) + Gateway-Backend-Routing scharf schalten.
