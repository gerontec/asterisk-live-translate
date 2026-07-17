# SIP-End-to-End-Test mit Latenzmessung

`sip_e2e_test.py` misst die Übersetzungskette so, wie ein Anrufer sie erlebt:
Es registriert sich als **echte SIP-Nebenstelle**, ruft eine Echo-Nebenstelle an,
spielt einen deutschen Satz ein, schneidet die Antwort mit und protokolliert die
Zeiten nach `wagodb.sip_latency`.

Der Bot geht denselben Weg wie das Pixel: SIP über public IPv6 nach ipgate1,
Loopback im Translator, Inferenz auf der Tesla P4 im dell. Damit grenzt ein
Vergleich Bot ↔ Handy Fehler sauber ein — genau so kam heraus, dass Linphone
`ulaw` (8 kHz) aushandelte, während der Bot `G.722` (16 kHz) bekam.

## Aufbau

```
baresip (dell, SIP-User 6002)
   │  REGISTER/INVITE über ipgate1.heissa.de   (public IPv6, G.722 16 kHz)
   ▼
Asterisk ipgate1 · exten 2039 → Ansage → Beep → AudioSocket :9093
   ▼
audiosocket-translator (ipgate1)  ── INFER ──►  yt6.heissa.de:9095 (Tesla P4)
   ▼
englisches/italienisches TTS zurück in denselben Call → Mitschnitt → STT-Kontrolle
```

## Aufruf

```bash
export SIP_PASS=...          # Passwort der Nebenstelle 6002 (pjsip.conf, ipgate1)
export WAGODB_PASS=...       # sonst --no-db

/home/gh/python/sip_e2e_test.py --exten 2039 --lang it
/home/gh/python/sip_e2e_test.py --exten 2044 --lang en --wav test_data/q2_de.wav
/home/gh/python/sip_e2e_test.py --exten 2049 --lang de --no-db
```

| Option | Bedeutung |
|--------|-----------|
| `--exten` | Echo-Nebenstelle (siehe `TranslateServicePaths.md`) |
| `--lang` | Zielsprache — **nur für die Kontroll-STT**; ohne sie transkribiert `/stt?lang=` in der falschen Sprache und „übersetzt" die Antwort zurück |
| `--wav` | deutsche Eingabe (16 kHz mono) |
| `--lead` | Stille vor dem Satz, Vorgabe 9 s (Ansage ~6,5 s + `Wait(1)` + Beep) |
| `--wait` | wie lange auf das Echo gewartet wird |
| `--no-db` | kein DB-Insert |

## Gemessene Werte → `wagodb.sip_latency`

| Spalte | Bedeutung |
|--------|-----------|
| `setup_ms` | Dial → Answer |
| `speech_end_ms` | **die interessante Zahl**: Ende des deutschen Satzes → erster Ton der Antwort |
| `stt_ms`, `trl_ms`, `tts_ms` | Einzelzeiten aus dem Translator-Journal auf ipgate1 |
| `sample_rate` | aus dem Mitschnitt — zeigt, ob wirklich 16 kHz ankamen |
| `en_text` | was zurückkam, per Kontroll-STT gegengelesen |

Referenzmessung 17.07.2026, „Wie geht es Ihnen heute?" → „Come stai oggi?":
`setup 906 ms · speech_end 1090 ms · stt 810 ms · trl 290 ms · tts 220 ms · 16000 Hz`

## Fallstricke, die beim Bau Zeit gekostet haben

* **`menu.so` laden.** Ohne dieses baresip-Modul kennt `ctrl_tcp` keine
  Kommandos: `{"response":true,"ok":false,"data":"command not found (dial)"}`.
  Ebenso nötig: `account.so` (sonst gibt es gar keinen User-Agent) und
  `module_path /usr/lib/baresip/modules`.
* **`aufile` legt am Dateiende auf.** Die Quell-WAV braucht hinten Stille, sonst
  bricht der Call nach der Satzlänge ab (`Inbound connection lost` nach 1,26 s
  bei einer 1,36-s-Datei).
* **Vorlauf-Stille.** Die Nebenstelle spielt erst Ansage und Beep; wer sofort
  redet, spricht ins Leere (`segs=0`). Daher `--lead`.
* **baresip 1.1.0 kennt kein `auresamp`/`auconv`**, und `ausrc_srate` /
  `audio_channels` in der Konfig verhindern den Start der Sendeseite — im Log
  fehlt dann `Set audio encoder`.
* **`/stt` will rohes PCM**, nicht WAV, und die Sprache als Query-Parameter:
  `POST /stt?lang=en`, `Content-Type: application/octet-stream`.
* **Die `-dec`-Spur enthält nur empfangenes Audio**, ohne Stille dazwischen —
  Ansage, Beep und Echo liegen direkt hintereinander. Ein Zeitversatz *innerhalb*
  dieser Datei misst deshalb nichts; die Latenz wird per Wanduhr genommen.
