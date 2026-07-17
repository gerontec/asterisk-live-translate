#!/home/gh/python/venv_tgcall/bin/python3
"""AudioSocket Selbst-Echo über den Tesla-P4-Inferenz-Pfad.
Anrufer spricht Deutsch → hört das englische Echo. Nutzt die Translator-Klasse
(VAD→STT→MT→TTS) mit der GPU-Inferenz auf dem dell-3660. Start:
    INFER=http://[::1]:9095 audiosocket_echo.py
Dialplan: Answer() + AudioSocket(<uuid>,127.0.0.1:9098)
"""
import asyncio, logging, os, struct
from collections import deque
import numpy as np
import telegram_interpreter as TI          # INFER aus Env; Translator/Resampling

AS_UUID, AS_AUDIO, AS_HANGUP = 0x01, 0x10, 0xFF
HOST = "127.0.0.1"
PORT = int(os.environ.get("ECHO_PORT", 9098))
BACKEND = os.environ.get("BACKEND", "P4")  # nur fürs Log
SR = 16000
FRAME_B = SR * 20 // 1000 * 2              # 640 Bytes = 20 ms @16k

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("g3echo")


async def as_read(r: asyncio.StreamReader):
    hdr = await r.readexactly(3)
    ln = struct.unpack(">H", hdr[1:3])[0]
    return hdr[0], (await r.readexactly(ln) if ln else b"")

def as_audio(pcm: bytes) -> bytes:
    return struct.pack(">BH", AS_AUDIO, len(pcm)) + pcm


class Session:
    def __init__(self, writer):
        self.w = writer
        self.out = deque()
        self.closed = False
        self.tr = TI.Translator("de", "en", self._emit, "g3echo")

    def _emit(self, x16: np.ndarray):                       # englisches TTS-Audio (16k)
        self.out.append(x16.clip(-32768, 32767).astype(np.int16).tobytes())

    async def reader(self, r):
        try:
            while not self.closed:
                t, p = await as_read(r)
                if t == AS_HANGUP:
                    break
                if t == AS_AUDIO:
                    self.tr.feed(p)                          # 16k DE → Translator
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.closed = True

    async def writer(self):
        buf = bytearray()
        try:
            while not self.closed:
                while self.out and len(buf) < FRAME_B:
                    buf.extend(self.out.popleft())
                if len(buf) >= FRAME_B:
                    chunk = bytes(buf[:FRAME_B]); del buf[:FRAME_B]
                else:
                    chunk = bytes(buf).ljust(FRAME_B, b"\x00"); buf = bytearray()
                self.w.write(as_audio(chunk))               # inkl. Stille (keepalive)
                await self.w.drain()
                await asyncio.sleep(0.02)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            self.closed = True


async def handle(reader, writer):
    try:
        t, p = await asyncio.wait_for(as_read(reader), timeout=5)
    except Exception:
        writer.close(); return
    if t != AS_UUID:
        writer.close(); return
    log.info(f"Echo-Call {p.hex()[:12]}")
    s = Session(writer)
    await asyncio.gather(s.reader(reader), s.writer())
    try: writer.close()
    except Exception: pass
    log.info("Call beendet")


async def main():
    srv = await asyncio.start_server(handle, HOST, PORT)
    log.info(f"[{BACKEND}] Echo AudioSocket auf {HOST}:{PORT}  INFER={TI.INFER}")
    async with srv:
        await srv.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
