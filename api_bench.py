#!/usr/bin/env python3
"""
api_bench.py — Zeitverhalten ALLER Inference-API-Endpunkte messen.

Testet /stt, /translate, /tts, /nlu mit je >= N Messungen (Default 10) + 1 Warmup
(verworfen) und berichtet die RTT-Verteilung (min/median/avg/p95/max/stddev).

Das Test-Audio wird selbst per /tts erzeugt:
  - /stt : WAV → rohes SLIN16-PCM (Header ab)
  - /nlu : WAV-Kopien im --nlu-dir (der Endpoint LÖSCHT die Datei bei Erfolg,
           daher werden N+Puffer Kopien vorab abgelegt)

Aufruf (lokal auf dem GPU-Host):
  ./api_bench.py --host ::1 --n 10
Vom Netz aus (ohne /nlu, da server-lokale Datei nötig):
  ./api_bench.py --host yt6.heissa.de --n 10 --no-nlu
"""
import argparse, http.client, json, os, statistics as st, sys, time, wave

TEXT_TR  = "Guten Morgen, wie war Ihre Anreise hierher?"
TEXT_NUM = "plus vier neun eins sieben sieben drei vier fünf sechs, englisch."


def post(host, port, path, body, ctype="application/json", timeout=60):
    c = http.client.HTTPConnection(host, port, timeout=timeout)
    t0 = time.perf_counter()
    c.request("POST", path, body=body, headers={"Content-Type": ctype})
    r = c.getresponse(); data = r.read(); dt = time.perf_counter() - t0
    c.close()
    return dt, r.status, data


def pctl(s, q):
    if not s: return 0.0
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def measure(name, fn, n, expect=200):
    # Warmup (verworfen)
    try:
        wdt, wstatus, _ = fn()
    except Exception as e:
        return {"name": name, "err": f"warmup: {e}", "times": [], "warm": None}
    times, errs = [], 0
    for _ in range(n):
        try:
            dt, status, _ = fn()
            times.append(dt)
            if status != expect: errs += 1
        except Exception as e:
            errs += 1
    return {"name": name, "times": times, "errs": errs, "warm": wdt,
            "warm_status": wstatus}


def fmt_row(r):
    if r.get("err"):
        return f"  {r['name']:<11} FEHLER: {r['err']}"
    ms = sorted(t * 1000 for t in r["times"])
    if not ms:
        return f"  {r['name']:<11} keine Messung"
    std = st.pstdev(ms) if len(ms) > 1 else 0.0
    return (f"  {r['name']:<11} n={len(ms):<3} "
            f"min {ms[0]:7.1f}  med {st.median(ms):7.1f}  avg {sum(ms)/len(ms):7.1f}  "
            f"p95 {pctl(ms,0.95):7.1f}  max {ms[-1]:7.1f}  std {std:6.1f}  "
            f"warm {r['warm']*1000:7.1f}  err {r['errs']}")


def main():
    ap = argparse.ArgumentParser(description="Zeitverhalten aller Inference-API-Endpunkte")
    ap.add_argument("--host", default=os.environ.get("INFER_HOST", "::1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("INFER_PORT", "9095")))
    ap.add_argument("--n", type=int, default=10, help="Messungen je Endpunkt (>=10)")
    ap.add_argument("--nlu-dir", default="/tmp/api_bench_nlu")
    ap.add_argument("--no-nlu", action="store_true", help="/nlu überspringen (kein Server-Dateizugriff)")
    ap.add_argument("--json", metavar="DATEI", help="Ergebnisse als JSON speichern")
    a = ap.parse_args()
    H, P, N = a.host, a.port, max(1, a.n)

    print(f"== API-Benchmark  {H}:{P}  ·  n={N} je Endpunkt (+1 Warmup) ==")

    # ---- Test-Audio via /tts erzeugen ----
    print("Erzeuge Test-Audio per /tts …")
    try:
        _, s, wav_de = post(H, P, "/tts", json.dumps({"text": TEXT_TR, "lang": "de"}).encode())
        _, _, wav_num = post(H, P, "/tts", json.dumps({"text": TEXT_NUM, "lang": "de"}).encode())
    except Exception as e:
        print(f"FEHLER: Inference-Server nicht erreichbar: {e}"); sys.exit(1)
    if s != 200 or len(wav_de) < 100:
        print(f"FEHLER: /tts lieferte HTTP {s} / {len(wav_de)} B"); sys.exit(1)
    pcm = wav_de[44:]                       # SLIN16-Header ab → rohes PCM für /stt
    print(f"  WAV {len(wav_de)} B → PCM {len(pcm)} B ({len(pcm)/2/16000:.1f}s)")

    # ---- Endpunkt-Funktionen ----
    def f_tts():
        return post(H, P, "/tts", json.dumps({"text": TEXT_TR, "lang": "en"}).encode())
    def f_translate():
        return post(H, P, "/translate", json.dumps({"text": TEXT_TR, "from": "de", "to": "en"}).encode())
    def f_stt():
        return post(H, P, "/stt?lang=de", pcm, "application/octet-stream")

    results = []
    results.append(measure("/tts", f_tts, N))
    results.append(measure("/translate", f_translate, N))
    results.append(measure("/stt", f_stt, N))

    # ---- /nlu (server-lokale Datei, wird bei Erfolg gelöscht) ----
    nlu_paths = []
    if not a.no_nlu:
        os.makedirs(a.nlu_dir, exist_ok=True)
        for i in range(N + 2):              # Puffer, da /nlu die Datei löscht
            p = os.path.join(a.nlu_dir, f"probe_{i:03d}.wav")
            with open(p, "wb") as fh: fh.write(wav_num)
            os.chmod(p, 0o644)
            nlu_paths.append(p)
        it = iter(nlu_paths)
        def f_nlu():
            path = next(it)
            return post(H, P, "/nlu", json.dumps({"path": path, "lang": "de"}).encode())
        results.append(measure("/nlu", f_nlu, N))
        for p in nlu_paths:                 # Reste aufräumen (nicht gelöschte)
            try: os.unlink(p)
            except FileNotFoundError: pass
        try: os.rmdir(a.nlu_dir)
        except OSError: pass
    else:
        print("  (/nlu übersprungen — --no-nlu)")

    # ---- Report ----
    print("\n== RTT je Endpunkt (Millisekunden) ==")
    for r in results:
        print(fmt_row(r))
    print("\n  PIPE-Vergleich: ein Live-Segment = /stt + /translate + /tts")
    try:
        def med(name):
            r = next(x for x in results if x["name"] == name)
            return st.median([t*1000 for t in r["times"]]) if r.get("times") else 0
        print(f"  Σ median /stt+/translate+/tts = {med('/stt')+med('/translate')+med('/tts'):.1f} ms")
    except Exception:
        pass

    if a.json:
        out = {r["name"]: {"times_ms": [t*1000 for t in r.get("times", [])],
                           "warm_ms": (r["warm"]*1000 if r.get("warm") else None),
                           "errs": r.get("errs", 0), "err": r.get("err")} for r in results}
        json.dump({"host": H, "port": P, "n": N, "results": out},
                  open(a.json, "w"), indent=2, ensure_ascii=False)
        print(f"\nJSON → {a.json}")


if __name__ == "__main__":
    main()
