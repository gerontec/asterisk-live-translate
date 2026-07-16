#!/usr/bin/env python3
"""
On-device inference server for the Pixel 8 (Tensor G3, GrapheneOS) — fully offline,
no Google Play Services. Drop-in replacement for the GPU inference_server.py:
exposes the SAME HTTP API on 127.0.0.1:9095, so telegram_translate_bot.py runs
unchanged with INFER=http://127.0.0.1:9095.

    POST /stt?lang=de   raw SLIN16 PCM 16 kHz  → {"chunks":[…]}   (whisper.cpp)
    POST /translate     {"text","from","to"}   → {"result":"…"}   (Argos Translate)
    POST /tts           {"text","lang"}         → audio/wav 16 kHz (Piper)

Open-source stack (Google-free):
  STT : whisper.cpp  (prebuilt aarch64 whisper-cli + ggml model)
  MT  : Argos Translate (CTranslate2/OpenNMT, offline de→en package)
  TTS : Piper (onnxruntime, en_GB-alan-medium)

Everything runs at 16 kHz to match the pipeline.
"""
import io, json, os, subprocess, tempfile, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from scipy import signal as sp

# ── Configuration (paths are Termux/GrapheneOS defaults; override via env) ──
HOST, PORT = "127.0.0.1", 9095
SR = 16000

BASE        = os.environ.get("ONDEVICE_BASE", os.path.expanduser("~/telegram_translate"))
WHISPER_CLI = os.environ.get("WHISPER_CLI", f"{BASE}/android/whisper/arm64-v8a/whisper-cli")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", f"{BASE}/models/ggml-small.bin")
PIPER_MODEL = os.environ.get("PIPER_MODEL", f"{BASE}/models/en_GB-alan-medium.onnx")

# ── Lazy singletons ────────────────────────────────────────────────────────
_piper = None
_argos_ok = False

def _load():
    global _piper, _argos_ok
    from piper.voice import PiperVoice
    _piper = PiperVoice.load(PIPER_MODEL)
    try:
        import argostranslate.translate  # noqa
        _argos_ok = True
    except Exception as e:
        print(f"[warn] Argos Translate not available: {e}")

def _resample(x, a, b):
    from math import gcd
    if a == b:
        return x
    d = gcd(a, b)
    return sp.resample_poly(x, b // d, a // d).astype(np.float32)

# ── STT: whisper.cpp CLI on the PCM ───────────────────────────────────────
def stt(pcm16: bytes, lang: str) -> list[str]:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
            wf.writeframes(pcm16)
        out_prefix = wav_path + ".out"
        subprocess.run(
            [WHISPER_CLI, "-m", WHISPER_MODEL, "-f", wav_path, "-l", lang,
             "-nt", "-oj", "-of", out_prefix],
            check=True, capture_output=True, timeout=120,
        )
        with open(out_prefix + ".json", encoding="utf-8") as jf:
            data = json.load(jf)
        chunks = [seg.get("text", "").strip()
                  for seg in data.get("transcription", [])]
        return [c for c in chunks if c]
    finally:
        for p in (wav_path, wav_path + ".out.json"):
            try: os.unlink(p)
            except OSError: pass

# ── MT: Argos Translate (offline) ─────────────────────────────────────────
def translate(text: str, src: str, tgt: str) -> str:
    if src == tgt or not _argos_ok:
        return text
    import argostranslate.translate
    return argostranslate.translate.translate(text, src, tgt)

# ── TTS: Piper → SLIN16 @ 16 kHz ──────────────────────────────────────────
def tts(text: str) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        _piper.synthesize_wav(text, wf)
    buf.seek(0)
    with wave.open(buf, "rb") as wf:
        sr_p = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    audio = _resample(audio, sr_p, SR)
    return (audio * 32767).clip(-32768, 32767).astype(np.int16).tobytes()

# ── HTTP ──────────────────────────────────────────────────────────────────
class H(BaseHTTPRequestHandler):
    def _body(self):
        n = int(self.headers.get("content-length", 0))
        return self.rfile.read(n) if n else b""

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        qs = dict(p.split("=", 1) for p in self.path.split("?", 1)[1].split("&")
                  if "=" in p) if "?" in self.path else {}
        body = self._body()
        try:
            if path == "/stt":
                self._json({"chunks": stt(body, qs.get("lang", "de"))})
            elif path == "/translate":
                p = json.loads(body)
                self._json({"result": translate(p.get("text", "").strip(),
                                                p.get("from", "de"), p.get("to", "en"))})
            elif path == "/tts":
                p = json.loads(body)
                pcm = tts(p.get("text", "").strip())
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
                    wf.writeframes(pcm)
                data = buf.getvalue()
                self.send_response(200); self.send_header("content-type", "audio/wav")
                self.send_header("content-length", str(len(data))); self.end_headers()
                self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, *a):  # quiet
        pass

if __name__ == "__main__":
    print("Loading on-device models (whisper.cpp / Argos / Piper) …")
    _load()
    print(f"On-device inference server on {HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
