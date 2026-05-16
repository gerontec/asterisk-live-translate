#!/home/gh/python/venv_py311/bin/python3
"""
SIP Translation B2BUA via AudioSocket — Asterisk 22
====================================================
Linphone / Fritz!Box  →  Asterisk (AudioSocket TCP)
  → Whisper STT  →  Argostranslate  →  Piper TTS
  →  Asterisk (AudioSocket TCP)
→  Fritz!Box / PSTN  (IT / RU)

State Machine pro Anruf:
  INIT → AMI_WAIT → OUTBOUND_DIALING → OUTBOUND_WAIT
       → CONNECTED → TRANSLATING → HANGUP → DONE
"""

import asyncio, struct, logging, io, os, time, uuid as uuid_mod, json, wave
import warnings; warnings.filterwarnings("ignore", category=FutureWarning, module="pynvml")
from pathlib import Path
# .env laden (Schlüssel=Wert, keine Shell-Expansion)
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
import numpy as np
from enum import Enum, auto
from scipy import signal as sp
import soundfile as sf
import webrtcvad
from faster_whisper import WhisperModel
from piper.voice import PiperVoice
import argostranslate.package as argos_pkg
import argostranslate.translate as argos_trans

# ── Konfiguration ─────────────────────────────────────────────────
AS_HOST   = "127.0.0.1"
AS_PORT   = 9093

# HTTP-Registrierungs-Endpunkt (AGI ruft diesen auf)
REG_HOST  = "127.0.0.1"
REG_PORT  = 9094

SR_AS     = 8000
SR_WH     = 16000
FRAME_MS  = 20
FRAME_S8  = SR_AS  * FRAME_MS // 1000
FRAME_B8  = FRAME_S8 * 2

SILENCE_FR = 15
SPEECH_MIN = 8     # mind. 160ms echte Sprache — filtert kurze TTS-Artefakte

TRUNK     = "PJSIP/%s@fritzbox-out"
CALLERID  = "linuxsip <+4980425641873>"

AMI_HOST  = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT  = int(os.environ.get("AMI_PORT", 5038))
AMI_USER  = os.environ.get("AMI_USER", "admin")
AMI_PASS  = os.environ.get("AMI_PASS", "")

SUFFIX_LANG = {"39": "it", "99": "ru"}

# Eigene DIDs — Anruf darauf ist immer Loopback (kein Outbound)
LOCAL_DIDS = {"+4980424967"}

PIPER_MODELS_DIR = "/home/gh/python/translator/piper_models"
PIPER_VOICES = {
    "it": "it_IT-paola-medium",
    "de": "de_DE-thorsten-medium",
}
SAVE_MP3    = "/home/gh/python/ghit.mp3"
SAVE_DE_WAV = "/home/gh/python/gh_de_in.wav"
SAVE_IT_WAV = "/home/gh/python/gh_voip_in.wav"

# Loopback-Echo-Modus: TTS zurück auf den Anrufer statt an IT-Partner
LOOPBACK_ECHO = os.getenv("TRANSLATOR_LOOPBACK", "0") == "1"


# ══════════════════════════════════════════════════════════════════
# State Machine
# ══════════════════════════════════════════════════════════════════
class CallState(Enum):
    INIT             = auto()   # Verbindung eingegangen, UUID gelesen
    REGISTERED       = auto()   # UUID+Exten via AGI/HTTP registriert
    OUTBOUND_DIALING = auto()   # AMI-Originate abgeschickt
    OUTBOUND_WAIT    = auto()   # Warte auf Outbound-AudioSocket-Leg
    CONNECTED        = auto()   # Beide Legs verbunden, Audio läuft
    TRANSLATING      = auto()   # STT/Translate/TTS aktiv
    HANGUP           = auto()   # Hangup empfangen
    DONE             = auto()   # Aufräumen abgeschlossen
    ERROR            = auto()   # Fehler aufgetreten


class CallSession:
    """
    Hält Zustand + History eines einzelnen Anrufs.
    Jeder Übergang wird mit Timestamp und optionalem Kontext geloggt.
    """
    def __init__(self, uuid: str) -> None:
        self.uuid      = uuid
        self.state     = CallState.INIT
        self.history: list[tuple[float, CallState, str]] = [
            (time.monotonic(), CallState.INIT, "Verbindung eingegangen")
        ]
        self.exten: str = ""
        self.remote_lang: str = ""
        self.dial_number: str = ""
        self.error: str = ""

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
        log.error(
            f"[{self.uuid[:8]}] FEHLER in {self.state.name}: {reason}\n"
            f"  History: {self._history_str()}"
        )

    def _history_str(self) -> str:
        lines = []
        t0 = self.history[0][0]
        for ts, st, info in self.history:
            lines.append(f"{st.name}@+{ts-t0:.2f}s: {info}")
        return " | ".join(lines)

    def summary(self) -> str:
        dur = time.monotonic() - self.history[0][0]
        return (
            f"uuid={self.uuid[:8]} exten={self.exten} "
            f"state={self.state.name} dur={dur:.1f}s "
            f"history=[{self._history_str()}]"
        )


# ── Logging (rotierend, max 90 KB) ────────────────────────────────
import logging.handlers as _lh
_log_handler = _lh.RotatingFileHandler(
    "/tmp/translator.log", maxBytes=90_000, backupCount=1, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler, _stream_handler])
log = logging.getLogger("ast")

# ── GPU-Monitoring (pynvml, optional) ─────────────────────────────
try:
    import pynvml as _nv
    _nv.nvmlInit()
    _nv_handle = _nv.nvmlDeviceGetHandleByIndex(0)
    _nv_ok = True
    log.info("GPU-Monitoring aktiv (pynvml)")
except Exception as _e:
    _nv_ok = False
    log.info(f"GPU-Monitoring nicht verfügbar: {_e}")

def _gpu() -> str:
    if not _nv_ok:
        return ""
    try:
        u = _nv.nvmlDeviceGetUtilizationRates(_nv_handle)
        m = _nv.nvmlDeviceGetMemoryInfo(_nv_handle)
        t = _nv.nvmlDeviceGetTemperature(_nv_handle, _nv.NVML_TEMPERATURE_GPU)
        p = _nv.nvmlDeviceGetPowerUsage(_nv_handle)
        return (f"GPU {u.gpu:3d}%  "
                f"MEM {m.used//1024//1024}/{m.total//1024//1024}MiB  "
                f"{t}°C  {p/1000:.0f}W")
    except Exception:
        return "GPU err"

class _GpuPeak:
    """Peak-Werte für eine Session sammeln."""
    __slots__ = ("sm", "temp", "watt")
    def __init__(self): self.sm = self.temp = self.watt = 0
    def sample(self):
        if not _nv_ok: return
        try:
            u = _nv.nvmlDeviceGetUtilizationRates(_nv_handle)
            t = _nv.nvmlDeviceGetTemperature(_nv_handle, _nv.NVML_TEMPERATURE_GPU)
            p = _nv.nvmlDeviceGetPowerUsage(_nv_handle) / 1000
            self.sm   = max(self.sm,   u.gpu)
            self.temp = max(self.temp, t)
            self.watt = max(self.watt, p)
        except Exception: pass
    def __str__(self): return f"peak GPU {self.sm}%  {self.temp}°C  {self.watt:.0f}W"

# ── Globaler Zustand ──────────────────────────────────────────────
_whisper: WhisperModel | None = None
_whisper_lock_de = asyncio.Lock()   # separate Locks: beide Richtungen parallel
_whisper_lock_re = asyncio.Lock()
_piper_voices: dict[str, PiperVoice] = {}

_exten_map: dict[str, str] = {}
_out_waiters: dict[str, asyncio.Future] = {}
_sessions: dict[str, CallSession] = {}   # uuid → Session (für Übersicht)


# ══════════════════════════════════════════════════════════════════
# Modelle laden
# ══════════════════════════════════════════════════════════════════
def load_models() -> None:
    global _whisper
    log.info("Lade Whisper medium (CUDA) …")
    _whisper = WhisperModel("medium", device="cuda", compute_type="int8")
    log.info("Whisper geladen.")

    log.info("Lade Piper TTS-Modelle …")
    for lang, model_name in PIPER_VOICES.items():
        path = os.path.join(PIPER_MODELS_DIR, f"{model_name}.onnx")
        if os.path.exists(path):
            _piper_voices[lang] = PiperVoice.load(path)
            log.info(f"  Piper {lang}: {model_name} ({_piper_voices[lang].config.sample_rate}Hz)")
        else:
            log.warning(f"  Piper {lang}: Modell nicht gefunden: {path}")
    log.info("Piper bereit.")

    log.info("Prüfe Argostranslate-Pakete …")
    argos_pkg.update_package_index()
    installed = {(p.from_code, p.to_code)
                 for p in argos_pkg.get_installed_packages()}
    for fl, tl in [("de","it"),("it","de"),("de","en"),("en","it"),
                   ("it","en"),("en","de"),("ru","en"),("en","ru")]:
        if (fl, tl) not in installed:
            avail = argos_pkg.get_available_packages()
            pkg = next((p for p in avail
                        if p.from_code == fl and p.to_code == tl), None)
            if pkg:
                log.info(f"  Installiere {fl}→{tl} …")
                argos_pkg.install_from_path(pkg.download())
    log.info("Argostranslate bereit.")


# ══════════════════════════════════════════════════════════════════
# AudioSocket-Protokoll
# ══════════════════════════════════════════════════════════════════
# Asterisk 22 AudioSocket Protokoll (empirisch ermittelt per Sniffer):
# 0x01 = UUID-Paket (16 Byte Binary UUID, nicht ASCII)
# 0x10 = Audio-Paket (slin16, 8 kHz, 320 Bytes)
# 0xFF = Hangup
AS_UUID   = 0x01
AS_AUDIO  = 0x10
AS_HANGUP = 0xFF

async def as_read(r: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr     = await r.readexactly(3)
    mtype   = hdr[0]
    length  = struct.unpack(">H", hdr[1:3])[0]
    payload = await r.readexactly(length) if length else b""
    return mtype, payload

async def as_write_audio(w: asyncio.StreamWriter, pcm8: bytes) -> None:
    for i in range(0, len(pcm8), FRAME_B8):
        chunk = pcm8[i : i + FRAME_B8].ljust(FRAME_B8, b"\x00")
        w.write(struct.pack(">BH", AS_AUDIO, len(chunk)) + chunk)
        await w.drain()
        await asyncio.sleep(FRAME_MS / 1000)
async def as_hangup_send(w: asyncio.StreamWriter) -> None:
    w.write(struct.pack(">BH", AS_HANGUP, 0))
    await w.drain()


# ══════════════════════════════════════════════════════════════════
# Audio-Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════
def pcm8_to_float16(pcm8: bytes) -> np.ndarray:
    arr = np.frombuffer(pcm8, dtype=np.int16).astype(np.float32) / 32768.0
    n   = int(len(arr) * SR_WH / SR_AS)
    return sp.resample(arr, n)

def float16_to_pcm8(arr: np.ndarray) -> bytes:
    n   = int(len(arr) * SR_AS / SR_WH)
    arr = sp.resample(arr, n)
    return (arr * 32767).clip(-32768, 32767).astype(np.int16).tobytes()

def save_wav(path: str, frames: list[bytes], sr: int = SR_AS) -> None:
    try:
        pcm   = b"".join(frames)
        audio = np.frombuffer(pcm, dtype=np.int16)
        sf.write(path, audio, sr)
    except Exception as e:
        log.warning(f"WAV-Speichern ({path}): {e}")


# ══════════════════════════════════════════════════════════════════
# VAD-Sprachpuffer
# ══════════════════════════════════════════════════════════════════
class SpeechBuffer:
    def __init__(self) -> None:
        self.vad    = webrtcvad.Vad(2)
        self.buf: list[bytes] = []
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
# STT → Übersetzen → TTS
# ══════════════════════════════════════════════════════════════════
_SENT_END = frozenset({".", "!", "?", "...", "…"})

def _split_into_sentence_chunks(segs) -> list[str]:
    """Teilt Whisper-Segmente anhand von Satzgrenzen in Chunks auf.
    Nutzt word_timestamps; fällt auf segment.text zurück wenn keine Wörter vorhanden."""
    chunks: list[str] = []
    buf: list[str] = []
    for seg in segs:
        words = getattr(seg, "words", None) or []
        if words:
            for w in words:
                buf.append(w.word)
                stripped = w.word.rstrip()
                if stripped and stripped[-1] in _SENT_END:
                    text = "".join(buf).strip()
                    if text:
                        chunks.append(text)
                    buf = []
        else:
            # Kein word_timestamps: segment.text direkt als Chunk
            text = seg.text.strip()
            if text:
                chunks.append(text)
    if buf:
        text = "".join(buf).strip()
        if text:
            chunks.append(text)
    return chunks


async def stt_chunks(pcm8: bytes, lang: str, lock: asyncio.Lock,
                     gpu_peak: "_GpuPeak | None" = None) -> list[str]:
    """Transkribiert PCM8 und gibt eine Liste von Satz-Chunks zurück."""
    audio = pcm8_to_float16(pcm8)
    async with lock:
        log.info(f"STT start  {_gpu()}")
        loop = asyncio.get_running_loop()
        t0 = time.monotonic()
        segs, _ = await loop.run_in_executor(
            None,
            lambda: _whisper.transcribe(
                audio, language=lang, beam_size=5,
                vad_filter=True,
                word_timestamps=True,
                no_speech_threshold=0.7,
                log_prob_threshold=-1.0,
                condition_on_previous_text=False,
            ),
        )
        if gpu_peak: gpu_peak.sample()
        log.info(f"STT done   {_gpu()}  ({time.monotonic()-t0:.2f}s)")
    chunks = _split_into_sentence_chunks(segs)
    if chunks:
        log.debug(f"stt_chunks: {chunks}")
    return chunks


def translate_sync(text: str, fl: str, tl: str) -> str:
    if fl == tl:
        return text
    try:
        r = argos_trans.translate(text, fl, tl)
        if r:
            return r
    except Exception:
        pass
    en = argos_trans.translate(text, fl, "en")
    return argos_trans.translate(en, "en", tl)


def _tts_piper_sync(text: str, lang: str) -> bytes:
    """Synthese mit Piper (lokal, ~30ms). Gibt slin16 @ SR_AS zurück."""
    pv = _piper_voices.get(lang) or _piper_voices.get("de")
    buf = io.BytesIO()
    wf  = wave.open(buf, "wb")
    pv.synthesize_wav(text, wf)
    wf.close()
    sr_piper = pv.config.sample_rate          # 22050
    buf.seek(44)                               # WAV-Header überspringen
    pcm_raw = buf.read()
    audio_f = np.frombuffer(pcm_raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sr_piper != SR_AS:
        n       = int(len(audio_f) * SR_AS / sr_piper)
        audio_f = sp.resample(audio_f, n)
    return (audio_f * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


async def tts(text: str, lang: str) -> bytes:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _tts_piper_sync, text, lang)


# ══════════════════════════════════════════════════════════════════
# Translation-Worker  — kennt Session für State-Updates
# ══════════════════════════════════════════════════════════════════
class Worker:
    def __init__(
        self,
        from_lang: str,
        to_lang: str,
        writer: asyncio.StreamWriter,
        label: str,
        session: CallSession,
        whisper_lock: asyncio.Lock,
        echo_partner: "Worker | None" = None,
    ) -> None:
        self.fl    = from_lang
        self.tl    = to_lang
        self.w     = writer
        self.label = label
        self.sess  = session
        self.lock  = whisper_lock
        self.echo_partner: Worker | None = echo_partner  # wird nach Init gesetzt
        self._q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=3)
        self.segments_ok   = 0
        self.segments_skip = 0
        self._muted        = False
        self.gpu_peak      = _GpuPeak()

    def mute(self, on: bool) -> None:
        """Vom Partner aufgerufen: eigene Eingabe stumm schalten während TTS läuft."""
        self._muted = on
        if on:
            # Queue leeren — bereits gepufferte Frames während TTS verwerfen
            while not self._q.empty():
                try:
                    self._q.get_nowait()
                except asyncio.QueueEmpty:
                    break

    async def enqueue(self, pcm8: bytes) -> None:
        if self._muted:
            return   # Echo-Unterdrückung: eigene Frames während TTS des Partners ignorieren
        try:
            self._q.put_nowait(pcm8)
        except asyncio.QueueFull:
            self.segments_skip += 1
            log.warning(
                f"[{self.label}] Queue voll – Segment verworfen "
                f"(skip={self.segments_skip})"
            )

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            pcm8 = await self._q.get()
            prev_state = self.sess.state
            try:
                # State: TRANSLATING setzen während STT/TTS läuft
                if self.sess.state == CallState.CONNECTED:
                    self.sess.transition(
                        CallState.TRANSLATING,
                        f"{self.label} STT start"
                    )

                t0     = time.monotonic()
                chunks = await stt_chunks(pcm8, self.fl, self.lock, self.gpu_peak)
                t_stt  = time.monotonic() - t0

                if not chunks:
                    log.debug(f"[{self.label}] STT leer – übersprungen")
                    if self.sess.state == CallState.TRANSLATING:
                        self.sess.transition(CallState.CONNECTED, "STT leer")
                    continue

                full_text = " ".join(chunks)
                log.info(
                    f"[{self.label}] STT({t_stt:.2f}s) [{self.fl.upper()}] "
                    f"{full_text!r}  ({len(chunks)} Chunk(s))"
                )

                # Pro Satz-Chunk: übersetzen → TTS → sofort abspielen.
                # Mute bleibt für alle Chunks aktiv; erst nach dem letzten Chunk sleep.
                if self.echo_partner:
                    self.echo_partner.mute(True)

                total_tts_s = 0.0
                total_wr_s  = 0.0
                try:
                    for i, chunk in enumerate(chunks):
                        t0    = time.monotonic()
                        trans = await loop.run_in_executor(
                            None, translate_sync, chunk, self.fl, self.tl
                        )
                        t_tr  = time.monotonic() - t0
                        log.info(
                            f"[{self.label}] TRL[{i+1}/{len(chunks)}]"
                            f"({t_tr:.2f}s) [{self.tl.upper()}] {trans!r}"
                        )

                        t0      = time.monotonic()
                        pcm_out = await tts(trans, self.tl)
                        t_tts   = time.monotonic() - t0
                        tts_dur = len(pcm_out) / 2 / SR_AS
                        total_tts_s += tts_dur
                        log.info(
                            f"[{self.label}] TTS[{i+1}/{len(chunks)}]"
                            f"({t_tts:.2f}s) {len(pcm_out)//2} samples"
                        )

                        t_wr = time.monotonic()
                        await as_write_audio(self.w, pcm_out)
                        total_wr_s += time.monotonic() - t_wr

                    # Nach allen Chunks: Restzeit schlafen (Netzwerkpuffer).
                    # Pacing hat bereits ~total_tts_s verbraucht; 0.35s Puffer für Netz.
                    sleep_remaining = max(0.0, 0.35 - (total_wr_s - total_tts_s))
                    log.info(
                        f"[{self.label}] TIMING write_done total_tts={total_tts_s:.3f}s "
                        f"wr={total_wr_s:.3f}s sleep={sleep_remaining:.3f}s"
                    )
                    await asyncio.sleep(sleep_remaining)
                    log.info(f"[{self.label}] TIMING sleep_done → mute_OFF")
                finally:
                    if self.echo_partner:
                        self.echo_partner.mute(False)

                self.segments_ok += 1

                # Zurück zu CONNECTED
                if self.sess.state == CallState.TRANSLATING:
                    self.sess.transition(
                        CallState.CONNECTED,
                        f"{self.label} ok #{self.segments_ok} "
                        f"stt={t_stt:.2f}s trl={t_tr:.2f}s tts={t_tts:.2f}s"
                    )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.error(
                    f"[{self.label}] Fehler in state={self.sess.state.name}: "
                    f"{e}",
                    exc_info=True,
                )
                # State nicht auf ERROR setzen – Worker läuft weiter
                if self.sess.state == CallState.TRANSLATING:
                    self.sess.transition(
                        CallState.CONNECTED,
                        f"{self.label} Fehler: {e}"
                    )


# ══════════════════════════════════════════════════════════════════
# Audio-Empfangs-Loop
# ══════════════════════════════════════════════════════════════════
async def recv_loop(
    reader: asyncio.StreamReader,
    worker: Worker,
    wav_path: str,
    side: str,
    stop: asyncio.Event,
    session: CallSession,
) -> None:
    buf  = SpeechBuffer()
    rec: list[bytes] = []
    n    = 0
    try:
        while not stop.is_set():
            try:
                mtype, payload = await asyncio.wait_for(
                    as_read(reader), timeout=5.0
                )
            except asyncio.TimeoutError:
                log.debug(
                    f"[{side}] Audio-Timeout "
                    f"(state={session.state.name})"
                )
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
        session.transition(CallState.HANGUP, f"{side} Verbindung getrennt")
    except asyncio.CancelledError:
        raise
    except Exception as e:
        session.fail(f"{side} recv_loop: {e}")
    finally:
        stop.set()


# ══════════════════════════════════════════════════════════════════
# AMI – ausgehenden Anruf starten
# ══════════════════════════════════════════════════════════════════
async def ami_originate(
    number: str, partner_uuid: str, session: CallSession
) -> None:
    session.transition(
        CallState.OUTBOUND_DIALING,
        f"Originate → {number}"
    )
    r, w = await asyncio.open_connection(AMI_HOST, AMI_PORT)

    async def line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    await line()
    w.write(
        f"Action: Login\r\nUsername: {AMI_USER}\r\n"
        f"Secret: {AMI_PASS}\r\n\r\n".encode()
    )
    await w.drain()
    while "Authentication accepted" not in (await line()):
        pass

    w.write((
        f"Action: Originate\r\n"
        f"Channel: {TRUNK % number}\r\n"
        f"Context: audiosocket-out\r\n"
        f"Exten: s\r\nPriority: 1\r\n"
        f"CallerID: {CALLERID}\r\n"
        f"Timeout: 60000\r\n"
        f"Variable: PARTNER_UUID={partner_uuid}\r\n"
        f"Async: true\r\n\r\n"
    ).encode())
    await w.drain()
    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    log.info(f"[AMI] Originate abgeschickt → {number}  partner={partner_uuid}")


# ══════════════════════════════════════════════════════════════════
# HTTP-Registrierungs-Endpunkt  (Port 9094)
# AGI ruft POST /register auf mit {"uuid": "...", "exten": "..."}
# BEVOR AudioSocket() startet → absolut kein Race möglich
# ══════════════════════════════════════════════════════════════════
async def handle_register(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        data    = await asyncio.wait_for(reader.read(4096), timeout=3.0)
        request = data.decode(errors="replace")
        body    = ""
        if "\r\n\r\n" in request:
            body = request.split("\r\n\r\n", 1)[1]
        elif "\n\n" in request:
            body = request.split("\n\n", 1)[1]

        payload = json.loads(body)
        uid     = payload.get("uuid",  "").strip()
        exten   = payload.get("exten", "").strip()

        if uid and exten:
            _exten_map[uid] = exten
            log.info(f"[REG] uuid={uid[:8]} → exten={exten}")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        else:
            log.warning(f"[REG] Ungültige Payload: {body!r}")
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 3\r\n\r\nERR")
    except Exception as e:
        log.warning(f"[REG] Fehler: {e}")
        writer.write(b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 3\r\n\r\nERR")
    finally:
        await writer.drain()
        writer.close()


# ══════════════════════════════════════════════════════════════════
# Inbound-Call-Handler
# ══════════════════════════════════════════════════════════════════
async def handle_inbound(
    uuid: str,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    sess = CallSession(uuid)
    _sessions[uuid] = sess

    # ── Extension aus Registrierungs-Map ─────────────────────────
    # AGI hat die UUID bereits VOR AudioSocket() registriert → direkt verfügbar
    exten = _exten_map.get(uuid, "")
    if not exten:
        sess.fail(
            f"UUID {uuid[:8]} nicht registriert – "
            f"AGI-Skript nicht aufgerufen? Bekannte UUIDs: {list(_exten_map.keys())}"
        )
        await as_hangup_send(writer)
        writer.close()
        return

    sess.transition(CallState.REGISTERED, f"exten={exten}")

    sess.exten = exten
    suffix      = exten[-2:] if len(exten) >= 2 else ""
    remote_lang = SUFFIX_LANG.get(suffix, "it")
    dial_number = exten[:-2] if suffix in SUFFIX_LANG else exten
    sess.remote_lang  = remote_lang
    sess.dial_number  = dial_number

    log.info(
        f"[Inbound] uuid={uuid[:8]}  exten={exten}  "
        f"remote={remote_lang.upper()}  wähle={dial_number}"
    )

    # ── Loopback-Echo-Modus: TTS zurück auf den Anrufer ──────────
    # Aktiv wenn: globaler Flag, keine/kurze Zielnummer, oder eigene DID gewählt
    if LOOPBACK_ECHO or not dial_number or len(dial_number) <= 2 or dial_number in LOCAL_DIDS:
        log.info(f"[Inbound] LOOPBACK_ECHO aktiv — kein Outbound-Dial")
        sess.transition(CallState.CONNECTED, "Loopback-Modus")
        stop  = asyncio.Event()
        w_de  = Worker(
            "de", remote_lang, writer,
            f"DE→{remote_lang.upper()}[LOOP]", sess, _whisper_lock_de
        )
        recv_in = asyncio.create_task(
            recv_loop(reader, w_de, SAVE_DE_WAV, "Inbound", stop, sess)
        )
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
        log.info(f"[Inbound] {sess.summary()}  {w_de.gpu_peak}")
        _sessions.pop(uuid, None)
        return

    # ── Outbound starten ──────────────────────────────────────────
    partner_uuid = str(uuid_mod.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _out_waiters[partner_uuid] = fut

    await ami_originate(dial_number, partner_uuid, sess)

    sess.transition(
        CallState.OUTBOUND_WAIT,
        f"Warte auf Outbound-AudioSocket-Leg (max 60s)"
    )
    try:
        out_reader, out_writer = await asyncio.wait_for(fut, timeout=60.0)
    except asyncio.TimeoutError:
        sess.fail(
            f"Outbound-Leg Timeout nach 60s für {dial_number} "
            f"– FritzBox/PJSIP erreichbar?"
        )
        await as_hangup_send(writer)
        writer.close()
        return
    finally:
        _out_waiters.pop(partner_uuid, None)

    sess.transition(
        CallState.CONNECTED,
        f"Beide Legs verbunden  DE↔{remote_lang.upper()}"
    )

    # ── Audio-Bridge starten ──────────────────────────────────────
    stop = asyncio.Event()
    w_de = Worker("de",        remote_lang, out_writer, f"DE→{remote_lang.upper()}", sess, _whisper_lock_de)
    w_re = Worker(remote_lang, "de",        writer,     f"{remote_lang.upper()}→DE", sess, _whisper_lock_re)

    # Echo-Unterdrückung: jeder Worker kennt seinen Partner
    # w_de spielt TTS auf out_writer → muted w_re (IT-Eingang)
    # w_re spielt TTS auf writer     → muted w_de (DE-Eingang)
    w_de.echo_partner = w_re
    w_re.echo_partner = w_de

    recv_in  = asyncio.create_task(
        recv_loop(reader,     w_de, SAVE_DE_WAV, "Inbound",  stop, sess)
    )
    recv_out = asyncio.create_task(
        recv_loop(out_reader, w_re, SAVE_IT_WAV, "Outbound", stop, sess)
    )
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
        f"Segments ok: DE={w_de.segments_ok} {remote_lang.upper()}={w_re.segments_ok} "
        f"skip: DE={w_de.segments_skip} {remote_lang.upper()}={w_re.segments_skip}"
    )
    log.info(f"[Inbound] {sess.summary()}  {w_de.gpu_peak} / {w_re.gpu_peak}")
    _sessions.pop(uuid, None)


# ══════════════════════════════════════════════════════════════════
# TCP-Verbindungs-Handler
# ══════════════════════════════════════════════════════════════════
async def handle_connection(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    addr = writer.get_extra_info("peername")
    try:
        mtype, payload = await asyncio.wait_for(as_read(reader), timeout=5.0)
    except Exception as e:
        log.warning(f"[AS] UUID-Lesen fehlgeschlagen von {addr}: {e}")
        writer.close()
        return

    if mtype != AS_UUID:
        log.warning(f"[AS] Kein UUID-Paket (type={mtype:#x}) von {addr}")
        writer.close()
        return

    uuid = str(uuid_mod.UUID(bytes=payload))
    log.info(f"[AS] Neue Verbindung: uuid={uuid!r} von {addr}")

    # Outbound-Leg?
    if uuid in _out_waiters:
        if not _out_waiters[uuid].done():
            _out_waiters[uuid].set_result((reader, writer))
            log.info(f"[AS] Outbound-Leg verbunden: {uuid}")
        else:
            log.warning(
                f"[AS] Outbound-Waiter bereits erledigt für {uuid}"
            )
            writer.close()
        return

    asyncio.create_task(handle_inbound(uuid, reader, writer))


# ══════════════════════════════════════════════════════════════════
# Periodisches Status-Dump (alle 60s aktive Sessions)
# ══════════════════════════════════════════════════════════════════
async def status_dumper() -> None:
    while True:
        await asyncio.sleep(60)
        gpu = _gpu()
        if _sessions:
            log.info(f"[Status] {len(_sessions)} aktive Session(s)  {gpu}")
            for uuid, sess in list(_sessions.items()):
                log.info(f"  {sess.summary()}")
        else:
            log.info(f"[Status] idle  {gpu}")


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════
async def amain() -> None:
    log.info("Starte Modell-Load vor Server …")
    await asyncio.get_running_loop().run_in_executor(None, load_models)
    log.info("Modelle bereit — starte Server")

    asyncio.create_task(status_dumper())

    # HTTP-Registrierungs-Server (AGI → Python, Port 9094)
    reg_server = await asyncio.start_server(handle_register, REG_HOST, REG_PORT)
    log.info(f"Registrierungs-Endpunkt lauscht auf {REG_HOST}:{REG_PORT}")

    # AudioSocket-Server (Asterisk → Python, Port 9093)
    as_server = await asyncio.start_server(handle_connection, AS_HOST, AS_PORT)
    log.info(f"AudioSocket-Translator lauscht auf {AS_HOST}:{AS_PORT}")

    async with reg_server, as_server:
        await asyncio.gather(
            reg_server.serve_forever(),
            as_server.serve_forever(),
        )


if __name__ == "__main__":
    asyncio.run(amain())
