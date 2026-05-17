#!/usr/bin/python3
"""
voice_nlu.py — Asterisk AGI
Sends a recorded WAV file to the translator's /nlu endpoint.
Whisper transcribes in the caller's language (same as the announcement language)
for best accuracy.

Dialplan usage:
  same => n,AGI(voice_nlu.py,/tmp/ast_nlu_${UNIQUEID}.wav,${PROMPT_LANG})
  ; → sets DIAL_NUMBER (digit string), LANG_SUFFIX (e.g. "39" for Italy)
  ; Both are empty if NLU fails → dialplan should fall back to DTMF
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


env  = agi_read()
path = sys.argv[1] if len(sys.argv) > 1 else ""
lang = sys.argv[2] if len(sys.argv) > 2 else "de"

if not path:
    sys.stderr.write("voice_nlu: kein WAV-Pfad übergeben\n")
    agi_set("DIAL_NUMBER", "")
    agi_set("LANG_SUFFIX",  "")
    agi_result(1)
    sys.exit(1)

try:
    payload = json.dumps({"path": path, "lang": lang}).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:9094/nlu",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())

    number = result.get("number", "")
    suffix = result.get("suffix", "")
    sys.stderr.write(
        f"voice_nlu: lang={lang!r} text={result.get('text','')!r} "
        f"→ number={number!r} suffix={suffix!r}\n"
    )
    agi_set("DIAL_NUMBER", number)
    agi_set("LANG_SUFFIX",  suffix)
    agi_result(0)

except Exception as exc:
    sys.stderr.write(f"voice_nlu: Fehler: {exc}\n")
    agi_set("DIAL_NUMBER", "")
    agi_set("LANG_SUFFIX",  "")
    agi_result(1)
