#!/data/data/com.termux/files/usr/bin/python3
"""G3 on-device Inferenz-Server (Tensor G3, Termux). Gleiche API wie der P4-Server,
damit audiosocket_translator nur INFER_HOST wechseln muss:

  POST /stt?lang=de   raw SLIN16 PCM 16 kHz  -> {"chunks":[EN-Text]}   (whisper.cpp --translate: STT+MT)
  POST /translate     {"text","from","to"}    -> {"result": text}       (Passthrough, schon EN)
  POST /tts           {"text","lang"}          -> audio/wav 16 kHz       (espeak-ng)

Nur Standardbibliothek + whisper-cli + espeak-ng + ffmpeg.
"""
import json, os, subprocess, tempfile, urllib.parse, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME    = os.path.expanduser("~")
WHISPER = os.environ.get("WHISPER_CLI", f"{HOME}/whisper/whisper-cli")
WLIB    = os.path.dirname(WHISPER)
MODEL   = os.environ.get("MODEL", f"{HOME}/models/ggml-base.bin")
VOICE   = os.environ.get("ESPEAK_VOICE", "en-us")
HOST, PORT = "0.0.0.0", 9095


def whisper_translate(wav_path: str, lang: str) -> str:
    env = dict(os.environ, LD_LIBRARY_PATH=WLIB)
    r = subprocess.run([WHISPER, "-m", MODEL, "-l", lang, "--translate", "-nt", "-f", wav_path],
                       capture_output=True, text=True, env=env, timeout=180)
    return " ".join(l.strip() for l in r.stdout.splitlines() if l.strip())


def espeak_tts(text: str) -> bytes:
    raw = tempfile.mktemp(suffix=".wav"); out = tempfile.mktemp(suffix=".wav")
    try:
        subprocess.run(["espeak-ng", "-v", VOICE, "-s", "150", "-w", raw, text],
                       check=True, capture_output=True, timeout=40)
        subprocess.run(["ffmpeg", "-y", "-i", raw, "-ar", "16000", "-ac", "1",
                        "-c:a", "pcm_s16le", out], capture_output=True, timeout=40)
        with open(out, "rb") as f:
            return f.read()
    finally:
        for p in (raw, out):
            try: os.unlink(p)
            except OSError: pass


class H(BaseHTTPRequestHandler):
    def _body(self):
        n = int(self.headers.get("content-length", 0))
        return self.rfile.read(n) if n else b""

    def _json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        qs = urllib.parse.parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
        body = self._body()
        try:
            if path == "/stt":
                lang = qs.get("lang", ["de"])[0]
                wp = tempfile.mktemp(suffix=".wav")
                with wave.open(wp, "wb") as w:
                    w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(body)
                txt = whisper_translate(wp, lang)
                try: os.unlink(wp)
                except OSError: pass
                self._json({"chunks": [txt] if txt else []})
            elif path == "/translate":
                p = json.loads(body)
                self._json({"result": p.get("text", "")})       # already EN via whisper
            elif path == "/tts":
                p = json.loads(body)
                data = espeak_tts(p.get("text", "").strip() or " ")
                self.send_response(200); self.send_header("content-type", "audio/wav")
                self.send_header("content-length", str(len(data))); self.end_headers(); self.wfile.write(data)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"G3 inference server on {HOST}:{PORT}  model={MODEL}  voice={VOICE}")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
