# Übersetzungs-Pfad: Tesla P4 (GPU, dell-3660)

Das SIP↔Telegram-Gateway rechnet die Live-Übersetzung eines Anrufs auf der
**Tesla P4** im dell-3660:

| SIP-User | Backend | Wo | Modelle |
|----------|---------|----|---------|
| `pixel`, `poco`, … | **Tesla P4** | GPU auf dell-3660 (`[::1]:9095`) | Whisper medium + NLLB-200 + Piper (CUDA) |

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
             │                           │              │  + telegram_sip_gateway.py   │
             │                           │              │  + Tesla-P4-Inferenz :9095   │
             └───────────────────────────┘              │  + Schorsch (Telegram-Userbot)│
                                                        └──────────────────────────────┘

Anruf-Fluss:
  SIP-Client (DE) ──REGISTER/INVITE──► Asterisk (dell, über VPN 10.9.0.6)
     dialplan  ──AGI notifyuuid_gw──► Gateway (uuid → Zielnummer)
     Asterisk ──AudioSocket 16 kHz──► telegram_sip_gateway.py
        Gateway: STT(de)→de→en→TTS(en)  über INFER  → Schorsch ruft Ziel auf Telegram an → Partner hört EN
                 Partner EN → STT(en)→en→de→TTS(de) → AudioSocket → SIP-Client hört DE
```

## Routing

Die Nebenstellen laufen über ihren **Dialplan-Kontext** ins Gateway:

* Endpoint `pixel`  → context **`pixel-out`**
* Endpoint `poco`/… → context **`telegram-gateway`**

Das Gateway nutzt `INFER = http://[::1]:9095` (Tesla P4).

Der AGI `notifyuuid_gw.py` registriert pro Anruf `uuid → Zielnummer` beim Gateway
(HTTP `:9097`). Beide Kontexte reichen die Audio per `AudioSocket(…,127.0.0.1:9096)`
ins Gateway.

## Warum das VPN (WireGuard über ipgate1)

Die Nebenstellen brauchen stabile, überall erreichbare Adressen. WireGuard über
ipgate1 löst auf einen Schlag:

* **dynamischer IPv6-Präfix** zuhause — VPN-IP bleibt konstant
* **Fritzbox-DNS-Rebind** — VPN-IP ist nicht „lokal", kein Block
* **WiFi vs. LTE** — eine Config für überall

Der WG-Endpoint ist der **Hostname `ipgate1.heissa.de`** (→ statische VPS-IPv6),
nicht die per DHCP wechselnde IPv4.

## Komponenten & Orte

| Datei / Dienst | Ort | Aufgabe |
|----------------|-----|---------|
| `telegram_sip_gateway.py` | dell | AudioSocket-Server (9096) + Register (9097) + Schorsch-Userbot; Bridge SIP↔Telegram mit Übersetzung |
| `notifyuuid_gw.py` (AGI) | dell `/usr/share/asterisk/agi-bin` | registriert `uuid→Zielnummer` beim Gateway |
| `inference_server.py` | dell (`venv_py311`, systemd) | Tesla-P4-Inferenz (Whisper medium + NLLB + Piper, CUDA) |
| Asterisk pjsip `[pixel]`, `[poco]` | dell `/etc/asterisk/pjsip.conf` | SIP-Nebenstellen (Auth/AOR/Endpoint), Registrierung über VPN |
| Dialplan `[pixel-out]`, `[telegram-gateway]` | dell `extensions_translator.conf` | Routing zum Gateway |
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

## Test-Nebenstelle (Selbst-Echo: Deutsch sprechen → Englisch hören)

Vom SIP-Client (Linphone `pixel`) die Ziffer wählen, Deutsch sprechen, das
englische Echo hören:

| Wählen | Backend | Inferenz | AudioSocket |
|--------|---------|----------|-------------|
| **`201`** | **Tesla P4** (GPU, dell) | Whisper medium + NLLB, `[::1]:9095` | `127.0.0.1:9098` |

Ablauf:
```
Linphone (DE) → Asterisk [pixel-out] exten 201 → AudioSocket 9098
   → audiosocket_echo.py (Translator: VAD→STT→MT→TTS, INFER=P4)
   → englisches TTS zurück in denselben Call → du hörst das Echo
```

### Komponenten
- **`audiosocket_echo.py`** — Selbst-Echo-AudioSocket-Server; Backend per Env:
  `INFER` (`http://[::1]:9095`) + `ECHO_PORT` (9098).
- Dialplan `[pixel-out]`: `exten => 201` → `AudioSocket`.
