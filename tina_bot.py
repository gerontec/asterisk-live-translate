#!/usr/bin/env python3
'''Tina — persistenter englischer Test-Bot, Nebenstelle 9494 (baresip).
Auto-Answer via ctrl-Events (read-only Leser) + Kommandos ueber kurzlebige
Verbindungen (robust fuer beliebig viele Calls).'''
import os, socket, json, time, subprocess, wave, tempfile, glob, threading
DOMAIN='ipgate1.heissa.de'; USER='9494'; PW=os.environ.get('TINA_PASS','CHANGEME')
BASE='/home/gh/python/translator/tina'; HELLO=BASE+'/tina_hello.wav'; ANSWER=BASE+'/tina_answer.wav'
PORT=4455
def mkwav(p,pcm,sr=16000):
    with wave.open(p,'wb') as w: w.setnchannels(1);w.setsampwidth(2);w.setframerate(sr);w.writeframes(pcm)
tmp=tempfile.mkdtemp(prefix='tina_'); rec=tmp+'/rec'; os.makedirs(rec)
mkwav(tmp+'/sil.wav', b'\x00\x00'*(16000*300))
cfg=('module_path /usr/lib/baresip/modules\nmodule account.so\nmodule menu.so\n'
     'module ctrl_tcp.so\nctrl_tcp_listen 127.0.0.1:%d\nmodule aufile.so\n'
     'audio_source aufile,%s/sil.wav\nmodule aubridge.so\naudio_player aubridge,nil\n'
     'module sndfile.so\nsnd_path %s\nmodule g722.so\nmodule mixausrc.so\n'%(PORT,tmp,rec))
open(tmp+'/config','w').write(cfg)
open(tmp+'/accounts','w').write('<sip:%s@%s>;auth_pass=%s;regint=60\n'%(USER,DOMAIN,PW))
def cmd(c,params=None):
    d={'command':c}
    if params is not None: d['params']=params
    p=json.dumps(d).encode(); f=str(len(p)).encode()+b':'+p+b','
    try:
        s=socket.create_connection(('127.0.0.1',PORT),timeout=3); s.sendall(f); time.sleep(0.15); s.close()
    except Exception as e: print('[tina] cmd err',e,flush=True)
def decsz():
    fs=glob.glob(rec+'/dump-*-dec.wav'); return max((os.path.getsize(f) for f in fs),default=0)
busy=[False]
def handle_call():
    print('[tina] call -> hello',flush=True); time.sleep(0.4)
    cmd('mixausrc_enc_start','aufile '+HELLO); time.sleep(2.2)
    base=decsz(); t=time.time()
    while time.time()-t<16:
        if decsz()>base+6000: break
        time.sleep(0.1)
    print('[tina] Frage erkannt -> Antwort',flush=True)
    cmd('mixausrc_enc_start','aufile '+ANSWER); time.sleep(5.2)
    print('[tina] auflegen',flush=True); cmd('hangup')
proc=subprocess.Popen(['baresip','-f',tmp],stdout=open(tmp+'/baresip.log','w'),stderr=subprocess.STDOUT)
time.sleep(4); print('[tina] baresip up, warte auf Calls...',flush=True)
while True:
    try:
        s=socket.create_connection(('127.0.0.1',PORT)); s.settimeout(None); buf=b''
        while True:
            data=s.recv(4096)
            if not data: break
            buf+=data
            while b':' in buf:
                head,rest=buf.split(b':',1)
                if not head.isdigit(): buf=b''; break
                n=int(head)
                if len(rest)<n+1: break
                payload=rest[:n]; buf=rest[n+1:]
                try: ev=json.loads(payload.decode(errors='replace'))
                except Exception: continue
                typ=str(ev.get('type','')) if isinstance(ev,dict) else ''
                if 'INCOMING' in typ and not busy[0]:
                    busy[0]=True; cmd('accept')
                    def run():
                        try: handle_call()
                        finally: busy[0]=False
                    threading.Thread(target=run,daemon=True).start()
        try: s.close()
        except Exception: pass
    except Exception as e:
        print('[tina] event reconnect:',e,flush=True); time.sleep(2)
