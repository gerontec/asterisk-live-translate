#!/home/gh/python/venv_py311/bin/python3
"""
Bot-Call-Test: Ruft eine Nummer an, stellt 3 Fragen auf Deutsch (IT→DE übersetzt),
nimmt deutsche Antworten auf, übersetzt zurück auf Italienisch → answer_it.mp3.
"""

import argparse, asyncio, http.client, io, json, os, shutil, subprocess
import sys, time, wave
from pathlib import Path

import numpy as np
import soundfile as sf

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

REG_HOST = "127.0.0.1"
REG_PORT = 9094

AGI_HOST     = "127.0.0.1"
AGI_PORT     = 4573
SILENCE_SECS  = 2    # Sekunden Stille nach letztem Wort = Antwortende
LEAD_IN_SECS  = 0.5  # Pause nach Frage bevor Aufnahme startet (Anlaufzeit)

AMI_HOST   = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT   = int(os.environ.get("AMI_PORT", 5038))
AMI_USER   = os.environ.get("AMI_USER", "admin")
AMI_PASS   = os.environ.get("AMI_PASS", "")
TRUNK      = os.environ.get("TRUNK_OUT", "PJSIP/%s@vsip-trunk")
CALLER_ID  = os.environ.get("CALLER_ID", "+4980425659959")
VSIP_DOMAIN = os.environ.get("VSIP_DOMAIN", "i.vsip.eu")

SOUNDS_CUSTOM = "/usr/share/asterisk/sounds/custom"
if not os.path.isdir(SOUNDS_CUSTOM):
    os.makedirs(SOUNDS_CUSTOM, exist_ok=True)

ANSWERS     = [f"/tmp/bot_answer{i}.wav" for i in range(1, 4)]
N_QUESTIONS = 3

REMOTE_LANG = "it"   # Sprache des Angerufenen — per --lang überschreibbar

DEFAULT_QUESTIONS: dict[str, list[str]] = {
    "it": [
        "Come sta oggi?",
        "Cosa possiamo fare per lei?",
        "Ha altre domande per noi?",
    ],
    "ka": [
        "როგორ ხართ დღეს?",
        "როგორ შეგვიძლია დაგეხმაროთ?",
        "გაქვთ სხვა კითხვები ჩვენთვის?",
    ],
}
DEFAULT_THANKYOU: dict[str, str] = {
    "it": "Grazie per le sue risposte. Arrivederci.",
    "ka": "გმადლობთ თქვენი პასუხებისთვის. ნახვამდის.",
}
SR_AS = 8000


# ── HTTP-Hilfsfunktionen ──────────────────────────────────────────

def _http_post_json(path: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection(REG_HOST, REG_PORT, timeout=30)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json",
                           "Content-Length": str(len(body))})
    resp = conn.getresponse()
    return json.loads(resp.read().decode())


def _http_post_json_raw(path: str, payload: dict) -> bytes:
    body = json.dumps(payload).encode()
    conn = http.client.HTTPConnection(REG_HOST, REG_PORT, timeout=30)
    conn.request("POST", path, body=body,
                 headers={"Content-Type": "application/json",
                           "Content-Length": str(len(body))})
    return conn.getresponse().read()


def api_translate(text: str, src: str, tgt: str) -> str:
    r = _http_post_json("/translate", {"text": text, "from": src, "to": tgt})
    return r.get("result", "")


def api_tts(text: str, lang: str) -> bytes:
    return _http_post_json_raw("/tts", {"text": text, "lang": lang})


def api_nlu(wav_path: str, lang: str = "de") -> str:
    # FIX: Sprache explizit erzwingen damit Whisper nicht auf FR/IT wechselt
    r = _http_post_json("/nlu", {"path": wav_path, "lang": lang, "language": lang})
    return r.get("text", "")


def wav_bytes_to_pcm(wav_bytes: bytes) -> bytes:
    try:
        buf = io.BytesIO(wav_bytes)
        with wave.open(buf, "rb") as wf:
            return wf.readframes(wf.getnframes())
    except Exception as e:
        print(f"  WARN: WAV-Parsing fehlgeschlagen: {e}")
        return b""


def deploy_wav(wav_bytes: bytes, name: str) -> None:
    tmp = f"/tmp/{name}.wav"
    with open(tmp, "wb") as f:
        f.write(wav_bytes)
    dst = f"{SOUNDS_CUSTOM}/{name}.wav"
    r = subprocess.run(["sudo", "cp", tmp, dst], capture_output=True)
    if r.returncode != 0:
        try:
            shutil.copy(tmp, dst)
        except Exception as e:
            print(f"  WARN: deploy_wav fehlgeschlagen für {name}: {e}")


def save_mp3(pcm_parts: list[bytes], out_path: str) -> None:
    combined = b"".join(p for p in pcm_parts if p)
    if not combined:
        raise RuntimeError("Keine PCM-Daten zum Speichern")
    tmp_wav = "/tmp/bot_it_combined.wav"
    audio   = np.frombuffer(combined, dtype=np.int16)
    sf.write(tmp_wav, audio, SR_AS, subtype="PCM_16")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", tmp_wav, "-codec:a", "libmp3lame", "-qscale:a", "4", out_path],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {r.stderr.decode()}")


# ── FastAGI Session ───────────────────────────────────────────────

class BotAGISession:
    """
    Steuert einen Anruf via FastAGI.
    Pro Frage: Frage abspielen → aufnehmen bis Stille → nächste Frage.
    Wenn AGI-RECORD FILE zurückkehrt ist die WAV-Datei vollständig geschrieben.
    """

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
            print(f"  Verbindung getrennt nach {self.n_recorded} Antworten")
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

        # AGI-Header konsumieren (leer Zeile = Ende)
        while (await reader.readline()).decode().strip():
            pass

        await agi("ANSWER")
        await agi("EXEC Wait 1.0")

        for i in range(1, self.n_questions + 1):
            t_frage = time.monotonic()
            await agi(f'STREAM FILE custom/bot_frage{i} ""')
            await agi(f"EXEC Wait {LEAD_IN_SECS}")
            t_rec = time.monotonic()
            # Blockiert bis SILENCE_SECS Stille nach letztem Wort — WAV dann fertig
            await agi(
                f'RECORD FILE /tmp/bot_answer{i} wav "" 30000 0 s={SILENCE_SECS}'
            )
            t_done = time.monotonic()
            self.n_recorded += 1
            speech = t_done - t_rec - SILENCE_SECS
            print(f"  ✓ Antwort {i}: Frage {t_rec-t_frage:.1f}s | "
                  f"Rede ~{max(speech,0):.1f}s | Stille-Wait {SILENCE_SECS}s")

        await agi('STREAM FILE custom/bot_danke ""')
        await agi("EXEC Wait 0.5")


# ── AMI Originate ─────────────────────────────────────────────────

async def ami_originate(dest: str) -> bool:
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=5.0
        )
    except Exception as e:
        print(f"  FAIL AMI Verbindung: {e}")
        return False

    async def _line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    await _line()  # Banner
    w.write(
        f"Action: Login\r\nUsername: {AMI_USER}\r\n"
        f"Secret: {AMI_PASS}\r\n\r\n".encode()
    )
    await w.drain()
    while "Authentication accepted" not in await _line():
        pass

    action_id = f"bot_call_{int(time.time())}"
    w.write((
        f"Action: Originate\r\n"
        f"Channel: {TRUNK % dest}\r\n"
        f"Context: bot-interview\r\n"
        f"Exten: s\r\n"
        f"Priority: 1\r\n"
        f"Timeout: 30000\r\n"
        # FIX: Rufnummer korrekt durchreichen
        f"CallerID: IT-Bot <{CALLER_ID}>\r\n"
        f"Variable: CALLERID(num)={CALLER_ID}\r\n"
        f"Variable: CALLERID(name)=IT-Bot\r\n"
        f"Variable: PJSIP_HEADER(add,P-Asserted-Identity)="
        f"<sip:{CALLER_ID}@{VSIP_DOMAIN}>\r\n"
        f"Variable: PJSIP_HEADER(add,P-Preferred-Identity)="
        f"<sip:{CALLER_ID}@{VSIP_DOMAIN}>\r\n"
        f"Async: true\r\n"
        f"ActionID: {action_id}\r\n"
        f"\r\n"
    ).encode())
    await w.drain()

    # Warte auf OriginateResponse
    success = False
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        line = await _line()
        if "Response: Success" in line:
            success = True
        if "Response: Error" in line:
            print(f"  FAIL Originate abgelehnt")
            break
        if line == "" and success:
            break

    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    return success


# ── Haupt-Ablauf ──────────────────────────────────────────────────

async def main(dest: str, it_questions: list[str], remote_lang: str = "it") -> int:
    loop = asyncio.get_running_loop()
    out_path = Path(__file__).parent / "answer_it.mp3"

    print("=" * 60)
    print(f"Bot-Call-Test → {dest}  (Trunk: {TRUNK % dest})")
    print(f"CallerID:       {CALLER_ID}")
    print("=" * 60)

    # 1. Fragen übersetzen und TTS-WAVs generieren
    rl = remote_lang
    print(f"\nSchritt 1: {rl.upper()}-Fragen → DE-TTS-WAVs")
    for i, it_q in enumerate(it_questions, 1):
        de_text = await loop.run_in_executor(None, api_translate, it_q, rl, "de")
        print(f"  Frage {i}: {it_q!r} → {de_text!r}")
        wav_bytes = await loop.run_in_executor(None, api_tts, de_text, "de")
        deploy_wav(wav_bytes, f"bot_frage{i}")

    thankyou_src = DEFAULT_THANKYOU.get(rl, DEFAULT_THANKYOU["it"])
    danke_text = await loop.run_in_executor(
        None, api_translate, thankyou_src, rl, "de"
    )
    danke_wav = await loop.run_in_executor(None, api_tts, danke_text, "de")
    deploy_wav(danke_wav, "bot_danke")
    print(f"  Danke-Text: {danke_text!r}")

    # 2. Alte Antwort-WAVs löschen
    for path in ANSWERS:
        try:
            os.unlink(path)
        except OSError:
            pass

    # 3. FastAGI-Server starten (wartet auf Asterisk-Verbindung)
    session = BotAGISession(N_QUESTIONS)
    server  = await asyncio.start_server(session.run, AGI_HOST, AGI_PORT)

    print(f"\nSchritt 2: Anruf → {dest}")
    async with server:
        ok = await ami_originate(dest)
        if not ok:
            print("  FAIL: Originate fehlgeschlagen")
            return 1
        print("  OK: Anruf gestartet — AGI steuert Frage/Antwort-Fluss")
        try:
            await asyncio.wait_for(session.done.wait(), timeout=300)
        except asyncio.TimeoutError:
            print("  Timeout nach 5 Minuten")
            if session.n_recorded == 0:
                return 1

    if session.n_recorded == 0:
        print("  FAIL: Keine Aufnahmen (Anruf nicht angenommen?)")
        return 1
    if session.n_recorded < N_QUESTIONS:
        print(f"  Hinweis: nur {session.n_recorded}/{N_QUESTIONS} Antworten")

    # 5. Antworten transkribieren und übersetzen
    t_poststart = time.monotonic()
    print(f"\nSchritt 3: DE-Antworten → {rl.upper()}-TTS")
    it_pcm_parts: list[bytes] = []
    transcript_lines: list[str] = []
    for i, path in enumerate(ANSWERS, 1):
        if not os.path.exists(path) or os.path.getsize(path) < 200:
            print(f"  Antwort {i}: nicht aufgenommen")
            transcript_lines.append(f"Antwort {i}: nicht aufgenommen")
            continue

        t_nlu0 = time.monotonic()
        de_text = await loop.run_in_executor(None, api_nlu, path, "de")
        t_nlu1 = time.monotonic()

        if not de_text.strip():
            print(f"  [{i}] DE: (leer)  [{t_nlu1-t_nlu0:.1f}s NLU]")
            transcript_lines.append(f"Antwort {i} [DE]: (leer)")
            continue

        t_tr0 = time.monotonic()
        it_text = await loop.run_in_executor(None, api_translate, de_text, "de", rl)
        t_tr1 = time.monotonic()
        t_tts0 = time.monotonic()
        wav_bytes = await loop.run_in_executor(None, api_tts, it_text, rl)
        t_tts1 = time.monotonic()

        print(f"  [{i}] DE: {de_text}")
        print(f"       {rl.upper()}: {it_text}  [{t_nlu1-t_nlu0:.1f}s NLU  {t_tr1-t_tr0:.1f}s TRL  {t_tts1-t_tts0:.1f}s TTS]")

        transcript_lines.append(f"Antwort {i} [DE]: {de_text}")
        transcript_lines.append(f"Antwort {i} [{rl.upper()}]: {it_text}")

        pcm = wav_bytes_to_pcm(wav_bytes)
        if pcm:
            it_pcm_parts.append(pcm)

    if not it_pcm_parts:
        print("\nFAIL: keine verarbeitbaren Antworten")
        return 1

    # 6. Transkript zuerst anzeigen und speichern
    transcript_path = Path("/var/www/web1/answer_transcript.txt")
    transcript_path.write_text("\n".join(transcript_lines) + "\n", encoding="utf-8")
    print("\n" + "=" * 60)
    print("Transkript:")
    for line in transcript_lines:
        print(f"  {line}")
    print("=" * 60)

    # 7. Kombinieren und als MP3 speichern
    await loop.run_in_executor(None, save_mp3, it_pcm_parts, str(out_path))
    size_kb = out_path.stat().st_size // 1024
    t_post = time.monotonic() - t_poststart
    print(f"\n→ Gespeichert: {out_path}  ({size_kb} KB)  [Post-Processing: {t_post:.1f}s]")
    web_path = Path("/var/www/web1/answer_it.mp3")
    shutil.copy2(out_path, web_path)
    print(f"→ Web:         {web_path}")
    print(f"→ Transkript:  {transcript_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bot-Call-Test")
    ap.add_argument("--dest",      default="+4917625257878", help="Zielrufnummer")
    ap.add_argument("--trunk",     default="",
                    help="Trunk überschreiben z.B. fritzbox-out")
    ap.add_argument("--callerid",  default="",
                    help="CallerID überschreiben z.B. +4980425641873")
    ap.add_argument("--questions", default="",
                    help="Semikolon-getrennte Fragen in --lang Sprache")
    ap.add_argument("--lang", default="it",
                    help="Sprache des Angerufenen z.B. it ka fr ru (default: it)")
    args = ap.parse_args()

    if args.trunk:
        TRUNK = f"PJSIP/%s@{args.trunk}"
    if args.callerid:
        CALLER_ID = args.callerid

    lang = args.lang.lower()
    questions = (
        [q.strip() for q in args.questions.split(";") if q.strip()]
        if args.questions else DEFAULT_QUESTIONS.get(lang, DEFAULT_QUESTIONS["it"])
    )

    sys.exit(asyncio.run(main(args.dest, questions, remote_lang=lang)))
