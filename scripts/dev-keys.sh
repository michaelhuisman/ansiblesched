#!/usr/bin/env sh
# Genereert dev-secrets in .dev/secrets (staat in .gitignore).
#   .dev/secrets/ssh-target/id_ed25519   SSH-key voor de ssh-target container
#   .dev/secrets/vault/password          vault-wachtwoord voor tests
set -eu
cd "$(dirname "$0")/.."
dir=.dev/secrets
mkdir -p "$dir/ssh-target" "$dir/vault"
if [ ! -f "$dir/ssh-target/id_ed25519" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "ansible-scheduler-dev" -f "$dir/ssh-target/id_ed25519"
fi
if [ ! -f "$dir/vault/password" ]; then
    LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 >"$dir/vault/password"
fi
# De containers draaien onder een andere uid; dit zijn uitsluitend dev-secrets.
chmod 0644 "$dir/ssh-target/id_ed25519" "$dir/vault/password"
echo "dev secrets in $dir"
