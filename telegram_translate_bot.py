#!/home/gh/python/venv_tgcall/bin/python3
"""
Telegram Live-Übersetzungstelefon — DE→EN Echo
==============================================
Answers incoming Telegram voice calls. The caller speaks GERMAN; after every
utterance they hear the ENGLISH translation echoed back into the call.

Reuses the existing asterisk-live-translate inference server (HTTP 127.0.0.1:9095):
    POST /stt?lang=de   raw SLIN16 PCM 16 kHz  → {"chunks":[…]}
    POST /translate     {"text","from","to"}   → {"result":"…"}
    POST /tts           {"text","lang"}         → audio/wav 16 kHz

Audio path: Telegram delivers/consumes 48 kHz s16 mono; EVERYTHING is processed
at 16 kHz (resample 48↔16). VAD/segmentation mirrors audiosocket_translator.py
(webrtcvad aggressiveness 2, 20 ms frames, 15 silence-frames end an utterance).

Voice engine: self-built libtgvoip + pytgvoip (tgvoip / tgvoip_pyrogram), driven
by Pyrogram 1.x as a userbot.
"""
import io
import logging
import struct
import threading
import wave
from collections import deque

import numpy as np
import requests
import webrtcvad
from scipy import signal as sp

from pyrogram import Client, idle
# tgvoip_pyrogram was written for Pyrogram 1.x (client.send); on Pyrogram 2.x
# (needed so Telegram allows login — 1.x is rejected with UPDATE_APP_TO_LOGIN)
# the raw call is invoke(). Shim it back so tgvoip_pyrogram keeps working.
if not hasattr(Client, "send"):
    Client.send = Client.invoke
from tgvoip_pyrogram import VoIPService

from tg_credentials import API_ID, API_HASH

# ── Configuration ──────────────────────────────────────────────────
SESSION  = "/home/gh/python/telegram_translate/telegram_translate"

INFER      = "http://[::1]:9095"   # inference server binds IPv6 [::]:9095
SRC_LANG   = "de"          # caller speaks German
DST_LANG   = "en"          # echo back English

SR_TG      = 48000         # Telegram VoIP sample rate (s16 mono)
SR_WORK    = 16000         # inference pipeline sample rate — process EVERYTHING at 16 kHz
DECIM      = SR_TG // SR_WORK           # 3
FRAME_MS   = 20
FRAME_TG   = SR_TG  * FRAME_MS // 1000  # 960 samples @48k  → one VAD block
FRAME_WORK = SR_WORK * FRAME_MS // 1000 # 320 samples @16k  → one 20 ms VAD frame

VAD_AGGR    = 2
SILENCE_FR  = 15   # ≈300 ms of silence ends an utterance (matches the project)
MIN_SPEECH_FR = 8  # ignore blips shorter than ~160 ms

GREETING_EN = "Connected. Please speak German. You will hear the English translation."

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("/tmp/telegram_translate.log"), logging.StreamHandler()],
)
log = logging.getLogger("tgtranslate")


# ── Resampling helpers (exact integer ratio 48k↔16k) ───────────────
def down_48_to_16(pcm48: bytes) -> np.ndarray:
    x = np.frombuffer(pcm48, dtype=np.int16).astype(np.float32)
    return sp.resample_poly(x, 1, DECIM)

def up_16_to_48_bytes(x16: np.ndarray) -> bytes:
    y = sp.resample_poly(x16, DECIM, 1)
    return y.clip(-32768, 32767).astype(np.int16).tobytes()


# ── Inference-server calls (blocking; run in worker thread) ────────
def infer_stt(pcm16: bytes) -> list[str]:
    r = requests.post(f"{INFER}/stt?lang={SRC_LANG}", data=pcm16, timeout=60)
    r.raise_for_status()
    return r.json().get("chunks", [])

def infer_translate(text: str) -> str:
    r = requests.post(f"{INFER}/translate",
                      json={"text": text, "from": SRC_LANG, "to": DST_LANG}, timeout=60)
    r.raise_for_status()
    return r.json().get("result", "")

def infer_tts_16k(text: str) -> np.ndarray:
    r = requests.post(f"{INFER}/tts", json={"text": text, "lang": DST_LANG}, timeout=60)
    r.raise_for_status()
    with wave.open(io.BytesIO(r.content), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32)


# ── Per-call translation session ───────────────────────────────────
class CallSession:
    def __init__(self, call):
        self.call = call
        self.vad = webrtcvad.Vad(VAD_AGGR)
        self._raw48 = bytearray()        # incoming 48k bytes awaiting 20 ms framing
        self._speech = bytearray()       # accumulated 16k s16 speech of current utterance
        self._speech_fr = 0
        self._silence = 0
        self._in_speech = False
        self._play = deque()             # queued 48k s16 bytes to send back
        self._play_lock = threading.Lock()
        self._out_buf = bytearray()      # leftover 48k bytes for send-frame slicing
        self._busy = False               # a translation is currently being synthesised

        call.ctrl.set_recv_audio_frame_callback(self._on_recv)
        call.ctrl.set_send_audio_frame_callback(self._on_send)

    # queue 16k float audio for playback (resampled to 48k)
    def _enqueue_16k(self, x16: np.ndarray):
        pcm48 = up_16_to_48_bytes(x16)
        with self._play_lock:
            self._play.append(pcm48)

    def greet(self):
        threading.Thread(target=self._synthesize_and_play,
                         args=(GREETING_EN,), daemon=True).start()

    def _synthesize_and_play(self, english_text: str):
        try:
            self._enqueue_16k(infer_tts_16k(english_text))
        except Exception as e:
            log.warning(f"TTS(greeting) failed: {e}")

    # ── incoming caller audio (German), 48k s16, called from C++ thread ──
    def _on_recv(self, frame: bytes):
        try:
            self._raw48.extend(frame)
            need = FRAME_TG * 2  # bytes per 20 ms @48k
            while len(self._raw48) >= need:
                block = bytes(self._raw48[:need])
                del self._raw48[:need]
                x16 = down_48_to_16(block)                       # 320 samples @16k
                pcm16 = x16.clip(-32768, 32767).astype(np.int16).tobytes()
                is_speech = self.vad.is_speech(pcm16, SR_WORK)
                if is_speech:
                    self._speech.extend(pcm16)
                    self._speech_fr += 1
                    self._silence = 0
                    self._in_speech = True
                elif self._in_speech:
                    self._speech.extend(pcm16)
                    self._silence += 1
                    if self._silence >= SILENCE_FR:
                        self._finalize_utterance()
        except Exception as e:
            log.warning(f"recv error: {e}")

    def _finalize_utterance(self):
        pcm16 = bytes(self._speech)
        fr = self._speech_fr
        self._speech = bytearray()
        self._speech_fr = 0
        self._silence = 0
        self._in_speech = False
        if fr < MIN_SPEECH_FR or self._busy:
            return
        self._busy = True
        threading.Thread(target=self._process, args=(pcm16,), daemon=True).start()

    def _process(self, pcm16: bytes):
        try:
            chunks = infer_stt(pcm16)
            german = " ".join(c.strip() for c in chunks).strip()
            if not german:
                return
            english = infer_translate(german)
            log.info(f"DE {german!r} → EN {english!r}")
            if english.strip():
                self._enqueue_16k(infer_tts_16k(english))
        except Exception as e:
            log.warning(f"process error: {e}")
        finally:
            self._busy = False

    # ── outgoing audio to caller (English echo), must return `length` bytes ──
    def _on_send(self, length: int) -> bytes:
        with self._play_lock:
            while len(self._out_buf) < length and self._play:
                self._out_buf.extend(self._play.popleft())
        if len(self._out_buf) >= length:
            out = bytes(self._out_buf[:length])
            del self._out_buf[:length]
            return out
        out = bytes(self._out_buf)
        self._out_buf = bytearray()
        return out.ljust(length, b"\x00")   # silence-pad


# ── main ───────────────────────────────────────────────────────────
def main():
    app = Client(SESSION, api_id=API_ID, api_hash=API_HASH)
    app.start()
    me = app.get_me()
    log.info(f"Userbot online as {me.first_name} (id={me.id}); waiting for calls…")

    service = VoIPService(app)

    @service.on_incoming_call
    async def on_call(call):
        log.info(f"Incoming Telegram call {call.call_id} — accepting")
        session = CallSession(call)

        @call.on_call_started
        async def _started(c):
            log.info(f"Call {c.call_id} established — DE→EN echo active")
            session.greet()

        @call.on_call_ended
        async def _ended(c):
            log.info(f"Call {c.call_id} ended")

        await call.accept()

    idle()
    app.stop()


if __name__ == "__main__":
    main()
