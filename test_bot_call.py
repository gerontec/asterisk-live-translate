#!/home/gh/python/venv_py311/bin/python3
"""
Bot-Call-Test: Ruft eine Nummer an, stellt N Fragen auf Deutsch (remote-lang→DE),
nimmt deutsche Antworten auf, übersetzt zurück → answer_it.mp3.

Pipeline (alle Inferenz-Calls direkt → inference_server :9095):
  remote-lang-Fragen → /translate → DE-Text → /tts → DE-WAV → Asterisk Playback
  Asterisk RECORD FILE → WAV → resample 16kHz SLIN16 → /stt → DE-Text
  DE-Text → /translate → remote-lang-Text → /tts → WAV → answer_it.mp3
"""

import argparse, asyncio, http.client, io, json, os, shutil, subprocess
import sys, time, wave
from pathlib import Path

import numpy as np
from scipy import signal as sp
import soundfile as sf

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Inference-Server direkt (kein Proxy-Umweg über :9094) ────────
INFER_HOST = "127.0.0.1"
INFER_PORT = 9095

AGI_HOST      = "127.0.0.1"
AGI_PORT      = 4573
SILENCE_SECS  = 2    # seconds of silence = end of answer
LEAD_IN_SECS  = 0.5  # pause after question before recording starts

AMI_HOST   = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT   = int(os.environ.get("AMI_PORT", 5038))
AMI_USER   = os.environ.get("AMI_USER", "admin")
AMI_PASS   = os.environ.get("AMI_PASS", "")
TRUNK      = os.environ.get("TRUNK_OUT", "Local/%s@outbound-fallback")
CALLER_ID  = os.environ.get("CALLER_ID", "+4980425659959")
VSIP_DOMAIN = os.environ.get("VSIP_DOMAIN", "i.vsip.eu")

SOUNDS_CUSTOM = "/usr/share/asterisk/sounds/custom"
os.makedirs(SOUNDS_CUSTOM, exist_ok=True)

TEST_DIR = Path(__file__).parent / "test"
TEST_DIR.mkdir(exist_ok=True)

SR_INFER = 16000   # inference_server native sample rate (SLIN16)
SR_AST   = 16000   # Asterisk STREAM FILE — slin16 after dialplan audiowriteformat=slin16

DEFAULT_QUESTIONS: dict[str, list[str]] = {
    "it": [
        "Come sta oggi?",
        "Cosa possiamo fare per lei?",
        "Ha altre domande per noi?",
    ],
    "fr": [
        "Comment allez-vous aujourd'hui?",
        "Que pouvons-nous faire pour vous?",
        "Avez-vous d'autres questions pour nous?",
    ],
    "ka": [
        "როგორ ხართ დღეს?",
        "როგორ შეგვიძლია დაგეხმაროთ?",
        "გაქვთ სხვა კითხვები ჩვენთვის?",
    ],
}
DEFAULT_THANKYOU: dict[str, str] = {
    "it": "Grazie per le sue risposte. Arrivederci.",
    "fr": "Merci pour vos réponses. Au revoir.",
    "ka": "გმადლობთ თქვენი პასუხებისთვის. ნახვამდის.",
}


# ── HTTP → inference_server :9095 ────────────────────────────────

def _infer_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection(INFER_HOST, INFER_PORT, timeout=60)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json",
                          "Content-Length": str(len(body))})
    return json.loads(conn.getresponse().read().decode())


def _infer_raw(path: str, body: bytes, content_type: str) -> bytes:
    conn = http.client.HTTPConnection(INFER_HOST, INFER_PORT, timeout=60)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": content_type,
                          "Content-Length": str(len(body))})
    return conn.getresponse().read()


def api_translate(text: str, src: str, tgt: str) -> str:
    r = _infer_json("/translate", {"text": text, "from": src, "to": tgt})
    return r.get("result", "")


def api_tts(text: str, lang: str) -> bytes:
    return _infer_raw("/tts", json.dumps({"text": text, "lang": lang}).encode(),
                      "application/json")


def api_stt(wav_path: str, lang: str = "de") -> str:
    """Read WAV recorded by Asterisk (8 kHz), resample to 16 kHz SLIN16,
    POST raw PCM directly to inference_server /stt — no file-path proxy."""
    with wave.open(wav_path, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        sr  = wf.getframerate()
    if sr != SR_INFER:
        n   = int(len(pcm) * SR_INFER / sr)
        pcm = sp.resample(pcm, n).astype(np.int16)
    raw = pcm.tobytes()
    r   = _infer_raw(f"/stt?lang={lang}", raw, "application/octet-stream")
    chunks = json.loads(r).get("chunks", [])
    return " ".join(chunks).strip()


# ── Asterisk sound deployment ─────────────────────────────────────

def deploy_wav(wav_bytes: bytes, name: str) -> None:
    """Save 16 kHz WAV from TTS, downsample to 8 kHz for Asterisk STREAM FILE."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        src_rate = wf.getframerate()
    if src_rate != SR_AST:
        n   = int(len(pcm) * SR_AST / src_rate)
        pcm = sp.resample(pcm, n).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR_AST)
        wf.writeframes(pcm.tobytes())
    ast_wav = TEST_DIR / f"{name}_ast.wav"
    ast_wav.write_bytes(out.getvalue())
    dst = f"{SOUNDS_CUSTOM}/{name}.wav"
    r = subprocess.run(["sudo", "cp", str(ast_wav), dst], capture_output=True)
    if r.returncode != 0:
        shutil.copy(str(ast_wav), dst)


def save_mp3(pcm_parts: list[bytes], out_path: str) -> None:
    combined = b"".join(p for p in pcm_parts if p)
    if not combined:
        raise RuntimeError("no PCM data")
    tmp = str(TEST_DIR / "bot_combined.wav")
    sf.write(tmp, np.frombuffer(combined, dtype=np.int16), SR_INFER, subtype="PCM_16")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", tmp, "-codec:a", "libmp3lame", "-qscale:a", "4", out_path],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.decode()}")


def wav_bytes_to_pcm(wav_bytes: bytes) -> bytes:
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception:
        return b""


# ── FastAGI session ───────────────────────────────────────────────

class BotAGISession:
    def __init__(self, n_questions: int):
        self.n_questions = n_questions
        self.done        = asyncio.Event()
        self.n_recorded  = 0

    async def run(self, reader: asyncio.StreamReader,
                  writer: asyncio.StreamWriter) -> None:
        try:
            await self._session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError, OSError):
            print(f"  connection dropped after {self.n_recorded} answers")
        finally:
            self.done.set()
            try:
                writer.close()
            except Exception:
                pass

    async def _session(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter) -> None:
        async def agi(cmd: str) -> str:
            writer.write((cmd + "\n").encode())
            await writer.drain()
            return (await reader.readline()).decode().strip()

        while (await reader.readline()).decode().strip():   # consume AGI headers
            pass

        await agi("ANSWER")
        await agi("EXEC Wait 1.0")

        for i in range(1, self.n_questions + 1):
            t0 = time.monotonic()
            await agi(f'STREAM FILE custom/bot_frage{i} ""')
            await agi(f"EXEC Wait {LEAD_IN_SECS}")
            t_rec = time.monotonic()
            # blocks until SILENCE_SECS of silence after last word
            await agi(
                f'RECORD FILE {TEST_DIR}/bot_answer{i} wav "" 30000 0 s={SILENCE_SECS}'
            )
            t_done = time.monotonic()
            self.n_recorded += 1
            speech = max(t_done - t_rec - SILENCE_SECS, 0)
            print(f"  ✓ Antwort {i}: Frage {t_rec-t0:.1f}s | "
                  f"Rede ~{speech:.1f}s | Stille-Wait {SILENCE_SECS}s")

        await agi('STREAM FILE custom/bot_danke ""')
        await agi("EXEC Wait 0.5")


# ── AMI Originate ─────────────────────────────────────────────────

async def ami_originate(dest: str) -> bool:
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=5.0
        )
    except Exception as e:
        print(f"  FAIL AMI: {e}")
        return False

    async def _line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    await _line()
    w.write(f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_PASS}\r\n\r\n".encode())
    await w.drain()
    while "Authentication accepted" not in await _line():
        pass

    action_id = f"bot_{int(time.time())}"
    w.write((
        f"Action: Originate\r\n"
        f"Channel: {TRUNK % dest}\r\n"
        f"Context: bot-interview\r\nExten: s\r\nPriority: 1\r\n"
        f"Timeout: 30000\r\n"
        f"CallerID: IT-Bot <{CALLER_ID}>\r\n"
        f"Variable: PJSIP_HEADER(add,P-Asserted-Identity)=<sip:{CALLER_ID}@{VSIP_DOMAIN}>\r\n"
        f"Variable: PJSIP_HEADER(add,P-Preferred-Identity)=<sip:{CALLER_ID}@{VSIP_DOMAIN}>\r\n"
        f"Async: true\r\nActionID: {action_id}\r\n\r\n"
    ).encode())
    await w.drain()

    success = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = await _line()
        if "Response: Success" in line:
            success = True
        if "Response: Error" in line:
            print("  FAIL Originate abgelehnt")
            break
        if line == "" and success:
            break

    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    return success


# ── Main ──────────────────────────────────────────────────────────

async def main(dest: str, remote_questions: list[str], remote_lang: str) -> int:
    loop     = asyncio.get_running_loop()
    n_q      = len(remote_questions)
    answers  = [str(TEST_DIR / f"bot_answer{i}.wav") for i in range(1, n_q + 1)]
    out_path = Path(__file__).parent / "answer_it.mp3"
    rl       = remote_lang

    print("=" * 60)
    print(f"Bot-Call-Test → {dest}  (Trunk: {TRUNK % dest})")
    print(f"CallerID: {CALLER_ID}  |  Inference: {INFER_HOST}:{INFER_PORT}")
    print("=" * 60)

    # 1. remote-lang → DE TTS
    print(f"\nSchritt 1: {rl.upper()}-Fragen → DE-TTS ({n_q} Fragen)")
    for i, q in enumerate(remote_questions, 1):
        de = await loop.run_in_executor(None, api_translate, q, rl, "de")
        print(f"  [{i}] {q!r} → {de!r}")
        wav = await loop.run_in_executor(None, api_tts, de, "de")
        await loop.run_in_executor(None, deploy_wav, wav, f"bot_frage{i}")

    thankyou_src = DEFAULT_THANKYOU.get(rl, DEFAULT_THANKYOU["it"])
    danke_de  = await loop.run_in_executor(None, api_translate, thankyou_src, rl, "de")
    danke_wav = await loop.run_in_executor(None, api_tts, danke_de, "de")
    await loop.run_in_executor(None, deploy_wav, danke_wav, "bot_danke")

    # 2. delete old recordings
    for p in answers:
        try:
            os.unlink(p)
        except OSError:
            pass

    # 3. call + FastAGI
    session = BotAGISession(n_q)
    server  = await asyncio.start_server(session.run, AGI_HOST, AGI_PORT)

    print(f"\nSchritt 2: Anruf → {dest}")
    async with server:
        if not await ami_originate(dest):
            print("  FAIL: Originate fehlgeschlagen")
            return 1
        print("  OK: Anruf läuft — warte auf Antworten …")
        try:
            await asyncio.wait_for(session.done.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("  Timeout nach 5 Minuten")
            if session.n_recorded == 0:
                return 1

    if session.n_recorded == 0:
        print("  FAIL: keine Aufnahmen")
        return 1

    # 4. STT via /stt (raw PCM → inference_server :9095) + translate + TTS
    t0 = time.monotonic()
    print(f"\nSchritt 3: STT (/stt :9095) → DE-Text → {rl.upper()}-TTS")
    pcm_parts: list[bytes] = []
    transcript: list[str]  = []

    for i, path in enumerate(answers, 1):
        if not os.path.exists(path) or os.path.getsize(path) < 200:
            print(f"  [{i}] nicht aufgenommen")
            transcript.append(f"Antwort {i}: nicht aufgenommen")
            continue

        t_stt0 = time.monotonic()
        de_text = await loop.run_in_executor(None, api_stt, path, "de")
        t_stt1 = time.monotonic()

        if not de_text.strip():
            print(f"  [{i}] DE: (leer)  [{t_stt1-t_stt0:.1f}s STT]")
            transcript.append(f"Antwort {i} [DE]: (leer)")
            continue

        t_tr0   = time.monotonic()
        rl_text = await loop.run_in_executor(None, api_translate, de_text, "de", rl)
        t_tr1   = time.monotonic()
        t_tts0  = time.monotonic()
        wav     = await loop.run_in_executor(None, api_tts, rl_text, rl)
        t_tts1  = time.monotonic()

        print(f"  [{i}] DE: {de_text}")
        print(f"       {rl.upper()}: {rl_text}  "
              f"[{t_stt1-t_stt0:.1f}s STT  {t_tr1-t_tr0:.1f}s TRL  {t_tts1-t_tts0:.1f}s TTS]")

        transcript.append(f"Antwort {i} [DE]: {de_text}")
        transcript.append(f"Antwort {i} [{rl.upper()}]: {rl_text}")

        pcm = wav_bytes_to_pcm(wav)
        if pcm:
            pcm_parts.append(pcm)

    if not pcm_parts:
        print("\nFAIL: keine verarbeitbaren Antworten")
        return 1

    # 5. Transkript + MP3
    transcript_path = Path("/var/www/web1/answer_transcript.txt")
    transcript_path.write_text("\n".join(transcript) + "\n", encoding="utf-8")
    print("\n" + "=" * 60 + "\nTranskript:")
    for line in transcript:
        print(f"  {line}")
    print("=" * 60)

    await loop.run_in_executor(None, save_mp3, pcm_parts, str(out_path))
    size_kb = out_path.stat().st_size // 1024
    shutil.copy2(out_path, "/var/www/web1/answer_it.mp3")
    print(f"\n→ Gespeichert: {out_path}  ({size_kb} KB)  [{time.monotonic()-t0:.1f}s]")
    print(f"→ Web:         /var/www/web1/answer_it.mp3")
    print(f"→ Transkript:  {transcript_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bot-Call-Test")
    ap.add_argument("--dest",      default="+4917625257878")
    ap.add_argument("--trunk",     default="")
    ap.add_argument("--callerid",  default="")
    ap.add_argument("--questions", default="",
                    help="semicolon-separated questions in --lang language")
    ap.add_argument("--lang",      default="it",
                    help="remote party language (default: it)")
    args = ap.parse_args()

    if args.trunk:
        TRUNK = f"PJSIP/%s@{args.trunk}"
    if args.callerid:
        CALLER_ID = args.callerid

    rl = args.lang
    if args.questions:
        questions = [q.strip() for q in args.questions.split(";") if q.strip()]
    else:
        questions = DEFAULT_QUESTIONS.get(rl, DEFAULT_QUESTIONS["it"])

    sys.exit(asyncio.run(main(args.dest, questions, rl)))
