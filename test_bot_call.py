#!/home/gh/python/venv_py311/bin/python3
"""
Bot-Call-Test: Ruft eine Nummer an, stellt 3 Fragen auf Deutsch (IT→DE übersetzt),
nimmt deutsche Antworten auf, übersetzt zurück auf Italienisch → answer_it.mp3.

Pipeline:
  IT-Fragen → /translate (IT→DE) → /tts DE-WAV → Asterisk Playback
  Asterisk Record → DE-Antwort-WAV → /nlu (Whisper STT) → DE-Text
  DE-Text → /translate (DE→IT) → IT-Text → /tts IT-WAV → answer_it.mp3

Voraussetzungen:
  1. audiosocket_translator.py läuft (Port 9094 HTTP)
  2. Asterisk: extensions_bot.conf geladen (dialplan reload)
  3. .env: AMI_USER, AMI_PASS

Starten:
  ./test_bot_call.py [--dest +4917625257878] [--questions "Frage1;Frage2;Frage3"]
"""

import argparse, asyncio, http.client, io, json, os, shutil, struct, subprocess
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

AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", 5038))
AMI_USER = os.environ.get("AMI_USER", "admin")
AMI_PASS = os.environ.get("AMI_PASS", "")
TRUNK    = os.environ.get("TRUNK_OUT", "PJSIP/%s@fritzbox-out")

SOUNDS_CUSTOM = "/usr/share/asterisk/sounds/custom"
ANSWERS       = [f"/tmp/bot_answer{i}.wav" for i in range(1, 4)]
N_QUESTIONS   = 3

DEFAULT_QUESTIONS = [
    "Come sta oggi?",
    "Cosa possiamo fare per lei?",
    "Ha altre domande per noi?",
]
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
    """Gibt rohe Response-Bytes zurück (für /tts → WAV-Binary)."""
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


def api_nlu(wav_path: str) -> str:
    r = _http_post_json("/nlu", {"path": wav_path, "lang": "de"})
    return r.get("text", "")


def wav_bytes_to_pcm(wav_bytes: bytes) -> bytes:
    """Extrahiert PCM-Daten aus WAV-Bytes (überspringt Header)."""
    buf = io.BytesIO(wav_bytes)
    with wave.open(buf, "rb") as wf:
        return wf.readframes(wf.getnframes())


def deploy_wav(wav_bytes: bytes, name: str) -> None:
    """Speichert WAV in /tmp und kopiert mit sudo in SOUNDS_CUSTOM."""
    tmp = f"/tmp/{name}.wav"
    with open(tmp, "wb") as f:
        f.write(wav_bytes)
    dst = f"{SOUNDS_CUSTOM}/{name}.wav"
    r = subprocess.run(["sudo", "cp", tmp, dst], capture_output=True)
    if r.returncode != 0:
        # Fallback: direkter Kopierversuch ohne sudo
        shutil.copy(tmp, dst)


def save_mp3(pcm_parts: list[bytes], out_path: str) -> None:
    """Verbindet PCM-Teile, schreibt temporäres WAV, konvertiert zu MP3 via ffmpeg."""
    combined = b"".join(pcm_parts)
    tmp_wav  = "/tmp/bot_it_combined.wav"
    audio    = np.frombuffer(combined, dtype=np.int16)
    sf.write(tmp_wav, audio, SR_AS, subtype="PCM_16")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", tmp_wav, "-codec:a", "libmp3lame", "-qscale:a", "4", out_path],
        capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg fehlgeschlagen: {r.stderr.decode()}")


# ── AMI Originate ─────────────────────────────────────────────────

async def ami_originate(dest: str) -> bool:
    """Startet Anruf zu dest via Asterisk AMI. Gibt True zurück wenn erfolgreich."""
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

    action_id = "bot_call_001"
    w.write((
        f"Action: Originate\r\n"
        f"Channel: {TRUNK % dest}\r\n"
        f"Context: bot-interview\r\n"
        f"Exten: s\r\n"
        f"Priority: 1\r\n"
        f"Timeout: 30000\r\n"
        f"CallerID: IT-Bot <+4980425641873>\r\n"
        f"Async: true\r\n"
        f"ActionID: {action_id}\r\n"
        f"\r\n"
    ).encode())
    await w.drain()
    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()
    return True


# ── Haupt-Ablauf ──────────────────────────────────────────────────

async def main(dest: str, it_questions: list[str]) -> int:
    loop = asyncio.get_running_loop()
    out_path = Path(__file__).parent / "answer_it.mp3"

    print("=" * 60)
    print(f"Bot-Call-Test → {dest}")
    print("=" * 60)

    # 1. Fragen übersetzen und TTS-WAVs generieren
    print("\nSchritt 1: IT-Fragen → DE-TTS-WAVs")
    de_texts: list[str] = []
    for i, it_q in enumerate(it_questions, 1):
        de_text = await loop.run_in_executor(None, api_translate, it_q, "it", "de")
        de_texts.append(de_text)
        print(f"  Frage {i}: {it_q!r} → {de_text!r}")
        wav_bytes = await loop.run_in_executor(None, api_tts, de_text, "de")
        deploy_wav(wav_bytes, f"bot_frage{i}")

    danke_text = await loop.run_in_executor(
        None, api_translate,
        "Grazie per le sue risposte. Arrivederci.", "it", "de"
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

    # 3. Anruf starten
    print(f"\nSchritt 2: Anruf → {dest}")
    ok = await ami_originate(dest)
    if not ok:
        return 1

    # 4. Auf Antwort-WAVs warten (Dialplan läuft durch)
    print(f"  Warte auf Aufnahmen ({', '.join(ANSWERS)}) …")
    t0 = time.monotonic()
    while time.monotonic() - t0 < 120:
        done = sum(1 for p in ANSWERS if os.path.exists(p) and os.path.getsize(p) > 200)
        elapsed = time.monotonic() - t0
        if done == N_QUESTIONS:
            await asyncio.sleep(1.5)  # Asterisk flush
            break
        if elapsed > 5:
            print(f"  … {done}/{N_QUESTIONS} Aufnahmen ({elapsed:.0f}s)", end="\r")
        await asyncio.sleep(1)
    else:
        done = sum(1 for p in ANSWERS if os.path.exists(p) and os.path.getsize(p) > 200)
        if done == 0:
            print("\n  FAIL: Keine Aufnahmen (Anruf nicht angenommen?)")
            return 1
        print(f"\n  Timeout — nur {done}/{N_QUESTIONS} Aufnahmen vorhanden")

    # 5. Antworten transkribieren und übersetzen
    print("\nSchritt 3: DE-Antworten → IT-TTS")
    it_pcm_parts: list[bytes] = []
    for i, path in enumerate(ANSWERS, 1):
        if not os.path.exists(path) or os.path.getsize(path) < 200:
            print(f"  Antwort {i}: nicht aufgenommen")
            continue

        de_text = await loop.run_in_executor(None, api_nlu, path)
        print(f"  Antwort {i} [DE]: {de_text!r}")

        if not de_text.strip():
            print(f"  Antwort {i}: leere Transkription übersprungen")
            continue

        it_text = await loop.run_in_executor(None, api_translate, de_text, "de", "it")
        print(f"  Antwort {i} [IT]: {it_text!r}")

        wav_bytes = await loop.run_in_executor(None, api_tts, it_text, "it")
        it_pcm_parts.append(wav_bytes_to_pcm(wav_bytes))

    if not it_pcm_parts:
        print("\nFAIL: keine verarbeitbaren Antworten")
        return 1

    # 6. Kombinieren und als MP3 speichern
    await loop.run_in_executor(None, save_mp3, it_pcm_parts, str(out_path))
    size_kb = out_path.stat().st_size // 1024
    print(f"\n→ Gespeichert: {out_path}  ({size_kb} KB)")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Bot-Call-Test")
    ap.add_argument("--dest",      default="+4917625257878", help="Zielrufnummer")
    ap.add_argument("--questions", default="",
                    help="Semikolon-getrennte IT-Fragen (Standard: 3 Demo-Fragen)")
    args = ap.parse_args()

    if args.questions:
        questions = [q.strip() for q in args.questions.split(";") if q.strip()]
    else:
        questions = DEFAULT_QUESTIONS

    sys.exit(asyncio.run(main(args.dest, questions)))
