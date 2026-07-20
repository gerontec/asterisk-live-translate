#!/home/gh/python/venv_py311/bin/python3
"""
Inference Server — Whisper STT + NLLB-200 Translation + Piper TTS
==================================================================
Listens on port 9095 (localhost only).

  POST /stt?lang=de   Body: raw SLIN16 PCM 16 kHz   → {"chunks":[…]}
  POST /translate     {"text","from","to"}           → {"result":"…"}
  POST /tts           {"text","lang"}                → audio/wav 16 kHz
  POST /nlu           {"path","lang"}                → {"text","number","suffix"}
"""

# import torch before ctranslate2/transformers (prevents PyTorch deactivation)
import torch

import asyncio, io, json, logging, logging.handlers, os, re, time, wave
from math import gcd as _gcd
from typing import Any, Callable
import warnings; warnings.filterwarnings("ignore", category=FutureWarning)

import numpy as np
from scipy import signal as sp
import soundfile as sf
from faster_whisper import WhisperModel
from piper.voice import PiperVoice
from transformers import NllbTokenizer, AutoModelForSeq2SeqLM

# ── Configuration ──────────────────────────────────────────────────
INFER_HOST = "127.0.0.1"
INFER_PORT = 9095
# Server-Bind: alle Interfaces (IPv6 dual-stack). Der Zugriff wird per
# nftables-Whitelist (fw9095) auf dell beschränkt, nicht über die Bind-Adresse.
INFER_BIND = os.environ.get("INFER_BIND", "::")

SR_AS = 16000   # AudioSocket SLIN16
SR_WH = 16000   # Whisper float32 16 kHz

NLLB_MODEL = "facebook/nllb-200-distilled-1.3B"
NLLB_CACHE = os.path.join(os.path.dirname(__file__), "nllb_cache")
NLLB_LANG: dict[str, str] = {
    "de": "deu_Latn", "en": "eng_Latn", "fr": "fra_Latn", "it": "ita_Latn",
    "ru": "rus_Cyrl", "es": "spa_Latn", "el": "ell_Grek", "pl": "pol_Latn",
    "pt": "por_Latn", "uk": "ukr_Cyrl", "kk": "kaz_Cyrl", "zh": "zho_Hans",
    "tr": "tur_Latn", "hi": "hin_Deva", "fa": "pes_Arab", "ka": "kat_Geor",
}

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

SOUNDS_CUSTOM = "/usr/share/asterisk/sounds/custom"

# ── Logging ────────────────────────────────────────────────────────
_log_handler = logging.handlers.RotatingFileHandler(
    "/tmp/inference_server.log", maxBytes=90_000, backupCount=1, encoding="utf-8"
)
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[_log_handler, _stream_handler])
log = logging.getLogger("infer")

# ── GPU monitoring (pynvml, optional) ─────────────────────────────
try:
    import pynvml as _nv
    _nv.nvmlInit()
    _nv_handle = _nv.nvmlDeviceGetHandleByIndex(0)
    _nv_ok = True
    log.info("GPU monitoring active (pynvml)")
except Exception as _e:
    _nv_ok = False
    log.info(f"GPU monitoring not available: {_e}")

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


# ══════════════════════════════════════════════════════════════════
# GPU inference queue — serializes all GPU jobs in FIFO order
# ══════════════════════════════════════════════════════════════════
class GpuInferenceServer:
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
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._q.put((fn, fut))
        return await fut


# ── Model globals ─────────────────────────────────────────────────
_whisper:    WhisperModel | None          = None
_nllb_tok:   NllbTokenizer | None         = None
_nllb_model: AutoModelForSeq2SeqLM | None = None
_piper_voices: dict[str, PiperVoice]      = {}
_gpu_server: GpuInferenceServer


def load_models() -> None:
    global _whisper, _nllb_tok, _nllb_model

    log.info("Loading Whisper medium (CUDA, int8) ...")
    _whisper = WhisperModel("medium", device="cuda", compute_type="int8")
    log.info("Whisper ready.")

    log.info("Loading Piper TTS models ...")
    for lang, model_name in PIPER_VOICES.items():
        path = os.path.join(PIPER_MODELS_DIR, f"{model_name}.onnx")
        if os.path.exists(path):
            _piper_voices[lang] = PiperVoice.load(path)
            log.info(f"  Piper {lang}: {model_name} ({_piper_voices[lang].config.sample_rate} Hz)")
        else:
            log.warning(f"  Piper {lang}: not found: {path}")
    log.info("Piper ready.")

    log.info(f"Loading {NLLB_MODEL} ...")
    _nllb_tok = NllbTokenizer.from_pretrained(
        NLLB_MODEL, cache_dir=NLLB_CACHE, clean_up_tokenization_spaces=True
    )
    _nllb_model = AutoModelForSeq2SeqLM.from_pretrained(
        NLLB_MODEL, cache_dir=NLLB_CACHE,
    ).to("cuda")
    _nllb_model.eval()
    vram = torch.cuda.memory_allocated() // 1024 // 1024
    log.info(f"NLLB ready  ({vram} MB VRAM)  {_gpu()}")


# ══════════════════════════════════════════════════════════════════
# Audio-Hilfsfunktionen
# ══════════════════════════════════════════════════════════════════
def _resample(audio: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out:
        return audio
    d = _gcd(sr_in, sr_out)
    return sp.resample_poly(audio, sr_out // d, sr_in // d).astype(np.float32)


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def _read_wav_16k(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    return _resample(audio, sr, SR_WH)


# ══════════════════════════════════════════════════════════════════
# STT — Whisper
# ══════════════════════════════════════════════════════════════════
_SENT_END = frozenset({".", "!", "?", "...", "…"})


def _split_into_sentence_chunks(segs) -> list[str]:
    chunks: list[str] = []
    buf:    list[str] = []
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
            text = seg.text.strip()
            if text:
                chunks.append(text)
    if buf:
        text = "".join(buf).strip()
        if text:
            chunks.append(text)
    return chunks


async def _stt(pcm: bytes, lang: str) -> list[str]:
    audio = pcm_to_float(pcm)
    log.info(f"STT start lang={lang}  {_gpu()}")
    t0 = time.monotonic()

    def _run() -> list:
        segs, _ = _whisper.transcribe(
            audio, language=lang, beam_size=2,
            vad_filter=True,
            word_timestamps=True,
            no_speech_threshold=0.7,
            log_prob_threshold=-1.0,
            condition_on_previous_text=False,
        )
        return list(segs)  # consume generator inside executor

    segs_list = await _gpu_server.run(_run)
    log.info(f"STT done  {_gpu()}  ({time.monotonic()-t0:.2f}s)")
    return _split_into_sentence_chunks(segs_list)


# ══════════════════════════════════════════════════════════════════
# Übersetzung — NLLB
# ══════════════════════════════════════════════════════════════════
_SENT_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _translate_one(text: str, src: str, tgt_id: int) -> str:
    _nllb_tok.src_lang = src
    inputs = _nllb_tok(text, return_tensors="pt", padding=True).to("cuda")
    with torch.no_grad():
        out = _nllb_model.generate(
            **inputs,
            forced_bos_token_id=tgt_id,
            max_new_tokens=256,
            num_beams=2,
        )
    return _nllb_tok.batch_decode(out, skip_special_tokens=True)[0]


def translate_sync(text: str, fl: str, tl: str) -> str:
    if fl == tl:
        return text
    src    = NLLB_LANG.get(fl, "deu_Latn")
    tgt_id = _nllb_tok.convert_tokens_to_ids(NLLB_LANG.get(tl, "eng_Latn"))
    sentences = [s.strip() for s in _SENT_SPLIT_RE.split(text) if s.strip()]
    if len(sentences) <= 1:
        return _translate_one(text, src, tgt_id)
    return " ".join(_translate_one(s, src, tgt_id) for s in sentences)


# ══════════════════════════════════════════════════════════════════
# TTS — Piper
# ══════════════════════════════════════════════════════════════════
def _tts_piper_sync(text: str, lang: str) -> bytes:
    """Gibt SLIN16 @ SR_AS zurück."""
    pv = _piper_voices.get(lang) or _piper_voices.get("de")
    buf = io.BytesIO()
    wf  = wave.open(buf, "wb")
    pv.synthesize_wav(text, wf)
    wf.close()
    sr_piper = pv.config.sample_rate
    buf.seek(44)
    pcm_raw = buf.read()
    audio_f = np.frombuffer(pcm_raw, dtype=np.int16).astype(np.float32) / 32768.0
    audio_f = _resample(audio_f, sr_piper, SR_AS)
    return (audio_f * 32767).clip(-32768, 32767).astype(np.int16).tobytes()


# ══════════════════════════════════════════════════════════════════
# NLU-Prompt-Generierung (Asterisk custom sounds)
# ══════════════════════════════════════════════════════════════════
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
    os.makedirs(SOUNDS_CUSTOM, exist_ok=True)
    for lang, text in NLU_PROMPT_TEXTS.items():
        if lang not in _piper_voices:
            continue
        out = os.path.join(SOUNDS_CUSTOM, f"nlu_prompt_{lang}.wav")
        try:
            pcm      = _tts_piper_sync(text, lang)
            audio_f  = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
            audio_8k = _resample(audio_f, SR_AS, 8000)
            sf.write(out, audio_8k, 8000)
            log.info(f"[NLU-Prompt] {lang} → {out}")
        except Exception as exc:
            log.warning(f"[NLU-Prompt] {lang} Fehler: {exc}")


# ══════════════════════════════════════════════════════════════════
# NLU — Rufnummer + Sprache aus Transkription extrahieren
# ══════════════════════════════════════════════════════════════════
_LANG_KW: list[tuple[str, str | None]] = [
    ("kazakh",        "77"),  ("kasachisch",    "77"),  ("kazakhstan",  "77"),
    ("georgian",      "995"), ("georgisch",     "995"), ("georgien",   "995"),
    ("persian",       "98"),  ("farsi",         "98"),  ("iran",       "98"),
    ("hindi",         "91"),  ("indien",        "91"),  ("india",      "91"),
    ("chinese",       "86"),  ("chinesisch",    "86"),  ("china",      "86"),
    ("turkish",       "90"),  ("türkisch",      "90"),  ("türkei",     "90"),  ("turkey",       "90"),
    ("polish",        "48"),  ("polnisch",      "48"),  ("poland",     "48"),  ("polen",        "48"),
    ("portuguese",    "55"),  ("brasilianisch", "55"),  ("brazil",     "55"),  ("brasil",       "55"),
    ("ukrainian",     "38"),  ("ukrainisch",    "38"),  ("ukraine",    "38"),
    ("greek",         "30"),  ("griechisch",    "30"),  ("greece",     "30"),  ("griechenland", "30"),
    ("spanish",       "34"),  ("spanisch",      "34"),  ("spain",      "34"),  ("spanien",      "34"),
    ("french",        "33"),  ("französisch",   "33"),  ("france",     "33"),  ("frankreich",   "33"),
    ("english",       "44"),  ("englisch",      "44"),  ("england",    "44"),  ("britain",      "44"),
    ("russian",       "7"),   ("russisch",      "7"),   ("russia",     "7"),   ("russland",     "7"),
    ("italian",       "39"),  ("italienisch",   "39"),  ("italiano",   "39"),  ("italien",      "39"),  ("italia", "39"),
    ("american",      "1"),   ("usa",           "1"),
    ("deutsch",       None),  ("german",        None),  ("deutschland",None),  ("germany",      None),
]

_DIGIT_PREFIX_SUFFIX: list[tuple[str, str | None]] = [
    ("00995", "995"), ("00380", "38"),
    ("0039",  "39"),  ("0086",  "86"),  ("0090",  "90"),
    ("0091",  "91"),  ("0098",  "98"),  ("0049",  None),
    ("0044",  "44"),  ("0033",  "33"),  ("0034",  "34"),
    ("0048",  "48"),  ("0055",  "55"),  ("0030",  "30"),
    ("0077",  "77"),  ("007",   "7"),   ("001",   "1"),
    ("995",  "995"),  ("380",  "38"),
    ("39",   "39"),   ("86",   "86"),   ("90",   "90"),
    ("91",   "91"),   ("98",   "98"),   ("49",   None),
    ("44",   "44"),   ("33",   "33"),   ("34",   "34"),
    ("48",   "48"),   ("55",   "55"),   ("30",   "30"),
    ("77",   "77"),   ("7",    "7"),    ("1",    "1"),
]


def _build_german_number_words() -> dict[str, str]:
    ones  = ["", "ein", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun"]
    teens = {
        "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
        "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
    }
    tens = {
        20: "zwanzig", 30: "dreißig", 40: "vierzig", 50: "fünfzig",
        60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig",
    }
    words_1_99: dict[str, int] = dict(teens)
    for tv, tw in tens.items():
        words_1_99[tw] = tv
        for ov in range(1, 10):
            words_1_99[f"{ones[ov]}und{tw}"] = tv + ov
    for ov in range(1, 10):
        words_1_99[ones[ov]] = ov
    out: dict[str, str] = {w: str(v) for w, v in words_1_99.items()}
    for h in range(1, 10):
        h_words = [ones[h] + "hundert"] if h > 1 else ["hundert", "einhundert"]
        for hw in h_words:
            out[hw] = str(h * 100)
            for rem_word, rem_val in words_1_99.items():
                out[hw + rem_word] = str(h * 100 + rem_val)
    for ov in range(1, 10):
        out.pop(ones[ov], None)
    return out


_WORD_DIGIT: dict[str, str] = {
    "null": "0", "eins": "1", "ein": "1", "zwei": "2", "drei": "3",
    "vier": "4", "fünf": "5", "sechs": "6", "sieben": "7", "acht": "8", "neun": "9",
    "zero": "0", "uno": "1", "due": "2", "tre": "3", "quattro": "4",
    "cinque": "5", "sei": "6", "sette": "7", "otto": "8", "nove": "9",
    "one": "1", "two": "2", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9",
    "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9",
    **_build_german_number_words(),
}
_WORD_DIGIT_RE = re.compile(
    r'\b(' + '|'.join(re.escape(w) for w in sorted(_WORD_DIGIT, key=len, reverse=True)) + r')\b'
)
_TENS_WORDS = '|'.join(['zwanzig','dreißig','vierzig','fünfzig','sechzig','siebzig','achtzig','neunzig'])
_ONES_WORDS = 'ein|zwei|drei|vier|fünf|sechs|sieben|acht|neun'
_UND_RE     = re.compile(rf'\b({_ONES_WORDS})\s+und\s+({_TENS_WORDS})\b')


def extract_dial_info(text: str) -> tuple[str, str]:
    text_low   = text.lower()
    normalised = _UND_RE.sub(lambda m: m.group(1) + 'und' + m.group(2), text_low)
    normalised = _WORD_DIGIT_RE.sub(lambda m: _WORD_DIGIT[m.group(0)], normalised)
    normalised = re.sub(r'\bplus\b|\+', '', normalised)
    digits     = "".join(re.findall(r"\d+", normalised))
    if len(digits) < 5:
        return "", ""
    suffix: str | None = "_unset"
    for kw, sfx in _LANG_KW:
        if kw in text_low:
            suffix = sfx
            break
    if suffix == "_unset":
        for prefix, sfx in _DIGIT_PREFIX_SUFFIX:
            if digits.startswith(prefix):
                suffix = sfx
                break
    if suffix is None or suffix == "_unset":
        return "", ""
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif not digits.startswith("+"):
        digits = "+" + digits
    return digits, suffix


# ══════════════════════════════════════════════════════════════════
# HTTP-Server
# ══════════════════════════════════════════════════════════════════
def _ok_json(w: asyncio.StreamWriter, body: bytes) -> None:
    w.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Connection: keep-alive\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _ok_wav(w: asyncio.StreamWriter, body: bytes) -> None:
    w.write(
        b"HTTP/1.1 200 OK\r\nContent-Type: audio/wav\r\n"
        b"Connection: keep-alive\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


def _err(w: asyncio.StreamWriter, code: int, msg: str) -> None:
    body   = msg.encode()
    status = {400: "Bad Request", 500: "Internal Server Error"}.get(code, "Error")
    w.write(
        f"HTTP/1.1 {code} {status}\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
    )


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, dict[str, str], bytes]:
    """Liest HTTP-Request vollständig (inkl. Content-Length Body)."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
        if not chunk:
            break
        raw += chunk
        if len(raw) > 65536:   # Header-Limit 64 KB
            raise ValueError("Header zu groß")

    if b"\r\n\r\n" not in raw:
        return None                       # EOF / Verbindung leer geschlossen (Keep-Alive)
    header_raw, body = raw.split(b"\r\n\r\n", 1)
    lines   = header_raw.decode(errors="replace").split("\r\n")
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    content_length = int(headers.get("content-length", len(body)))
    while len(body) < content_length:
        chunk = await asyncio.wait_for(
            reader.read(min(65536, content_length - len(body))), timeout=30.0
        )
        if not chunk:
            break
        body += chunk

    first_line = lines[0].strip()
    parts      = first_line.split(" ")
    full_path  = parts[1] if len(parts) >= 2 else "/"
    if "?" in full_path:
        path, qs = full_path.split("?", 1)
        qparams  = dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
    else:
        path, qparams = full_path, {}

    return path, qparams, body[:content_length]


async def _handle_stt(body: bytes, lang: str, writer: asyncio.StreamWriter) -> None:
    if not body:
        _err(writer, 400, '{"error":"empty body"}')
        return
    t0     = time.monotonic()
    chunks = await _stt(body, lang)
    log.info(f"[/stt] lang={lang}  chunks={len(chunks)}  ({time.monotonic()-t0:.2f}s)")
    _ok_json(writer, json.dumps({"chunks": chunks}).encode())


async def _handle_translate(body: bytes, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    text = payload.get("text", "").strip()
    src  = payload.get("from", "de").strip()
    tgt  = payload.get("to",   "it").strip()
    if not text:
        _err(writer, 400, '{"error":"no text"}')
        return
    t0     = time.monotonic()
    result = await _gpu_server.run(lambda: translate_sync(text, src, tgt))
    log.info(f"[/translate] {src}→{tgt}  ({time.monotonic()-t0:.2f}s)  {result!r}")
    _ok_json(writer, json.dumps({"result": result}).encode())


async def _handle_tts(body: bytes, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    text = payload.get("text", "").strip()
    lang = payload.get("lang", "it").strip()
    if not text:
        _err(writer, 400, '{"error":"no text"}')
        return
    loop    = asyncio.get_running_loop()
    pcm     = await loop.run_in_executor(None, _tts_piper_sync, text, lang)
    buf     = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR_AS)
        wf.writeframes(pcm)
    _ok_wav(writer, buf.getvalue())


async def _handle_nlu(body: bytes, writer: asyncio.StreamWriter) -> None:
    payload = json.loads(body)
    path    = payload.get("path", "").strip()
    lang    = payload.get("lang", "de").strip()

    if not path or not os.path.exists(path):
        log.warning(f"[/nlu] Datei nicht gefunden: {path!r}")
        _err(writer, 400, '{"error":"path not found"}')
        return

    loop = asyncio.get_running_loop()
    try:
        audio = await loop.run_in_executor(None, _read_wav_16k, path)
    except Exception as exc:
        log.warning(f"[/nlu] WAV-Lesen fehlgeschlagen: {exc}")
        _err(writer, 500, '{"error":"wav read"}')
        return

    def _run_nlu():
        # lang-Hint direkt an Whisper durchreichen → spart den Auto-Detect-Vorpass.
        # Fallback auf Auto-Detect nur, wenn kein Hint mitkam.
        segs_gen, info = _whisper.transcribe(
            audio, language=(lang or None), beam_size=3,
            vad_filter=True,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
        )
        return list(segs_gen), info  # consume generator inside executor

    segs_list, info = await _gpu_server.run(_run_nlu)
    detected = info.language
    conf     = info.language_probability
    text     = " ".join(s.text.strip() for s in segs_list).strip()
    number, suffix = extract_dial_info(text)
    log.info(
        f"[/nlu] caller_lang={lang} detected={detected}({conf:.2f})"
        f" text={text!r} → number={number!r} suffix={suffix!r}"
    )
    if text:
        try:
            os.unlink(path)
        except Exception:
            pass
    else:
        log.warning(f"[/nlu] leere Transkription — WAV behalten: {path}")

    _ok_json(writer, json.dumps({"text": text, "number": number, "suffix": suffix}).encode())


async def handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:                       # Keep-Alive: mehrere Requests je Verbindung
            try:
                req = await _read_request(reader)
            except (asyncio.TimeoutError, ConnectionError,
                    asyncio.IncompleteReadError):
                break                     # Idle-Timeout / Verbindung weg
            if req is None:               # EOF
                break
            path, qparams, body = req
            try:
                if path.startswith("/stt"):
                    await _handle_stt(body, qparams.get("lang", "de"), writer)
                elif path.startswith("/translate"):
                    await _handle_translate(body, writer)
                elif path.startswith("/tts"):
                    await _handle_tts(body, writer)
                elif path.startswith("/nlu"):
                    await _handle_nlu(body, writer)
                else:
                    _err(writer, 404, "Not found")
            except Exception as e:
                log.warning(f"[HTTP] Fehler: {e}", exc_info=True)
                try:
                    _err(writer, 500, "ERR")
                except Exception:
                    pass
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════
async def amain() -> None:
    global _gpu_server
    log.info("Inference Server startet …")
    _gpu_server = GpuInferenceServer()
    _gpu_server.start()

    loop = asyncio.get_running_loop()
    log.info("Lade Modelle …")
    await loop.run_in_executor(None, load_models)

    server = await asyncio.start_server(handle_http, INFER_BIND, INFER_PORT)
    log.info(f"Inference Server lauscht auf [{INFER_BIND}]:{INFER_PORT}")
    asyncio.ensure_future(loop.run_in_executor(None, generate_nlu_prompts))
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(amain())
