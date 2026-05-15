#!/home/gh/python/venv_py311/bin/python3
"""
SIP Translation B2BUA
Fritz!Box (DE) <-> Asterisk ARI <-> Fritz!Box/Vodafone (IT/RU)
Whisper medium auf Tesla P4 CUDA 11.8
Zielsprache via letzte 2 Ziffern der Rufnummer: xx39=IT, xx99=RU
"""

import asyncio
import aiohttp
import websockets
import json
import socket
import struct
import numpy as np
import io
import os
import logging
import argparse
from pathlib import Path

from faster_whisper import WhisperModel
import argostranslate.package as argos_pkg
import argostranslate.translate as argos_trans
import edge_tts
import soundfile as sf
import webrtcvad
from scipy import signal as scipy_signal

# --- Konfiguration ---
ARI_HOST     = "127.0.0.1"
ARI_PORT     = 8088
ARI_USER     = "translator"
ARI_PASS     = "tr4nsl4t0r"
ARI_APP      = "translator"

# Ausgehende Leitung: Fritz!Box als Trunk (empfohlen)
# Fritz!Box IP und SIP-User, den wir dort angelegt haben
TRUNK_ENDPOINT = "PJSIP/%s@fritzbox-out"   # %s = Zielrufnummer

# Alternativ: direkter Vodafone-Trunk
# TRUNK_ENDPOINT = "PJSIP/%s@vodafone-trunk"

# RTP-Ports fuer ExternalMedia (Python lauscht hier)
PORT_FRITZ  = 10000  # DE-Audio von Fritz/Linphone leg
PORT_VOIP   = 10001  # Gegenstelle-Audio (IT/RU)

# Zielsprache anhand der letzten 2 Ziffern der Rufnummer
SUFFIX_LANG = {
    "39": "it",  # endet auf 39 -> Italienisch
    "99": "ru",  # endet auf 99 -> Russisch
}

TTS_VOICES = {
    "it": "it-IT-DiegoNeural",
    "de": "de-DE-ConradNeural",
    "ru": "ru-RU-DmitryNeural",
}


def detect_target_lang(number: str) -> str:
    """Zielsprache aus den letzten 2 Ziffern der Rufnummer bestimmen."""
    suffix = number[-2:] if len(number) >= 2 else ""
    return SUFFIX_LANG.get(suffix, "it")

SAMPLE_RATE       = 16000
FRAME_MS          = 30
FRAME_SAMPLES     = int(SAMPLE_RATE * FRAME_MS / 1000)   # 480
FRAME_BYTES       = FRAME_SAMPLES * 2                     # 960
SILENCE_FRAMES    = 33   # ~1 Sek Stille -> Segment verarbeiten
SPEECH_MIN_FRAMES = 5    # Mindest-Sprachframes vor Verarbeitung

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("translator")


# ================================================================
# Modelle laden
# ================================================================

_whisper_model: WhisperModel | None = None
_whisper_lock = asyncio.Lock()  # serialisiert GPU-Zugriffe bei Gleichzeitig-Sprechen


def get_whisper() -> WhisperModel:
    """Whisper aus GPU-Cache holen oder bei Bedarf neu laden."""
    global _whisper_model
    if _whisper_model is None:
        log.info("Lade Whisper medium (CUDA 11.8)...")
        _whisper_model = WhisperModel("medium", device="cuda", compute_type="int8")
        log.info("Whisper geladen.")
    return _whisper_model


def load_models():
    get_whisper()  # Vorladen beim Start

    log.info("Pruefe Argos-Uebersetzungsmodelle DE<->IT/RU...")
    _ensure_argos_models()
    log.info("Argostranslate bereit.")


def _ensure_argos_models():
    argos_pkg.update_package_index()
    available  = argos_pkg.get_available_packages()
    installed  = {(p.from_code, p.to_code) for p in argos_pkg.get_installed_packages()}

    for fl, tl in [("de", "it"), ("it", "de"), ("de", "en"), ("en", "it"),
                   ("it", "en"), ("en", "de"),
                   ("ru", "en"), ("en", "ru"), ("ru", "de"), ("de", "ru")]:
        if (fl, tl) not in installed:
            pkg = next((p for p in available if p.from_code == fl and p.to_code == tl), None)
            if pkg:
                log.info(f"Installiere Argos {fl}->{tl} ...")
                argos_pkg.install_from_path(pkg.download())


def translate(text: str, from_lang: str, to_lang: str) -> str:
    if from_lang == to_lang:
        return text
    # Direktübersetzung bevorzugen
    try:
        result = argos_trans.translate(text, from_lang, to_lang)
        if result:
            return result
    except Exception:
        pass
    # Pivot über Englisch
    en = argos_trans.translate(text, from_lang, "en")
    return argos_trans.translate(en, "en", to_lang)


SAVE_MP3_PATH = "/home/gh/python/ghit.mp3"


async def tts_to_pcm(text: str, lang: str) -> bytes:
    """edge-tts -> 16kHz 16-bit PCM mono. Speichert non-DE TTS als MP3."""
    voice = TTS_VOICES.get(lang, TTS_VOICES["de"])
    communicate = edge_tts.Communicate(text, voice)

    chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            chunks.append(chunk["data"])

    if not chunks:
        return b""

    mp3_bytes = b"".join(chunks)

    if lang != "de":
        try:
            with open(SAVE_MP3_PATH, "wb") as fh:
                fh.write(mp3_bytes)
            log.info(f"Uebersetzung ({lang}) gespeichert: {SAVE_MP3_PATH}")
        except Exception as e:
            log.warning(f"MP3-Speichern fehlgeschlagen: {e}")

    audio, sr = sf.read(io.BytesIO(mp3_bytes), dtype="int16", always_2d=False)

    if sr != SAMPLE_RATE:
        samples = int(len(audio) * SAMPLE_RATE / sr)
        audio = scipy_signal.resample(audio, samples).astype(np.int16)

    return audio.tobytes()


# ================================================================
# RTP Hilfsfunktionen
# ================================================================

def make_rtp_packet(seq: int, ts: int, ssrc: int, payload: bytes,
                    payload_type: int = 11) -> bytes:
    header = struct.pack("!BBHII",
        0x80,
        payload_type & 0x7F,
        seq & 0xFFFF,
        ts & 0xFFFFFFFF,
        ssrc & 0xFFFFFFFF,
    )
    return header + payload


def strip_rtp_header(data: bytes) -> bytes:
    if len(data) < 12:
        return b""
    # CSRC count aus CC-Feld
    cc = data[0] & 0x0F
    offset = 12 + cc * 4
    # Extension-Header
    if data[0] & 0x10:
        if len(data) < offset + 4:
            return b""
        ext_len = struct.unpack("!H", data[offset+2:offset+4])[0]
        offset += 4 + ext_len * 4
    return data[offset:]


# ================================================================
# Sprachpuffer mit VAD
# ================================================================

class SpeechBuffer:
    def __init__(self, vad_instance):
        self.vad   = vad_instance
        self.buf   = []
        self.sil   = 0
        self.active = False

    def push(self, frame: bytes) -> bytes | None:
        """Gibt komplettes Sprachsegment zurueck wenn Stille erkannt, sonst None."""
        # Frame muss genau FRAME_BYTES lang sein fuer VAD
        padded = frame.ljust(FRAME_BYTES, b"\x00")[:FRAME_BYTES]

        try:
            is_speech = self.vad.is_speech(padded, SAMPLE_RATE)
        except Exception:
            is_speech = False

        if is_speech:
            self.buf.append(frame)
            self.sil    = 0
            self.active = True
        elif self.active:
            self.sil += 1
            self.buf.append(frame)
            if self.sil >= SILENCE_FRAMES:
                if len(self.buf) >= SPEECH_MIN_FRAMES + SILENCE_FRAMES:
                    segment = b"".join(self.buf[:-SILENCE_FRAMES])
                    self.buf    = []
                    self.sil    = 0
                    self.active = False
                    return segment
                else:
                    self.buf    = []
                    self.sil    = 0
                    self.active = False
        return None


# ================================================================
# UDP RTP Socket (non-blocking, asyncio)
# ================================================================

class UDPStream:
    def __init__(self, local_port: int):
        self.port   = local_port
        self.remote = None   # (host, port) gelernt aus eingehenden Paketen
        self.seq    = 0
        self.ts     = 0
        self.ssrc   = int.from_bytes(os.urandom(4), "big")
        self._sock  = None

    def open(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.setblocking(False)

    async def recv_frame(self) -> bytes:
        loop = asyncio.get_running_loop()
        while True:
            fut = loop.create_future()
            loop.add_reader(self._sock.fileno(), lambda: fut.set_result(None) if not fut.done() else None)
            await fut
            loop.remove_reader(self._sock.fileno())
            try:
                data, addr = self._sock.recvfrom(4096)
                if self.remote is None:
                    self.remote = addr
                    log.info(f"Port {self.port}: Asterisk RTP von {addr}")
                return strip_rtp_header(data)
            except BlockingIOError:
                continue

    def send_pcm(self, pcm: bytes):
        if not self.remote or not pcm:
            return
        chunk_size = FRAME_BYTES
        for i in range(0, len(pcm), chunk_size):
            chunk = pcm[i:i + chunk_size]
            pkt = make_rtp_packet(self.seq, self.ts, self.ssrc, chunk)
            try:
                self._sock.sendto(pkt, self.remote)
            except Exception as e:
                log.warning(f"RTP senden fehlgeschlagen: {e}")
            self.seq = (self.seq + 1) & 0xFFFF
            self.ts  = (self.ts + len(chunk) // 2) & 0xFFFFFFFF


# ================================================================
# Asterisk ARI Client
# ================================================================

class ARIClient:
    def __init__(self):
        self._base = f"http://{ARI_HOST}:{ARI_PORT}/ari"
        self._auth = aiohttp.BasicAuth(ARI_USER, ARI_PASS)
        self._sess: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self._sess = aiohttp.ClientSession(auth=self._auth)
        return self

    async def __aexit__(self, *_):
        if self._sess:
            await self._sess.close()

    async def get(self, path: str) -> dict:
        async with self._sess.get(f"{self._base}{path}") as r:
            return await r.json()

    async def post(self, path: str, **kwargs) -> dict:
        async with self._sess.post(f"{self._base}{path}", **kwargs) as r:
            text = await r.text()
            if r.status not in (200, 201, 204):
                log.error(f"ARI POST {path} -> HTTP {r.status}: {text}")
                return {}
            try:
                return json.loads(text) if text else {}
            except Exception:
                return {}

    async def delete(self, path: str):
        async with self._sess.delete(f"{self._base}{path}") as r:
            return r.status

    async def answer(self, ch_id: str):
        await self.post(f"/channels/{ch_id}/answer")

    async def hangup(self, ch_id: str):
        await self.delete(f"/channels/{ch_id}")

    async def external_media(self, port: int, channel_id_hint: str) -> str:
        result = await self.post("/channels/externalMedia", json={
            "app":           ARI_APP,
            "external_host": f"127.0.0.1:{port}",
            "format":        "slin16",
            "direction":     "both",
            "channel_id":    channel_id_hint,
        })
        return result.get("id", "")

    async def create_bridge(self) -> str:
        result = await self.post("/bridges", json={"type": "mixing"})
        return result.get("id", "")

    async def bridge_add(self, bridge_id: str, ch_id: str):
        await self.post(f"/bridges/{bridge_id}/addChannel",
                        json={"channel": ch_id})

    async def originate(self, number: str, caller_id: str = "linuxsip <+4980425641873>") -> str:
        endpoint = TRUNK_ENDPOINT % number
        safe_id = number.lstrip("+").replace("+", "00")
        result = await self.post("/channels", json={
            "endpoint":   endpoint,
            "app":        ARI_APP,
            "appArgs":    "outbound",
            "callerId":   caller_id,
            "channelId":  f"voip-out-{safe_id}",
        })
        return result.get("id", "")


# ================================================================
# Translation Worker
# ================================================================

class TranslationWorker:
    """Verarbeitet ein Sprachsegment: STT -> Uebersetzen -> TTS -> senden."""

    def __init__(self, from_lang: str, to_lang: str, output_stream: UDPStream):
        self.from_lang  = from_lang
        self.to_lang    = to_lang
        self.out        = output_stream
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=4)

    async def enqueue(self, pcm_bytes: bytes):
        try:
            self._queue.put_nowait(pcm_bytes)
        except asyncio.QueueFull:
            log.warning(f"{self.from_lang}: Queue voll, Segment verworfen")

    async def run(self):
        while True:
            pcm = await self._queue.get()
            try:
                await self._process(pcm)
            except Exception as e:
                log.error(f"Verarbeitung fehlgeschlagen: {e}", exc_info=True)

    async def _process(self, pcm: bytes):
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0

        # Whisper STT — Lock verhindert gleichzeitige GPU-Zugriffe beider Seiten
        loop = asyncio.get_running_loop()
        async with _whisper_lock:
            segments, _ = await loop.run_in_executor(
                None,
                lambda: get_whisper().transcribe(
                    audio,
                    language=self.from_lang,
                    beam_size=5,
                    vad_filter=True,
                )
            )
        text = " ".join(s.text for s in segments).strip()
        if not text:
            return

        log.info(f"[{self.from_lang.upper()}] {text}")

        # Uebersetzen
        translated = await loop.run_in_executor(
            None, lambda: translate(text, self.from_lang, self.to_lang)
        )
        log.info(f"[{self.to_lang.upper()}] {translated}")

        # TTS
        tts_pcm = await tts_to_pcm(translated, self.to_lang)

        # In Ausgangs-Stream senden
        self.out.send_pcm(tts_pcm)


# ================================================================
# Haupt-Bridge fuer ein Gespraeich
# ================================================================

class CallBridge:
    def __init__(self, ari: ARIClient, fritz_ch_id: str, dial_number: str):
        self.ari         = ari
        self.fritz_id    = fritz_ch_id
        self.dial_number = dial_number
        self.voip_id: str | None = None

        self.fritz_stream = UDPStream(PORT_FRITZ)
        self.voip_stream  = UDPStream(PORT_VOIP)

        self.remote_lang = detect_target_lang(dial_number)
        suffix = dial_number[-2:] if len(dial_number) >= 2 else ""
        self.dial_target = dial_number[:-2] if suffix in SUFFIX_LANG else dial_number
        log.info(f"Zielsprache fuer {dial_number}: {self.remote_lang.upper()} (Suffix: {suffix}), waehle {self.dial_target}")
        self.worker_de:     TranslationWorker | None = None  # DE -> remote
        self.worker_remote: TranslationWorker | None = None  # remote -> DE

    async def setup(self):
        self.fritz_stream.open()
        self.voip_stream.open()

        # Fritz-Leitung antworten
        await self.ari.answer(self.fritz_id)

        # Fritz-Channel zuerst in Bridge (er ist sicher in Stasis)
        bridge_fritz = await self.ari.create_bridge()
        await self.ari.bridge_add(bridge_fritz, self.fritz_id)

        # ExternalMedia erstellen, kurz warten bis Stasis bereit
        ext_fritz = await self.ari.external_media(PORT_FRITZ, f"ext-fritz-{self.fritz_id[:8]}")
        await asyncio.sleep(0.3)
        await self.ari.bridge_add(bridge_fritz, ext_fritz)
        log.info(f"Fritz-Bridge {bridge_fritz} mit ExternalMedia {ext_fritz} aktiv")

        # Ausgehenden Anruf aufbauen (Sprachsuffix bereits entfernt)
        self.voip_id = await self.ari.originate(self.dial_target)
        log.info(f"Ausgehender Anruf zu {self.dial_target} gestartet: {self.voip_id}")

        # Translation Worker initialisieren
        self.worker_de     = TranslationWorker("de", self.remote_lang, self.voip_stream)
        self.worker_remote = TranslationWorker(self.remote_lang, "de", self.fritz_stream)

    async def voip_connected(self):
        """Aufgerufen wenn VoIP-Leitung antwortet."""
        # VoIP-Channel zuerst (er ist sicher in Stasis)
        bridge_voip = await self.ari.create_bridge()
        await self.ari.bridge_add(bridge_voip, self.voip_id)

        # ExternalMedia erstellen, kurz warten bis Stasis bereit
        ext_voip = await self.ari.external_media(PORT_VOIP, f"ext-voip-{self.voip_id[:8]}")
        await asyncio.sleep(0.3)
        await self.ari.bridge_add(bridge_voip, ext_voip)
        log.info(f"VoIP-Bridge {bridge_voip} mit ExternalMedia {ext_voip} aktiv")

    async def start(self):
        """Setup abschliessen, dann Audio-Loops starten."""
        await self.setup()
        await asyncio.gather(
            self._recv_fritz_loop(),
            self._recv_voip_loop(),
            self.worker_de.run(),
            self.worker_remote.run(),
        )

    async def _recv_fritz_loop(self):
        vad     = webrtcvad.Vad(2)
        buf     = SpeechBuffer(vad)
        rec     = []  # alle Frames fuer Aufnahme sammeln
        while True:
            frame = await self.fritz_stream.recv_frame()
            rec.append(frame)
            segment = buf.push(frame)
            if segment:
                await self.worker_de.enqueue(segment)
            if len(rec) % 500 == 0:  # alle ~15s sichern
                self._save_wav("/home/gh/python/gh_de_in.wav", rec)

    async def _recv_voip_loop(self):
        vad     = webrtcvad.Vad(2)
        buf     = SpeechBuffer(vad)
        rec     = []
        while True:
            frame = await self.voip_stream.recv_frame()
            rec.append(frame)
            segment = buf.push(frame)
            if segment:
                await self.worker_remote.enqueue(segment)
            if len(rec) % 500 == 0:
                self._save_wav("/home/gh/python/gh_voip_in.wav", rec)

    @staticmethod
    def _save_wav(path: str, frames: list):
        try:
            pcm = b"".join(frames)
            audio = np.frombuffer(pcm, dtype=np.int16)
            sf.write(path, audio, SAMPLE_RATE)
        except Exception as e:
            log.warning(f"WAV-Speichern fehlgeschlagen ({path}): {e}")


# ================================================================
# ARI Event Loop
# ================================================================

async def main():
    load_models()

    ws_url = (f"ws://{ARI_HOST}:{ARI_PORT}/ari/events"
              f"?app={ARI_APP}&api_key={ARI_USER}:{ARI_PASS}")

    active: dict[str, CallBridge] = {}

    async with ARIClient() as ari:
        log.info("Verbinde mit Asterisk ARI ...")
        async with websockets.connect(ws_url) as ws:
            log.info("ARI bereit. Warte auf Anrufe ...")

            async for raw in ws:
                event = json.loads(raw)
                etype = event.get("type")

                if etype == "StasisStart":
                    ch     = event["channel"]
                    ch_id  = ch["id"]
                    args   = event.get("args", [])

                    if "outbound" not in args:
                        # Local-Channel ;1 ignorieren (nur ;2 verarbeiten)
                        ch_name = ch.get("name", "")
                        if "Local/" in ch_name and ";1" in ch_name:
                            log.debug(f"Local ;1 ignoriert: {ch_id}")
                            continue
                        exten = ch.get("dialplan", {}).get("exten", "")
                        if not exten.startswith("+"):
                            log.debug(f"Interner Channel ignoriert: {ch_id} exten={exten!r}")
                            continue
                        log.info(f"Eingehend: {ch_id} -> {exten}")
                        bridge = CallBridge(ari, ch_id, exten)
                        active[ch_id] = bridge
                        asyncio.create_task(bridge.start())

                    else:
                        # VoIP-Leitung hat sich verbunden
                        log.info(f"VoIP verbunden: {ch_id}")
                        for bridge in active.values():
                            if bridge.voip_id and bridge.voip_id.startswith(ch_id[:12]):
                                asyncio.create_task(bridge.voip_connected())
                                break

                elif etype == "StasisEnd":
                    ch_id = event["channel"]["id"]
                    if ch_id in active:
                        log.info(f"Gespraech beendet: {ch_id}")
                        del active[ch_id]

                elif etype == "ChannelHangupRequest":
                    ch_id = event["channel"]["id"]
                    log.info(f"Auflegen: {ch_id}")


if __name__ == "__main__":
    asyncio.run(main())
