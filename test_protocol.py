#!/home/gh/python/venv_py311/bin/python3
"""
test_protocol.py — AudioSocket Translator Protokoll-Test
=========================================================
Ziel: +4917625257878  →  Suffix "78" → Sprache: it (default)

Beobachtet das AudioSocket-Protokoll auf beiden Seiten ohne echtes Asterisk:

  Inbound  (DE-Sprecher) → spielt deutschen Satz ein
  Outbound (IT-Partner)  → spielt italienischen Satz ein

Jeder eingehende/ausgehende AudioSocket-Frame wird geloggt.
Kein GPU / kein echtes Whisper nötig — STT/TRL/TTS sind gemockt.
"""

import asyncio
import io
import json
import logging
import struct
import sys
import time
import uuid as uuid_mod
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

sys.path.insert(0, "/home/gh/python/translator")

# ── Protokoll-Konstanten (identisch mit Translator) ──────────────────────────
AS_UUID_T   = 0x01
AS_AUDIO_T  = 0x10
AS_HANGUP_T = 0xFF
SR_AS       = 8000
FRAME_MS    = 20
FRAME_S8    = SR_AS * FRAME_MS // 1000   # 160 samples
FRAME_B8    = FRAME_S8 * 2              # 320 bytes

# ── Test-Konfiguration ────────────────────────────────────────────────────────
EXTEN       = "+4917625257878"   # Suffix "78" → remote_lang="it", kein Suffix-Stripping
T_AS_PORT   = 19093
T_REG_PORT  = 19094

MOCK_DE_IN  = "Guten Tag, wie geht es Ihnen?"
MOCK_IT_IN  = "Buongiorno, come stai?"
MOCK_DE_IT  = "Buon giorno, come stai?"    # DE→IT mock
MOCK_IT_DE  = "Guten Tag, wie geht es dir?" # IT→DE mock

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
)
log = logging.getLogger("test_proto")


# ══════════════════════════════════════════════════════════════════════════════
# AudioSocket Helfer
# ══════════════════════════════════════════════════════════════════════════════

def pkt_uuid(uuid_str: str) -> bytes:
    return struct.pack(">BH", AS_UUID_T, 16) + uuid_mod.UUID(uuid_str).bytes

def pkt_audio(pcm: bytes) -> bytes:
    return struct.pack(">BH", AS_AUDIO_T, len(pcm)) + pcm

def pkt_hangup() -> bytes:
    return struct.pack(">BH", AS_HANGUP_T, 0)

async def as_read(r: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr     = await r.readexactly(3)
    mtype   = hdr[0]
    length  = struct.unpack(">H", hdr[1:3])[0]
    payload = await r.readexactly(length) if length else b""
    return mtype, payload

def make_speech_pcm(n_frames: int = 40) -> bytes:
    """440 Hz Sinuswelle — VAD-Patch erkennt als Sprache."""
    n   = n_frames * FRAME_S8
    t   = np.arange(n) / SR_AS
    arr = (np.sin(2 * np.pi * 440 * t) * 20000).astype(np.int16)
    return arr.tobytes()

def make_silence_pcm(n_frames: int) -> bytes:
    return b"\x00" * (n_frames * FRAME_B8)

def make_tts_pcm(lang: str, duration_ms: int = 600) -> bytes:
    """Synthetisches TTS-PCM: kurzer Sinuston (differenziert nach Sprache)."""
    n    = int(SR_AS * duration_ms / 1000)
    freq = 800 if lang == "it" else 600
    t    = np.arange(n) / SR_AS
    arr  = (np.sin(2 * np.pi * freq * t) * 15000).astype(np.int16)
    return arr.tobytes()

type_name = {AS_UUID_T: "UUID", AS_AUDIO_T: "AUDIO", AS_HANGUP_T: "HANGUP"}


# ══════════════════════════════════════════════════════════════════════════════
# Protokoll-Sniffer: sendet und empfängt AudioSocket-Pakete und loggt alles
# ══════════════════════════════════════════════════════════════════════════════

class ProtoSide:
    """Verwaltet eine AudioSocket-Verbindung, loggt jeden Frame."""

    def __init__(self, label: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.label   = label
        self.r       = reader
        self.w       = writer
        self.sent:   list[tuple[str, int]] = []   # (type_name, bytes)
        self.recvd:  list[tuple[str, int]] = []

    def send_pkt(self, raw: bytes, ptype: str, size: int) -> None:
        self.w.write(raw)
        self.sent.append((ptype, size))
        log.info(f"  [{self.label}] → {ptype:5s}  {size:5d} B")

    def send_uuid(self, uuid_str: str) -> None:
        self.send_pkt(pkt_uuid(uuid_str), "UUID", 16)

    async def drain(self) -> None:
        await self.w.drain()

    def send_speech(self, n_frames: int = 40) -> None:
        """Schickt Sprach-Frames (Sinuswelle) an den Translator."""
        pcm = make_speech_pcm(n_frames)
        for i in range(0, len(pcm), FRAME_B8):
            frame = pcm[i : i + FRAME_B8].ljust(FRAME_B8, b"\x00")
            self.send_pkt(pkt_audio(frame), "AUDIO", len(frame))

    def send_silence(self, n_frames: int) -> None:
        """Schickt Stille-Frames — triggert VAD-Segmenterkennung."""
        pcm = make_silence_pcm(n_frames)
        for i in range(0, len(pcm), FRAME_B8):
            frame = pcm[i : i + FRAME_B8]
            self.send_pkt(pkt_audio(frame), "AUDIO", len(frame))

    async def collect_audio(self, timeout: float = 20.0) -> bytes:
        """Sammelt AudioSocket-Audio-Pakete bis zum Timeout ohne neues Paket."""
        collected = bytearray()
        deadline  = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                mtype, payload = await asyncio.wait_for(
                    as_read(self.r), timeout=min(remaining, 0.5)
                )
            except asyncio.TimeoutError:
                if collected:
                    break
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break

            tname = type_name.get(mtype, f"0x{mtype:02x}")
            self.recvd.append((tname, len(payload)))
            log.info(f"  [{self.label}] ← {tname:5s}  {len(payload):5d} B")

            if mtype == AS_HANGUP_T:
                break
            if mtype == AS_AUDIO_T and payload:
                collected.extend(payload)
        return bytes(collected)

    def summary(self) -> str:
        total_sent  = sum(b for _, b in self.sent  if _ == "AUDIO")
        total_recvd = sum(b for _, b in self.recvd if _ == "AUDIO")
        return (
            f"{self.label}  "
            f"gesendet: {len(self.sent)} Pkts / {total_sent} B Audio  |  "
            f"empfangen: {len(self.recvd)} Pkts / {total_recvd} B Audio"
        )


# ══════════════════════════════════════════════════════════════════════════════
# HTTP-Registrierung
# ══════════════════════════════════════════════════════════════════════════════

async def register_uuid(uuid: str, exten: str, port: int = T_REG_PORT) -> bool:
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        body    = json.dumps({"uuid": uuid, "exten": exten}).encode()
        request = (
            f"POST /register HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"\r\n"
        ).encode() + body
        w.write(request)
        await w.drain()
        resp = await asyncio.wait_for(r.read(256), timeout=3.0)
        w.close()
        ok = b"200" in resp
        log.info(f"[REG] uuid={uuid[:8]} exten={exten!r} → {'OK' if ok else 'FEHLER'}")
        return ok
    except Exception as exc:
        log.error(f"[REG] Fehler: {exc}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Fake-AMI (socketpair — kein echtes Asterisk)
# ══════════════════════════════════════════════════════════════════════════════

class FakeAMI:
    """
    Akzeptiert die AMI-Verbindung vom Translator, antwortet auf Login/Originate,
    und signalisiert dem Test via asyncio.Event wenn Originate eintrifft.
    """
    def __init__(self):
        self.triggered    = asyncio.Event()
        self.number       = ""
        self.partner_uuid = ""
        self._server      = None

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0           # OS wählt freien Port
        )
        log.info(f"[FakeAMI] Port {self.port}")

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        log.info("[FakeAMI] AMI-Verbindung eingegangen")
        w.write(b"Asterisk Call Manager/5.0.0\r\n")
        await w.drain()

        buf = ""
        while True:
            try:
                line = (await asyncio.wait_for(r.readline(), timeout=10.0)).decode("utf-8", errors="replace")
            except asyncio.TimeoutError:
                break
            if not line.strip() and buf.strip():
                action = ""
                for l in buf.splitlines():
                    l = l.strip()
                    if l.lower().startswith("action:"):
                        action = l.split(":", 1)[1].strip().lower()
                    if l.lower().startswith("channel:"):
                        self.number = l.split(":", 1)[1].strip()
                    if "partner_uuid" in l.lower() and "=" in l:
                        self.partner_uuid = l.split("=", 1)[1].strip()

                log.info(f"[FakeAMI] Action={action!r}  Channel={self.number!r}  Partner={self.partner_uuid!r}")

                if action == "login":
                    w.write(b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n")
                    await w.drain()
                elif action == "originate":
                    w.write(b"Response: Success\r\nMessage: Originate successfully queued\r\n\r\n")
                    await w.drain()
                    self.triggered.set()
                elif action == "logoff":
                    w.write(b"Response: Goodbye\r\n\r\n")
                    await w.drain()
                    break
                buf = ""
            else:
                buf += line
        w.close()


# ══════════════════════════════════════════════════════════════════════════════
# Outbound-Leg via socketpair direkt in _out_waiters injizieren
# ══════════════════════════════════════════════════════════════════════════════

async def inject_outbound_leg(ast_module, partner_uuid: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """
    Erstellt ein Socketpaar, gibt eine Seite dem Translator-Future,
    die andere Seite ist die Test-Kontrolle des Outbound-Legs.
    """
    import socket as _socket
    s_test, s_translator = _socket.socketpair()
    s_test.setblocking(False)
    s_translator.setblocking(False)

    loop = asyncio.get_running_loop()

    # Translator-Seite: StreamReader/Writer für _out_waiters
    tr_reader  = asyncio.StreamReader()
    tr_protocol = asyncio.StreamReaderProtocol(tr_reader)
    tr_transport, _ = await loop.create_connection(lambda: tr_protocol, sock=s_translator)
    tr_writer = asyncio.StreamWriter(tr_transport, tr_protocol, tr_reader, loop)

    # Test-Seite
    te_reader  = asyncio.StreamReader()
    te_protocol = asyncio.StreamReaderProtocol(te_reader)
    te_transport, _ = await loop.create_connection(lambda: te_protocol, sock=s_test)
    te_writer = asyncio.StreamWriter(te_transport, te_protocol, te_reader, loop)

    # Future im Translator auflösen
    if partner_uuid in ast_module._out_waiters and not ast_module._out_waiters[partner_uuid].done():
        ast_module._out_waiters[partner_uuid].set_result((tr_reader, tr_writer))
        log.info(f"[Inject]  Outbound-Future aufgelöst: {partner_uuid}")
    else:
        raise RuntimeError(f"Kein Waiter für {partner_uuid!r} — bekannte: {list(ast_module._out_waiters)}")

    return te_reader, te_writer


# ══════════════════════════════════════════════════════════════════════════════
# Haupt-Test
# ══════════════════════════════════════════════════════════════════════════════

SEPARATOR = "═" * 70

async def run() -> bool:
    print(f"\n{SEPARATOR}")
    print("  AudioSocket Translator — Protokoll-Test")
    print(f"  Ziel: {EXTEN}  (Suffix '78' → remote_lang=it)")
    print(SEPARATOR)

    # ── Mocks definieren ────────────────────────────────────────────────────
    async def mock_stt(pcm8: bytes, lang: str, lock) -> str:
        text = MOCK_DE_IN if lang == "de" else MOCK_IT_IN
        log.info(f"[MockSTT] lang={lang.upper()}  → {text!r}")
        return text

    def mock_translate(text: str, fl: str, tl: str) -> str:
        result = MOCK_DE_IT if fl == "de" else MOCK_IT_DE
        log.info(f"[MockTRL] {fl.upper()}→{tl.upper()}  {text!r} → {result!r}")
        return result

    async def mock_tts(text: str, lang: str) -> bytes:
        pcm = make_tts_pcm(lang)
        log.info(f"[MockTTS] lang={lang.upper()}  {text!r}  → {len(pcm)//2} samples")
        return pcm

    def fake_is_speech(self, frame: bytes, sample_rate: int) -> bool:
        pcm = np.frombuffer(frame[:FRAME_B8], dtype=np.int16)
        return bool(np.abs(pcm).mean() > 500)

    # ── Fake-AMI starten ────────────────────────────────────────────────────
    ami = FakeAMI()
    await ami.start()

    import webrtcvad as wrtcvad

    with patch.object(wrtcvad.Vad, "is_speech", fake_is_speech):

        # Translator-Modul laden und konfigurieren
        import audiosocket_translator as ast
        ast.AS_PORT       = T_AS_PORT
        ast.REG_PORT      = T_REG_PORT
        ast.AMI_PORT      = ami.port
        ast.load_models   = lambda: None
        ast.stt           = mock_stt
        ast.translate_sync = mock_translate
        ast.tts           = mock_tts

        server_task = asyncio.create_task(ast.amain())
        await asyncio.sleep(0.4)
        log.info(f"Translator gestartet  AS={T_AS_PORT}  REG={T_REG_PORT}  AMI→{ami.port}")

        inbound_uuid  = str(uuid_mod.uuid4())
        outbound_uuid = f"out-{inbound_uuid}"

        # ── Registrierung ────────────────────────────────────────────────────
        print(f"\n{SEPARATOR}")
        print(f"  SCHRITT 1 · Registrierung  uuid={inbound_uuid[:8]}…  exten={EXTEN}")
        print(SEPARATOR)
        ok = await register_uuid(inbound_uuid, EXTEN)
        assert ok, "HTTP-Registrierung fehlgeschlagen"

        # ── Inbound-Leg ──────────────────────────────────────────────────────
        print(f"\n{SEPARATOR}")
        print(f"  SCHRITT 2 · Inbound-Leg (DE-Sprecher) verbindet")
        print(SEPARATOR)
        raw_in_r, raw_in_w = await asyncio.open_connection("127.0.0.1", T_AS_PORT)
        inbound  = ProtoSide("Inbound ", raw_in_r, raw_in_w)
        inbound.send_uuid(inbound_uuid)
        await inbound.drain()

        # ── Warten auf AMI-Originate ─────────────────────────────────────────
        print(f"\n{SEPARATOR}")
        print(f"  SCHRITT 3 · Warte auf AMI-Originate …")
        print(SEPARATOR)
        await asyncio.wait_for(ami.triggered.wait(), timeout=5.0)
        log.info(f"[FakeAMI] Originate erhalten: {ami.number}  partner={ami.partner_uuid}")

        # ── Outbound-Leg injizieren ──────────────────────────────────────────
        print(f"\n{SEPARATOR}")
        print(f"  SCHRITT 4 · Outbound-Leg (IT-Partner) injizieren")
        print(SEPARATOR)
        await asyncio.sleep(0.1)
        raw_out_r, raw_out_w = await inject_outbound_leg(ast, outbound_uuid)
        outbound = ProtoSide("Outbound", raw_out_r, raw_out_w)
        await asyncio.sleep(0.4)
        log.info("Beide Legs verbunden — Audio-Bridge aktiv")

        # ══════════════════════════════════════════════════════════════════════
        # TEST A: DE-Sprecher → IT-Partner
        # ══════════════════════════════════════════════════════════════════════
        print(f"\n{SEPARATOR}")
        print(f"  TEST A · DE→IT")
        print(f"  Eingabe  : {MOCK_DE_IN!r}")
        print(f"  Erwartet : IT-TTS von {MOCK_DE_IT!r} auf Outbound-Leg")
        print(SEPARATOR)

        collect_out = asyncio.create_task(outbound.collect_audio(timeout=20.0))
        inbound.send_speech(n_frames=40)
        inbound.send_silence(n_frames=30)
        await inbound.drain()
        out_pcm = await collect_out

        # Warten bis Echo-Unterdrückungs-Sleep im Translator abgelaufen ist
        # (tts_duration_s + 0.35 ≈ 0.95 s) — sonst ist w_re noch stumm wenn Test B startet
        await asyncio.sleep(1.5)

        print(f"\n  Outbound empfangen: {len(out_pcm)} Bytes  "
              f"({len(out_pcm)//2/SR_AS*1000:.0f} ms Audio)")
        a_ok = len(out_pcm) > 0
        print(f"  {'✓ IT-Partner hört die Übersetzung' if a_ok else '✗ KEIN AUDIO!'}")

        # ══════════════════════════════════════════════════════════════════════
        # TEST B: IT-Partner → DE-Sprecher
        # ══════════════════════════════════════════════════════════════════════
        print(f"\n{SEPARATOR}")
        print(f"  TEST B · IT→DE")
        print(f"  Eingabe  : {MOCK_IT_IN!r}")
        print(f"  Erwartet : DE-TTS von {MOCK_IT_DE!r} auf Inbound-Leg")
        print(SEPARATOR)

        collect_in = asyncio.create_task(inbound.collect_audio(timeout=20.0))
        outbound.send_speech(n_frames=40)
        outbound.send_silence(n_frames=30)
        await outbound.drain()
        in_pcm = await collect_in

        print(f"\n  Inbound empfangen: {len(in_pcm)} Bytes  "
              f"({len(in_pcm)//2/SR_AS*1000:.0f} ms Audio)")
        b_ok = len(in_pcm) > 0
        print(f"  {'✓ DE-Sprecher hört die Übersetzung' if b_ok else '✗ KEIN AUDIO!'}")

        # ══════════════════════════════════════════════════════════════════════
        # Zusammenfassung
        # ══════════════════════════════════════════════════════════════════════
        print(f"\n{SEPARATOR}")
        print("  PROTOKOLL-ZUSAMMENFASSUNG")
        print(SEPARATOR)
        print(f"  AMI Originate  : {ami.number or '(nicht empfangen)'}")
        print(f"  Partner UUID   : {ami.partner_uuid or '(unbekannt)'}")
        print()
        print(f"  {inbound.summary()}")
        print(f"  {outbound.summary()}")
        print()
        print(f"  DE-Eingabe  : {MOCK_DE_IN!r}")
        print(f"  IT-Ausgabe  : {MOCK_DE_IT!r}  →  {len(out_pcm)} B auf Outbound")
        print(f"  IT-Eingabe  : {MOCK_IT_IN!r}")
        print(f"  DE-Ausgabe  : {MOCK_IT_DE!r}  →  {len(in_pcm)} B auf Inbound")
        print()
        ok = a_ok and b_ok
        print(f"  {'✓ ALLE TESTS BESTANDEN' if ok else '✗ FEHLGESCHLAGEN'}")
        print(SEPARATOR)

        # Aufräumen
        inbound.w.write(pkt_hangup())
        try:
            await inbound.drain()
        except Exception:
            pass
        await asyncio.sleep(0.3)
        for w in (inbound.w, outbound.w):
            try:
                w.close()
            except Exception:
                pass

        server_task.cancel()
        try:
            await asyncio.wait_for(server_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass

    await ami.stop()
    return ok


if __name__ == "__main__":
    success = asyncio.run(run())
    sys.exit(0 if success else 1)
