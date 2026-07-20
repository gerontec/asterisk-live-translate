#!/opt/translator-venv/bin/python3
"""
SIP Translation B2BUA via AudioSocket — Asterisk 22
====================================================
Pure AudioSocket client: VAD + protocol + AMI.
GPU inference (STT / translation / TTS) via HTTP → inference_server :9095.

State machine per call:
  INIT → AMI_WAIT → OUTBOUND_DIALING → OUTBOUND_WAIT
       → CONNECTED → TRANSLATING → HANGUP → DONE

Fixes vs v2.0.0:
  - HTTP body parsed via Content-Length (kein Truncation bei großen Bodies)
  - Inference-Server Retry (3×) bei Ausfall während eines Calls
  - Worker Queue maxsize 3→10
  - _exten_map wird bei DONE/ERROR bereinigt (kein Memory Leak)
  - Worker.run() mit wait_for(5s) auf Queue statt ewigem get()
  - SR_AS Mismatch Warnung beim Start
  - _out_waiters Cleanup bei Timeout gesichert
  - status_dumper zeigt auch _exten_map Größe
"""

import asyncio, http.client, io, json, logging, logging.handlers
import os, re, struct, subprocess, threading, time, uuid as uuid_mod, wave, datetime
from pathlib import Path
from typing import Any
import warnings; warnings.filterwarnings("ignore", category=FutureWarning)

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

import numpy as np
from enum import Enum, auto
import soundfile as sf
import webrtcvad

_SEMVER = "2.1.0"

def _build_version() -> str:
    try:
        repo = str(Path(__file__).parent)
        rev  = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return f"v{_SEMVER} git:{rev}"
    except Exception:
        return f"v{_SEMVER} git:unknown"

VERSION     = _build_version()
DEPLOYED_AT = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# ── Configuration ──────────────────────────────────────────────────
AS_HOST   = "127.0.0.1"
AS_PORT   = 9093

REG_HOST  = "127.0.0.1"
REG_PORT  = 9094

INFER_HOST = os.environ.get("INFER_HOST", "127.0.0.1")
INFER_PORT = int(os.environ.get("INFER_PORT", 9095))

SR_AS     = 16000   # SLIN16 (patched AudioSocket — see README)
FRAME_MS  = 20
FRAME_S8  = SR_AS * FRAME_MS // 1000   # 320 samples @ 16 kHz
FRAME_B8  = FRAME_S8 * 2               # 640 bytes / 20 ms

SILENCE_FR = 25   # 25 × 20 ms = 500 ms Pause bis Segment-Flush (weniger Satz-Zerhackung)
SPEECH_MIN = 8

# FIX: Inference-Retry-Konfiguration
INFER_RETRIES   = 3
INFER_RETRY_S   = 1.0

TRUNK    = os.environ.get("TEST_TRUNK", "Local/%s@outbound-fallback")
CALLERID = "+4980425659959 <+4980425659959>"

AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", 5038))
AMI_USER = os.environ.get("AMI_USER", "admin")
AMI_PASS = os.environ.get("AMI_PASS", "")

SUFFIX_LANG = {
    # "49" = de: Quell- und Zielsprache Deutsch (Echo-Test 249, STT+TTS ohne Uebersetzung)
    "49":  "de",
    "1":   "en",  "7":   "ru",  "30":  "el",  "33":  "fr",
    "34":  "es",  "38":  "uk",  "39":  "it",  "44":  "en",
    "48":  "pl",  "55":  "pt",  "77":  "kk",  "86":  "zh",
    "90":  "tr",  "91":  "hi",  "98":  "fa",  "995": "ka",
}

CALLERID_PREFIX_LANG: list[tuple[str, str]] = [
    ("+995", "ka"), ("00995", "ka"),
    ("+380", "uk"), ("00380", "uk"),
    ("+49",  "de"), ("0049",  "de"),
    ("+39",  "it"), ("0039",  "it"),
    ("+44",  "en"), ("0044",  "en"),
    ("+33",  "fr"), ("0033",  "fr"),
    ("+34",  "es"), ("0034",  "es"),
    ("+48",  "pl"), ("0048",  "pl"),
    ("+55",  "pt"), ("0055",  "pt"),
    ("+30",  "el"), ("0030",  "el"),
    ("+90",  "tr"), ("0090",  "tr"),
    ("+91",  "hi"), ("0091",  "hi"),
    ("+98",  "fa"), ("0098",  "fa"),
    ("+86",  "zh"), ("0086",  "zh"),
    ("+77",  "kk"), ("0077",  "kk"),
    ("+7",   "ru"), ("007",   "ru"),
    ("+1",   "en"), ("001",   "en"),
]

def callerid_to_lang(callerid: str) -> str:
    for prefix, lang in CALLERID_PREFIX_LANG:
        if callerid.startswith(prefix):
            return lang
    return "de"

LOCAL_DIDS  = {"+4980424967"}
SAVE_DE_WAV = "/var/lib/asterisk/rec/last_de_bot.wav"
SAVE_IT_WAV = "/var/lib/asterisk/rec/last_remote_orig.wav"
LOOPBACK_ECHO = os.getenv("TRANSLATOR_LOOPBACK", "0") == "1"


# ══════════════════════════════════════════════════════════════════
# State Machine
# ══════════════════════════════════════════════════════════════════
class CallState(Enum):
    INIT             = auto()
    REGISTERED       = auto()
    OUTBOUND_DIALING = auto()
    OUTBOUND_WAIT    = auto()
    CONNECTED        = auto()
    TRANSLATING      = auto()
    HANGUP           = auto()
    DONE             = auto()
    ERROR            = auto()


class CallSession:
    def __init__(self, uuid: str) -> None:
        self.uuid      = uuid
        self.state     = CallState.INIT
        self.history: list[tuple[float, CallState, str]] = [
            (time.monotonic(), CallState.INIT, "connection received")
        ]
        self.exten:       str = ""
        self.remote_lang: str = ""
        self.dial_number: str = ""
        self.error:       str = ""
        self.callerid:    str = ""   # Anrufer (für DB-Log src)

    def transition(self, new_state: CallState, info: str = "") -> None:
        old = self.state
        self.state = new_state
        ts = time.monotonic()
        self.history.append((ts, new_state, info))
        elapsed = ts - self.history[0][0]
        log.info(
            f"[{self.uuid[:8]}] "
            f"{old.name:20s} → {new_state.name:20s} "
            f"+{elapsed:6.2f}s  {info}"
        )

    def fail(self, reason: str) -> None:
        self.error = reason
        self.transition(CallState.ERROR, reason)
        log.error(f"[{self.uuid[:8]}] ERROR in {self.state.name}: {reason}")
        # FIX: cleanup exten_map on error
        _exten_map.pop(self.uuid, None); _caller_map.pop(self.uuid, None)
        _sessions.pop(self.uuid, None)

    def summary(self) -> str:
        dur = time.monotonic() - self.history[0][0]
        return (
            f"uuid={self.uuid[:8]} exten={self.exten} "
            f"state={self.state.name} dur={dur:.1f}s"
        )


# ── Logging ────────────────────────────────────────────────────────
_log_handler = logging.handlers.RotatingFileHandler(
    "/tmp/translator.log", maxBytes=90_000, backupCount=1, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler, _stream_handler])
log = logging.getLogger("ast")

# ── Global state ──────────────────────────────────────────────────
_exten_map:   dict[str, str]              = {}
_caller_map:  dict[str, str]              = {}   # uuid → CallerID (Anrufer)
_out_waiters: dict[str, asyncio.Future]   = {}
_sessions:    dict[str, CallSession]      = {}


# ══════════════════════════════════════════════════════════════════
# HTTP-Client → Inference-Server :9095  (mit Retry)
# ══════════════════════════════════════════════════════════════════
# Persistente Verbindung je Executor-Thread (Keep-Alive) — spart den TCP-Handshake
# pro Infer-Call (~130 ms über den Netz-Hop). Bei Fehler wird sie verworfen + neu.
_infer_tls = threading.local()


def _infer_conn() -> http.client.HTTPConnection:
    c = getattr(_infer_tls, "conn", None)
    if c is None:
        c = http.client.HTTPConnection(INFER_HOST, INFER_PORT, timeout=30)
        _infer_tls.conn = c
    return c


def _infer_drop() -> None:
    c = getattr(_infer_tls, "conn", None)
    if c is not None:
        try:
            c.close()
        except Exception:
            pass
        _infer_tls.conn = None


def _infer_post_json_sync(path: str, body: bytes, content_type: str = "application/json") -> dict:
    """Synchroner HTTP-POST mit Keep-Alive + Retry bei Verbindungsfehler."""
    last_exc: Exception | None = None
    for attempt in range(INFER_RETRIES):
        try:
            conn = _infer_conn()
            conn.request("POST", path, body, {
                "Content-Type":   content_type,
                "Content-Length": str(len(body)),
                "Connection":     "keep-alive",
            })
            return json.loads(conn.getresponse().read())
        except Exception as e:
            last_exc = e
            _infer_drop()                 # stale/kaputte Verbindung verwerfen
            if attempt < INFER_RETRIES - 1:
                log.warning(f"[INFER] {path} attempt {attempt+1} failed: {e} — retrying")
                time.sleep(INFER_RETRY_S)
    raise ConnectionError(f"Inference-Server nicht erreichbar nach {INFER_RETRIES} Versuchen: {last_exc}")


def _infer_post_raw_sync(path: str, body: bytes, content_type: str = "application/json") -> bytes:
    """Synchroner HTTP-POST mit Keep-Alive + Retry, gibt rohe Bytes zurück."""
    last_exc: Exception | None = None
    for attempt in range(INFER_RETRIES):
        try:
            conn = _infer_conn()
            conn.request("POST", path, body, {
                "Content-Type":   content_type,
                "Content-Length": str(len(body)),
                "Connection":     "keep-alive",
            })
            return conn.getresponse().read()
        except Exception as e:
            last_exc = e
            _infer_drop()
            if attempt < INFER_RETRIES - 1:
                log.warning(f"[INFER] {path} attempt {attempt+1} failed: {e} — retrying")
                time.sleep(INFER_RETRY_S)
    raise ConnectionError(f"Inference-Server nicht erreichbar nach {INFER_RETRIES} Versuchen: {last_exc}")


async def _stt(pcm: bytes, lang: str) -> list[str]:
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None,
        lambda: _infer_post_json_sync(f"/stt?lang={lang}", pcm, "application/octet-stream"),
    )
    return data.get("chunks", [])


async def _translate(text: str, fl: str, tl: str, sess: "CallSession | None" = None) -> str:
    caller = sess.callerid if sess else ""
    callee = sess.exten    if sess else ""
    uid    = sess.uuid     if sess else ""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(
        None,
        lambda: _infer_post_json_sync(
            "/translate",
            json.dumps({"text": text, "from": fl, "to": tl,
                        "caller": caller, "callee": callee, "uniqueid": uid,
                        "channel": "sip"}).encode(),
        ),
    )
    return data.get("result", text)


async def _tts(text: str, lang: str) -> bytes:
    """WAV response from inference_server → raw SLIN16 PCM (strip 44-byte header)."""
    loop = asyncio.get_running_loop()
    wav  = await loop.run_in_executor(
        None,
        lambda: _infer_post_raw_sync("/tts", json.dumps({"text": text, "lang": lang}).encode()),
    )
    return wav[44:]


async def _wait_for_inference_server() -> None:
    log.info(f"Waiting for inference server {INFER_HOST}:{INFER_PORT} ...")
    while True:
        try:
            conn = http.client.HTTPConnection(INFER_HOST, INFER_PORT, timeout=3)
            conn.request("POST", "/translate",
                         b'{"text":"OK","from":"de","to":"it"}',
                         {"Content-Type": "application/json"})
            if conn.getresponse().status == 200:
                log.info("Inference server ready.")
                return
        except Exception:
            pass
        await asyncio.sleep(3)


# ══════════════════════════════════════════════════════════════════
# AudioSocket-Protokoll
# ══════════════════════════════════════════════════════════════════
AS_UUID   = 0x01
AS_AUDIO  = 0x10
AS_HANGUP = 0xFF

async def as_read(r: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr     = await r.readexactly(3)
    mtype   = hdr[0]
    length  = struct.unpack(">H", hdr[1:3])[0]
    payload = await r.readexactly(length) if length else b""
    return mtype, payload

async def as_write_audio(w: asyncio.StreamWriter, pcm: bytes) -> None:
    for i in range(0, len(pcm), FRAME_B8):
        chunk = pcm[i : i + FRAME_B8].ljust(FRAME_B8, b"\x00")
        w.write(struct.pack(">BH", AS_AUDIO, len(chunk)) + chunk)
        await w.drain()
        await asyncio.sleep(FRAME_MS / 1000)

async def as_hangup_send(w: asyncio.StreamWriter) -> None:
    w.write(struct.pack(">BH", AS_HANGUP, 0))
    await w.drain()

async def _drain_inbound(
    r: asyncio.StreamReader, stop: asyncio.Event, fut: asyncio.Future
) -> None:
    try:
        while not stop.is_set():
            try:
                mtype, _ = await asyncio.wait_for(as_read(r), timeout=0.5)
                if mtype == AS_HANGUP:
                    if not fut.done():
                        fut.cancel()
                    return
            except asyncio.TimeoutError:
                continue
    except Exception:
        if not fut.done():
            fut.cancel()


# ══════════════════════════════════════════════════════════════════
# Audio helpers
# ══════════════════════════════════════════════════════════════════
def save_wav(path: str, frames: list[bytes], sr: int = SR_AS) -> None:
    try:
        audio = np.frombuffer(b"".join(frames), dtype=np.int16)
        sf.write(path, audio, sr)
    except Exception as e:
        log.warning(f"WAV save failed ({path}): {e}")


# ══════════════════════════════════════════════════════════════════
# VAD speech buffer
# ══════════════════════════════════════════════════════════════════
class SpeechBuffer:
    def __init__(self) -> None:
        self.vad    = webrtcvad.Vad(2)
        self.buf:   list[bytes] = []
        self.sil    = 0
        self.active = False

    def push(self, frame: bytes) -> bytes | None:
        padded = frame.ljust(FRAME_B8, b"\x00")[:FRAME_B8]
        try:
            is_speech = self.vad.is_speech(padded, SR_AS)
        except Exception:
            is_speech = False

        if is_speech:
            self.buf.append(frame)
            self.sil    = 0
            self.active = True
        elif self.active:
            self.sil += 1
            self.buf.append(frame)
            if self.sil >= SILENCE_FR:
                net = len(self.buf) - self.sil
                if net >= SPEECH_MIN:
                    seg         = b"".join(self.buf[:-self.sil])
                    self.buf    = []
                    self.sil    = 0
                    self.active = False
                    return seg
                self.buf = []; self.sil = 0; self.active = False
        return None


# ══════════════════════════════════════════════════════════════════
# Translation-Worker
# ══════════════════════════════════════════════════════════════════
class Worker:
    def __init__(
        self,
        from_lang: str,
        to_lang:   str,
        writer:    asyncio.StreamWriter,
        label:     str,
        session:   CallSession,
        echo_partner: "Worker | None" = None,
    ) -> None:
        self.fl           = from_lang
        self.tl           = to_lang
        self.w            = writer
        self.label        = label
        self.sess         = session
        self.echo_partner: Worker | None = echo_partner
        # FIX: Queue maxsize 3→10 für schnelle aufeinanderfolgende Sätze
        self._q:  asyncio.Queue[bytes]   = asyncio.Queue(maxsize=10)
        self.segments_ok   = 0
        self.segments_skip = 0
        self._muted        = False

    def mute(self, on: bool) -> None:
        self._muted = on
        if on:
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def enqueue(self, pcm: bytes) -> None:
        if self._muted:
            return
        try:
            self._q.put_nowait(pcm)
        except asyncio.QueueFull:
            self.segments_skip += 1
            log.warning(f"[{self.label}] queue full — segment dropped (skip={self.segments_skip})")

    async def run(self) -> None:
        _silence = struct.pack(">BH", AS_AUDIO, FRAME_B8) + b"\x00" * FRAME_B8
        while True:
            try:
                pcm = await asyncio.wait_for(self._q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                if self.sess.state in (CallState.HANGUP, CallState.DONE, CallState.ERROR):
                    log.debug(f"[{self.label}] worker exiting (state={self.sess.state.name})")
                    return
                # keepalive: prevent Asterisk AudioSocket 2s inactivity timeout
                try:
                    self.w.write(_silence)
                    await self.w.drain()
                except Exception:
                    return
                continue
            except asyncio.CancelledError:
                raise

            try:
                if self.sess.state == CallState.CONNECTED:
                    self.sess.transition(CallState.TRANSLATING, f"{self.label} STT start")

                t0     = time.monotonic()
                chunks = await _stt(pcm, self.fl)
                t_stt  = time.monotonic() - t0

                if not chunks:
                    log.debug(f"[{self.label}] STT empty — skipping")
                    if self.sess.state == CallState.TRANSLATING:
                        self.sess.transition(CallState.CONNECTED, "STT empty")
                    continue

                full_text = " ".join(chunks)
                log.info(
                    f"[{self.label}] STT({t_stt:.2f}s) [{self.fl.upper()}] "
                    f"{full_text!r}  ({len(chunks)} Chunk(s))"
                )

                if self.echo_partner:
                    self.echo_partner.mute(True)

                total_tts_s = 0.0
                total_wr_s  = 0.0
                next_trans_task: asyncio.Task | None = None
                t_tr  = 0.0
                t_tts = 0.0
                try:
                    for i, chunk in enumerate(chunks):
                        fl, tl = self.fl, self.tl

                        t0 = time.monotonic()
                        if next_trans_task is not None:
                            trans = await next_trans_task
                            next_trans_task = None
                        else:
                            trans = await _translate(chunk, fl, tl, self.sess)
                        t_tr = time.monotonic() - t0
                        log.info(
                            f"[{self.label}] TRL[{i+1}/{len(chunks)}]({t_tr:.2f}s) "
                            f"[{tl.upper()}] {trans!r}"
                        )

                        t0      = time.monotonic()
                        pcm_out = await _tts(trans, tl)
                        t_tts   = time.monotonic() - t0
                        tts_dur = len(pcm_out) / 2 / SR_AS
                        total_tts_s += tts_dur
                        log.info(
                            f"[{self.label}] TTS[{i+1}/{len(chunks)}]({t_tts:.2f}s) "
                            f"{len(pcm_out)//2} samples"
                        )

                        # Parallelisierung: nächste Übersetzung starten während TTS läuft
                        if i + 1 < len(chunks):
                            next_ch = chunks[i + 1]
                            next_trans_task = asyncio.create_task(
                                _translate(next_ch, fl, tl, self.sess)
                            )

                        t_wr = time.monotonic()
                        await as_write_audio(self.w, pcm_out)
                        total_wr_s += time.monotonic() - t_wr

                    sleep_remaining = max(0.0, 0.35 - (total_wr_s - total_tts_s))
                    await asyncio.sleep(sleep_remaining)
                finally:
                    if next_trans_task and not next_trans_task.done():
                        next_trans_task.cancel()
                    if self.echo_partner:
                        self.echo_partner.mute(False)

                self.segments_ok += 1
                if self.sess.state == CallState.TRANSLATING:
                    self.sess.transition(
                        CallState.CONNECTED,
                        f"{self.label} ok #{self.segments_ok} "
                        f"stt={t_stt:.2f}s trl={t_tr:.2f}s tts={t_tts:.2f}s"
                    )

            except asyncio.CancelledError:
                raise
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                log.warning(f"[{self.label}] connection closed — worker stopped: {e}")
                return
            except ConnectionError as e:
                # FIX: Inference-Server ausgefallen — Worker warnt aber läuft weiter
                log.error(f"[{self.label}] inference-server unreachable: {e}")
                if self.sess.state == CallState.TRANSLATING:
                    self.sess.transition(CallState.CONNECTED, f"{self.label} infer-error")
                if self.echo_partner:
                    self.echo_partner.mute(False)
            except Exception as e:
                log.error(
                    f"[{self.label}] error in state={self.sess.state.name}: {e}",
                    exc_info=True
                )
                if self.sess.state == CallState.TRANSLATING:
                    self.sess.transition(CallState.CONNECTED, f"{self.label} error: {e}")
                if self.echo_partner:
                    self.echo_partner.mute(False)


# ══════════════════════════════════════════════════════════════════
# Audio-Empfangs-Loop
# ══════════════════════════════════════════════════════════════════
async def recv_loop(
    reader:  asyncio.StreamReader,
    worker:  Worker,
    wav_path: str,
    side:    str,
    stop:    asyncio.Event,
    session: CallSession,
) -> None:
    buf  = SpeechBuffer()
    rec: list[bytes] = []
    n    = 0
    try:
        while not stop.is_set():
            try:
                mtype, payload = await asyncio.wait_for(as_read(reader), timeout=5.0)
            except asyncio.TimeoutError:
                log.debug(f"[{side}] audio timeout (state={session.state.name})")
                continue
            if mtype == AS_HANGUP:
                session.transition(CallState.HANGUP, f"{side} Hangup")
                break
            if mtype != AS_AUDIO or not payload:
                continue
            rec.append(payload)
            n += 1
            seg = buf.push(payload)
            if seg:
                await worker.enqueue(seg)
            if n % 750 == 0:
                save_wav(wav_path, rec)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        session.transition(CallState.HANGUP, f"{side} connection lost")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        session.fail(f"{side} recv_loop: {e}")
    finally:
        stop.set()
        try:
            if rec:
                save_wav(wav_path, rec)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# AMI — initiate outbound call
# ══════════════════════════════════════════════════════════════════
async def ami_originate(number: str, partner_uuid: str, session: CallSession) -> None:
    session.transition(CallState.OUTBOUND_DIALING, f"Originate → {number}")
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=10.0
        )
    except asyncio.TimeoutError as exc:
        raise ConnectionError(f"AMI not reachable ({AMI_HOST}:{AMI_PORT})") from exc

    async def line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    await line()
    w.write(f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_PASS}\r\n\r\n".encode())
    await w.drain()
    while "Authentication accepted" not in (await line()):
        pass
    w.write((
        f"Action: Originate\r\n"
        f"Channel: {('PJSIP/'+number) if (number.isdigit() and len(number)<=5) else (TRUNK % number)}\r\n"
        f"Context: audiosocket-out\r\n"
        f"Exten: s\r\n"
        f"Priority: 1\r\n"
        f"Variable: PARTNER_UUID={partner_uuid}\r\n"
        f"CallerID: {CALLERID}\r\n"
        f"Timeout: 60000\r\nAsync: true\r\n\r\n"
    ).encode())
    await w.drain()
    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    log.info(f"[AMI] Originate → {number}  partner={partner_uuid}")


# ══════════════════════════════════════════════════════════════════
# HTTP-Body Parser (FIX: Content-Length basiert, kein Truncation)
# ══════════════════════════════════════════════════════════════════
async def _read_http_request(reader: asyncio.StreamReader) -> tuple[str, bytes, bytes]:
    """
    Liest HTTP-Request vollständig anhand Content-Length.
    Gibt (path, raw_body, headers_str) zurück.
    """
    # Header lesen bis \r\n\r\n
    header_buf = b""
    while b"\r\n\r\n" not in header_buf:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
        if not chunk:
            break
        header_buf += chunk

    if b"\r\n\r\n" in header_buf:
        header_part, rest = header_buf.split(b"\r\n\r\n", 1)
    else:
        header_part = header_buf
        rest = b""

    headers_str = header_part.decode(errors="replace")
    first_line  = headers_str.split("\n", 1)[0].strip()
    parts       = first_line.split(" ")
    path        = parts[1] if len(parts) >= 2 else "/"

    # Content-Length auslesen
    content_length = 0
    for hline in headers_str.splitlines():
        if hline.lower().startswith("content-length:"):
            try:
                content_length = int(hline.split(":", 1)[1].strip())
            except ValueError:
                pass

    # Body vollständig lesen
    raw_body = rest
    while len(raw_body) < content_length:
        needed = content_length - len(raw_body)
        chunk  = await asyncio.wait_for(reader.read(min(needed, 65536)), timeout=10.0)
        if not chunk:
            break
        raw_body += chunk

    return path, raw_body, headers_str


# ══════════════════════════════════════════════════════════════════
# HTTP-Endpunkt  (Port 9094)
# ══════════════════════════════════════════════════════════════════
def _http_ok_json(writer: asyncio.StreamWriter, body: bytes) -> None:
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )

def _http_err(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    body   = msg.encode()
    status = {400: "Bad Request", 502: "Bad Gateway", 500: "Internal Server Error"}.get(code, "Error")
    writer.write(
        f"HTTP/1.1 {code} {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )


async def _handle_register_body(body: bytes, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    uid   = payload.get("uuid",  "").strip()
    exten = payload.get("exten", "").strip()
    if uid and exten:
        _exten_map[uid] = exten
        cid = payload.get("callerid", "").strip()
        if cid:
            _caller_map[uid] = cid
        log.info(f"[REG] uuid={uid[:8]} → exten={exten} callerid={cid or '-'}")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    else:
        _http_err(writer, 400, "ERR")


async def _handle_lang_body(body: bytes, writer: asyncio.StreamWriter) -> None:
    payload  = json.loads(body)
    callerid = payload.get("callerid", "").strip()
    lang     = callerid_to_lang(callerid)
    log.info(f"[LANG] callerid={callerid!r} → lang={lang}")
    _http_ok_json(writer, json.dumps({"lang": lang}).encode())


async def _forward_to_infer(raw_body: bytes, path: str, writer: asyncio.StreamWriter) -> None:
    try:
        loop = asyncio.get_running_loop()
        resp = await loop.run_in_executor(
            None,
            lambda: _infer_post_raw_sync(path, raw_body),
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            + f"Content-Length: {len(resp)}\r\n\r\n".encode()
            + resp
        )
    except Exception as e:
        log.warning(f"[FWD {path}] error: {e}")
        _http_err(writer, 502, f'{{"error":"inference_server: {e}"}}')


async def _forward_tts_to_infer(raw_body: bytes, writer: asyncio.StreamWriter) -> None:
    try:
        loop = asyncio.get_running_loop()
        wav  = await loop.run_in_executor(
            None,
            lambda: _infer_post_raw_sync("/tts", raw_body),
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
            + f"Content-Length: {len(wav)}\r\n\r\n".encode()
            + wav
        )
    except Exception as e:
        log.warning(f"[FWD /tts] error: {e}")
        _http_err(writer, 502, "ERR")


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        # FIX: Content-Length basierter Body-Parser
        path, raw_body, _ = await _read_http_request(reader)

        if path.startswith("/nlu") or path.startswith("/translate"):
            await _forward_to_infer(raw_body, path, writer)
        elif path.startswith("/tts"):
            await _forward_tts_to_infer(raw_body, writer)
        elif path.startswith("/lang"):
            await _handle_lang_body(raw_body, writer)
        else:
            await _handle_register_body(raw_body, writer)

    except Exception as e:
        log.warning(f"[HTTP] error: {e}")
        try:
            _http_err(writer, 500, "ERR")
        except Exception:
            pass
    finally:
        try:
            await writer.drain()
            writer.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# Inbound-Call-Handler
# ══════════════════════════════════════════════════════════════════
async def handle_inbound(uuid: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    sess = CallSession(uuid)
    _sessions[uuid] = sess

    exten = _exten_map.get(uuid, "")
    if not exten:
        sess.fail(f"UUID {uuid[:8]} not registered")
        await as_hangup_send(writer)
        writer.close()
        return

    sess.transition(CallState.REGISTERED, f"exten={exten}")
    sess.exten = exten
    sess.callerid = _caller_map.get(uuid, "")
    if "~" in exten:
        dial_number, remote_lang = exten.rsplit("~", 1)
    else:
        matched_suffix = next(
            (s for s in sorted(SUFFIX_LANG, key=len, reverse=True) if exten.endswith(s)),
            None
        )
        remote_lang = SUFFIX_LANG[matched_suffix] if matched_suffix else "it"
        dial_number = exten[:-len(matched_suffix)] if matched_suffix else exten
    sess.remote_lang = remote_lang
    sess.dial_number = dial_number

    log.info(
        f"[Inbound] uuid={uuid[:8]}  exten={exten}  "
        f"remote={remote_lang.upper()}  dialing={dial_number}"
    )

    if LOOPBACK_ECHO or not dial_number or len(dial_number) <= 2 or dial_number in LOCAL_DIDS:
        log.info("[Inbound] LOOPBACK_ECHO active")
        sess.transition(CallState.CONNECTED, "Loopback mode")
        stop  = asyncio.Event()
        w_de  = Worker("de", remote_lang, writer, f"DE→{remote_lang.upper()}[LOOP]", sess)
        recv_in = asyncio.create_task(recv_loop(reader, w_de, SAVE_DE_WAV, "Inbound", stop, sess))
        work_de = asyncio.create_task(w_de.run())
        await asyncio.gather(recv_in, return_exceptions=True)
        work_de.cancel()
        await asyncio.gather(work_de, return_exceptions=True)
        try:
            writer.close()
        except Exception:
            pass
        sess.transition(
            CallState.DONE,
            f"Loopback done: segs={w_de.segments_ok} skip={w_de.segments_skip}"
        )
        log.info(f"[Inbound] {sess.summary()}")
        # FIX: cleanup exten_map bei DONE
        _exten_map.pop(uuid, None); _caller_map.pop(uuid, None)
        _sessions.pop(uuid, None)
        return

    partner_uuid = str(uuid_mod.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _out_waiters[partner_uuid] = fut

    try:
        await ami_originate(dial_number, partner_uuid, sess)
    except Exception as e:
        # FIX: cleanup out_waiters bei AMI-Fehler
        _out_waiters.pop(partner_uuid, None)
        sess.fail(f"AMI-Originate failed: {e}")
        await as_hangup_send(writer)
        writer.close()
        return

    sess.transition(CallState.OUTBOUND_WAIT, "waiting for outbound leg (max 60s)")
    stop_drain = asyncio.Event()
    drain_task = asyncio.create_task(_drain_inbound(reader, stop_drain, fut))
    try:
        out_reader, out_writer = await asyncio.wait_for(fut, timeout=60.0)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        stop_drain.set()
        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)
        # FIX: out_waiters cleanup gesichert
        _out_waiters.pop(partner_uuid, None)
        sess.fail(f"Outbound-Timeout: {exc}")
        await as_hangup_send(writer)
        writer.close()
        return
    finally:
        _out_waiters.pop(partner_uuid, None)

    stop_drain.set()
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)

    sess.transition(CallState.CONNECTED, f"DE↔{remote_lang.upper()}")

    stop  = asyncio.Event()
    w_de  = Worker("de",        remote_lang, out_writer, f"DE→{remote_lang.upper()}", sess)
    w_re  = Worker(remote_lang, "de",        writer,     f"{remote_lang.upper()}→DE", sess)
    w_de.echo_partner = w_re
    w_re.echo_partner = w_de

    recv_in  = asyncio.create_task(recv_loop(reader,     w_de, SAVE_DE_WAV, "Inbound",  stop, sess))
    recv_out = asyncio.create_task(recv_loop(out_reader, w_re, SAVE_IT_WAV, "Outbound", stop, sess))
    work_de  = asyncio.create_task(w_de.run())
    work_re  = asyncio.create_task(w_re.run())

    await asyncio.gather(recv_in, recv_out, return_exceptions=True)
    work_de.cancel()
    work_re.cancel()
    await asyncio.gather(work_de, work_re, return_exceptions=True)

    for w in (writer, out_writer):
        try:
            w.close()
        except Exception:
            pass

    sess.transition(
        CallState.DONE,
        f"ok: DE={w_de.segments_ok} {remote_lang.upper()}={w_re.segments_ok} "
        f"skip: DE={w_de.segments_skip} {remote_lang.upper()}={w_re.segments_skip}"
    )
    log.info(f"[Inbound] {sess.summary()}")
    # FIX: cleanup exten_map bei DONE
    _exten_map.pop(uuid, None); _caller_map.pop(uuid, None)
    _sessions.pop(uuid, None)


# ══════════════════════════════════════════════════════════════════
# TCP connection handler
# ══════════════════════════════════════════════════════════════════
def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error(f"[Task] {task.get_name()}: {exc}", exc_info=exc)


async def handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    addr = writer.get_extra_info("peername")
    try:
        mtype, payload = await asyncio.wait_for(as_read(reader), timeout=5.0)
    except Exception as e:
        log.warning(f"[AS] UUID read failed from {addr}: {e}")
        writer.close()
        return
    if mtype != AS_UUID:
        log.warning(f"[AS] no UUID packet (type={mtype:#x}) from {addr}")
        writer.close()
        return

    uuid = str(uuid_mod.UUID(bytes=payload))
    log.info(f"[AS] new connection: uuid={uuid!r} from {addr}")

    if uuid in _out_waiters:
        if not _out_waiters[uuid].done():
            _out_waiters[uuid].set_result((reader, writer))
            log.info(f"[AS] outbound leg connected: {uuid}")
        else:
            log.warning(f"[AS] outbound waiter already done for {uuid}")
            writer.close()
        return

    task = asyncio.create_task(handle_inbound(uuid, reader, writer))
    task.add_done_callback(_log_task_exception)


# ══════════════════════════════════════════════════════════════════
# Periodic status dump
# ══════════════════════════════════════════════════════════════════
async def status_dumper() -> None:
    while True:
        await asyncio.sleep(60)
        # FIX: zeigt auch exten_map Größe für Memory-Leak-Diagnose
        log.info(
            f"[Status] sessions={len(_sessions)} "
            f"exten_map={len(_exten_map)} "
            f"out_waiters={len(_out_waiters)}"
        )
        for uuid, sess in list(_sessions.items()):
            log.info(f"  {sess.summary()}")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════
async def amain() -> None:
    log.info(f"AudioSocket-Translator  version={VERSION}  deployed={DEPLOYED_AT}")

    # FIX: SR_AS Warnung damit Fehlkonfiguration sofort sichtbar ist
    if SR_AS == 16000:
        log.info("SR_AS=16000 (SLIN16) — Asterisk AudioSocket muss auf SLIN16 konfiguriert sein")
    elif SR_AS == 8000:
        log.info("SR_AS=8000 (SLIN) — Standard G.711 Modus")
    else:
        log.warning(f"SR_AS={SR_AS} — unbekannte Sample-Rate, VAD könnte fehlschlagen")

    await _wait_for_inference_server()

    asyncio.create_task(status_dumper())

    reg_server = await asyncio.start_server(handle_http, REG_HOST, REG_PORT)
    log.info(f"HTTP endpoint listening on {REG_HOST}:{REG_PORT}")

    as_server = await asyncio.start_server(handle_connection, AS_HOST, AS_PORT)
    log.info(f"AudioSocket translator listening on {AS_HOST}:{AS_PORT}")

    async with reg_server, as_server:
        await asyncio.gather(
            reg_server.serve_forever(),
            as_server.serve_forever(),
        )


if __name__ == "__main__":
    asyncio.run(amain())
