#!/usr/bin/python3
"""
caller_lang.py — Asterisk AGI
Determines the announcement/Whisper language from the caller's phone number prefix.

Dialplan usage:
  same => n,AGI(caller_lang.py,${CALLERID(num)})
  ; → sets PROMPT_LANG (e.g. "de", "it", "ru")
"""
import sys, urllib.request, json


def agi_read() -> dict:
    env = {}
    while True:
        line = sys.stdin.readline().strip()
        if not line:
            break
        if ": " in line:
            k, v = line.split(": ", 1)
            env[k] = v
    return env


def agi_set(var: str, val: str) -> None:
    sys.stdout.write(f"SET VARIABLE {var} {val}\n")
    sys.stdout.flush()
    sys.stdin.readline()   # consume "200 result=1"


def agi_result(code: int = 0) -> None:
    sys.stdout.write(f"200 result={code}\n")
    sys.stdout.flush()


env      = agi_read()
callerid = sys.argv[1] if len(sys.argv) > 1 else ""

try:
    payload = json.dumps({"callerid": callerid}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:9094/lang",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as resp:
        result = json.loads(resp.read())
    lang = result.get("lang", "de")
except Exception as exc:
    sys.stderr.write(f"caller_lang: Fehler: {exc}\n")
    lang = "de"

agi_set("PROMPT_LANG", lang)
sys.stderr.write(f"caller_lang: callerid={callerid!r} → PROMPT_LANG={lang}\n")
agi_result(0)
