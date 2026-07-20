#!/usr/bin/env python3
"""
call_latency.py — Latenz-Messung je Call für den AudioSocket-Translator (ipgate1).

Nicht-invasiv: liest den Translator-Log (/tmp/translator.log[.1]) und wertet pro
Call (uuid) die Pipeline-Latenz aus. Kein Eingriff in den laufenden Dienst.

Pro übersetztem Segment:
  STT   Whisper-Zeit
  TRL   Übersetzungs-Zeit (Summe aller Chunks)
  TTS   Sprachsynthese-Zeit (Summe aller Chunks)
  PIPE  = STT + TRL + TTS      (reine Inferenz-Arbeit, inkl. Netz-Hop nach yt6)
  WALL  = Zeit STT-start→fertig (inkl. GPU-Queue-Wartezeit + Pacing)

Modi:
  --report            Einmal-Auswertung des vorhandenen Logs (Default wenn kein --follow)
  --follow            Live: berichtet jedes Segment + Call-Zusammenfassung fortlaufend
  --csv DATEI         zusätzlich je Segment eine CSV-Zeile anhängen
  --probe [TEXT]      aktiver Latenz-Test gegen den Inference-Server (translate+tts RTT)
  --log DATEI         abweichender Logpfad (Default /tmp/translator.log)
"""
import argparse, os, re, subprocess, sys, statistics as st
from collections import defaultdict

LOG_DEFAULT = "/tmp/translator.log"

# ── Zeilen-Parser ──────────────────────────────────────────────────
LINE = re.compile(
    r'^(?P<ts>\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(?P<ms>\d{3}) '
    r'(?P<lvl>\w+) \[(?P<tag>.*?)\] (?P<msg>.*)$'
)
UUID8      = re.compile(r'^[0-9a-f]{8}$')
RE_STT     = re.compile(r'^STT\(([\d.]+)s\)')
RE_TRL     = re.compile(r'^TRL\[(\d+)/(\d+)\]\(([\d.]+)s\)')
RE_TTS     = re.compile(r'^TTS\[(\d+)/(\d+)\]\(([\d.]+)s\)')
RE_STTSTART= re.compile(r'→\s+TRANSLATING\b.*\+\s*([\d.]+)s\s+(.*?)\s+STT start')
RE_OK      = re.compile(
    r'→\s+CONNECTED\b.*\+\s*([\d.]+)s\s+(?P<dir>\S+?)\s+ok\s+#(?P<n>\d+)\s+'
    r'stt=([\d.]+)s\s+trl=([\d.]+)s\s+tts=([\d.]+)s')
RE_INBOUND = re.compile(
    r'uuid=(?P<uuid>[0-9a-f]{8})\S*\s+exten=(?P<exten>\S+)'
    r'(?:.*?remote=(?P<remote>\w+))?(?:.*?state=(?P<state>\w+))?(?:.*?dur=(?P<dur>[\d.]+)s)?')


class Call:
    __slots__ = ("uuid", "exten", "direction", "segs", "dur", "done")
    def __init__(self, uuid):
        self.uuid = uuid; self.exten = "?"; self.direction = "?"
        self.segs = []            # list of dict(stt,trl,tts,pipe,wall,chunks)
        self.dur = None; self.done = False


class Tracker:
    def __init__(self, on_segment=None, on_call=None, csv=None):
        self.calls = {}
        self.active = None        # uuid des Segments zwischen STT-start und ok
        self.acc = None           # {stt,trl,tts,chunks,start_elapsed}
        self.on_segment = on_segment
        self.on_call = on_call
        self.csv = csv

    def _call(self, uuid):
        c = self.calls.get(uuid)
        if c is None:
            c = self.calls[uuid] = Call(uuid)
        return c

    def feed(self, line):
        m = LINE.match(line.rstrip("\n"))
        if not m:
            return
        tag, msg, ts = m["tag"], m["msg"], m["ts"] + "," + m["ms"]

        # Inbound-Metadaten / Call-Ende
        if tag == "Inbound":
            im = RE_INBOUND.search(msg)
            if im:
                c = self._call(im["uuid"])
                if im["exten"]:  c.exten = im["exten"]
                if im["remote"]: c.direction = f"DE↔{im['remote'].upper()}"
                if im["dur"]:    c.dur = float(im["dur"])
                if im["state"] == "DONE" and not c.done:
                    c.done = True
                    if self.on_call: self.on_call(c)
            return

        # Session-Transitions (tag = uuid8)
        if UUID8.match(tag):
            sm = RE_STTSTART.search(msg)
            if sm:
                self.active = tag
                self.acc = {"stt": 0.0, "trl": 0.0, "tts": 0.0,
                            "chunks": 0, "start": float(sm.group(1))}
                d = sm.group(2).strip()
                if d: self._call(tag).direction = d
                return
            om = RE_OK.search(msg)
            if om:
                c = self._call(tag)
                c.direction = om["dir"]
                a = self.acc if (self.active == tag and self.acc) else None
                if a and a["chunks"]:            # Worker-Zeilen gesehen → exakte Summen
                    stt, trl, tts = a["stt"], a["trl"], a["tts"]; chunks = a["chunks"]
                else:                            # Fallback: Werte aus der ok-Zeile
                    stt, trl, tts = float(om.group(3)), float(om.group(4)), float(om.group(5))
                    chunks = 1
                pipe = stt + trl + tts
                wall = float(om.group(1)) - (a["start"] if a else float(om.group(1)))
                seg = {"stt": stt, "trl": trl, "tts": tts, "pipe": pipe,
                       "wall": max(0.0, wall), "chunks": chunks}
                c.segs.append(seg)
                self.active = None; self.acc = None
                if self.csv:
                    self.csv.write(f"{ts},{c.uuid},{c.exten},{c.direction},"
                                   f"{len(c.segs)},{chunks},{stt:.2f},{trl:.2f},"
                                   f"{tts:.2f},{pipe:.2f},{seg['wall']:.2f}\n")
                    self.csv.flush()
                if self.on_segment: self.on_segment(c, seg)
            return

        # Worker-Zeilen (tag = Richtung, z.B. DE→EN[LOOP]) — zum aktiven Segment
        if self.acc is not None:
            mm = RE_STT.match(msg)
            if mm: self.acc["stt"] += float(mm.group(1)); return
            mm = RE_TRL.match(msg)
            if mm: self.acc["trl"] += float(mm.group(3)); self.acc["chunks"] = int(mm.group(2)); return
            mm = RE_TTS.match(msg)
            if mm: self.acc["tts"] += float(mm.group(3)); return


# ── Ausgabe-Helfer ─────────────────────────────────────────────────
def _stats(vals):
    if not vals: return (0, 0, 0, 0)
    s = sorted(vals)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return (min(s), sum(s) / len(s), p95, max(s))

def call_summary(c):
    if not c.segs:
        return f"call {c.uuid} exten={c.exten} {c.direction}  segs=0  (keine Übersetzung)"
    pipe = [s["pipe"] for s in c.segs]; wall = [s["wall"] for s in c.segs]
    stt = [s["stt"] for s in c.segs]; trl = [s["trl"] for s in c.segs]; tts = [s["tts"] for s in c.segs]
    pmn, pav, p95, pmx = _stats(pipe); wmn, wav, w95, wmx = _stats(wall)
    dur = f"{c.dur:.1f}s" if c.dur is not None else "?"
    return (f"call {c.uuid} exten={c.exten:<5} {c.direction:<12} "
            f"segs={len(c.segs):<2} dur={dur}\n"
            f"    PIPE  avg {pav:5.2f}s  p95 {p95:5.2f}s  max {pmx:5.2f}s   "
            f"(STT {sum(stt)/len(stt):.2f} · TRL {sum(trl)/len(trl):.2f} · TTS {sum(tts)/len(tts):.2f})\n"
            f"    WALL  avg {wav:5.2f}s  p95 {w95:5.2f}s  max {wmx:5.2f}s   (inkl. GPU-Queue+Pacing)")


# ── Modi ───────────────────────────────────────────────────────────
def read_lines_batch(logpath):
    for p in (logpath + ".1", logpath):
        if os.path.exists(p):
            with open(p, encoding="utf-8", errors="replace") as f:
                yield from f

def do_report(args):
    tr = Tracker()
    for ln in read_lines_batch(args.log):
        tr.feed(ln)
    calls = [c for c in tr.calls.values() if c.segs or c.done]
    if not calls:
        print("Keine Calls im Log gefunden."); return
    all_pipe, all_wall = [], []
    print(f"\n===== Call-Latenz-Report ({len(calls)} Call(s), Quelle {args.log}[.1]) =====\n")
    for c in sorted(calls, key=lambda x: x.uuid):
        print(call_summary(c)); print()
        all_pipe += [s["pipe"] for s in c.segs]; all_wall += [s["wall"] for s in c.segs]
    if all_pipe:
        pmn, pav, p95, pmx = _stats(all_pipe); wmn, wav, w95, wmx = _stats(all_wall)
        print(f"----- gesamt: {len(all_pipe)} Segment(e) -----")
        print(f"  PIPE  min {pmn:.2f}  avg {pav:.2f}  p95 {p95:.2f}  max {pmx:.2f}  s")
        print(f"  WALL  min {wmn:.2f}  avg {wav:.2f}  p95 {w95:.2f}  max {wmx:.2f}  s")

def do_follow(args):
    csv = None
    if args.csv:
        new = not os.path.exists(args.csv)
        csv = open(args.csv, "a", encoding="utf-8")
        if new: csv.write("ts,uuid,exten,dir,seg,chunks,stt,trl,tts,pipe,wall\n")
    def on_seg(c, s):
        print(f"  {c.uuid} {c.direction:<12} seg#{len(c.segs):<2} "
              f"STT {s['stt']:.2f} + TRL {s['trl']:.2f} + TTS {s['tts']:.2f} = "
              f"PIPE {s['pipe']:.2f}s   WALL {s['wall']:.2f}s", flush=True)
    def on_call(c):
        print("─" * 72); print(call_summary(c)); print("─" * 72, flush=True)
    tr = Tracker(on_segment=on_seg, on_call=on_call, csv=csv)
    print(f"[follow] überwache {args.log} … (Ctrl-C zum Beenden)\n", flush=True)
    proc = subprocess.Popen(["tail", "-n", "0", "-F", args.log],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    try:
        for ln in proc.stdout:
            tr.feed(ln)
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        if csv: csv.close()

def do_probe(args):
    import http.client, json, time
    host = os.environ.get("INFER_HOST"); port = int(os.environ.get("INFER_PORT", "9095"))
    if not host:  # aus /opt/translator/.env lesen
        try:
            for l in open("/opt/translator/.env"):
                if l.startswith("INFER_HOST"): host = l.split("=", 1)[1].strip()
                if l.startswith("INFER_PORT"): port = int(l.split("=", 1)[1].strip())
        except FileNotFoundError:
            pass
    host = host or "127.0.0.1"
    text = args.probe if isinstance(args.probe, str) and args.probe else "Guten Morgen, wie geht es Ihnen?"
    print(f"[probe] Inference-Server {host}:{port}")
    def post(path, body, ctype="application/json"):
        c = http.client.HTTPConnection(host, port, timeout=30)
        t0 = time.monotonic()
        c.request("POST", path, body=body, headers={"Content-Type": ctype})
        r = c.getresponse(); data = r.read(); dt = time.monotonic() - t0
        c.close(); return dt, r.status, data
    try:
        dt_tr, s1, d1 = post("/translate", json.dumps({"text": text, "from": "de", "to": "en"}).encode())
        res = json.loads(d1).get("result", "") if s1 == 200 else f"HTTP {s1}"
        dt_tts, s2, d2 = post("/tts", json.dumps({"text": res if s1 == 200 else text, "lang": "en"}).encode())
        print(f"  /translate  {dt_tr*1000:6.0f} ms  (HTTP {s1})  → {res!r}")
        print(f"  /tts        {dt_tts*1000:6.0f} ms  (HTTP {s2}, {len(d2)} B WAV)")
        print(f"  Σ RTT       {(dt_tr+dt_tts)*1000:6.0f} ms  (translate+tts, inkl. Netz-Hop)")
    except Exception as e:
        print(f"  FEHLER: {e}")


def main():
    ap = argparse.ArgumentParser(description="Latenz-Messung je Call (AudioSocket-Translator)")
    ap.add_argument("--follow", action="store_true", help="Live-Modus")
    ap.add_argument("--report", action="store_true", help="Einmal-Report des Logs")
    ap.add_argument("--csv", metavar="DATEI", help="Segmente als CSV anhängen")
    ap.add_argument("--log", default=LOG_DEFAULT, help=f"Logpfad (Default {LOG_DEFAULT})")
    ap.add_argument("--probe", nargs="?", const="", default=None,
                    help="aktiver RTT-Test gegen Inference-Server (optional Text)")
    a = ap.parse_args()
    if a.probe is not None:
        do_probe(a); return
    if a.follow:
        do_follow(a)
    else:
        do_report(a)

if __name__ == "__main__":
    main()
