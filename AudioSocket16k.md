# AudioSocket auf 16 kHz patchen (slin16)

Der Translator arbeitet durchgängig mit **16 kHz SLIN**. Asterisks
`app_audiosocket` unterstützt das **nicht von Haus aus** — es zwingt den Kanal
hart auf `slin` (8 kHz). Ohne Patch klingt jede Rückgabe halb so schnell und
eine Oktave zu tief, weil 16-kHz-Daten als 8 kHz ausgegeben werden.

Auf **dell-3660** liegt der Patch seit dem 17.05.2026 als Binärdatei über den
Debian-Paketdateien (`dpkg -V asterisk-modules` zeigt Prüfsummen-Abweichung bei
`app_audiosocket.so`, `res_audiosocket.so`, `format_wav.so`). Quelle und
Patchfile dazu existieren nicht mehr. Dieses Dokument rekonstruiert ihn.

## Der Patch (3 Stellen, gegen 22.9.0 verifiziert)

### 1. `apps/app_audiosocket.c` — Kanalformat

```c
/* Zeile ~117 und ~123 */
-    if (ast_set_write_format(chan, ast_format_slin)) {
+    if (ast_set_write_format(chan, ast_format_slin16)) {
-    if (ast_set_read_format(chan, ast_format_slin)) {
+    if (ast_set_read_format(chan, ast_format_slin16)) {
```

Ohne das überschreibt `AudioSocket()` jedes `Set(CHANNEL(audioreadformat)=slin16)`
aus dem Dialplan wieder auf 8 kHz.

### 2. `res/res_audiosocket.c` — Empfang (Python → Asterisk)

```c
/* Zeile ~335, Zweig AST_AUDIOSOCKET_KIND_AUDIO */
-            f.subclass.format = ast_format_slin;
+            f.subclass.format = ast_format_slin16;
```

### 3. `res/res_audiosocket.c` — **Senden (Asterisk → Python)**

```c
/* Zeile ~237 */
             } else if (ast_format_cmp(f->subclass.format, ast_format_slin16) == AST_FORMAT_CMP_EQUAL) {
-                buf[0] = AST_AUDIOSOCKET_KIND_AUDIO_SLIN16;   /* 0x12 */
+                buf[0] = AST_AUDIOSOCKET_KIND_AUDIO;          /* 0x10 */
```

**Das ist die Stelle, die man übersieht — und sie kostet Stunden.** Das
AudioSocket-Protokoll kodiert die Abtastrate im Frame-Typ:

```
AST_AUDIOSOCKET_KIND_AUDIO         = 0x10   (laut Spec: 8 kHz)
AST_AUDIOSOCKET_KIND_AUDIO_SLIN16  = 0x12
```

Der Python-Translator kennt aber **ausschließlich `AS_AUDIO = 0x10`**
(`audiosocket_translator.py`) und interpretiert dessen Nutzdaten als 16 kHz.
Patcht man nur 1. und 2., sendet Asterisk plötzlich `0x12`, der Translator
verwirft jeden Frame, und im Log steht `Loopback done: segs=0` — Audio kommt an,
aber die VAD sieht nie Sprache. Beide Richtungen müssen auf `0x10` bleiben.

## Bauen (ipgate1, Quellbaum unter `/usr/src/asterisk-22.9.0`)

```bash
cd /usr/src/asterisk-22.9.0
cp -a apps/app_audiosocket.c res/res_audiosocket.c /root/backup/   # erst sichern
# ... Patch anwenden ...
make apps                       # NICHT "make -C apps": ASTTOPDIR fehlt dann,
make res                        # -> "asterisk.h: No such file or directory"
install -m755 apps/app_audiosocket.so /usr/lib/asterisk/modules/
install -m755 res/res_audiosocket.so  /usr/lib/asterisk/modules/
systemctl restart asterisk
```

Kontrolle — im gepatchten Modul darf nur noch `slin16` vorkommen:

```bash
strings /usr/lib/asterisk/modules/app_audiosocket.so | grep -oE "ast_format_slin[0-9]*" | sort -u
# ast_format_slin16
```

## Nebenbedingungen

* **Codec des Anrufers.** Der Patch bringt nur etwas, wenn auch die SIP-Strecke
  16 kHz liefert. G.722 ist nativ 16 kHz; ulaw/alaw sind 8 kHz und werden
  hochgerechnet. Endpoints deshalb mit
  `codec_prefs_incoming_offer = prefer:configured, operation:intersect, keep:all, transcode:allow`
  konfigurieren — sonst gewinnt die Präferenz des Anrufers, und Linphone wählt
  gern ulaw.
* **PSTN bleibt 8 kHz.** Anrufe über die Trunks kommen mit `alaw` an
  (`NativeFormats: (alaw)`); daran ändert weder der Patch noch VoLTE etwas, der
  Flaschenhals ist die Zusammenschaltung.
* **Sounds.** `astdatadir` ist `/var/lib/asterisk` → Ansagen gehören nach
  `/var/lib/asterisk/sounds/en/`. `format_gsm` ist in diesem Build **nicht**
  vorhanden; registriert sind nur `g722, ulaw, alaw, wav, wav16`. 16-kHz-Ansagen
  daher als `.wav16` ablegen.
