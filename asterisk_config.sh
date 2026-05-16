#!/bin/bash
# asterisk_config.sh — save or apply Asterisk configuration
#
# save  : /etc/asterisk → asterisk/ (passwords replaced with CHANGEME)
# apply : asterisk/ → /etc/asterisk (passwords from .asterisk-secrets), then reload

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
REPO_AST="$REPO/asterisk"
ETC_AST="/etc/asterisk"
SECRETS="$REPO/.asterisk-secrets"

usage() {
    echo "Usage: $0 [save|apply]"
    echo "  save  — /etc/asterisk → asterisk/  (passwords sanitized)"
    echo "  apply — asterisk/ → /etc/asterisk  (passwords from .asterisk-secrets)"
    exit 1
}

save_configs() {
    echo ">>> saving extensions_translator.conf..."
    sudo cp "$ETC_AST/extensions_translator.conf" "$REPO_AST/"
    sudo chown "$USER:$USER" "$REPO_AST/extensions_translator.conf"

    echo ">>> saving pjsip.conf (passwords sanitized)..."
    sudo python3 -c "
import re, sys
content = open('$ETC_AST/pjsip.conf').read()
sanitized = re.sub(r'(password\s*=\s*)\S+', r'\1CHANGEME', content)
open('$REPO_AST/pjsip.conf', 'w').write(sanitized)
"
    sudo chown "$USER:$USER" "$REPO_AST/pjsip.conf"
    echo ">>> Saved to $REPO_AST/ — run git add/commit to record changes."
}

apply_configs() {
    if [ ! -f "$SECRETS" ]; then
        echo "ERROR: $SECRETS not found."
        echo "Copy the example and fill in passwords:"
        echo "  cp $REPO/.asterisk-secrets.example $SECRETS"
        exit 1
    fi

    echo ">>> applying extensions_translator.conf..."
    sudo cp "$REPO_AST/extensions_translator.conf" "$ETC_AST/"

    echo ">>> applying pjsip.conf (passwords from .asterisk-secrets)..."
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
    print('WARNING: missing passwords in .asterisk-secrets (left as CHANGEME):')
    for m in missing:
        print(m)

open('$ETC_AST/pjsip.conf', 'w').write('\n'.join(result))
print('pjsip.conf written.')
PYEOF

    echo ">>> reloading Asterisk..."
    sudo asterisk -rx "module reload res_pjsip.so"
    sudo asterisk -rx "dialplan reload"
    echo ">>> Done."
}

case "${1:-}" in
    save)  save_configs ;;
    apply) apply_configs ;;
    *)     usage ;;
esac
