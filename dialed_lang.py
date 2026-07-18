#!/usr/bin/python3
"""dialed_lang.py — bestimmt aus gewaehlter Nummer die Zielsprache.
Setzt Channel-Vars DO_TRANSLATE (1/0) und LANGCODE (it/en/...).
Regeln: +4980429494 -> en (Ausnahme); +49 sonst -> keine Uebersetzung;
Landesvorwahl in Map -> deren Sprache; unbekannt -> en (Fallback)."""
import sys
CC_LANG = {"1":"en","7":"ru","30":"el","33":"fr","34":"es","38":"uk","39":"it",
           "44":"en","48":"pl","55":"pt","77":"kk","86":"zh","90":"tr","91":"hi",
           "98":"fa","995":"ka"}
EXCEPTION = {"+4980429494": "en"}

def read_env():
    while True:
        line = sys.stdin.readline()
        if not line or not line.strip():
            break

def setvar(k, v):
    sys.stdout.write('SET VARIABLE %s "%s"\n' % (k, v)); sys.stdout.flush()
    sys.stdin.readline()

read_env()
num = sys.argv[1] if len(sys.argv) > 1 else ""
if num.startswith("00"):
    num = "+" + num[2:]
translate, lang = "0", "en"
if num in EXCEPTION:
    translate, lang = "1", EXCEPTION[num]
elif num.startswith("+49"):
    translate = "0"
elif num.startswith("+"):
    digits = num[1:]
    match = next((cc for cc in sorted(CC_LANG, key=len, reverse=True) if digits.startswith(cc)), None)
    translate, lang = "1", (CC_LANG[match] if match else "en")
else:
    translate = "0"
setvar("DO_TRANSLATE", translate)
setvar("LANGCODE", lang)
sys.stderr.write("dialed_lang: %s -> translate=%s lang=%s\n" % (num, translate, lang))
