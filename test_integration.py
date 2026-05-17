#!/home/gh/python/venv_py311/bin/python3
"""
Integration-Test: Direkte Python AudioSocket Verbindung zur laufenden Produktion.

Kein Asterisk, kein TEST_TRUNK, kein VRAM-Duplikat.
Extension "3939" → dial_number="39" (len=2 ≤ 2) → LOOPBACK_ECHO aktiviert.
Worker übersetzt DE→IT, schickt Piper-TTS zurück auf denselben Socket.

Lauf parallel zu Produktion — echte Anrufer nicht beeinträchtigt.
Zieldauer: <1 Minute für alle Testfälle.

Voraussetzungen:
  1. audiosocket_translator.py läuft (Port 9093 AudioSocket, Port 9094 HTTP)
  2. test_data/*.wav vorhanden: ./generate_test_data.py

Starten:
  ./test_integration.py
"""
import asyncio, json, os, struct, sys, time
import uuid as uuid_mod
import http.client
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal as sp

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

AS_HOST  = "127.0.0.1"
AS_PORT  = 9093
REG_HOST = "127.0.0.1"
REG_PORT = 9094

SR_AS    = 8000
FRAME_B  = 320        # 160 samples × 2 bytes  (slin16 8kHz 20ms)
FRAME_S  = 0.020      # Sekunden pro Frame

TEST_DATA      = Path(__file__).parent / "test_data"
LOOPBACK_EXTEN = "3939"   # dial_number="39", len=2 ≤ 2 → loopback DE→IT

TEST_CASES = [
    ("q1_de", "Wie geht es Ihnen heute?"),
    ("q2_de", "Können Sie mir bitte helfen?"),
    ("q3_de", "Wann kommt der nächste Zug?"),
]

AS_UUID_T  = 0x01
AS_AUDIO_T = 0x10
AS_HANGUP  = 0xFF


def _load_wav_8k(path: Path) -> bytes:
    audio, sr = sf.read(str(path), dtype="int16", always_2d=False)
    if sr != SR_AS:
        af = audio.astype(np.float32) / 32768.0
        af = sp.resample(af, max(1, int(len(af) * SR_AS / sr)))
        audio = (af * 32767).clip(-32768, 32767).astype(np.int16)
    return audio.tobytes()


def _http_register(uid: str, exten: str) -> bool:
    body = json.dumps({"uuid": uid, "exten": exten}).encode()
    try:
        conn = http.client.HTTPConnection(REG_HOST, REG_PORT, timeout=5)
        conn.request("POST", "/register", body=body,
                     headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        return conn.getresponse().status == 200
    except Exception as e:
        print(f"  FAIL /register: {e}")
        return False


def _http_nlu(path: str) -> str:
    body = json.dumps({"path": path, "lang": "it"}).encode()
    try:
        conn = http.client.HTTPConnection(REG_HOST, REG_PORT, timeout=30)
        conn.request("POST", "/nlu", body=body,
                     headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        data = json.loads(conn.getresponse().read().decode())
        return data.get("text", "")
    except Exception as e:
        return f"(Fehler: {e})"


def _save_wav(path: str, frames: list[bytes]) -> None:
    if not frames:
        return
    audio = np.frombuffer(b"".join(frames), dtype=np.int16)
    sf.write(path, audio, SR_AS, subtype="PCM_16")


async def _as_read(r: asyncio.StreamReader) -> tuple[int, bytes]:
    hdr     = await r.readexactly(3)
    mtype   = hdr[0]
    length  = struct.unpack(">H", hdr[1:3])[0]
    payload = await r.readexactly(length) if length else b""
    return mtype, payload


async def run_case(num: int, wav_name: str, phrase: str) -> bool:
    print(f"\n── Fall {num}: {wav_name}  {phrase!r}")
    wav_path = TEST_DATA / f"{wav_name}.wav"
    if not wav_path.exists():
        print(f"  FAIL: {wav_path} fehlt")
        return False

    pcm = _load_wav_8k(wav_path)
    uid = str(uuid_mod.uuid4())

    if not _http_register(uid, LOOPBACK_EXTEN):
        return False
    print(f"  Registriert  uuid={uid[:8]} exten={LOOPBACK_EXTEN}")

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(AS_HOST, AS_PORT), timeout=5.0
        )
    except Exception as e:
        print(f"  FAIL Verbindung {AS_HOST}:{AS_PORT}: {e}")
        return False

    writer.write(struct.pack(">BH", AS_UUID_T, 16) + uuid_mod.UUID(uid).bytes)
    await writer.drain()

    t_start   = time.monotonic()
    rx_frames: list[bytes] = []

    async def _send() -> None:
        for i in range(0, len(pcm), FRAME_B):
            chunk = pcm[i : i + FRAME_B].ljust(FRAME_B, b"\x00")
            writer.write(struct.pack(">BH", AS_AUDIO_T, FRAME_B) + chunk)
            await writer.drain()
            await asyncio.sleep(FRAME_S)
        silence = struct.pack(">BH", AS_AUDIO_T, FRAME_B) + b"\x00" * FRAME_B
        for _ in range(20):                # SILENCE_FR=15 → 20 sicher
            writer.write(silence)
            await writer.drain()
            await asyncio.sleep(FRAME_S)

    async def _recv() -> None:
        last_rx: float | None = None
        while True:
            remaining = 35.0 - (time.monotonic() - t_start)
            if remaining <= 0:
                break
            try:
                mtype, payload = await asyncio.wait_for(
                    _as_read(reader), timeout=min(5.0, remaining)
                )
            except asyncio.TimeoutError:
                if last_rx is not None and time.monotonic() - last_rx > 2.5:
                    break
                continue
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
                break
            if mtype == AS_HANGUP:
                break
            if mtype == AS_AUDIO_T and payload:
                rx_frames.append(payload)
                last_rx = time.monotonic()

    send_task = asyncio.create_task(_send())
    recv_task = asyncio.create_task(_recv())

    await asyncio.gather(send_task, return_exceptions=True)
    try:
        await asyncio.wait_for(recv_task, timeout=30.0)
    except asyncio.TimeoutError:
        recv_task.cancel()
        await asyncio.gather(recv_task, return_exceptions=True)

    try:
        writer.write(struct.pack(">BH", AS_HANGUP, 0))
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

    elapsed  = time.monotonic() - t_start
    rx_bytes = sum(len(f) for f in rx_frames)
    rx_sec   = rx_bytes / (SR_AS * 2)
    print(f"  Empfangen: {len(rx_frames)} Frames = {rx_bytes:,} Bytes ≈ {rx_sec:.1f}s  ({elapsed:.1f}s)")

    if not rx_frames:
        print(f"  FAIL — keine Audio-Antwort")
        return False

    rx_path = f"/tmp/test_rx_it_case{num}.wav"
    _save_wav(rx_path, rx_frames)

    loop = asyncio.get_running_loop()
    it_text = await loop.run_in_executor(None, _http_nlu, rx_path)
    print(f"  IT-TTS-Inhalt: {it_text!r}")

    ok = bool(it_text.strip()) and "(Fehler" not in it_text
    print(f"  {'PASS ✓' if ok else 'FAIL ✗ — leere oder fehlerhafte Transkription'}")
    return ok


async def main() -> int:
    print("=" * 60)
    print("Integration-Test: DE→IT DirectAudioSocket")
    print("=" * 60)

    if not TEST_DATA.exists() or not any(TEST_DATA.glob("q*_de.wav")):
        print("FAIL: test_data/*.wav fehlt — ausführen: ./generate_test_data.py")
        return 1

    t0 = time.monotonic()
    passed = failed = 0
    for i, (wav_name, phrase) in enumerate(TEST_CASES, 1):
        ok = await run_case(i, wav_name, phrase)
        if ok:
            passed += 1
        else:
            failed += 1

    elapsed = time.monotonic() - t0
    print(f"\n{'═' * 60}")
    print(f"Ergebnis: {passed} bestanden, {failed} fehlgeschlagen  ({elapsed:.1f}s)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
