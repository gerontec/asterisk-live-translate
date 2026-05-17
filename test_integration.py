#!/home/gh/python/venv_py311/bin/python3
"""
Integration-Test: vollständige DE→IT→DE-Pipeline via Asterisk Local-Channels.

Voraussetzungen:
  1. Asterisk läuft, extensions_test.conf eingebunden, Dialplan geladen:
       asterisk -rx "dialplan reload"
  2. audiosocket_translator.py läuft MIT:
       TEST_TRUNK=Local/%s@test-it-phones
  3. AMI-Zugangsdaten in .env (AMI_USER, AMI_PASS)
  4. test_data/ existiert:  ./generate_test_data.py

Starten:
  TEST_TRUNK='Local/%s@test-it-phones' ./test_integration.py
"""

import asyncio, json, os, shutil, sys, time
from pathlib import Path

SOUNDS_CUSTOM    = "/usr/share/asterisk/sounds/custom"
AMI_HOST         = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT         = int(os.environ.get("AMI_PORT", "5038"))
AMI_USER         = os.environ.get("AMI_USER", "admin")
AMI_PASS         = os.environ.get("AMI_PASS", "")
TRANSLATOR_HTTP  = ("127.0.0.1", 9094)

TEST_DATA = Path(__file__).parent / "test_data"
RX_DE     = "/tmp/test_rx_de.wav"   # was DE-Seite empfängt = Ital. TTS
RX_IT     = "/tmp/test_rx_it.wav"   # was IT-Seite empfängt = Dt. TTS

# Testnummer: beliebige +39-Nummer + Suffix 39 → Übersetzer → test-it-phones
TEST_NUMBER = "+391234567839"

# (deutsches WAV, italienisches WAV)  — aus test_data/
TEST_CASES = [
    ("q1_de", "a1_it"),   # "Wie geht es Ihnen heute?"  ↔  "Mi sento molto bene, grazie."
    ("q2_de", "a2_it"),   # "Können Sie mir bitte helfen?" ↔  "Certamente, posso aiutarla."
    ("q3_de", "a3_it"),   # "Wann kommt der nächste Zug?" ↔  "Il prossimo treno arriva …"
    ("q1_de", "a4_it"),   # wiederhole q1              ↔  "Prego, non c'è problema."
]


# ── Hilfsfunktionen ───────────────────────────────────────────

def _copy_wavs(de_name: str, it_name: str) -> None:
    """Setzt die aktuellen Test-WAVs in Asterisks Custom-Sounds-Verzeichnis."""
    os.makedirs(SOUNDS_CUSTOM, exist_ok=True)
    shutil.copy(TEST_DATA / f"{de_name}.wav",
                os.path.join(SOUNDS_CUSTOM, "test_de_input.wav"))
    shutil.copy(TEST_DATA / f"{it_name}.wav",
                os.path.join(SOUNDS_CUSTOM, "test_it_input.wav"))


def _clear_recordings() -> None:
    for f in (RX_DE, RX_IT):
        try:
            os.unlink(f)
        except OSError:
            pass


async def _ami_originate() -> None:
    """AMI Originate: Local/s@test-de-inject → translator-in/{TEST_NUMBER}."""
    r, w = await asyncio.wait_for(
        asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=5.0
    )

    async def line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    await line()  # Asterisk-Greeting
    w.write(
        f"Action: Login\r\nUsername: {AMI_USER}\r\n"
        f"Secret: {AMI_PASS}\r\n\r\n".encode()
    )
    await w.drain()
    while "Authentication accepted" not in await line():
        pass

    w.write((
        f"Action: Originate\r\n"
        f"Channel: Local/s@test-de-inject\r\n"
        f"Context: translator-in\r\n"
        f"Exten: {TEST_NUMBER}\r\n"
        f"Priority: 1\r\n"
        f"Timeout: 90000\r\n"
        f"Async: true\r\n"
        f"\r\n"
    ).encode())
    await w.drain()
    w.write(b"Action: Logoff\r\n\r\n")
    await w.drain()
    w.close()


async def _wait_file(path: str, min_bytes: int = 4096, timeout: int = 90) -> bool:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(path) and os.path.getsize(path) >= min_bytes:
            return True
        await asyncio.sleep(1)
    return False


def _nlu_transcribe(path: str) -> str:
    """Sendet WAV an Übersetzers /nlu-Endpunkt; gibt Transkriptionstext zurück."""
    import http.client
    body = json.dumps({"path": path, "lang": "de"}).encode()
    try:
        conn = http.client.HTTPConnection(*TRANSLATOR_HTTP, timeout=30)
        conn.request("POST", "/nlu", body=body,
                     headers={"Content-Type": "application/json",
                               "Content-Length": str(len(body))})
        data = json.loads(conn.getresponse().read().decode())
        return data.get("text", "")
    except Exception as exc:
        return f"(Fehler: {exc})"


# ── Test-Ablauf ───────────────────────────────────────────────

async def run_case(num: int, de_name: str, it_name: str) -> bool:
    print(f"\n── Fall {num}: {de_name} × {it_name} {'─' * 30}")
    _clear_recordings()
    _copy_wavs(de_name, it_name)
    print(f"  DE: {de_name}.wav → custom/test_de_input")
    print(f"  IT: {it_name}.wav → custom/test_it_input")

    try:
        await _ami_originate()
        print(f"  AMI Originate → translator-in/{TEST_NUMBER}")
    except Exception as exc:
        print(f"  FAIL AMI: {exc}")
        return False

    print(f"  Warte auf IT-Aufnahme ({RX_IT}) …")
    if not await _wait_file(RX_IT, timeout=90):
        print(f"  FAIL Timeout — {RX_IT} nicht erzeugt")
        print("       Prüfe: Übersetzer läuft? TEST_TRUNK gesetzt? Dialplan geladen?")
        return False
    print(f"  {RX_IT}: {os.path.getsize(RX_IT):,} Bytes")

    print(f"  Warte auf DE-Aufnahme ({RX_DE}) …")
    if not await _wait_file(RX_DE, timeout=15):
        print(f"  FAIL Timeout — {RX_DE} nicht erzeugt")
        return False
    print(f"  {RX_DE}: {os.path.getsize(RX_DE):,} Bytes")

    loop = asyncio.get_running_loop()
    it_text = await loop.run_in_executor(None, _nlu_transcribe, RX_IT)
    de_text = await loop.run_in_executor(None, _nlu_transcribe, RX_DE)

    print(f"  IT-Seite empfing (erwartet: Deutsch):    {it_text!r}")
    print(f"  DE-Seite empfing (erwartet: Italienisch): {de_text!r}")

    ok = bool(it_text.strip()) and bool(de_text.strip())
    print(f"  {'PASS ✓' if ok else 'FAIL ✗ — leere Transkription'}")
    return ok


async def main() -> int:
    print("=" * 60)
    print("Integration-Test: DE↔IT Übersetzungs-Pipeline")
    print("=" * 60)

    trunk = os.environ.get("TEST_TRUNK", "")
    if "test-it-phones" not in trunk:
        print(f"\nWARN: TEST_TRUNK={trunk!r}")
        print("      Outbound geht zur Fritz!Box — nicht zum Test-Kontext!")
        print("      Setze: export TEST_TRUNK='Local/%s@test-it-phones'\n")

    if not TEST_DATA.exists() or not any(TEST_DATA.glob("*.wav")):
        print("FAIL: test_data/ fehlt — ausführen: ./generate_test_data.py")
        return 1

    passed = failed = 0
    for i, (de, it) in enumerate(TEST_CASES, 1):
        ok = await run_case(i, de, it)
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\n{'═' * 60}")
    print(f"Ergebnis: {passed} bestanden, {failed} fehlgeschlagen")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
