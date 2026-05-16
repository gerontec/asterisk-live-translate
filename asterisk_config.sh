#!/bin/bash
# asterisk_config.sh — Asterisk-Konfiguration sichern oder einspielen
#
# save  : /etc/asterisk → asterisk/ (Passwörter → CHANGEME)
# apply : asterisk/ → /etc/asterisk (Passwörter aus .asterisk-secrets), dann Reload

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
REPO_AST="$REPO/asterisk"
ETC_AST="/etc/asterisk"
SECRETS="$REPO/.asterisk-secrets"

usage() {
    echo "Verwendung: $0 [save|apply]"
    echo "  save  — /etc/asterisk → asterisk/  (Passwörter bereinigt)"
    echo "  apply — asterisk/ → /etc/asterisk  (Passwörter aus .asterisk-secrets)"
    exit 1
}

save_configs() {
    echo ">>> extensions_translator.conf sichern..."
    sudo cp "$ETC_AST/extensions_translator.conf" "$REPO_AST/"
    sudo chown "$USER:$USER" "$REPO_AST/extensions_translator.conf"

    echo ">>> pjsip.conf sichern (Passwörter bereinigt)..."
    sudo python3 -c "
import re, sys
content = open('$ETC_AST/pjsip.conf').read()
sanitized = re.sub(r'(password\s*=\s*)\S+', r'\1CHANGEME', content)
open('$REPO_AST/pjsip.conf', 'w').write(sanitized)
"
    sudo chown "$USER:$USER" "$REPO_AST/pjsip.conf"
    echo ">>> Gespeichert in $REPO_AST/ — bitte git add/commit."
}

apply_configs() {
    if [ ! -f "$SECRETS" ]; then
        echo "FEHLER: $SECRETS nicht gefunden."
        echo "Vorlage kopieren und Passwörter eintragen:"
        echo "  cp $REPO/.asterisk-secrets.example $SECRETS"
        exit 1
    fi

    echo ">>> extensions_translator.conf einspielen..."
    sudo cp "$REPO_AST/extensions_translator.conf" "$ETC_AST/"

    echo ">>> pjsip.conf einspielen (Passwörter aus .asterisk-secrets)..."
    sudo python3 - <<PYEOF
import re, sys

secrets = {}
for line in open('$SECRETS'):
    line = line.strip()
    if '=' in line and not line.startswith('#'):
        k, v = line.split('=', 1)
        secrets[k.strip()] = v.strip()

lines = open('$REPO_AST/pjsip.conf').read().split('\n')
current_section = None
result = []
missing = []

for line in lines:
    m = re.match(r'^\[(\S+)\]', line)
    if m:
        current_section = m.group(1)

    if re.match(r'\s*password\s*=\s*CHANGEME', line) and current_section:
        key = current_section.upper().replace('-', '_') + '_PASSWORD'
        if key in secrets:
            indent = re.match(r'^(\s*)', line).group(1)
            line = f'{indent}password = {secrets[key]}'
        else:
            missing.append(f'  {key}  (Sektion [{current_section}])')

    result.append(line)

if missing:
    print('WARNUNG: Folgende Passwörter fehlen in .asterisk-secrets:')
    for m in missing:
        print(m)

open('$ETC_AST/pjsip.conf', 'w').write('\n'.join(result))
print('pjsip.conf geschrieben.')
PYEOF

    echo ">>> Asterisk neu laden..."
    sudo asterisk -rx "module reload res_pjsip.so"
    sudo asterisk -rx "dialplan reload"
    echo ">>> Fertig."
}

case "${1:-}" in
    save)  save_configs ;;
    apply) apply_configs ;;
    *)     usage ;;
esac
