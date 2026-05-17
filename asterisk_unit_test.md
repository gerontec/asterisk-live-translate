# Asterisk Integration-Test — Aufbau & Betrieb

Testet die vollständige Übersetzungspipeline:
**Asterisk Local-Channel → AudioSocket → Whisper STT → NLLB → Piper TTS → AudioSocket → Asterisk**

---

## Architektur

```
  AMI Originate
  ┌─────────────────────────────────────────────────────────────┐
  │  Channel: Local/s@test-de-inject                            │
  │  Context: translator-in  Exten: +391234567839               │
  └─────────────────┬───────────────────────────────────────────┘
                    │
         ┌──────────┴──────────┐
         │  Local-Channel      │
         │  A-Seite            │  B-Seite (test-de-inject)
         │  translator-in      │  Answer → MixMonitor
         │  UUID() →           │  Playback(custom/test_de_input)
         │  notifyuuid.py →    │  ← empfängt Ital. TTS → /tmp/test_rx_de.wav
         │  AudioSocket(…)     │
         └──────────┬──────────┘
                    │ AudioSocket TCP :9093
                    ▼
         ┌──────────────────────┐
         │  audiosocket_        │
         │  translator.py       │
         │  Whisper STT (DE)    │
         │  NLLB DE→IT          │
         │  Piper TTS (IT)      │
         └──────────┬───────────┘
                    │ AMI Originate (TEST_TRUNK)
                    │  Channel: Local/+39…@test-it-phones
                    │  Application: AudioSocket
                    ▼
         ┌──────────┴──────────┐
         │  Local-Channel      │
         │  A-Seite            │  B-Seite (test-it-phones)
         │  AudioSocket(…)     │  Answer → MixMonitor
         │  ← empfängt IT-WAV  │  Playback(custom/test_it_input)
         │  → spielt Dt. TTS   │  ← empfängt Dt. TTS → /tmp/test_rx_it.wav
         └─────────────────────┘
```

---

## Dateien

| Datei | Zweck |
|---|---|
| `asterisk/pjsip.conf` | PJSIP-Accounts `test-de` / `test-it` (für manuelle Softphone-Tests) |
| `asterisk/extensions_test.conf` | Dialplan-Kontexte `test-de-inject` und `test-it-phones` |
| `generate_test_data.py` | Erzeugt 7 WAV-Testdaten via Piper (CPU, kein VRAM-Konflikt) |
| `test_data/*.wav` | Fertige Testdaten (versioniert) |
| `test_integration.py` | Führt den vollständigen Integration-Test durch |
| `test_robustness.py` | Unit-Tests der 4 Robustheitsfixes (läuft ohne Dienst-Neustart) |

---

## Testdaten

```
test_data/
  q1_de.wav  1.4s  "Wie geht es Ihnen heute?"
  q2_de.wav  1.3s  "Können Sie mir bitte helfen?"
  q3_de.wav  1.3s  "Wann kommt der nächste Zug?"
  a1_it.wav  1.8s  "Mi sento molto bene, grazie."
  a2_it.wav  1.6s  "Certamente, posso aiutarla."
  a3_it.wav  2.2s  "Il prossimo treno arriva tra dieci minuti."
  a4_it.wav  1.3s  "Prego, non c'è problema."
```

Neu generieren (CPU, keine Modelle neu laden):
```bash
./generate_test_data.py
```

---

## Einmalige Einrichtung

### 1. Asterisk-Dialplan laden

```bash
# Dialplan einbinden (falls noch nicht geschehen):
echo '#include /home/gh/python/translator/asterisk/extensions_test.conf' \
  | sudo tee -a /etc/asterisk/extensions.conf

# Dialplan neu laden (kein Neustart):
sudo asterisk -rx "dialplan reload"
sudo asterisk -rx "pjsip reload"

# Prüfen:
sudo asterisk -rx "dialplan show test-de-inject"
sudo asterisk -rx "dialplan show test-it-phones"
```

### 2. PJSIP-Accounts für manuelle Tests (optional)

Die Accounts `test-de` / `test-it` können mit jedem SIP-Client verwendet werden:

| Account | Benutzername | Passwort | Kontext |
|---|---|---|---|
| test-de | test-de | test1234 | from-internal |
| test-it | test-it | test1234 | from-internal |

Registrar: `127.0.0.1:5060`

---

## Integration-Test ausführen

### Voraussetzungen

```bash
# Asterisk läuft
sudo asterisk -rx "core show version"

# Übersetzer läuft MIT Test-Trunk:
export TEST_TRUNK='Local/%s@test-it-phones'
sudo systemctl stop audiosocket-translator    # alt stoppen (falls als Dienst)
/home/gh/python/venv_py311/bin/python3 \
  /home/gh/python/translator/audiosocket_translator.py &

# Oder .env ergänzen:
echo "TEST_TRUNK=Local/%s@test-it-phones" >> /home/gh/python/translator/.env
```

### Test starten

```bash
cd /home/gh/python/translator
TEST_TRUNK='Local/%s@test-it-phones' ./test_integration.py
```

### Erwartete Ausgabe

```
════════════════════════════════════════════════════════════
Integration-Test: DE↔IT Übersetzungs-Pipeline
════════════════════════════════════════════════════════════

── Fall 1: q1_de × a1_it ──────────────────────────────────
  DE: q1_de.wav → custom/test_de_input
  IT: a1_it.wav → custom/test_it_input
  AMI Originate → translator-in/+391234567839
  Warte auf IT-Aufnahme (/tmp/test_rx_it.wav) …
  /tmp/test_rx_it.wav: 47.200 Bytes
  Warte auf DE-Aufnahme (/tmp/test_rx_de.wav) …
  /tmp/test_rx_de.wav: 38.400 Bytes
  IT-Seite empfing (erwartet: Deutsch):    'Wie geht es Ihnen heute?'
  DE-Seite empfing (erwartet: Italienisch): 'Mi sento molto bene, grazie.'
  PASS ✓
...
Ergebnis: 4 bestanden, 0 fehlgeschlagen
```

---

## Aufnahmedateien

| Datei | Inhalt | Sprache erwartet |
|---|---|---|
| `/tmp/test_rx_it.wav` | Was IT-Seite empfängt (= dt. TTS des Übersetzers) | Deutsch |
| `/tmp/test_rx_de.wav` | Was DE-Seite empfängt (= ital. TTS des Übersetzers) | Italienisch |
| `/tmp/test_mix_de.wav` | Gemischtes Audio DE-Seite (TX+RX) | — |
| `/tmp/test_mix_it.wav` | Gemischtes Audio IT-Seite (TX+RX) | — |

---

## Diagnose

### Übersetzer läuft nicht / falsche Ports

```bash
ss -tlnp | grep 909
# Erwarte: :9093 (AudioSocket) und :9094 (HTTP)
```

### TEST_TRUNK nicht gesetzt

```
WARN: TEST_TRUNK=''
      Outbound geht zur Fritz!Box — nicht zum Test-Kontext!
```
→ Übersetzer mit `TEST_TRUNK=Local/%s@test-it-phones` neu starten.

### Dialplan nicht geladen

```bash
sudo asterisk -rx "dialplan show test-it-phones"
# Muss Einträge zeigen. Sonst:
sudo asterisk -rx "dialplan reload"
```

### Timeout bei IT-Aufnahme

Ursachen in Reihenfolge:
1. Übersetzer hat `TEST_TRUNK` nicht → wählt Fritz!Box → Timeout
2. Asterisk-Kontext `test-it-phones` nicht geladen → `dialplan reload`
3. NLLB-Übersetzung dauert länger als 90s (unwahrscheinlich bei P4)

### AudioSocket-Fehler in Asterisk-Log

```
WARNING app_audiosocket.c: Failed to receive frame from channel …
```
→ Python-Translator ist abgestürzt oder noch nicht gestartet. Log prüfen:
```bash
tail -f /tmp/translator.log
```

---

## Unit-Tests (ohne Dienst-Neustart)

Die Robustheitstests laufen gegen das Produktions-Modul ohne Modell-Load:

```bash
./test_robustness.py
# Ran 9 tests in 10.027s — OK
```

---

## Produktionsbetrieb wiederherstellen

```bash
# TEST_TRUNK aus .env entfernen (oder auskommentieren)
# Dann Übersetzer neu starten:
sudo systemctl start audiosocket-translator
```
