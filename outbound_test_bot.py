#!/usr/bin/env python3
"""Outbound-Test-Bot mit reaktions-getriggerter Frage.
Ruft --dest über ipgate1 an, wartet auf die erste Reaktion der Gegenstelle
(egal was, meist 'hello') und spielt DANN sofort die Frage ein (mixausrc →
Encoding-Stream, kein fixer Vorlauf). Zeichnet die (rückübersetzte) Antwort auf.
Benötigt: SIP_PASS aus ~/.sip_e2e.env, baresip mit mixausrc-Modul."""
import os, socket, json, time, subprocess, wave, tempfile, glob, shutil, argparse

def mkwav(path, pcm, sr=16000):
    with wave.open(path, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dest', default='+4980429494')
    ap.add_argument('--question', default='/tmp/frage_de.wav')
    ap.add_argument('--domain', default='ipgate1.heissa.de')
    ap.add_argument('--user', default='6002')
    ap.add_argument('--talk', type=float, default=25.0, help='Sek. nach Frage fuers Gespraech')
    ap.add_argument('--fallback', type=float, default=25.0, help='Sek. bis Frage auch ohne Reaktion')
    ap.add_argument('--thresh', type=int, default=6000, help='dec-Bytes = Reaktion erkannt')
    a = ap.parse_args()
    SIP_PASS = os.environ.get('SIP_PASS', '')
    tmp = tempfile.mkdtemp(prefix='obot_'); rec = tmp + '/rec'; os.makedirs(rec)
    mkwav(tmp + '/silence.wav', b'\x00\x00' * (16000 * 180))
    cfg = ('module_path /usr/lib/baresip/modules\n'
           'module account.so\nmodule menu.so\nmodule ctrl_tcp.so\n'
           'ctrl_tcp_listen 127.0.0.1:4446\n'
           'module aufile.so\naudio_source aufile,%s/silence.wav\n'
           'module aubridge.so\naudio_player aubridge,nil\n'
           'module sndfile.so\nsnd_path %s\n'
           'module g722.so\nmodule mixausrc.so\n' % (tmp, rec))
    open(tmp + '/config', 'w').write(cfg)
    open(tmp + '/accounts', 'w').write('<sip:%s@%s>;auth_pass=%s;regint=60\n' % (a.user, a.domain, SIP_PASS))
    def ctrl(cmd, params=None):
        d = {'command': cmd}
        if params is not None: d['params'] = params
        p = json.dumps(d).encode(); f = str(len(p)).encode() + b':' + p + b','
        s = socket.create_connection(('127.0.0.1', 4446), timeout=5)
        s.sendall(f); time.sleep(0.2)
        try: return s.recv(4096)
        finally: s.close()
    proc = subprocess.Popen(['baresip', '-f', tmp], stdout=open(tmp + '/baresip.log', 'w'), stderr=subprocess.STDOUT)
    time.sleep(4)
    print('[obot] dialing', a.dest, flush=True); ctrl('dial', '%s@%s' % (a.dest, a.domain))
    def decsz():
        fs = glob.glob(rec + '/dump-*-dec.wav')
        return max((os.path.getsize(f) for f in fs), default=0)
    t0 = time.time(); triggered = False
    while not triggered and time.time() - t0 < a.fallback:
        time.sleep(0.15)
        if decsz() > a.thresh:
            print('[obot] Reaktion erkannt nach %.1fs -> Frage sofort' % (time.time() - t0), flush=True)
            triggered = True
    if not triggered:
        print('[obot] keine Reaktion -> Frage per Fallback', flush=True)
    ctrl('mixausrc_enc_start', 'aufile %s' % a.question)
    time.sleep(a.talk)
    ctrl('mixausrc_enc_stop'); ctrl('hangup'); time.sleep(1); proc.terminate()
    recs = sorted(glob.glob(rec + '/dump-*-dec.wav')); big = [r for r in recs if os.path.getsize(r) > 1000]
    if big:
        shutil.copy(big[-1], '/tmp/antwort.wav')
        with wave.open('/tmp/antwort.wav') as w:
            print('[obot] antwort.wav %.1fs' % (w.getnframes() / w.getframerate()), flush=True)
    else:
        print('[obot] keine Bot-Aufnahme', flush=True)

if __name__ == '__main__':
    main()
