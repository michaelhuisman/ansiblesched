#!/usr/bin/env sh
# Genereert dev-secrets in .dev/ (staat in .gitignore). Bestaande waarden blijven staan.
#   .dev/secrets/ssh-target/id_ed25519   SSH-key voor de ssh-target container
#   .dev/secrets/vault/password          vault-wachtwoord voor tests
#   .dev/secrets/keycloak/*              client-secret, admin- en testwachtwoorden
#   .dev/secrets/oidc.env                SCHED_OIDC_CLIENT_SECRET voor de api
#   .dev/secrets/keycloak.env            bootstrap-admin voor de Keycloak-console
#   .dev/keycloak/realm-scheduler.json   realm-import, gerenderd uit dev/keycloak/*.tpl
set -eu
cd "$(dirname "$0")/.."
dir=.dev/secrets
mkdir -p "$dir/ssh-target" "$dir/vault" "$dir/keycloak" .dev/keycloak

rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "${1:-32}"; }
secret() {  # secret <bestand>: aanmaken als hij nog niet bestaat, dan uitlezen
    [ -f "$1" ] || rand 32 >"$1"
    cat "$1"
}

if [ ! -f "$dir/ssh-target/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "ansible-scheduler-dev" -f "$dir/ssh-target/id_ed25519"
fi
secret "$dir/vault/password" >/dev/null

client_secret=$(secret "$dir/keycloak/client-secret")
kc_admin=$(secret "$dir/keycloak/console-admin")
pw_viewer=$(secret "$dir/keycloak/kc-viewer")
pw_operator=$(secret "$dir/keycloak/kc-operator")
pw_admin=$(secret "$dir/keycloak/kc-admin")
pw_norole=$(secret "$dir/keycloak/kc-norole")

sed -e "s/__CLIENT_SECRET__/$client_secret/" \
    -e "s/__PW_VIEWER__/$pw_viewer/" -e "s/__PW_OPERATOR__/$pw_operator/" \
    -e "s/__PW_ADMIN__/$pw_admin/" -e "s/__PW_NOROLE__/$pw_norole/" \
    dev/keycloak/realm-scheduler.json.tpl >.dev/keycloak/realm-scheduler.json
printf 'SCHED_OIDC_CLIENT_SECRET=%s\n' "$client_secret" >"$dir/oidc.env"
printf 'KC_BOOTSTRAP_ADMIN_USERNAME=admin\nKC_BOOTSTRAP_ADMIN_PASSWORD=%s\n' "$kc_admin" >"$dir/keycloak.env"

# De containers draaien onder andere uid's; dit zijn uitsluitend dev-secrets.
chmod -R a+rX .dev
echo "dev secrets in $dir"
