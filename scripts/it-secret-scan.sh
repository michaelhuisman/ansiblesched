#!/usr/bin/env bash
# Secret-scan van de containerlogs (draait op de host; de dev-container heeft geen
# toegang tot de containerlogs). Zoekt alle dev-secrets uit .dev/secrets in de logs van de
# app-containers. De OpenBao-dev-server zelf valt erbuiten: die print zijn root-token.
#
# Draai na de integratietests, zodat de logs runs met al deze secrets bevatten.
set -euo pipefail
cd "$(dirname "$0")/.."

. scripts/lib.sh
SERVICES="api worker scheduler migrate"
dir=.dev/secrets

needles=()
add() { [ -n "$1" ] && needles+=("$1"); }
add "$(cat "$dir/vault/password")"
add "$(cat "$dir/git/token")"
add "$(cat "$dir/webhook/hmac")"
add "$(cat "$dir/keycloak/client-secret")"
for f in "$dir"/openbao/*-secret-id "$dir"/keycloak/kc-*; do add "$(cat "$f")"; done
# SSH-key: elke inhoudsregel afzonderlijk (een deel van een key is ook een lek).
while IFS= read -r line; do
    case "$line" in -----*|"") ;; *) add "$line" ;; esac
done <"$dir/ssh-target/id_ed25519"

logs=$($COMPOSE logs --no-color $SERVICES 2>/dev/null)
lines=$(printf '%s\n' "$logs" | wc -l | tr -d ' ')
found=0
for needle in "${needles[@]}"; do
    if printf '%s' "$logs" | grep -qF -- "$needle"; then
        echo "FAIL: secret gevonden in de logs (begint met ${needle:0:4}…)" >&2
        found=1
    fi
done
[ "$found" = 0 ] || exit 1
echo "OK: ${#needles[@]} secrets gezocht in $lines logregels van: $SERVICES; niets gevonden"
