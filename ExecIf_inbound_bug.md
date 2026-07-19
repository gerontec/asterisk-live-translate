# Inbound calls die at `ExecIf` — `No application 'ExecIf'` (app_exec.so missing)

## Symptom
- Incoming trunk calls (vsip / dusnet) to the DID never reach the translator / Tina.
- The **provider CDR shows every inbound call with duration `00:00:01`**.
- Caller hears nothing / immediate hangup after ~1 second.
- `core show channels` shows the channel appear briefly, then gone.

## Root cause
`from-vsip` / `from-dusnet` / `from-vodafone` (and their shared `voicedial` label) in
`extensions_translator.conf` used:

    same => n,ExecIf($["${DIAL_NUMBER}"="9494"]?Set(LANG_SUFFIX=~en))

On the self-built Asterisk on the public node (verified: **Asterisk 22.5.2**) `app_exec.so`
is **not built / not loaded**, so `ExecIf` does not exist:

    WARNING pbx.c: No application 'ExecIf' for extension (from-vsip, +49..., 17)
    DEBUG   pbx.c: Spawn extension (from-vsip, +49..., 17) exited non-zero

The dialplan spawn exits non-zero at that priority, so the call is torn down after ~1 s
(SIP cause 16 / normal clearing) **before** it ever reaches the AudioSocket translator leg.

## Why it was hard to find (dead ends)
- `cause=16` on a vsip self-hairpin (vsip-out -> vsip-in) looks like provider **loop-prevention**
  — it is NOT. The loop works fine once ExecIf is fixed.
- NOT a firewall problem: `nmap` shows `5060/udp open` on the public IPv4 and IPv6.
- NOT a SIP `identify` problem: `vsip-identify` matches the source IP; the call *does* enter
  `from-vsip` and starts executing — it only dies at the `ExecIf` line.
- NOT a provider "Rufumleitung"/announcement (that was a *separate*, outbound caller-ID issue).
- On the public node the Asterisk log target is **`debug.log`** (not `full.log`), and
  `tcpdump -i any` is unreliable there. Use `pjsip set logger on` and read `debug.log`.

## Fix
Replace `ExecIf(...)` with `Set(...)` using the `IF()` function (from `func_logic`, which IS present):

    same => n,Set(LANG_SUFFIX=${IF($["${DIAL_NUMBER}"="9494"]?~en:${LANG_SUFFIX})})

Apply in all three inbound contexts (`from-vsip`, `from-dusnet`, `from-vodafone`).

Alternative: build/load `app_exec.so` (menuselect -> Applications -> app_exec) so `ExecIf` works.
