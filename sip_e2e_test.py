#!/home/gh/python/venv_py311/bin/python3
"""
SIP-End-to-End-Testbot — misst die Uebersetzungs-Latenz ueber den echten SIP-Weg.

Registriert sich per baresip als Nebenstelle 6002 auf ipgate1 (public IPv6),
ruft eine Test-Nebenstelle (z.B. 201 = Selbst-Echo DE->EN) an, spielt eine
deutsche WAV als Mikrofon ein und schneidet die Antwort mit. Gemessen wird die
gefuehlte Latenz: Ende der deutschen Sprache -> erster englischer Ton.
Die Einzelzeiten (STT/TRL/TTS) kommen aus dem Translator-Journal auf ipgate1.

  ./sip_e2e_test.py --exten 201 --wav test_data/q1_de.wav
  ./sip_e2e_test.py --exten 201 --wav test_data/q1_de.wav --no-db
"""
import argparse, datetime, json, os, re, shutil, socket, subprocess, sys, tempfile, time, wave

REG_DOMAIN = "ipgate1.heissa.de"
SIP_USER   = "6002"
SIP_PASS   = os.environ.get("SIP_PASS", "")
CTRL_PORT  = 4444
INFER      = ("yt6.heissa.de", 9095)
DB_CFG     = dict(host=os.environ.get("WAGODB_HOST", "localhost"),
                  user=os.environ.get("WAGODB_USER", "gh"),
                  password=os.environ.get("WAGODB_PASS", ""),   # aus ~/.sip_e2e.env
                  db=os.environ.get("WAGODB_DB", "wagodb"), charset="utf8mb4")
SILENCE_DB = 500          # RMS-Schwelle fuer "hier faengt Audio an"


def log(m): print(f"{datetime.datetime.now():%H:%M:%S} {m}", flush=True)


def wav_info(path):
    with wave.open(path, "rb") as w:
        return w.getframerate(), w.getnframes() / w.getframerate()



def pad_with_silence(src, dst, lead_s, tail_s):
    """Vorne Stille, damit wir erst nach Ansage+Beep sprechen (wie ein Mensch);
    hinten Stille, weil aufile den Call am Dateiende auflegt."""
    with wave.open(src, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = w.readframes(w.getnframes())
    dur = len(pcm) / (sr * ch * sw)
    q = lambda t: b"\x00" * int(sr * ch * sw * t)
    with wave.open(dst, "wb") as o:
        o.setnchannels(ch); o.setsampwidth(sw); o.setframerate(sr)
        o.writeframes(q(lead_s) + pcm + q(tail_s))
    return dur, sr


def build_config(cfgdir, rec_dir):
    """baresip-Konfig: aufile als Mikrofon, sndfile als Mitschnitt, ctrl_tcp zur Steuerung."""
    os.makedirs(cfgdir, exist_ok=True)
    cfg = f"""
module_path             /usr/lib/baresip/modules
sip_listen              0.0.0.0:0
audio_source            aufile,{{WAV}}
audio_player            aubridge,nil
module                  account.so
module                  menu.so
module                  ctrl_tcp.so
ctrl_tcp_listen         127.0.0.1:{CTRL_PORT}
module                  aufile.so
module                  aubridge.so
module                  sndfile.so
snd_path                {rec_dir}
module                  g722.so
"""
    open(os.path.join(cfgdir, "config"), "w").write(cfg)
    accts = f"<sip:{SIP_USER}@{REG_DOMAIN}>;auth_pass={SIP_PASS};regint=60\n"
    open(os.path.join(cfgdir, "accounts"), "w").write(accts)


def ctrl(cmd, params=None, timeout=5):
    """baresip ctrl_tcp: netstring-gerahmtes JSON, Kommando und Parameter getrennt."""
    d = {"command": cmd}
    if params is not None:
        d["params"] = params
    payload = json.dumps(d).encode()
    frame = str(len(payload)).encode() + b":" + payload + b","
    s = socket.create_connection(("127.0.0.1", CTRL_PORT), timeout=timeout)
    s.sendall(frame)
    time.sleep(0.4)
    try:
        return s.recv(8192).decode(errors="replace")
    finally:
        s.close()


def last_segment(path, dst, thresh=SILENCE_DB, gap_s=0.6):
    """Die Aufnahme enthaelt Ansage, Beep und Echo hintereinander (Stille wird nicht
    mitgeschrieben). Das Echo ist der letzte zusammenhaengende Abschnitt."""
    import numpy as np
    with wave.open(path, "rb") as w:
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    win = int(sr * 0.02)
    loud = [np.sqrt((pcm[i:i+win].astype(np.float32) ** 2).mean()) > thresh
            for i in range(0, len(pcm) - win, win)]
    if not any(loud):
        return None
    gap = int(gap_s / 0.02)
    end = len(loud) - 1
    while end >= 0 and not loud[end]:
        end -= 1
    start = end
    run = 0
    while start > 0:
        if loud[start - 1]:
            run = 0
        else:
            run += 1
            if run >= gap:
                break
        start -= 1
    a0, a1 = start * win, min(len(pcm), (end + 1) * win)
    with wave.open(dst, "wb") as o:
        o.setnchannels(ch); o.setsampwidth(sw); o.setframerate(sr)
        o.writeframes(pcm[a0:a1].tobytes())
    return (a1 - a0) / sr


def first_audio_offset(path, thresh=SILENCE_DB):
    """Sekunden bis zum ersten Sample ueber der Schwelle (RMS je 20-ms-Fenster)."""
    import numpy as np
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    win = int(sr * 0.02)
    for i in range(0, len(pcm) - win, win):
        if np.sqrt((pcm[i:i+win] ** 2).mean()) > thresh:
            return i / sr, sr
    return None, sr


def stt(path, lang="en"):
    """Aufnahme durch den Inferenz-Endpunkt: rohes PCM, Sprache als Query-Parameter.
    Ohne lang=en nagelt der Server auf Deutsch fest und uebersetzt zurueck."""
    import http.client
    with wave.open(path, "rb") as w:
        pcm = w.readframes(w.getnframes())
    c = http.client.HTTPConnection(*INFER, timeout=60)
    c.request("POST", f"/stt?lang={lang}", body=pcm,
              headers={"Content-Type": "application/octet-stream"})
    r = c.getresponse().read().decode(errors="replace")
    try:
        j = json.loads(r)
        return " ".join(j.get("chunks", [])) or j.get("text", "") or r[:200]
    except Exception:
        return r[:200]


def translator_times(since_ts):
    """STT/TRL/TTS aus dem Translator-Journal auf ipgate1 fischen."""
    out = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "root@ipgate1",
         f"journalctl -u audiosocket-translator --since '{since_ts}' --no-pager -o cat"],
        capture_output=True, text=True, timeout=30).stdout
    res = {"stt_ms": None, "trl_ms": None, "tts_ms": None, "en_text": None}
    m = re.search(r"STT\(([\d.]+)s\)\s*\[DE\]\s*'([^']*)'", out)
    if m: res["stt_ms"] = int(float(m.group(1)) * 1000)
    m = re.search(r"TRL\[\d+/\d+\]\(([\d.]+)s\)\s*\[EN\]\s*'([^']*)'", out)
    if m: res["trl_ms"], res["en_text"] = int(float(m.group(1)) * 1000), m.group(2)
    m = re.search(r"TTS\[\d+/\d+\]\(([\d.]+)s\)", out)
    if m: res["tts_ms"] = int(float(m.group(1)) * 1000)
    return res


def to_db(row):
    import pymysql
    cols = ", ".join(row)
    ph   = ", ".join(["%s"] * len(row))
    con = pymysql.connect(**DB_CFG)
    with con, con.cursor() as cur:
        cur.execute(f"INSERT INTO sip_latency ({cols}) VALUES ({ph})", list(row.values()))
        con.commit()
        return cur.lastrowid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exten", default="201")
    ap.add_argument("--wav", default="/home/gh/python/translator/test_data/q1_de.wav")
    ap.add_argument("--wait", type=int, default=15, help="Sekunden auf die Antwort warten")
    ap.add_argument("--lead", type=float, default=9.0,
                    help="Stille vor dem Satz: Ansage (~6.5s) + Wait(1) + Beep abwarten")
    ap.add_argument("--lang", default="en", help="Zielsprache der Nebenstelle (fuer die Kontroll-STT)")
    ap.add_argument("--no-db", action="store_true")
    a = ap.parse_args()

    if not SIP_PASS:
        sys.exit("SIP_PASS nicht gesetzt (export SIP_PASS=...)")
    if not os.path.exists(a.wav):
        sys.exit(f"WAV fehlt: {a.wav}")

    sr_in, dur_in = wav_info(a.wav)
    log(f"DE-Eingabe: {os.path.basename(a.wav)}  {sr_in} Hz  {dur_in:.2f}s")

    tmp = tempfile.mkdtemp(prefix="sipbot_")
    padded = os.path.join(tmp, "src_padded.wav")
    dur_in, sr_in = pad_with_silence(a.wav, padded, a.lead, a.wait + 3)
    log(f"Vorlauf {a.lead}s (Ansage+Beep abwarten), Nachlauf {a.wait + 3}s")
    rec = os.path.join(tmp, "rec"); os.makedirs(rec)
    cfgdir = os.path.join(tmp, "cfg")
    build_config(cfgdir, rec)
    open(os.path.join(cfgdir, "config"), "a")
    cfgtxt = open(os.path.join(cfgdir, "config")).read().replace("{WAV}", padded)
    open(os.path.join(cfgdir, "config"), "w").write(cfgtxt)

    row = dict(ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               exten=a.exten, wav=os.path.basename(a.wav), ok=0)
    since = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    proc = None
    try:
        log("baresip starten...")
        blog = open(os.path.join(tmp, "baresip.log"), "w")
        proc = subprocess.Popen(["baresip", "-f", cfgdir],
                                stdout=blog, stderr=subprocess.STDOUT, text=True)
        time.sleep(4)

        log(f"waehle {a.exten}@{REG_DOMAIN} ...")
        t_dial = time.time()
        ctrl("dial", f"{a.exten}@{REG_DOMAIN}")

        # auf Gespraechsaufbau warten
        t_ans = None
        for _ in range(int(a.wait * 2)):
            time.sleep(0.5)
            if glob_rec(rec):
                t_ans = time.time(); break
        if t_ans:
            row["setup_ms"] = int((t_ans - t_dial) * 1000)
            log(f"verbunden nach {row['setup_ms']} ms")
        else:
            log("kein Gespraechsaufbau erkannt")

        # Gefuehlte Latenz = Ende der deutschen Sprache -> erster empfangener Ton.
        # Die -dec-Spur waechst nur, wenn wirklich Audio ankommt (Stille wird nicht
        # geschrieben), also ist ihr Wachstum der ehrlichste Zeitstempel den wir haben.
        t_speech_end = (t_ans or t_dial) + a.lead + dur_in
        log(f"warte auf das englische Echo (max {a.wait}s) ...")
        # Bis zum Satzende laeuft die Ansage -- vorher zu messen misst die Ansage.
        while time.time() < t_speech_end:
            time.sleep(0.05)
        t_first, base = None, None
        deadline = time.time() + a.wait
        while time.time() < deadline:
            f = pick_incoming(glob_rec(rec))
            if f:
                sz = os.path.getsize(f)
                if base is None:
                    base = sz
                elif sz > base + 4000:          # ~0.12s Audio -> eindeutig kein Rauschen
                    t_first = time.time()
                    break
            time.sleep(0.05)
        if t_first:
            row["speech_end_ms"] = int(max(0.0, t_first - t_speech_end) * 1000)
            log(f"erster EN-Ton -> gefuehlte Latenz {row['speech_end_ms']} ms")
        else:
            log("kein Echo innerhalb des Zeitfensters")
        time.sleep(2)                            # Rest des TTS noch aufnehmen
        ctrl("hangup")
        time.sleep(1)
    finally:
        if proc:
            proc.terminate()
            try: proc.wait(timeout=5)
            except Exception: proc.kill()

    files = glob_rec(rec)
    log(f"Mitschnitte: {[os.path.basename(f) for f in files]}")
    inc = pick_incoming(files)
    if not inc:
        row["error"] = "keine eingehende Aufnahme"
        finish(row, a, tmp); return

    off, sr_out = first_audio_offset(inc)
    row["sample_rate"] = sr_out
    if off is None:
        row["error"] = "kein Ton in der Antwort"
    else:
        seg = os.path.join(os.path.dirname(inc), "echo_only.wav")
        seg_dur = last_segment(inc, seg)
        target = seg if seg_dur else inc
        if seg_dur:
            log(f"letzter Abschnitt = Echo ({seg_dur:.2f}s), Ansage/Beep abgeschnitten")
        row["en_text"] = stt(target, lang=a.lang)
        log(f"{a.lang.upper()} erkannt: '{row['en_text']}'")
        row["ok"] = 1

    t = translator_times(since)
    row.update({k: v for k, v in t.items() if v is not None and k != "en_text"})
    if t.get("en_text"): row["en_text"] = t["en_text"]
    if all(row.get(k) for k in ("stt_ms", "trl_ms", "tts_ms")):
        row["pipeline_ms"] = row["stt_ms"] + row["trl_ms"] + row["tts_ms"]

    finish(row, a, tmp, keep=inc)


def glob_rec(d):
    import glob
    return sorted(glob.glob(os.path.join(d, "*.wav")))


def pick_incoming(files):
    """sndfile schreibt dump-<ts>-<peer>-{recv,send}.wav — wir wollen recv."""
    for f in files:
        if "recv" in os.path.basename(f) or "dec" in os.path.basename(f):
            return f
    return files[0] if files else None


def finish(row, a, tmp, keep=None):
    bl = os.path.join(tmp, "baresip.log")
    if os.path.exists(bl):
        shutil.copy(bl, "/tmp/sipbot_baresip.log")
    print("\n─── Ergebnis " + "─" * 50)
    for k, v in row.items():
        print(f"  {k:16s} {v}")
    if not a.no_db:
        try:
            rid = to_db(row)
            print(f"\n  -> wagodb.sip_latency id={rid}")
        except Exception as e:
            print(f"\n  !! DB-Insert fehlgeschlagen: {e}")
    if keep:
        dst = f"/tmp/sipbot_last_recv.wav"
        shutil.copy(keep, dst)
        print(f"  Aufnahme: {dst}")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
