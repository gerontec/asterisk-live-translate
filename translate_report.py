#!/usr/bin/env python3
"""
translate_report.py — Report der letzten Calls aus wagodb.ast_translate.

Zeigt je Call (uniqueid) alle Sätze im Zeitverlauf mit **Originaltext** und
**Übersetzung in voller Länge** (keine Kürzung).

Nutzung:
  ./translate_report.py            # letzte 3 Calls (Default)
  ./translate_report.py 5          # letzte 5 Calls
Env-Overrides:
  DB_HOST (Default 192.168.5.23), DB_USER=gh, DB_PASS=a12345, DB_NAME=wagodb
"""
import os, sys
import pymysql

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 3

CFG = dict(
    host=os.environ.get("DB_HOST", "192.168.5.23"),
    user=os.environ.get("DB_USER", "gh"),
    password=os.environ.get("DB_PASS", "a12345"),
    database=os.environ.get("DB_NAME", "wagodb"),
    charset="utf8mb4", connect_timeout=6,
)

SEG_SQL = ("SELECT calldate, src, dst, sip, telegram, sourcetext, translatedtext "
           "FROM ast_translate WHERE {} ORDER BY id")


def main() -> None:
    conn = pymysql.connect(**CFG)
    cur = conn.cursor()
    # letzte N Calls nach jüngstem Segment
    cur.execute("SELECT uniqueid FROM ast_translate "
                "GROUP BY uniqueid ORDER BY MAX(calldate) DESC LIMIT %s", (N,))
    call_ids = [r[0] for r in cur.fetchall()]
    if not call_ids:
        print("Keine Einträge in ast_translate.")
        return

    print(f"═══ Letzte {len(call_ids)} Call(s) — wagodb.ast_translate ═══\n")
    for uid in call_ids:
        if uid is None:
            cur.execute(SEG_SQL.format("uniqueid IS NULL"))
        else:
            cur.execute(SEG_SQL.format("uniqueid = %s"), (uid,))
        rows = cur.fetchall()
        if not rows:
            continue
        f = rows[0]
        channel = "SIP" if f[3] else ("Telegram" if f[4] else "?")
        print(f"── Call {uid or '(ohne uniqueid)'}")
        print(f"   {rows[0][0]} … {rows[-1][0]}  |  {f[1] or '?'} → {f[2] or '?'}"
              f"  |  {channel}  |  {len(rows)} Satz/Sätze")
        for (ts, src, dst, sip, tg, st, tt) in rows:
            print(f"   [{str(ts)[11:]}]")
            print(f"     Original   : {st}")
            print(f"     Übersetzung: {tt}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
