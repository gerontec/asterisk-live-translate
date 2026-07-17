#!/home/gh/python/venv_tgcall/bin/python3
"""
Telegram Live-Dolmetscher — bidirektional, EIN Call
===================================================
Der Bot ruft einen beliebigen Telegram-Partner an (ausgehend). Du bist an *deinem*
Ende eine echte Gesprächspartei (Mikro + Lautsprecher des Geräts, auf dem der Bot
läuft — Ziel: Pixel):

    [dein Mikro DE] → STT(de)→de→en→TTS(en) → Telegram → Partner hört EN
    [Partner EN]    → Telegram → STT(en)→en→de→TTS(de) → [dein Lautsprecher DE]

Alles bei 16 kHz (Telegram-Transport 48 kHz ↔ resample). Nutzt denselben
Inferenz-Server (/stt /translate /tts). Der symmetrische Übersetzer wird für beide
Richtungen wiederverwendet.

Aufruf:  telegram_interpreter.py <ziel>  [--audio sounddevice|files] …
  <ziel> = @username, numerische User-ID oder Telefonnummer (muss auflösbar sein).
"""
import argparse
import io
import logging
import os
import queue
import threading
import time
import wave
from collections import deque

import numpy as np
import requests
import webrtcvad
from scipy import signal as sp

from pyrogram import Client, idle
if not hasattr(Client, "send"):
    Client.send = Client.invoke
from tgvoip_pyrogram import VoIPService

from tg_credentials import API_ID, API_HASH

# ── Configuration ──────────────────────────────────────────────────
SESSION = os.environ.get("TG_SESSION", "/home/gh/python/telegram_translate/telegram_translate")
# Inference runs on the dell-3660 GPU (Tesla P4): INFER=http://[::1]:9095.
INFER   = os.environ.get("INFER", "http://127.0.0.1:9095")
SR_TG   = 48000
SR_WORK = 16000
DECIM   = SR_TG // SR_WORK
FRAME_WORK = SR_WORK * 20 // 1000        # 320 samples = 20 ms @16k
FRAME_TG_B = SR_TG * 20 // 1000 * 2      # bytes per 20 ms @48k

LOCAL_LANG  = "de"   # you speak German
REMOTE_LANG = "en"   # partner hears/speaks English

VAD_AGGR, SILENCE_FR, MIN_SPEECH_FR = 2, 15, 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("/tmp/telegram_interpreter.log"),
                              logging.StreamHandler()])
log = logging.getLogger("interp")


# ── Resampling ─────────────────────────────────────────────────────
def down_48_16(pcm48: bytes) -> np.ndarray:
    return sp.resample_poly(np.frombuffer(pcm48, np.int16).astype(np.float32), 1, DECIM)

def up_16_48_bytes(x16: np.ndarray) -> bytes:
    return sp.resample_poly(x16, DECIM, 1).clip(-32768, 32767).astype(np.int16).tobytes()


# ── Inference (blocking; called from worker threads) ───────────────
def _stt(pcm16: bytes, lang: str) -> str:
    r = requests.post(f"{INFER}/stt?lang={lang}", data=pcm16, timeout=60); r.raise_for_status()
    return " ".join(c.strip() for c in r.json().get("chunks", [])).strip()

def _translate(text: str, src: str, tgt: str) -> str:
    r = requests.post(f"{INFER}/translate", json={"text": text, "from": src, "to": tgt},
                      timeout=60); r.raise_for_status()
    return r.json().get("result", "")

def _tts16(text: str, lang: str) -> np.ndarray:
    r = requests.post(f"{INFER}/tts", json={"text": text, "lang": lang}, timeout=60)
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content), "rb") as wf:
        return np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32)


# ── Symmetric translator (one direction) ───────────────────────────
class Translator:
    """Feed 16 kHz s16 frames (any size). On each utterance:
       STT(src) → translate(src→tgt) → TTS(tgt); result (16 kHz float) → on_output()."""
    def __init__(self, src: str, tgt: str, on_output, tag: str):
        self.src, self.tgt, self.on_output, self.tag = src, tgt, on_output, tag
        self.vad = webrtcvad.Vad(VAD_AGGR)
        self._buf = bytearray()          # leftover for 20 ms framing
        self._speech = bytearray()
        self._fr = 0
        self._sil = 0
        self._in = False
        self._busy = False

    def feed(self, pcm16: bytes):
        self._buf.extend(pcm16)
        need = FRAME_WORK * 2
        while len(self._buf) >= need:
            frame = bytes(self._buf[:need]); del self._buf[:need]
            speech = self.vad.is_speech(frame, SR_WORK)
            if speech:
                self._speech.extend(frame); self._fr += 1; self._sil = 0; self._in = True
            elif self._in:
                self._speech.extend(frame); self._sil += 1
                if self._sil >= SILENCE_FR:
                    self._finalize()

    def _finalize(self):
        pcm, fr = bytes(self._speech), self._fr
        self._speech = bytearray(); self._fr = 0; self._sil = 0; self._in = False
        if fr < MIN_SPEECH_FR or self._busy:
            return
        self._busy = True
        threading.Thread(target=self._process, args=(pcm,), daemon=True).start()

    def _process(self, pcm: bytes):
        try:
            text = _stt(pcm, self.src)
            if not text:
                return
            out = _translate(text, self.src, self.tgt)
            log.info(f"[{self.tag}] {self.src}:{text!r} → {self.tgt}:{out!r}")
            if out.strip():
                self.on_output(_tts16(out, self.tgt))
        except Exception as e:
            log.warning(f"[{self.tag}] error: {e}")
        finally:
            self._busy = False


# ── Local audio backends ───────────────────────────────────────────
class SoundDeviceAudio:
    """Real device mic + speaker at 16 kHz (PortAudio)."""
    def __init__(self, on_mic):
        import sounddevice as sd
        self._sd = sd
        self._on_mic = on_mic
        self._spk = queue.Queue()
        self._spk_buf = bytearray()
        self.in_stream = sd.RawInputStream(samplerate=SR_WORK, blocksize=FRAME_WORK,
                                           channels=1, dtype="int16", callback=self._mic_cb)
        self.out_stream = sd.RawOutputStream(samplerate=SR_WORK, blocksize=FRAME_WORK,
                                             channels=1, dtype="int16", callback=self._spk_cb)

    def _mic_cb(self, indata, frames, t, status):
        self._on_mic(bytes(indata))

    def _spk_cb(self, outdata, frames, t, status):
        need = frames * 2
        while len(self._spk_buf) < need and not self._spk.empty():
            self._spk_buf.extend(self._spk.get_nowait())
        n = min(need, len(self._spk_buf))
        outdata[:n] = bytes(self._spk_buf[:n]); del self._spk_buf[:n]
        if n < need:
            outdata[n:] = b"\x00" * (need - n)

    def play16(self, x16: np.ndarray):
        self._spk.put(x16.clip(-32768, 32767).astype(np.int16).tobytes())

    def start(self):
        self.in_stream.start(); self.out_stream.start()
    def stop(self):
        self.in_stream.stop(); self.out_stream.stop()


class FileAudio:
    """Server-side logic test: mic ← a DE wav (streamed), speaker → out wav."""
    def __init__(self, on_mic, mic_wav: str, speaker_out: str):
        self._on_mic, self._mic_wav, self._out_path = on_mic, mic_wav, speaker_out
        self._spk = bytearray()

    def play16(self, x16: np.ndarray):
        self._spk.extend(x16.clip(-32768, 32767).astype(np.int16).tobytes())

    def start(self):
        threading.Thread(target=self._feed, daemon=True).start()
    def _feed(self):
        with wave.open(self._mic_wav, "rb") as wf:
            sr = wf.getframerate()
            data = np.frombuffer(wf.readframes(wf.getnframes()), np.int16).astype(np.float32)
        if sr != SR_WORK:
            data = sp.resample_poly(data, SR_WORK, sr)
        pcm = data.clip(-32768, 32767).astype(np.int16).tobytes()
        for i in range(0, len(pcm), FRAME_WORK * 2):        # stream in 20 ms frames
            self._on_mic(pcm[i:i + FRAME_WORK * 2]); time.sleep(0.02)
    def stop(self):
        with wave.open(self._out_path, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR_WORK)
            wf.writeframes(bytes(self._spk))


# ── Interpreter (wires one call + both directions) ─────────────────
class Interpreter:
    def __init__(self, audio_factory):
        self._tg_send = deque()                 # 48 kHz bytes → partner
        self._tg_send_buf = bytearray()
        self._lock = threading.Lock()
        # mic (DE) → EN → partner
        self.mic_tr = Translator(LOCAL_LANG, REMOTE_LANG, self._to_partner, "mic→partner")
        # partner (EN) → DE → speaker
        self.spk_tr = Translator(REMOTE_LANG, LOCAL_LANG, self._to_speaker, "partner→me")
        self.audio = audio_factory(self.mic_tr.feed)   # local mic frames → mic translator
        self._recv_buf = bytearray()

    def _to_partner(self, x16: np.ndarray):
        with self._lock:
            self._tg_send.append(up_16_48_bytes(x16))

    def _to_speaker(self, x16: np.ndarray):
        self.audio.play16(x16)

    # Telegram callbacks (native thread) --------------------------------
    def on_tg_recv(self, frame: bytes):          # partner audio 48 kHz
        try:
            self._recv_buf.extend(frame)
            while len(self._recv_buf) >= FRAME_TG_B:
                block = bytes(self._recv_buf[:FRAME_TG_B]); del self._recv_buf[:FRAME_TG_B]
                x16 = down_48_16(block)
                self.spk_tr.feed(x16.clip(-32768, 32767).astype(np.int16).tobytes())
        except Exception as e:
            log.warning(f"tg recv: {e}")

    def on_tg_send(self, length: int) -> bytes:  # audio to partner 48 kHz
        with self._lock:
            while len(self._tg_send_buf) < length and self._tg_send:
                self._tg_send_buf.extend(self._tg_send.popleft())
        out = bytes(self._tg_send_buf[:length]); del self._tg_send_buf[:length]
        return out.ljust(length, b"\x00")

    def attach(self, call):
        call.ctrl.set_recv_audio_frame_callback(self.on_tg_recv)
        call.ctrl.set_send_audio_frame_callback(self.on_tg_send)
        self.audio.start()


# ── main ───────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="@username, user id or phone number to call")
    ap.add_argument("--audio", choices=["sounddevice", "files"], default="sounddevice")
    ap.add_argument("--mic-wav", help="files mode: DE input wav")
    ap.add_argument("--speaker-out", default="/tmp/interp_speaker_de.wav")
    args = ap.parse_args()

    def audio_factory(on_mic):
        if args.audio == "files":
            return FileAudio(on_mic, args.mic_wav, args.speaker_out)
        return SoundDeviceAudio(on_mic)

    interp = Interpreter(audio_factory)

    app = Client(SESSION, api_id=API_ID, api_hash=API_HASH)
    app.start()
    me = app.get_me()
    log.info(f"Interpreter online as {me.first_name}; calling {args.target} …")
    service = VoIPService(app)

    call = service.outgoing_call_class(args.target, client=app)

    @call.on_call_started
    async def _started(c):
        log.info(f"Call {c.call_id} established — bidirectional DE↔EN interpreter active")
        interp.attach(c)

    @call.on_call_ended
    async def _ended(c):
        log.info(f"Call {c.call_id} ended")

    app.loop.create_task(call.request())
    idle()
    app.stop()


if __name__ == "__main__":
    main()
