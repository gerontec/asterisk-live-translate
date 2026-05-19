#!/home/gh/python/venv_py311/bin/python3
"""
Test: EU country numbers via fritzbox-out (5-digit incomplete = intentionally invalid)
Shows which countries Fritz rejects (603 Decline / immediate 4xx) vs. routes.

Run: ./test_fritz_dialout.py
"""
import asyncio, os, time
from pathlib import Path

_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

AMI_HOST = os.environ.get("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("AMI_PORT", 5038))
AMI_USER = os.environ.get("AMI_USER", "admin")
AMI_PASS = os.environ.get("AMI_PASS", "")

TRUNK      = "PJSIP/%s@fritzbox-out"
TIMEOUT_MS = 10000  # 10s — enough to see 183/480/603 from fritz

# Asterisk cause codes → human-readable label
_CAUSE = {
    0:  "blocked",      # fritz 603 Decline (no intl plan) → needs vsip
    1:  "unallocated",
    3:  "no-route",     # number incomplete/non-existent in fritz's network
    16: "normal",
    17: "busy",
    18: "no-answer",
    19: "no-answer",
    20: "absent",
    21: "rejected",     # SIP 603 Decline
    22: "rejected",
    27: "dest-out-of-order",
    28: "invalid-number",
    31: "normal",
    34: "congestion",   # SIP 503
    38: "net-failure",
    41: "temp-failure",
    58: "incompatible",
    88: "incompatible",
}

# EU country prefixes + 5-digit incomplete suffix (never starting with 1 = emergency)
EU_COUNTRIES = [
    ("+4922222",  "DE"),
    ("+3322222",  "FR"),
    ("+3922222",  "IT"),
    ("+3422222",  "ES"),
    ("+3122222",  "NL"),
    ("+3222222",  "BE"),
    ("+4322222",  "AT"),
    ("+4122222",  "CH"),
    ("+4422222",  "UK"),
    ("+4822222",  "PL"),
    ("+4222222",  "CZ"),
    ("+42122222", "SK"),
    ("+35122222", "PT"),
    ("+3622222",  "HU"),
    ("+4022222",  "RO"),
    ("+35922222", "BG"),
    ("+3022222",  "GR"),
    ("+4622222",  "SE"),
    ("+4722222",  "NO"),
    ("+4522222",  "DK"),
    ("+35822222", "FI"),
    ("+37022222", "LT"),
    ("+37122222", "LV"),
    ("+37222222", "EE"),
    ("+38522222", "HR"),
    ("+38622222", "SI"),
]


async def test_one(number: str, label: str) -> tuple[str, str, float]:
    t0 = time.monotonic()
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection(AMI_HOST, AMI_PORT), timeout=5.0
        )
    except Exception as e:
        return label, f"AMI-FAIL({e})", 0.0

    async def line() -> str:
        return (await r.readline()).decode(errors="replace").rstrip("\r\n")

    await line()  # banner
    w.write(f"Action: Login\r\nUsername: {AMI_USER}\r\nSecret: {AMI_PASS}\r\n\r\n".encode())
    await w.drain()
    while True:
        l = await asyncio.wait_for(line(), timeout=5.0)
        if l == "":
            break

    action_id = f"test_{label}_{int(time.time()*1000)}"
    # Async: true → returns immediately; watches for OriginateResponse event
    w.write((
        f"Action: Originate\r\n"
        f"Channel: {TRUNK % number}\r\n"
        f"Application: Wait\r\nData: 0.1\r\n"
        f"CallerID: Test <+4980425641873>\r\n"
        f"Timeout: {TIMEOUT_MS}\r\n"
        f"Async: true\r\n"
        f"ActionID: {action_id}\r\n\r\n"
    ).encode())
    await w.drain()

    # OriginateResponse event: Response=Success/Failure, Reason=<cause_code>
    response = "TIMEOUT"
    reason   = -1
    deadline = time.monotonic() + (TIMEOUT_MS / 1000) + 5.0
    block: list[str] = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            l = await asyncio.wait_for(line(), timeout=min(5.0, remaining))
        except asyncio.TimeoutError:
            break
        if l == "":
            joined = "\n".join(block)
            if "Event: OriginateResponse" in joined and action_id in joined:
                for bl in block:
                    if bl.startswith("Response:"):
                        response = bl.split(":", 1)[1].strip()
                    elif bl.startswith("Reason:"):
                        try:
                            reason = int(bl.split(":", 1)[1].strip())
                        except ValueError:
                            pass
                break
            block = []
        else:
            block.append(l)

    try:
        w.write(b"Action: Logoff\r\n\r\n")
        await w.drain()
        w.close()
    except Exception:
        pass

    elapsed = time.monotonic() - t0

    if response == "Success":
        result = "ANSWERED"
    elif response == "Failure":
        cause_label = _CAUSE.get(reason, f"cause-{reason}")
        result = f"FAIL/{cause_label}"
    else:
        result = response  # TIMEOUT

    return label, result, elapsed


async def main() -> None:
    print(f"{'Nummer':<14} {'Land':<6} {'Fritz-Status':<22} {'Zeit':>6}")
    print("-" * 56)
    for number, label in EU_COUNTRIES:
        try:
            _, result, elapsed = await test_one(number, label)
        except Exception as e:
            result, elapsed = f"ERROR: {e}", 0.0
        marker = "→" if result == "ANSWERED" else ("✗" if "blocked" in result else "~")
        print(f"{number:<14} {label:<6} {result:<22} {elapsed:>5.1f}s  {marker}")
        await asyncio.sleep(0.5)


if __name__ == "__main__":
    asyncio.run(main())
