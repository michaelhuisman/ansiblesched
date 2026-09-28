#!/usr/bin/env bash
# deploy-test: runs the Ansible role against this host (CI runner, Docker) and checks the
# acceptance criteria of phase 5b-2. Never run this on a machine you care about: it
# writes to /opt/lamplighter and /etc/systemd/system.
#
# Requires: scripts/dev-keys.sh, the fixtures (see the CI job), an image
# localhost/lamplighter:ci, ansible-core and deploy/requirements.yml.
set -euo pipefail
cd "$(dirname "$0")/../.."

DIR=/opt/lamplighter
API=http://127.0.0.1:8000
METRICS_TOKEN=$(cat .dev/secrets/metrics-token)
playbook() {
    ANSIBLE_CONFIG=deploy/ansible.cfg ansible-playbook -i deploy/tests/inventory.yml \
        -e @deploy/tests/vars.yml "$@"
}
compose() { sudo docker compose -f "$DIR/compose.yml" "$@"; }
fail() { echo "FAIL: $*" >&2; exit 1; }
step() { echo; echo "=== $*"; }

step "1. fresh install"
playbook deploy/site.yml -e lamplighter_image_tag=ci
curl -sf "$API/readyz" >/dev/null || fail "api not ready after install"

step "2. second run changes nothing"
out=$(playbook deploy/site.yml -e lamplighter_image_tag=ci)
echo "$out" | tail -4
echo "$out" | grep -Eq 'changed=0 +unreachable=0 +failed=0' || fail "role is not idempotent"

step "3. admin, token and a long run"
# Not via `tr </dev/urandom | head`: with pipefail tr's broken pipe fails the script.
openssl rand -hex 16 | compose run --rm -T --no-deps api create-user deploy-test --role admin >/dev/null
TOKEN=$(compose run --rm -T --no-deps api create-token deploy-test --name ci --expires-days 1 |
    grep '^lamplighter_')
[ -n "$TOKEN" ] || fail "no API token"
auth=(-H "Authorization: Bearer $TOKEN")
post() { curl -sf "${auth[@]}" -H 'content-type: application/json' -X POST "$API/api/v1$1" -d "$2"; }
get() { curl -sf "${auth[@]}" "$API/api/v1$1"; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }

ssh=$(post /credentials '{"name":"ssh","type":"ssh_key","openbao_path":"ssh/ssh-target","openbao_key":"id_ed25519"}' | field id)
git=$(post /credentials '{"name":"git","type":"git_token","openbao_path":"git/fixtures","openbao_key":"token"}' | field id)
proj=$(post /projects "{\"name\":\"fixtures\",\"git_url\":\"http://git-http:8080/repo.git\",\"branch\":\"main\",\"credential_id\":$git}" | field id)
inv=$(post /inventories '{"name":"target","source_type":"inline","content":"ssh-target ansible_user=ansible ansible_python_interpreter=/usr/bin/python3\n"}' | field id)
tpl=$(post /templates "{\"name\":\"sleep\",\"project_id\":$proj,\"playbook_path\":\"sleep.yml\",\"inventory_id\":$inv,\"machine_credential_id\":$ssh,\"extra_vars\":{\"sleep_s\":60}}" | field id)
run=$(post "/templates/$tpl/launch" '{}' | field id)
status() { get "/runs/$run" | field status; }
for _ in $(seq 1 60); do [ "$(status)" = running ] && break; sleep 1; done
[ "$(status)" = running ] || fail "run $run did not start (status $(status))"
echo "run $run is running"

step "4. image update during the run"
sudo docker tag localhost/lamplighter:ci localhost/lamplighter:ci2
playbook deploy/site.yml -e lamplighter_image_tag=ci2
for _ in $(seq 1 90); do case $(status) in queued|running) sleep 1 ;; *) break ;; esac; done
[ "$(status)" = successful ] || fail "run $run ended as $(status) after the update"
for svc in api scheduler worker-a worker-b; do
    image=$(sudo docker inspect --format '{{.Config.Image}}' "$(compose ps -q "$svc" | head -n 1)")
    [ "$image" = localhost/lamplighter:ci2 ] || fail "$svc still runs $image"
done
echo "run $run finished successfully; all services on ci2"

step "5. backup via the systemd timer's service"
systemctl is-enabled lamplighter-backup.timer >/dev/null || fail "backup timer not enabled"
sudo systemctl start lamplighter-backup.service
dump=$(sudo ls -1t "$DIR/backups" | head -n 1)
[[ "$dump" == lamplighter-*.dump ]] || fail "no dump in $DIR/backups"
metrics=$(curl -sf -H "Authorization: Bearer $METRICS_TOKEN" "$API/metrics")
echo "$metrics" | grep -q 'lamplighter_maintenance_last_attempt_failed{task="backup"} 0.0' ||
    fail "backup status missing in /metrics"
echo "backup $dump written"

step "6. restore"
compose exec -T postgres psql -qX -U lamplighter -d lamplighter -c "DELETE FROM runs WHERE id = $run"
get "/runs/$run" >/dev/null 2>&1 && fail "run $run should be gone before the restore"
playbook deploy/restore.yml -e lamplighter_image_tag=ci2 -e "lamplighter_restore_file=$dump"
[ "$(status)" = successful ] || fail "run $run not back after the restore"
echo "run $run is back after the restore"

echo; echo "OK: install, idempotence, update without interrupting a run, backup and restore"
