#!/home/gh/python/venv_py311/bin/python3
"""
SIP Translation B2BUA via AudioSocket — Asterisk 22
====================================================
Linphone / Fritz!Box  →  Asterisk (AudioSocket TCP)
  → Whisper STT  →  NLLB-200  →  Piper TTS
  →  Asterisk (AudioSocket TCP)
→  Fritz!Box / PSTN  (IT / RU / FR / …)

State Machine pro Anruf:
  INIT → AMI_WAIT → OUTBOUND_DIALING → OUTBOUND_WAIT
       → CONNECTED → TRANSLATING → HANGUP → DONE
"""

# torch must be imported before ctranslate2/transformers to prevent
# ctranslate2 from disabling PyTorch globally (version-check side-effect)
import torch

import asyncio, struct, logging, io, os, re, time, uuid as uuid_mod, json, wave, subprocess, datetime
from typing import Any, Callable
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
from transformers import NllbTokenizer, AutoModelForSeq2SeqLM

_SEMVER = "1.1.0"

def _build_version() -> str:
    """Return 'v<semver> git:<short-hash>' using the repo the script lives in."""
    try:
        repo = str(Path(__file__).parent)
        rev  = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return f"v{_SEMVER} git:{rev}"
    except Exception:
        return f"v{_SEMVER} git:unknown"

VERSION      = _build_version()
DEPLOYED_AT  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

TRUNK     = os.environ.get("TEST_TRUNK", "PJSIP/%s@fritzbox-out")
CALLERID  = "linuxsip <+4980425641873>"

NLLB_MODEL  = "facebook/nllb-200-distilled-1.3B"
NLLB_CACHE  = os.path.join(os.path.dirname(__file__), "nllb_cache")
NLLB_LANG: dict[str, str] = {
    "de": "deu_Latn", "en": "eng_Latn", "fr": "fra_Latn", "it": "ita_Latn",
    "ru": "rus_Cyrl", "es": "spa_Latn", "el": "ell_Grek", "pl": "pol_Latn",
    "pt": "por_Latn", "uk": "ukr_Cyrl", "kk": "kaz_Cyrl", "zh": "zho_Hans",
    "tr": "tur_Latn", "hi": "hin_Deva", "fa": "pes_Arab", "ka": "kat_Geor",
}

_nllb_tok:   "NllbTokenizer | None"          = None
_nllb_model: "AutoModelForSeq2SeqLM | None"  = None

AMI_HOST  = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT  = int(os.environ.get("AMI_PORT", 5038))
AMI_USER  = os.environ.get("AMI_USER", "admin")
AMI_PASS  = os.environ.get("AMI_PASS", "")

SUFFIX_LANG = {
    "1":   "en",   # +1  USA
    "7":   "ru",   # +7  Russia
    "30":  "el",   # +30 Greece
    "33":  "fr",   # +33 France
    "34":  "es",   # +34 Spain
    "38":  "uk",   # +380 Ukraine
    "39":  "it",   # +39 Italy
    "44":  "en",   # +44 UK
    "48":  "pl",   # +48 Poland
    "55":  "pt",   # +55 Brazil
    "77":  "kk",   # +77 Kazakhstan
    "86":  "zh",   # +86 China
    "90":  "tr",   # +90 Turkey
    "91":  "hi",   # +91 India
    "98":  "fa",   # +98 Iran
    "995": "ka",   # +995 Georgia
}

# CallerID prefix → announcement/Whisper language
# Ordered longest-first so +380 matches before +38 etc.
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
    """Map an incoming CallerID number to a language code (default: 'de')."""
    for prefix, lang in CALLERID_PREFIX_LANG:
        if callerid.startswith(prefix):
            return lang
    return "de"

# NLU: language keyword substrings → dial suffix key (None = German, no suffix)
_LANG_KW: list[tuple[str, str | None]] = [
    ("kazakh",      "77"), ("kasachisch", "77"), ("kazakhstan", "77"),
    ("georgian",    "995"), ("georgisch", "995"), ("georgien",  "995"),
    ("persian",     "98"),  ("farsi",     "98"),  ("iran",      "98"),
    ("hindi",       "91"),  ("indien",    "91"),  ("india",     "91"),
    ("chinese",     "86"),  ("chinesisch","86"),  ("china",     "86"),
    ("turkish",     "90"),  ("türkisch",  "90"),  ("türkei",    "90"),  ("turkey","90"),
    ("polish",      "48"),  ("polnisch",  "48"),  ("poland",    "48"),  ("polen", "48"),
    ("portuguese",  "55"),  ("brasilianisch","55"),("brazil",   "55"),  ("brasil","55"),
    ("ukrainian",   "38"),  ("ukrainisch","38"),  ("ukraine",   "38"),
    ("greek",       "30"),  ("griechisch","30"),  ("greece",    "30"),  ("griechenland","30"),
    ("spanish",     "34"),  ("spanisch",  "34"),  ("spain",     "34"),  ("spanien","34"),
    ("french",      "33"),  ("französisch","33"), ("france",    "33"),  ("frankreich","33"),
    ("english",     "44"),  ("englisch",  "44"),  ("england",   "44"),  ("britain","44"),
    ("russian",     "7"),   ("russisch",  "7"),   ("russia",    "7"),   ("russland","7"),
    ("italian",     "39"),  ("italienisch","39"), ("italiano",  "39"),  ("italien","39"), ("italia","39"),
    ("american",    "1"),   ("usa",       "1"),
    ("deutsch",     None),  ("german",    None),  ("deutschland",None), ("germany",None),
]

# Digit prefix in extracted phone number → SUFFIX_LANG key (None = German, skip)
# Listed longest-first so "00995" matches before "009" etc.
_DIGIT_PREFIX_SUFFIX: list[tuple[str, str | None]] = [
    # 00-international format
    ("00995", "995"), ("00380", "38"),
    ("0039",  "39"),  ("0086",  "86"),  ("0090",  "90"),
    ("0091",  "91"),  ("0098",  "98"),  ("0049",  None),
    ("0044",  "44"),  ("0033",  "33"),  ("0034",  "34"),
    ("0048",  "48"),  ("0055",  "55"),  ("0030",  "30"),
    ("0077",  "77"),  ("007",   "7"),   ("001",   "1"),
    # bare country-code format (Whisper drops leading 00 or strips +)
    ("995",  "995"),  ("380",  "38"),
    ("39",   "39"),   ("86",   "86"),   ("90",   "90"),
    ("91",   "91"),   ("98",   "98"),   ("49",   None),
    ("44",   "44"),   ("33",   "33"),   ("34",   "34"),
    ("48",   "48"),   ("55",   "55"),   ("30",   "30"),
    ("77",   "77"),   ("7",    "7"),    ("1",    "1"),
]

def _build_german_number_words() -> dict[str, str]:
    """Generate German number words 10–999 → digit string."""
    ones = ["", "ein", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"]
    teens = {
        "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
        "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
    }
    tens = {
        20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig",
        60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig",
    }

    # 10–99
    words_1_99: dict[str, int] = {w: v for w, v in teens.items()}
    for tv, tw in tens.items():
        words_1_99[tw] = tv
        for ov in range(1, 10):
            words_1_99[f"{ones[ov]}und{tw}"] = tv + ov
    # single digits as remainder fragments (used for 101–109 etc.)
    for ov in range(1, 10):
        words_1_99[ones[ov]] = ov          # "ein"→1, "zwei"→2, …

    out: dict[str, str] = {w: str(v) for w, v in words_1_99.items()}

    # 100–999: {prefix}hundert{optional_remainder}
    for h in range(1, 10):
        # "hundert", "zweihundert", …; also "einhundert" as alias for "hundert"
        h_words = [ones[h] + "hundert"] if h > 1 else ["hundert", "einhundert"]
        for hw in h_words:
            out[hw] = str(h * 100)
            for rem_word, rem_val in words_1_99.items():
                out[hw + rem_word] = str(h * 100 + rem_val)

    # Remove bare single-digit remainder entries (they live in _WORD_DIGIT directly)
    for ov in range(1, 10):
        out.pop(ones[ov], None)

    return out

# Spoken number words → digit string (de / it / en / ru)
# German 10-99 is generated; all lists sorted longest-first so the regex
# matches "achtundsiebzig" before "acht" or "siebzig".
_WORD_DIGIT: dict[str, str] = {
    # German single digits
    "null": "0", "eins": "1", "ein": "1", "zwei": "2", "drei": "3",
    "vier": "4", "fünf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9",
    # Italian single digits
    "zero": "0", "uno": "1", "due": "2", "tre": "3", "quattro": "4",
    "cinque": "5", "sei": "6", "sette": "7", "otto": "8", "nove": "9",
    # English single digits
    "one": "1", "two": "2", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    # Russian single digits
    "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9",
    # German 10–99 compound words (generated)
    **_build_german_number_words(),
}

# Regex built once; longest entries first so compound words beat single-digit prefixes
import re as _re
_WORD_DIGIT_RE = _re.compile(
    r'\b(' + '|'.join(
        _re.escape(w) for w in sorted(_WORD_DIGIT, key=len, reverse=True)
    ) + r')\b'
)

# Also match the space-separated German form "acht und siebzig" → merge to "achtundsiebzig"
_TENS_WORDS = '|'.join([
    'zwanzig','dreißig','vierzig','fünfzig','sechzig','siebzig','achtzig','neunzig'
])
_ONES_WORDS = 'ein|zwei|drei|vier|fünf|sechs|sieben|acht|neun'
_UND_RE = _re.compile(
    rf'\b({_ONES_WORDS})\s+und\s+({_TENS_WORDS})\b'
)


def extract_dial_info(text: str) -> tuple[str, str]:
    """Extract (phone_digits, lang_suffix) from NLU transcription.

    Returns ("", "") if a phone number or target language cannot be determined.
    Handles:
    - Spoken number words (eins→1, null→0, uno→1 …)
    - "plus"/"+" stripped (Whisper transcribes +49 as "plus 4,9" or "plus vier neun")
    - Both 0039 and bare 39 country-code prefixes
    """
    import re
    text_low = text.lower()

    # Merge space-separated German compound numbers: "acht und siebzig" → "achtundsiebzig"
    normalised = _UND_RE.sub(lambda m: m.group(1) + 'und' + m.group(2), text_low)

    # Replace spoken number words with digit strings (longest match wins)
    normalised = _WORD_DIGIT_RE.sub(lambda m: _WORD_DIGIT[m.group(0)], normalised)

    # Strip "plus" and "+" (international prefix marker — Whisper writes +49 as "plus 4,9")
    normalised = re.sub(r'\bplus\b|\+', '', normalised)

    # Collapse all digit sequences into one string
    digits = "".join(re.findall(r"\d+", normalised))

    if len(digits) < 5:
        return "", ""

    # Language keyword match (run against original lowercased text)
    suffix: str | None = "_unset"
    for kw, sfx in _LANG_KW:
        if kw in text_low:
            suffix = sfx
            break

    # Country-prefix detection (00XX then bare XX)
    if suffix == "_unset":
        for prefix, sfx in _DIGIT_PREFIX_SUFFIX:
            if digits.startswith(prefix):
                suffix = sfx
                break

    # German or still unresolved → fall back to DTMF
    if suffix is None or suffix == "_unset":
        return "", ""

    # Normalize to E.164 (+XX…) — Fritz!Box and SIP both require an explicit prefix.
    # Without it, bare "4917625257878" would be routed as a local extension.
    if digits.startswith("00"):
        digits = "+" + digits[2:]          # 0039… → +39…
    elif not digits.startswith("+"):
        digits = "+" + digits              # 4917… → +4917…

    return digits, suffix

# Eigene DIDs — Anruf darauf ist immer Loopback (kein Outbound)
LOCAL_DIDS = {"+4980424967"}

PIPER_MODELS_DIR = "/home/gh/python/translator/piper_models"
PIPER_VOICES = {
    "de": "de_DE-thorsten-medium",
    "it": "it_IT-paola-medium",
    "ru": "ru_RU-dmitri-medium",
    "en": "en_GB-alan-medium",
    "fr": "fr_FR-siwis-medium",
    "es": "es_ES-davefx-medium",
    "el": "el_GR-rapunzelina-medium",
    "pl": "pl_PL-darkman-medium",
    "pt": "pt_BR-faber-medium",
    "uk": "uk_UA-ukrainian_tts-medium",
    "tr": "tr_TR-dfki-medium",
    "zh": "zh_CN-huayan-medium",
    "hi": "hi_IN-rohan-medium",
    "fa": "fa_IR-amir-medium",
    "kk": "kk_KZ-issai-high",
    "ka": "ka_GE-natia-medium",
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
        _sessions.pop(self.uuid, None)   # sofort entfernen — kein Zombie im Statuslog

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


# ══════════════════════════════════════════════════════════════════
# GPU-Inference-Server — strikte Trennung simultaner Anrufe
# ══════════════════════════════════════════════════════════════════
class GpuInferenceServer:
    """FIFO-Queue für alle GPU-Jobs (Whisper + NLLB).

    Jeder Anruf reiht seine Segmente als Future-Jobs ein.
    Der Consumer arbeitet sequenziell — bei N simultanen Calls
    blockiert kein Anruf einen anderen; alle Segmente werden
    fair abgearbeitet ohne globale Locks.
    """

    def __init__(self) -> None:
        self._q: asyncio.Queue[tuple[Callable, asyncio.Future]] = asyncio.Queue()

    def start(self) -> None:
        asyncio.create_task(self._consumer())

    async def _consumer(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            fn, fut = await self._q.get()
            if fut.cancelled():
                continue
            try:
                result = await loop.run_in_executor(None, fn)
                if not fut.done():
                    fut.set_result(result)
            except Exception as exc:
                if not fut.done():
                    fut.set_exception(exc)

    async def run(self, fn: Callable[[], Any]) -> Any:
        """Sync-Callable in die GPU-Queue einreihen und Ergebnis awaiten."""
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._q.put((fn, fut))
        return await fut


# ── Globaler Zustand ──────────────────────────────────────────────
_whisper: WhisperModel | None = None
_gpu_server: GpuInferenceServer          # wird in amain() gestartet
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

    global _nllb_tok, _nllb_model
    log.info("Lade NLLB-200-distilled-1.3B …")
    warnings.filterwarnings("ignore", category=FutureWarning)
    _nllb_tok = NllbTokenizer.from_pretrained(
        NLLB_MODEL, cache_dir=NLLB_CACHE, clean_up_tokenization_spaces=True
    )
    _nllb_model = AutoModelForSeq2SeqLM.from_pretrained(
        NLLB_MODEL, cache_dir=NLLB_CACHE, torch_dtype=torch.float16,
    ).to("cuda")
    _nllb_model.eval()
    vram = torch.cuda.memory_allocated() // 1024 // 1024
    log.info(f"NLLB bereit  ({vram} MB VRAM)")


# ══════════════════════════════════════════════════════════════════
# NLU-Ansage-Generierung (Piper → Asterisk custom sounds)
# ══════════════════════════════════════════════════════════════════
SOUNDS_CUSTOM = "/usr/share/asterisk/sounds/custom"

NLU_PROMPT_TEXTS: dict[str, str] = {
    "de": "Bitte Zielrufnummer und Sprache nennen.",
    "it": "Prego indicare il numero di destinazione e la lingua.",
    "ru": "Пожалуйста, назовите номер назначения и язык.",
    "en": "Please state the destination number and language.",
    "fr": "Veuillez indiquer le numéro de destination et la langue.",
    "es": "Por favor, indique el número de destino y el idioma.",
    "el": "Παρακαλώ αναφέρετε τον αριθμό προορισμού και τη γλώσσα.",
    "pl": "Proszę podać numer docelowy i język.",
    "pt": "Por favor, indique o número de destino e o idioma.",
    "uk": "Будь ласка, назвіть номер призначення та мову.",
    "tr": "Lütfen hedef numarayı ve dili belirtin.",
    "zh": "请说出目标号码和语言。",
    "hi": "कृपया गंतव्य नंबर और भाषा बताएं।",
    "fa": "لطفاً شماره مقصد و زبان را بگویید.",
    "kk": "Тағайындалу нөмірі мен тілді айтыңыз.",
    "ka": "გთხოვთ, მიუთითოთ დანიშნულების ნომერი და ენა.",
}


def generate_nlu_prompts() -> None:
    """Synthesise NLU announcement WAVs (8 kHz mono s16le) for every loaded Piper voice."""
    os.makedirs(SOUNDS_CUSTOM, exist_ok=True)
    for lang, text in NLU_PROMPT_TEXTS.items():
        if lang not in _piper_voices:
            log.debug(f"[NLU-Prompt] kein Piper-Modell für {lang}")
            continue
        out = os.path.join(SOUNDS_CUSTOM, f"nlu_prompt_{lang}.wav")
        try:
            pcm8  = _tts_piper_sync(text, lang)
            audio = np.frombuffer(pcm8, dtype=np.int16)
            sf.write(out, audio, SR_AS)
            log.info(f"[NLU-Prompt] {lang} → {out}")
        except Exception as exc:
            log.warning(f"[NLU-Prompt] {lang} Fehler: {exc}")


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



async def _drain_inbound(
    r: asyncio.StreamReader, stop: asyncio.Event, fut: asyncio.Future
) -> None:
    """Drain incoming audio while in OUTBOUND_WAIT; cancel fut on inbound hangup."""
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


def _read_wav_16k(path: str) -> np.ndarray:
    """Read any WAV file and return 16 kHz mono float32 array (for Whisper)."""
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SR_WH:
        n = max(1, int(len(audio) * SR_WH / sr))
        audio = sp.resample(audio, n).astype(np.float32)
    return audio


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


async def stt_chunks(pcm8: bytes, lang: str, gpu_server: "GpuInferenceServer",
                     gpu_peak: "_GpuPeak | None" = None) -> list[str]:
    """Transkribiert PCM8 und gibt eine Liste von Satz-Chunks zurück."""
    audio = pcm8_to_float16(pcm8)
    log.info(f"STT start  {_gpu()}")
    t0 = time.monotonic()
    segs, _ = await gpu_server.run(
        lambda: _whisper.transcribe(
            audio, language=lang, beam_size=1,
            vad_filter=True,
            word_timestamps=True,
            no_speech_threshold=0.7,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )
    )
    if gpu_peak: gpu_peak.sample()
    log.info(f"STT done   {_gpu()}  ({time.monotonic()-t0:.2f}s)")
    chunks = _split_into_sentence_chunks(segs)
    if chunks:
        log.debug(f"stt_chunks: {chunks}")
    return chunks


_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')

def _translate_one(text: str, src: str, tgt_id: int) -> str:
    _nllb_tok.src_lang = src
    inputs = _nllb_tok(text, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad():
        out = _nllb_model.generate(
            **inputs,
            forced_bos_token_id=tgt_id,
            max_new_tokens=256,
            num_beams=4,
        )
    return _nllb_tok.batch_decode(out, skip_special_tokens=True)[0]


def translate_sync(text: str, fl: str, tl: str) -> str:
    if fl == tl:
        return text
    src    = NLLB_LANG.get(fl, "deu_Latn")
    tgt_id = _nllb_tok.convert_tokens_to_ids(NLLB_LANG.get(tl, "eng_Latn"))
    # Satzweise übersetzen — verhindert dass NLLB Sätze bei Mehrfacheingabe weglässt
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        return _translate_one(text, src, tgt_id)
    return " ".join(_translate_one(s, src, tgt_id) for s in sentences)


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
        gpu_server: "GpuInferenceServer",
        echo_partner: "Worker | None" = None,
    ) -> None:
        self.fl         = from_lang
        self.tl         = to_lang
        self.w          = writer
        self.label      = label
        self.sess       = session
        self.gpu_server = gpu_server
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
                chunks = await stt_chunks(pcm8, self.fl, self.gpu_server, self.gpu_peak)
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
                # Nächste Übersetzung läuft parallel während as_write_audio spielt.
                # GPU ist während des Echtzeit-Pacing idle → kostenlose Überlappung.
                next_trans_task: asyncio.Task | None = None
                try:
                    for i, chunk in enumerate(chunks):
                        fl, tl = self.fl, self.tl

                        # Übersetzung: vorberechnete Task verwenden oder frisch starten
                        t0 = time.monotonic()
                        if next_trans_task is not None:
                            trans = await next_trans_task
                            next_trans_task = None
                        else:
                            trans = await self.gpu_server.run(
                                lambda ch=chunk: translate_sync(ch, fl, tl)
                            )
                        t_tr = time.monotonic() - t0
                        log.info(
                            f"[{self.label}] TRL[{i+1}/{len(chunks)}]"
                            f"({t_tr:.2f}s) [{tl.upper()}] {trans!r}"
                        )

                        t0      = time.monotonic()
                        pcm_out = await tts(trans, tl)
                        t_tts   = time.monotonic() - t0
                        tts_dur = len(pcm_out) / 2 / SR_AS
                        total_tts_s += tts_dur
                        log.info(
                            f"[{self.label}] TTS[{i+1}/{len(chunks)}]"
                            f"({t_tts:.2f}s) {len(pcm_out)//2} samples"
                        )

                        # Nächste Übersetzung während Abspielen starten (GPU idle)
                        if i + 1 < len(chunks):
                            next_ch = chunks[i + 1]
                            next_trans_task = asyncio.create_task(
                                self.gpu_server.run(
                                    lambda ch=next_ch: translate_sync(ch, fl, tl)
                                )
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
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                log.warning(
                    f"[{self.label}] Schreibverbindung getrennt — Worker beendet: {e}"
                )
                return
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
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=10.0
        )
    except asyncio.TimeoutError as exc:
        raise ConnectionError(
            f"AMI nicht erreichbar ({AMI_HOST}:{AMI_PORT})"
        ) from exc

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
        f"Application: AudioSocket\r\n"
        f"Data: {partner_uuid},{AS_HOST}:{AS_PORT}\r\n"
        f"CallerID: {CALLERID}\r\n"
        f"Timeout: 60000\r\n"
        f"Async: true\r\n\r\n"
    ).encode())
    await w.drain()
    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    log.info(f"[AMI] Originate abgeschickt → {number}  partner={partner_uuid}")


# ══════════════════════════════════════════════════════════════════
# HTTP-Endpunkt  (Port 9094)
# POST /register  — AGI registriert UUID+Exten vor AudioSocket()
# POST /nlu       — AGI schickt WAV-Pfad, erhält Nummer+Suffix
# POST /lang      — AGI fragt CallerID-Sprache ab
# ══════════════════════════════════════════════════════════════════
def _http_ok_json(writer: asyncio.StreamWriter, body: bytes) -> None:
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _http_err(writer: asyncio.StreamWriter, code: int, msg: str) -> None:
    body  = msg.encode()
    status = {400: "Bad Request", 500: "Internal Server Error"}.get(code, "Error")
    writer.write(
        f"HTTP/1.1 {code} {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )


async def _handle_register_body(body: str, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    uid   = payload.get("uuid",  "").strip()
    exten = payload.get("exten", "").strip()
    if uid and exten:
        _exten_map[uid] = exten
        log.info(f"[REG] uuid={uid[:8]} → exten={exten}")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
    else:
        log.warning(f"[REG] Ungültige Payload: {body!r}")
        _http_err(writer, 400, "ERR")


async def _handle_nlu_body(body: str, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    path    = payload.get("path", "").strip()
    lang    = payload.get("lang", "de").strip()

    if not path or not os.path.exists(path):
        log.warning(f"[NLU] Datei nicht gefunden: {path!r}")
        _http_err(writer, 400, '{"error":"path not found"}')
        return

    loop = asyncio.get_running_loop()

    # File read + resample (fast, no GPU lock needed)
    try:
        audio = await loop.run_in_executor(None, _read_wav_16k, path)
    except Exception as exc:
        log.warning(f"[NLU] WAV-Lesen fehlgeschlagen: {exc}")
        _http_err(writer, 500, '{"error":"wav read"}')
        return

    # Whisper transcription — auto-detect language so any spoken language is captured.
    # Caller language (lang) is kept only for logging context.
    segs, info = await _gpu_server.run(
        lambda: _whisper.transcribe(
            audio,
            language=None,          # auto-detect
            beam_size=3,
            vad_filter=True,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )
    )

    detected = info.language
    conf     = info.language_probability
    text     = " ".join(s.text.strip() for s in segs).strip()
    number, suffix = extract_dial_info(text)
    log.info(
        f"[NLU] caller_lang={lang} detected={detected}({conf:.2f})"
        f" text={text!r} → number={number!r} suffix={suffix!r}"
    )

    if not text:
        # Keep the WAV for post-mortem debugging — do NOT delete
        log.warning(f"[NLU] leere Transkription — WAV behalten: {path}")
    else:
        try:
            os.unlink(path)
        except Exception:
            pass

    resp = json.dumps({"text": text, "number": number, "suffix": suffix}).encode()
    _http_ok_json(writer, resp)


async def _handle_translate_body(body: str, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    text = payload.get("text", "").strip()
    src  = payload.get("from", "de").strip()
    tgt  = payload.get("to",   "it").strip()
    if not text:
        _http_err(writer, 400, '{"error":"no text"}')
        return
    result = await _gpu_server.run(lambda: translate_sync(text, src, tgt))
    _http_ok_json(writer, json.dumps({"result": result}).encode())


async def _handle_tts_body(body: str, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    text = payload.get("text", "").strip()
    lang = payload.get("lang", "it").strip()
    if not text:
        _http_err(writer, 400, '{"error":"no text"}')
        return
    pcm8 = await tts(text, lang)
    buf  = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR_AS)
        wf.writeframes(pcm8)
    wav_bytes = buf.getvalue()
    writer.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
        + f"Content-Length: {len(wav_bytes)}\r\n\r\n".encode()
        + wav_bytes
    )


async def _handle_lang_body(body: str, writer: asyncio.StreamWriter) -> None:
    payload  = json.loads(body)
    callerid = payload.get("callerid", "").strip()
    lang     = callerid_to_lang(callerid)
    log.info(f"[LANG] callerid={callerid!r} → lang={lang}")
    _http_ok_json(writer, json.dumps({"lang": lang}).encode())


async def handle_http(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        data    = await asyncio.wait_for(reader.read(8192), timeout=10.0)
        request = data.decode(errors="replace")
        body    = ""
        if "\r\n\r\n" in request:
            body = request.split("\r\n\r\n", 1)[1]
        elif "\n\n" in request:
            body = request.split("\n\n", 1)[1]

        first_line = request.split("\n", 1)[0].strip()
        parts      = first_line.split(" ")
        path       = parts[1] if len(parts) >= 2 else "/"

        if path.startswith("/nlu"):
            await _handle_nlu_body(body, writer)
        elif path.startswith("/lang"):
            await _handle_lang_body(body, writer)
        elif path.startswith("/translate"):
            await _handle_translate_body(body, writer)
        elif path.startswith("/tts"):
            await _handle_tts_body(body, writer)
        else:
            await _handle_register_body(body, writer)

    except Exception as e:
        log.warning(f"[HTTP] Fehler: {e}")
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
    matched_suffix = next(
        (s for s in sorted(SUFFIX_LANG, key=len, reverse=True) if exten.endswith(s)),
        None
    )
    remote_lang = SUFFIX_LANG[matched_suffix] if matched_suffix else "it"
    dial_number = exten[:-len(matched_suffix)] if matched_suffix else exten
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
            f"DE→{remote_lang.upper()}[LOOP]", sess, _gpu_server
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

    try:
        await ami_originate(dial_number, partner_uuid, sess)
    except Exception as e:
        _out_waiters.pop(partner_uuid, None)
        sess.fail(f"AMI-Originate fehlgeschlagen: {e}")
        await as_hangup_send(writer)
        writer.close()
        return

    sess.transition(
        CallState.OUTBOUND_WAIT,
        f"Warte auf Outbound-AudioSocket-Leg (max 60s)"
    )
    stop_drain = asyncio.Event()
    drain_task = asyncio.create_task(_drain_inbound(reader, stop_drain, fut))
    try:
        out_reader, out_writer = await asyncio.wait_for(fut, timeout=60.0)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        stop_drain.set()
        drain_task.cancel()
        await asyncio.gather(drain_task, return_exceptions=True)
        if isinstance(exc, asyncio.TimeoutError):
            sess.fail(
                f"Outbound-Leg Timeout nach 60s für {dial_number} "
                f"– FritzBox/PJSIP erreichbar?"
            )
        else:
            sess.fail(f"Inbound-Hangup während Warten auf {dial_number}")
        await as_hangup_send(writer)
        writer.close()
        return
    finally:
        _out_waiters.pop(partner_uuid, None)

    stop_drain.set()
    drain_task.cancel()
    await asyncio.gather(drain_task, return_exceptions=True)

    sess.transition(
        CallState.CONNECTED,
        f"Beide Legs verbunden  DE↔{remote_lang.upper()}"
    )

    # ── Audio-Bridge starten ──────────────────────────────────────
    stop = asyncio.Event()
    w_de = Worker("de",        remote_lang, out_writer, f"DE→{remote_lang.upper()}", sess, _gpu_server)
    w_re = Worker(remote_lang, "de",        writer,     f"{remote_lang.upper()}→DE", sess, _gpu_server)

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
def _log_task_exception(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        log.error(
            f"[Task] Unbehandelte Exception in {task.get_name()}: {exc}",
            exc_info=exc,
        )


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

    task = asyncio.create_task(handle_inbound(uuid, reader, writer))
    task.add_done_callback(_log_task_exception)


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
    global _gpu_server
    log.info(f"AudioSocket-Translator  version={VERSION}  deployed={DEPLOYED_AT}")
    _gpu_server = GpuInferenceServer()
    _gpu_server.start()
    log.info("GPU-Inference-Server gestartet")
    log.info("Starte Modell-Load vor Server …")
    await asyncio.get_running_loop().run_in_executor(None, load_models)
    log.info("Modelle bereit — starte Server")

    asyncio.create_task(status_dumper())

    # NLU-Ansage-WAVs generieren (nach Piper-Load, vor Server-Start)
    await asyncio.get_running_loop().run_in_executor(None, generate_nlu_prompts)

    # HTTP-Server (AGI → Python, Port 9094): /register, /nlu, /lang
    reg_server = await asyncio.start_server(handle_http, REG_HOST, REG_PORT)
    log.info(f"HTTP-Endpunkt lauscht auf {REG_HOST}:{REG_PORT}")

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
