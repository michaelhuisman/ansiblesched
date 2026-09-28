#!/usr/bin/env bash
# Failover-test (draait op de host; de dev-container kan geen containers killen).
#
# Vereist: <podman|docker> compose -f compose.dev.yml up -d --scale scheduler=2 --scale worker=2
#
# 1. Maakt een template en een schedule `* * * * *`.
# 2. Killt de scheduler-leider hard (SIGKILL), vlak vóór een minuutgrens.
# 3. Controleert: een andere replica is binnen 30s leider, de afvuring op die
#    minuutgrens is niet verloren gegaan en er zijn geen dubbele runs.
set -euo pipefail
cd "$(dirname "$0")/.."

API=${LAMPLIGHTER_IT_API_URL:-http://127.0.0.1:8000}/api/v1
. scripts/lib.sh
NS_SCHEDULER=$((0x5343))

sql() { $COMPOSE exec -T postgres psql -U lamplighter -d lamplighter -Atc "$1" 2>/dev/null; }

# Lokale admin + API-token via de CLI (het wachtwoord is niet nodig en gaat via stdin).
LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24 |
    $COMPOSE run --rm -T dev python -m app create-user it-failover --role admin >/dev/null 2>&1 || true
TOKEN=$($COMPOSE run --rm -T dev python -m app create-token it-failover --name failover --expires-days 1 2>/dev/null | grep '^lamplighter_')
[ -n "$TOKEN" ] || { echo "FAIL: kon geen API-token aanmaken" >&2; exit 1; }
AUTH="Authorization: Bearer $TOKEN"

post() { curl -sf -H "$AUTH" -H 'content-type: application/json' -X POST "$API$1" -d "$2"; }
field() { python3 -c "import json,sys; print(json.load(sys.stdin)['$1'])"; }
leader() {
    sql "SELECT a.application_name FROM pg_locks l JOIN pg_stat_activity a USING (pid)
         WHERE l.locktype = 'advisory' AND l.classid = $NS_SCHEDULER AND l.objid = 1 AND l.granted"
}
fail() { echo "FAIL: $*" >&2; exit 1; }

suffix=$RANDOM$RANDOM
cred=$(post /credentials "{\"name\":\"fo-key-$suffix\",\"type\":\"ssh_key\",\"openbao_path\":\"ssh/ssh-target\",\"openbao_key\":\"id_ed25519\"}" | field id)
proj=$(post /projects "{\"name\":\"fo-proj-$suffix\",\"git_url\":\"file:///fixtures/repo.git\"}" | field id)
inv=$(post /inventories "{\"name\":\"fo-inv-$suffix\",\"source_type\":\"inline\",\"content\":\"ssh-target ansible_user=ansible ansible_python_interpreter=/usr/bin/python3\\n\"}" | field id)
tpl=$(post /templates "{\"name\":\"fo-ping-$suffix\",\"project_id\":$proj,\"playbook_path\":\"ping.yml\",\"inventory_id\":$inv,\"machine_credential_id\":$cred}" | field id)
sched=$(post /schedules "{\"template_id\":$tpl,\"cron\":\"* * * * *\"}" | field id)
echo "schedule $sched aangemaakt"

cleanup() {
    curl -sf -H "$AUTH" -X DELETE "$API/schedules/$sched" >/dev/null || true
    $COMPOSE up -d --scale scheduler=2 --scale worker=2 >/dev/null 2>&1 || true
}
trap cleanup EXIT

old=$(leader)
[ -n "$old" ] || fail "geen leider gevonden"
container=${old#scheduler:}
echo "leider: $old"

# Wacht tot seconde 55, zodat de kill vlak vóór de afvuring valt.
while [ "$(date -u +%S)" -ne 55 ]; do sleep 0.5; done
boundary=$(date -u -v+1M +%Y-%m-%dT%H:%M:00 2>/dev/null || date -u -d '+1 min' +%Y-%m-%dT%H:%M:00)
killed_at=$(date +%s)
$CONTAINER kill "$container" >/dev/null
echo "leider gekild om $(date -u +%T), minuutgrens $boundary"

new=""
while [ $(( $(date +%s) - killed_at )) -le 30 ]; do
    new=$(leader)
    if [ -n "$new" ] && [ "$new" != "$old" ]; then break; fi
    new=""
    sleep 1
done
[ -n "$new" ] || fail "geen nieuwe leider binnen 30s"
echo "nieuwe leider: $new na $(( $(date +%s) - killed_at ))s"

# Nog een volle minuut wachten, dan de runs van deze schedule controleren.
sleep 75
runs=$(sql "SELECT to_char(scheduled_for AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')
            FROM runs WHERE schedule_id = $sched ORDER BY scheduled_for")
echo "runs (scheduled_for):"; echo "$runs" | sed 's/^/  /'

dupes=$(sql "SELECT count(*) - count(DISTINCT scheduled_for) FROM runs WHERE schedule_id = $sched")
[ "$dupes" = "0" ] || fail "$dupes dubbele runs"
echo "$runs" | grep -q "^$boundary$" || fail "afvuring op $boundary ontbreekt"
bad=$(sql "SELECT count(*) FROM runs WHERE schedule_id = $sched AND status NOT IN ('successful', 'running', 'queued')")
[ "$bad" = "0" ] || fail "$bad runs met onverwachte status"
echo "OK: takeover binnen 30s, afvuring op $boundary aanwezig, geen dubbele runs"
