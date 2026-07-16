#!/home/gh/python/venv_tgcall/bin/python3
"""
SIP ↔ Telegram Übersetzungs-Gateway
===================================
Du rufst per SIP-Client den Asterisk (192.168.5.23) an; die gewählte Nummer ist
der Telegram-Partner. Asterisk übergibt die Audio per AudioSocket an dieses
Gateway; der Telegram-Userbot (Schorsch) ruft den Partner an und übersetzt live:

    SIP (du, DE) → STT(de)→de→en→TTS(en) → Telegram → Partner hört EN
    Partner (EN) → Telegram → STT(en)→en→de→TTS(de) → AudioSocket → du hörst DE

Nur EIN Telegram-Account nötig. Reuse: AudioSocket-Protokoll + notifyuuid-Muster
(wie audiosocket_translator), der bidirektionale `Translator` und der
Inferenz-Server. Eigene Ports (AudioSocket 9096, Register 9097), damit der
Produktions-Translator (9093/9094) unangetastet bleibt.

Start:  INFER=http://[::1]:9095 telegram_sip_gateway.py
"""
import asyncio
import json
import logging
import struct
import threading
import uuid as uuidmod
from collections import deque

import numpy as np

import telegram_interpreter as TI          # reuse Translator, resampling, INFER, Client-shim
from pyrogram import Client
from tgvoip_pyrogram import VoIPService
from tg_credentials import API_ID, API_HASH

# ── Configuration ──────────────────────────────────────────────────
SESSION            = "/home/gh/python/telegram_translate/telegram_translate"
AS_HOST, AS_PORT   = "127.0.0.1", 9096      # AudioSocket (Asterisk connects here)
REG_HOST, REG_PORT = "127.0.0.1", 9097      # notifyuuid_gw.py registers uuid→number
SR_AS              = 16000                   # SLIN16 == SR_WORK (no resampling on SIP side)
FRAME_B            = SR_AS * 20 // 1000 * 2  # 640 bytes = 20 ms @16k

AS_UUID, AS_AUDIO, AS_HANGUP = 0x01, 0x10, 0xFF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler("/tmp/telegram_sip_gateway.log"),
                              logging.StreamHandler()])
log = logging.getLogger("gw")

_exten_map: dict[str, str] = {}             # uuid → dialed Telegram number
_app: Client | None = None
_service: VoIPService | None = None
_loop: asyncio.AbstractEventLoop | None = None


# ── AudioSocket helpers ────────────────────────────────────────────
async def as_read(r: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr = await r.readexactly(3)
    length = struct.unpack(">H", hdr[1:3])[0]
    payload = await r.readexactly(length) if length else b""
    return hdr[0], payload

def as_audio(pcm: bytes) -> bytes:
    return struct.pack(">BH", AS_AUDIO, len(pcm)) + pcm


# ── Telegram target resolution (import contact if needed) ──────────
async def resolve_target(number: str):
    num = number.strip()
    if num.startswith("00"):
        num = "+" + num[2:]
    elif num.startswith("0"):
        num = "+49" + num[1:]
    elif not num.startswith("+"):
        num = "+" + num
    try:
        return await _app.resolve_peer(num)
    except Exception:
        from pyrogram.raw import functions, types
        imported = await _app.invoke(functions.contacts.ImportContacts(contacts=[
            types.InputPhoneContact(client_id=0, phone=num, first_name="gw", last_name="")]))
        if imported.users:
            return await _app.resolve_peer(imported.users[0].id)
        raise


# ── Per-call bridge session ────────────────────────────────────────
class GatewaySession:
    def __init__(self, uuid: str, number: str, reader, writer):
        self.uuid, self.number = uuid, number
        self.reader, self.writer = reader, writer
        self.up = False                         # Telegram call established?
        self._tg_send = deque(); self._tg_send_buf = bytearray()
        self._lock = threading.Lock()
        self._sip_out = deque()                 # 16k s16 bytes → SIP
        self._recv48 = bytearray()              # partner 48k accumulator
        # SIP mic (DE) → EN → partner
        self.mic_tr = TI.Translator("de", "en", self._to_partner, f"{uuid[:8]} sip→tg")
        # partner (EN) → DE → SIP
        self.spk_tr = TI.Translator("en", "de", self._to_sip, f"{uuid[:8]} tg→sip")
        self.call = None
        self._closed = False

    # translator outputs ------------------------------------------------
    def _to_partner(self, x16: np.ndarray):
        with self._lock:
            self._tg_send.append(TI.up_16_48_bytes(x16))

    def _to_sip(self, x16: np.ndarray):
        self._sip_out.append(x16.clip(-32768, 32767).astype(np.int16).tobytes())

    # Telegram audio callbacks (native thread) -------------------------
    def _tg_recv(self, frame: bytes):
        try:
            self._recv48.extend(frame)
            need = 48000 * 20 // 1000 * 2        # 20 ms @48k
            while len(self._recv48) >= need:
                block = bytes(self._recv48[:need]); del self._recv48[:need]
                x16 = TI.down_48_16(block)
                self.spk_tr.feed(x16.clip(-32768, 32767).astype(np.int16).tobytes())
        except Exception as e:
            log.warning(f"[{self.uuid[:8]}] tg_recv: {e}")

    def _tg_send(self, length: int) -> bytes:
        with self._lock:
            while len(self._tg_send_buf) < length and self._tg_send:
                self._tg_send_buf.extend(self._tg_send.popleft())
        out = bytes(self._tg_send_buf[:length]); del self._tg_send_buf[:length]
        return out.ljust(length, b"\x00")

    # place the outgoing Telegram call ---------------------------------
    async def place_call(self):
        peer = await resolve_target(self.number)
        self.call = _service.outgoing_call_class(getattr(peer, "user_id", self.number),
                                                 client=_app)

        @self.call.on_call_started
        async def _started(c):
            c.ctrl.set_recv_audio_frame_callback(self._tg_recv)
            c.ctrl.set_send_audio_frame_callback(self._tg_send)
            self.up = True
            log.info(f"[{self.uuid[:8]}] Telegram call up → bridge active ({self.number})")

        @self.call.on_call_ended
        async def _ended(c):
            log.info(f"[{self.uuid[:8]}] Telegram call ended")
            self._closed = True

        # VoIPOutgoingCall wants the resolved id; resolve_peer already done above.
        self.call.user_id = getattr(peer, "user_id", self.number)
        await self.call.request()

    # AudioSocket loops -------------------------------------------------
    async def sip_reader(self):
        try:
            while not self._closed:
                mtype, payload = await as_read(self.reader)
                if mtype == AS_HANGUP:
                    break
                if mtype == AS_AUDIO and self.up:
                    self.mic_tr.feed(payload)     # 16k DE (feed only once call up)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self._closed = True

    async def sip_writer(self):
        buf = bytearray()
        try:
            while not self._closed:
                while self._sip_out and len(buf) < FRAME_B:
                    buf.extend(self._sip_out.popleft())
                if len(buf) >= FRAME_B:
                    chunk = bytes(buf[:FRAME_B]); del buf[:FRAME_B]
                else:
                    chunk = bytes(buf).ljust(FRAME_B, b"\x00"); buf = bytearray()
                self.writer.write(as_audio(chunk))     # keepalive silence too
                await self.writer.drain()
                await asyncio.sleep(0.02)
        except (ConnectionError, RuntimeError):
            pass
        finally:
            self._closed = True

    async def run(self):
        try:
            await self.place_call()
        except Exception as e:
            log.warning(f"[{self.uuid[:8]}] place_call failed for {self.number!r}: {e}")
            self.writer.close(); return
        await asyncio.gather(self.sip_reader(), self.sip_writer())
        # teardown
        try:
            if self.call:
                await self.call.discard_call()
        except Exception:
            pass
        try:
            self.writer.close()
        except Exception:
            pass
        log.info(f"[{self.uuid[:8]}] session closed")


# ── AudioSocket + register servers ─────────────────────────────────
async def handle_as(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        mtype, payload = await asyncio.wait_for(as_read(reader), timeout=5.0)
    except Exception:
        writer.close(); return
    if mtype != AS_UUID:
        writer.close(); return
    uuid = str(uuidmod.UUID(bytes=payload))
    number = _exten_map.pop(uuid, "")
    if not number:
        log.warning(f"[AS] uuid {uuid[:8]} not registered — no target")
        writer.close(); return
    log.info(f"[AS] SIP call {uuid[:8]} → Telegram target {number}")
    await GatewaySession(uuid, number, reader, writer).run()


async def handle_reg(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    raw = b""
    while b"\r\n\r\n" not in raw:
        c = await reader.read(4096)
        if not c: break
        raw += c
    _, _, body = raw.partition(b"\r\n\r\n")
    clen = 0
    for line in raw.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            clen = int(line.split(b":")[1])
    while len(body) < clen:
        c = await reader.read(clen - len(body))
        if not c: break
        body += c
    try:
        p = json.loads(body)
        uid, ext = p.get("uuid", "").strip(), p.get("exten", "").strip()
        if uid and ext:
            _exten_map[uid] = ext
            log.info(f"[REG] uuid={uid[:8]} → number={ext}")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        else:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 3\r\n\r\nERR")
    except Exception:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 3\r\n\r\nERR")
    await writer.drain(); writer.close()


async def amain():
    global _app, _service, _loop
    _loop = asyncio.get_running_loop()
    _app = Client(SESSION, api_id=API_ID, api_hash=API_HASH)
    await _app.start()
    me = await _app.get_me()
    _service = VoIPService(_app, receive_calls=False)
    log.info(f"Gateway userbot online as {me.first_name} (id={me.id})")

    reg = await asyncio.start_server(handle_reg, REG_HOST, REG_PORT)
    aso = await asyncio.start_server(handle_as, AS_HOST, AS_PORT)
    log.info(f"Register :{REG_PORT}  AudioSocket :{AS_PORT}  INFER={TI.INFER}")
    async with reg, aso:
        await asyncio.gather(reg.serve_forever(), aso.serve_forever())


if __name__ == "__main__":
    asyncio.run(amain())
