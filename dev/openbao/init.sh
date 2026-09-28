#!/bin/sh
# Sets up the dev OpenBao (in-memory; runs again on every start of the stack):
# AppRoles with least-privilege policies and the dev secrets from .dev/secrets.
set -eu
export BAO_ADDR=http://openbao:8200   # BAO_TOKEN (root, dev only) comes from init.env

until bao status >/dev/null 2>&1; do sleep 1; done

bao auth list -format=json | grep -q '"approle/"' || bao auth enable approle >/dev/null

bao policy write lamplighter-worker - >/dev/null <<'POLICY'
path "secret/data/ssh/*"   { capabilities = ["read"] }
path "secret/data/vault/*" { capabilities = ["read"] }
path "secret/data/git/*"   { capabilities = ["read"] }
POLICY
bao policy write lamplighter-scheduler - >/dev/null <<'POLICY'
path "secret/data/webhooks/*" { capabilities = ["read"] }
POLICY

# Short TTLs in dev, so token renewal and re-login run during normal use.
role() {
    bao write "auth/approle/role/$1" token_policies="$1" token_ttl=60s token_max_ttl=300s \
        secret_id_ttl=0 secret_id_num_uses=0 >/dev/null
    bao write "auth/approle/role/$1/role-id" role_id="$2" >/dev/null
    bao write "auth/approle/role/$1/custom-secret-id" secret_id="$3" >/dev/null 2>&1 || true
}
role lamplighter-worker "$WORKER_ROLE_ID" "$WORKER_SECRET_ID"
role lamplighter-scheduler "$SCHEDULER_ROLE_ID" "$SCHEDULER_SECRET_ID"

bao kv put -mount=secret ssh/ssh-target id_ed25519=@/secrets/ssh-target/id_ed25519 >/dev/null
bao kv put -mount=secret ssh/ssh-target-known-hosts known_hosts=@/secrets/ssh-target/known_hosts >/dev/null
bao kv put -mount=secret vault/dev password=@/secrets/vault/password >/dev/null
bao kv put -mount=secret git/fixtures token=@/secrets/git/token username=scheduler >/dev/null
bao kv put -mount=secret webhooks/default urls='["http://webhook-sink:8080/hook"]' \
    hmac_secret=@/secrets/webhook/hmac >/dev/null
echo "openbao initialised"
