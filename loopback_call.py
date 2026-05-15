#!/home/gh/python/venv_py311/bin/python3
"""
loopback_call.py  —  Loopback-Test für den AudioSocket-Translator.

Architektur (Local-Channel):
  Local;2 → from-internal-Dialplan (AGI → AudioSocket → Translator)
  Local;1 → Dial(PJSIP/mobile@fritzbox-out)
  Der Local-Channel antwortet intern sofort; Fritz!Box-200-OK nicht nötig.

Ablauf:
  1. Translator läuft mit TRANSLATOR_LOOPBACK=1
  2. Dieses Skript starten → Handy klingelt
  3. Handy abnehmen, Deutsch sprechen → Italienisch hören
  4. Auflegen → Skript schreibt Testbericht nach /tmp/loopback_report_<ts>.log
"""

import asyncio, logging, os, subprocess, sys, time
from datetime import datetime

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s.%(msecs)03d  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("loopback_call")

import glob, subprocess as _sp

AMI_HOST        = "127.0.0.1"
AMI_PORT        = 5038
AMI_USER        = "admin"
AMI_PASS        = "asterisk123"
TARGET          = "+4917625257878"
CALLERID        = "+4980425641873 <+4980425641873>"
ASTERISK_LOG    = "/var/log/asterisk/full.log"


def _find_translator_log() -> str:
    """Aktuellstes /tmp/translator_loop*.log des laufenden Translator-Prozesses."""
    try:
        pid = _sp.check_output(
            ["pgrep", "-f", "audiosocket_translator"], text=True
        ).split()[0]
        fds = glob.glob(f"/proc/{pid}/fd/*")
        for fd in fds:
            try:
                target = os.readlink(fd)
                if "translator_loop" in target:
                    return target
            except OSError:
                pass
    except Exception:
        pass
    # Fallback: neuestes File
    files = sorted(glob.glob("/tmp/translator_loop*.log"), key=os.path.getmtime)
    return files[-1] if files else "/tmp/translator_loop.log"


TRANSLATOR_LOG = _find_translator_log()


def _read_tail(path: str, from_offset: int) -> str:
    """Liest Datei ab from_offset. Versucht zuerst direkt, dann via sudo."""
    try:
        with open(path, "rb") as f:
            f.seek(from_offset)
            return f.read().decode(errors="replace")
    except PermissionError:
        try:
            result = subprocess.run(
                ["sudo", "tail", "-c", f"+{from_offset + 1}", path],
                capture_output=True, text=True, timeout=5,
            )
            return result.stdout
        except Exception:
            return f"[nicht lesbar: {path}]"


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        try:
            r = subprocess.run(["sudo", "stat", "-c", "%s", path],
                               capture_output=True, text=True, timeout=5)
            return int(r.stdout.strip())
        except Exception:
            return 0


async def run() -> None:
    ts_start = datetime.now()
    translator_offset = _file_size(TRANSLATOR_LOG)
    asterisk_offset   = _file_size(ASTERISK_LOG)

    log.info(f"Test-Start: {ts_start:%H:%M:%S}")
    log.info(f"Translator-Log-Offset: {translator_offset}")
    log.info(f"Asterisk-Log-Offset:   {asterisk_offset}")

    log.info(f"Verbinde zu Asterisk AMI {AMI_HOST}:{AMI_PORT} …")
    r, w = await asyncio.open_connection(AMI_HOST, AMI_PORT)

    async def line() -> str:
        return (await r.readline()).decode(errors="replace").strip()

    banner = await line()
    log.info(f"AMI: {banner}")

    w.write(
        f"Action: Login\r\nUsername: {AMI_USER}\r\n"
        f"Secret: {AMI_PASS}\r\n\r\n".encode()
    )
    await w.drain()

    while True:
        ln = await line()
        if ln:
            log.debug(f"< {ln}")
        if "Authentication accepted" in ln:
            log.info("AMI Login OK")
            break
        if "Authentication failed" in ln:
            log.error("AMI-Authentifizierung fehlgeschlagen")
            w.close()
            return

    # Asterisk-Verbosity hochsetzen
    w.write("Action: Command\r\nCommand: core set verbose 5\r\n\r\n".encode())
    await w.drain()
    while True:
        ln = await line()
        if not ln:
            break

    # Originate via Local-Channel:
    # Local;2 → from-internal (AGI→AudioSocket→Translator)
    # Local;1 → Dial(Handy via Fritz!Box)
    log.info(f"Rufe {TARGET} über Local-Channel an …")
    w.write((
        f"Action: Originate\r\n"
        f"Channel: Local/{TARGET}@from-internal/n\r\n"
        f"Application: Dial\r\n"
        f"Data: PJSIP/{TARGET}@fritzbox-out,60\r\n"
        f"CallerID: {CALLERID}\r\n"
        f"Timeout: 60000\r\n"
        f"Async: true\r\n\r\n"
    ).encode())
    await w.drain()
    log.info("Originate gesendet — Handy klingelt, bitte abnehmen und Deutsch sprechen …")
    log.info("Ctrl+C oder Auflegen beendet den Test")

    ami_events: list[str] = []
    hangup_seen = False

    try:
        while True:
            ln = await line()
            if not ln:
                continue
            if any(k in ln for k in ("Event:", "State:", "Cause", "Channel:", "Uniqueid:",
                                     "ChannelState", "SentPackets", "PT:")):
                log.info(f"AMI> {ln}")
            else:
                log.debug(f"AMI> {ln}")
            ami_events.append(ln)

            if "Event: Hangup" in ln or "Event: OriginateResponse" in ln:
                hangup_seen = True
            # Warte nach Hangup noch 3s für letzte Events, dann Report
            if hangup_seen and "Cause-txt" in ln:
                log.info("Hangup erkannt — warte 3s für Log-Flush …")
                await asyncio.sleep(3)
                break

    except asyncio.CancelledError:
        pass
    except Exception as e:
        log.info(f"AMI-Verbindung beendet: {e}")
    finally:
        try:
            w.close()
        except Exception:
            pass

    # ── Testbericht schreiben ────────────────────────────────────────
    ts_end = datetime.now()
    report_path = f"/tmp/loopback_report_{ts_start:%Y%m%d_%H%M%S}.log"

    translator_section = _read_tail(TRANSLATOR_LOG, translator_offset)
    asterisk_section   = _read_tail(ASTERISK_LOG, asterisk_offset)

    with open(report_path, "w") as f:
        f.write(f"# Loopback-Testbericht\n")
        f.write(f"# Start:  {ts_start:%Y-%m-%d %H:%M:%S}\n")
        f.write(f"# Ende:   {ts_end:%Y-%m-%d %H:%M:%S}\n")
        f.write(f"# Dauer:  {(ts_end - ts_start).total_seconds():.1f}s\n")
        f.write(f"# Target: {TARGET}\n\n")

        f.write("## AMI-Events (Auszug)\n")
        for ev in ami_events:
            if any(k in ev for k in ("Event:", "Cause", "ChannelState", "SentPackets")):
                f.write(f"  {ev}\n")

        f.write("\n## Translator-Log (neu seit Teststart)\n")
        f.write(translator_section if translator_section.strip()
                else "  [leer — kein AudioSocket-Connect]\n")

        f.write("\n## Asterisk full.log (neu seit Teststart)\n")
        f.write(asterisk_section if asterisk_section.strip()
                else "  [leer — Verbosity zu niedrig oder kein Dialplan-Event]\n")

    log.info(f"Testbericht geschrieben: {report_path}")
    print(f"\n{'='*60}")
    print(f"TESTBERICHT: {report_path}")
    print(f"{'='*60}")
    print(f"\n[Translator-Log]\n{translator_section or '  (leer)'}")
    print(f"\n[Asterisk-Log]\n{asterisk_section or '  (leer)'}")


async def main() -> None:
    task = asyncio.create_task(run())
    try:
        await task
    except KeyboardInterrupt:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
