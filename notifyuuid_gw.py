#!/usr/bin/python3
"""
notify_uuid.py  —  Asterisk AGI-Skript
Registriert UUID+Exten beim Translator BEVOR AudioSocket startet.

Dialplan-Aufruf:
  same => n,AGI(notify_uuid.py,${AS_UUID},${EXTEN})
"""
import sys, urllib.request, json, os

def agi_read() -> dict:
    """Liest alle AGI-Variablen vom stdin."""
    env = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ": " in line:
            k, v = line.split(": ", 1)
            env[k] = v
    return env

def agi_result(code: int = 0) -> None:
    sys.stdout.write(f"200 result={code}\n")
    sys.stdout.flush()

# AGI-Umgebung lesen
env = agi_read()

# Argumente aus Dialplan
uuid  = sys.argv[1] if len(sys.argv) > 1 else ""
exten = sys.argv[2] if len(sys.argv) > 2 else ""

if not uuid or not exten:
    sys.stderr.write(f"notify_uuid: fehlendes Argument uuid={uuid!r} exten={exten!r}\n")
    agi_result(1)
    sys.exit(1)

# HTTP-POST an Python-Translator
payload = json.dumps({"uuid": uuid, "exten": exten}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:9097/register",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=2) as resp:
        status = resp.status
        if status == 200:
            sys.stderr.write(f"notify_uuid: OK uuid={uuid[:8]} exten={exten}\n")
        else:
            sys.stderr.write(f"notify_uuid: HTTP {status}\n")
except Exception as e:
    sys.stderr.write(f"notify_uuid: Fehler: {e}\n")
    agi_result(1)
    sys.exit(1)

agi_result(0)
