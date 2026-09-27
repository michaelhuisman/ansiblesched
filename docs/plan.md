# ansible-scheduler — ontwerp en fasering

## Doel

Ansible-playbooks uit Git op schema en on-demand uitvoeren, met live logs, runhistorie,
secrets uit OpenBao en authenticatie via Keycloak. Draait voorlopig met Docker Compose
op één LXC/VM, maar is zo opgezet dat meerdere workers en scheduler-replicas later
zonder refactor kunnen.

## Architectuur

| Rol         | Verantwoordelijkheid                                                     |
|-------------|--------------------------------------------------------------------------|
| `api`       | REST API (FastAPI), CRUD op configuratie, runs starten, logs via SSE     |
| `scheduler` | Schedules evalueren (APScheduler) en runs op `queued` zetten             |
| `worker`    | Runs claimen, repo uitchecken, ansible-runner uitvoeren, events opslaan  |
| `migrate`   | One-shot `alembic upgrade head` vóór de andere services starten          |

De scheduler voert niets uit, hij maakt alleen run-records aan. Postgres is de enige
state-store: jobstore, queue en locks.

### Leader election

De scheduler neemt bij het opstarten `pg_try_advisory_lock(<SCHEDULER_LOCK_ID>)` op een
eigen, langlevende connectie. Alleen de lock-houder start APScheduler. Anderen proberen
het elke 10 seconden opnieuw. Valt de connectie weg, dan stopt de scheduler direct.

### Queue

De worker claimt via:

```sql
SELECT id FROM runs
WHERE status = 'queued'
ORDER BY created_at
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

Daarna gaat de status naar `running` met `worker_id` en `started_at`. Pollen gebeurt met
een interval (default 2s), eventueel later aangevuld met `LISTEN/NOTIFY`.

### Overlap

Per template neemt de worker `pg_try_advisory_xact_lock(template_id)` of een
sessie-lock tijdens de run. De policy staat op de schedule:

- `skip`: de nieuwe run krijgt status `skipped` met een reden.
- `queue`: de run blijft `queued` tot de lock vrij is.

## Datamodel

**projects**
id, name, git_url, branch, credential_id (nullable), created_at, updated_at

**inventories**
id, name, project_id (nullable), source_type (`project_file` | `inline`),
path (bij project_file), content (bij inline), created_at, updated_at

**credentials**
id, name, type (`ssh_key` | `vault_password` | `git_token`), openbao_path, openbao_key,
created_at. Alleen referenties, nooit secretwaarden.

**templates**
id, name, project_id, playbook_path, inventory_id, extra_vars (jsonb), limit, tags,
skip_tags, verbosity, machine_credential_id, vault_credential_id (nullable), timeout_s,
created_at, updated_at

**schedules**
id, template_id, cron, timezone, enabled, overlap_policy (`skip` | `queue`),
misfire_grace_s, extra_vars_override (jsonb), created_at, updated_at

**runs**
id, template_id, schedule_id (nullable, vanaf fase 2), triggered_by (`schedule` | `user:<sub>`),
status, extra_vars (jsonb, effectief), limit (effectief), commit_sha, worker_id, created_at,
started_at, finished_at, cancel_requested_at, rc, status_reason,
stats (jsonb: ok/changed/failed/unreachable/skipped/rescued/ignored per host)

`extra_vars` en `limit` zijn de effectieve launch-parameters (template samengevoegd met
de overrides uit `/launch`). `cancel_requested_at` wordt gezet door `/cancel` bij een
lopende run; de worker pikt dat op via `cancel_callback`.

**run_events**
id (bigserial), run_id, seq, event, host, task, created_at, stdout, data (jsonb, gefilterd)

### Run-statussen

`queued` → `running` → `successful` | `failed` | `error` | `timeout` | `canceled`
`queued` → `skipped` | `canceled`

## Uitvoering van een run

1. Claim de run (zie Queue) en neem de overlap-lock.
2. Clone of fetch de repo in de cache (`SCHED_REPO_CACHE_DIR/<project_id>`), check de
   branch-HEAD uit in een per-run worktree en sla `commit_sha` op.
3. Maak de private data dir onder `SCHED_RUNTIME_DIR/<run_id>` aan. De worktree staat in
   `<run_id>/project`, een inline inventory in `<run_id>/inventory/hosts`. Er worden geen
   `env/`-bestanden geschreven (`suppress_env_files`): extravars gaan als argument mee,
   de SSH-key via de FIFO van ansible-runner naar ssh-agent, en een vault-wachtwoord als
   `--vault-password-file` (0600) in de private data dir.
4. `ansible_runner.run(..., event_handler=..., cancel_callback=..., timeout=...)` met
   `process_isolation=False`.
5. De event_handler filtert en schrijft batchgewijs naar `run_events` (50 events of 1s).
   Hij geeft `False` terug, zodat ansible-runner de ongefilterde events niet naar disk
   schrijft. De stats komen uit het `playbook_on_stats`-event.
6. Afronding: status, rc, stats en `finished_at`. Verwijder de worktree en de private
   data dir in `finally`.
7. Bij SIGTERM claimt de worker geen nieuwe runs meer en laat hij de lopende run
   afronden binnen de grace period.

## API (v1)

```
GET/POST        /api/v1/projects
GET/PUT/DELETE  /api/v1/projects/{id}
(zelfde CRUD voor inventories, credentials, templates, schedules)

POST  /api/v1/templates/{id}/launch     body: extra_vars, limit (optioneel)
GET   /api/v1/runs                      filters: template_id, status, since
GET   /api/v1/runs/{id}
GET   /api/v1/runs/{id}/events          paginated
GET   /api/v1/runs/{id}/stream          SSE, live events
POST  /api/v1/runs/{id}/cancel

GET   /healthz                          liveness
GET   /readyz                           db-connectie
GET   /metrics                          (fase 4)
```

## Configuratie (env, prefix `SCHED_`)

| Variabele               | Default                        |
|-------------------------|--------------------------------|
| `SCHED_DATABASE_URL`    | —                              |
| `SCHED_RUNTIME_DIR`     | `/run/scheduler`               |
| `SCHED_REPO_CACHE_DIR`  | `/var/cache/scheduler/repos`   |
| `SCHED_WORKER_ID`       | hostname                       |
| `SCHED_POLL_INTERVAL_S` | `2`                            |
| `SCHED_LOG_LEVEL`       | `INFO`                         |
| `SCHED_API_HOST`        | `0.0.0.0`                      |
| `SCHED_API_PORT`        | `8000`                         |
| `SCHED_DEV_SECRETS_DIR` | — (fase 1, vervalt in fase 3)  |
| `SCHED_ANSIBLE_HOST_KEY_CHECKING` | `true`               |
| `SCHED_OPENBAO_ADDR`    | — (fase 3)                     |
| `SCHED_OPENBAO_ROLE_ID` | — (fase 3)                     |
| `SCHED_OPENBAO_SECRET_ID` | — (fase 3)                   |
| `SCHED_OIDC_ISSUER`     | — (fase 3)                     |
| `SCHED_OIDC_AUDIENCE`   | — (fase 3)                     |

## Dev-omgeving (`compose.dev.yml`)

- `postgres`: Postgres 16
- `migrate`, `api`, `scheduler`, `worker`: gebouwd uit de lokale Dockerfile
- `ssh-target`: Debian-container met sshd en python3, key-auth met een gegenereerde
  dev-key. Dit is het enige doel voor integratietests.
- `tests/fixtures/repo/`: test-playbooks als gewone bestanden (ping, een bestand
  plaatsen, een bewust falende task, een lange sleep voor timeout en cancel, no_log).
  De init-container `fixture-repo` bouwt daaruit een bare repo in het `fixtures`-volume
  (`file:///fixtures/repo.git`).
- `dev`: tooling-container (profiel `tools`) met ruff, mypy en pytest. Hij heeft de
  broncode gemount en draait in het compose-netwerk.
- De runtime-dir is een named tmpfs-volume, gedeeld tussen workers en read-only met
  `dev` (voor de cleanup-tests).
- Credentials in fase 1: `DevFileResolver` leest
  `SCHED_DEV_SECRETS_DIR/<openbao_path>/<openbao_key>`. `scripts/dev-keys.sh` genereert
  de SSH-key en een vault-wachtwoord in `.dev/secrets`.

---

## Fase 1 — MVP: handmatige runs

**Scope:** projectstructuur, config, db, models en migraties voor projects,
inventories, credentials (dev: lokale key-referentie), templates, runs en run_events.
Worker met ansible-runner, API voor CRUD, launch, runs en events. Compose-bestanden
en Dockerfile. Nog geen auth, schedules of OpenBao.

**Acceptatiecriteria**
- `docker compose -f compose.dev.yml up` start alle services en `migrate` slaagt.
- Een template tegen `ssh-target` starten via `POST /launch` levert een run op met
  status `successful`, een correcte `commit_sha`, stats en events.
- De falende playbook geeft `failed` met rc ≠ 0.
- Timeout en cancel werken en geven `timeout` en `canceled`.
- De private data dir bestaat na afloop niet meer, ook niet na een exception.
- Twee workers claimen nooit dezelfde run (integratietest met `--scale worker=2`).
- ruff, mypy en pytest zijn groen.

## Fase 2 — Scheduling

**Scope:** schedules-model en -API, APScheduler met Postgres-jobstore, leader election,
synchronisatie tussen de schedules-tabel en APScheduler-jobs (create, update, delete,
enable), overlap-policies en misfire-afhandeling.

**Acceptatiecriteria**
- Een schedule met cron `* * * * *` levert elke minuut precies één run op, ook met
  `--scale scheduler=2`.
- Kill de leider: een andere replica neemt het binnen 30s over, zonder dubbele runs.
- `skip` geeft een run met status `skipped` als de vorige nog loopt. `queue` wacht.
- Een wijziging van een schedule via de API is actief zonder herstart.
- DST-overgang: een unit test met tijdzone `Europe/Amsterdam` vuurt correct.

## Fase 3 — Secrets en auth

**Scope:** een OpenBao-client (AppRole-login, token renew), credential resolutie in de
worker, Keycloak OIDC-validatie (JWT via JWKS) en RBAC op client roles `viewer`,
`operator` en `admin`. Een audit log-tabel en `triggered_by` met subject.

**Acceptatiecriteria**
- De worker haalt SSH-key en vault-password just-in-time op. Niets daarvan staat in
  de db, logs of events (test met een bekende secret-string).
- `viewer` kan alleen lezen, `operator` kan launchen en cancelen, `admin` kan
  configuratie wijzigen.
- Een verlopen of ongeldige token geeft 401, een ontbrekende rol 403.
- In dev: OpenBao in dev-mode en een Keycloak-container in `compose.dev.yml`.

## Fase 4 — UI en observability

**Scope:** een frontend (runs-overzicht, live log-view, templates en schedules
beheren), `/metrics` met Prometheus (run-duur-histogram, runs per status per template,
queue-diepte, laatste succesvolle run per schedule) en webhook-notificaties bij
`failed`, `error` en `timeout`.

**Acceptatiecriteria**
- Het live log in de UI volgt een lopende run via SSE.
- De metrics zijn scrapebaar en worden bijgewerkt bij statuswijzigingen.
- De webhook stuurt JSON met run-id, template, status en een link. Retry met backoff.

## Fase 5 — Hardening en productie

**Scope:** een productie-`compose.yml`, CI-pipeline (lint, test, build, push naar
Harbor), een Ansible-rol `deploy/roles/ansible_scheduler`, een retentie-job voor
`run_events` en oude runs, een backup van Postgres (`pg_dump`) en TLS via een reverse
proxy.

**Acceptatiecriteria**
- Een verse LXC/VM komt met één playbook-run van de rol volledig op, met een groene
  healthcheck.
- Een image-update via `-e image_tag=<sha>` onderbreekt geen lopende run.
- Retentie en backup draaien als ingeplande taak en zijn getest met restore.

---

## Open punten

- **Overlap-lock (fase 2):** gebruik een sessie-lock (`pg_try_advisory_lock(template_id)`)
  op een eigen connectie die de hele run openblijft, geen `pg_try_advisory_xact_lock`:
  een run beslaat meerdere transacties. In fase 1 draaien handmatige runs van hetzelfde
  template parallel.
- **Runs van verdwenen workers:** een worker zet bij het opstarten alleen zijn eigen
  `running` runs op `error`. Met een willekeurige container-hostname als `worker_id`
  blijven runs van een gecrashte, niet-herstarte worker op `running` staan. Nodig:
  heartbeat-kolom plus reaper (fase 2 of 5).
- **SSE `/runs/{id}/stream`:** verschoven naar fase 4 (acceptatiecriterium daar).
- **Host key checking in productie:** in dev staat het uit. Voor productie is een
  known_hosts-beheer nodig (per inventory of project), anders falen runs of moet
  checking uit (fase 3 of 5).
- **Git-credentials:** `projects.credential_id` (type `git_token`) wordt nog niet
  gebruikt; alleen publieke of `file://`-repo's werken. Oppakken in fase 3 met OpenBao.
- **Remote processen bij cancel/timeout:** ansible-runner stopt het lokale
  ansible-proces; een lopend commando op de target (bv. `sleep`) loopt daar door.
- **Productie-credentials:** tot fase 3 heeft `compose.yml` geen credential-backend.
- **ssh-agent-melding:** het eerste event bevat `Identity added: .../ssh_key_data (<comment>)`.
  Onschuldig, maar de key-comment is zichtbaar; eventueel wegfilteren.
