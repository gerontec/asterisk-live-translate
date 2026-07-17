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

## Echo-Test-Nebenstellen (Deutsch sprechen → Zielsprache hören)

Vom SIP-Client wählen, die deutsche Ansage abwarten, nach dem Beep einen
deutschen Satz sprechen — die Übersetzung kommt in denselben Call zurück.

**Schema: `20` + Ländervorwahl, durchgehend vierstellig.** Gleiche Länge heißt
keine Mehrdeutigkeit beim Overlap-Dialing.

| Nst. | Sprache | | Nst. | Sprache | | Nst. | Sprache |
|------|---------|-|------|---------|-|------|---------|
| `2049` | Deutsch¹ | | `2034` | Spanisch | | `2090` | Türkisch |
| `2044` | Englisch | | `2030` | Griechisch | | `2091` | Hindi |
| `2039` | Italienisch | | `2038` | Ukrainisch | | `2098` | Persisch |
| `2033` | Französisch | | `2048` | Polnisch | | `2077` | Kasachisch |
| `2007` | Russisch² | | `2055` | Portugiesisch | | `2995` | Georgisch² |
| `2086` | Chinesisch | | | | | | |

¹ DE→DE: transkribieren und vorlesen, prüft STT+TTS ohne Übersetzung. Erforderte
den Eintrag `"49": "de"` in `SUFFIX_LANG` — die Tabelle kannte nur Zielsprachen.
² Vorwahl 7 mit Null aufgefüllt bzw. dreistellige Vorwahl 995.

`201` bleibt als Alias auf `2044` (Englisch) erhalten.

### Wie die Sprachwahl funktioniert

Der Dialplan registriert per AGI die Nummer **`1` + Vorwahl** (z. B. `139`):

```
exten => 2039 → AGI(notifyuuid.py,${AS_UUID},139) → AudioSocket :9093
                                          │└┴─ Suffix 39 → SUFFIX_LANG → "it"
                                          └─── Rest "1", ≤2-stellig → LOOPBACK_ECHO
```

Der Translator liest die Zielsprache am **Suffix** und schaltet in den Loopback,
weil die Restziffer höchstens zweistellig ist (`audiosocket_translator.py`:
`if LOOPBACK_ECHO or not dial_number or len(dial_number) <= 2 …`).

### Von außen über die Trunks

Externe Anrufer landen in `[from-vodafone]` / `[from-dusnet]` / `[from-vsip]` und
erreichen den Extension-Nummernplan **nicht**. Dort gilt die interne Kodierung
direkt: DID wählen, die 4 s NLU-Aufnahme abwarten (schweigen), nach dem Beep
**`1` + Vorwahl** tippen — also `139#` für Italienisch. `Set(LANG_SUFFIX=)` leert
vorher den Sprach-Suffix, die getippten Ziffern gehen unverändert an den Translator.

### Ansagen

`/var/lib/asterisk/sounds/en/echotest<nst>.wav16`, erzeugt mit Piper über
`/tts` (16 kHz). Der Dialplan wartet nach `Answer()` **eine Sekunde**, bevor er
sie abspielt — vorher steht der RTP-Pfad nicht, und der Anfang verpufft.
Beep: `beep.wav16` ebendort. Siehe `AudioSocket16k.md` zu `astdatadir` und
fehlendem `format_gsm`.

### Codec

Endpoints und Trunks stehen auf `allow = g722,ulaw,alaw` mit
`codec_prefs_incoming_offer = prefer:configured, …` — G.722 (16 kHz) zuerst,
8 kHz nur als Rückfall. Ohne `prefer:configured` gewinnt die Präferenz des
Anrufers, und Linphone wählt `ulaw`.

Messungen: `SipE2eTest.md`.
