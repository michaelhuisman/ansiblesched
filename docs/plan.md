# lamplighter — ontwerp en fasering

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

De scheduler neemt bij het opstarten `pg_try_advisory_lock(0x5343, 1)` op een eigen,
langlevende connectie. Alleen de lock-houder start APScheduler. Anderen proberen het
elke 10 seconden opnieuw (`LAMPLIGHTER_SCHEDULER_LOCK_RETRY_S`). Dezelfde connectie doet
`LISTEN schedules_changed` en dient als health-check. Valt de connectie weg, dan stopt
APScheduler direct en gaat de replica terug naar follower-modus.

Postgres staat op korte TCP-keepalives. Een bevroren leider (die niet crasht) verliest de
lock zo binnen ongeveer 15 seconden.

Vangrail: `runs(schedule_id, scheduled_for)` is uniek en inserts gebruiken
`ON CONFLICT DO NOTHING`. Ook als twee replicas kort allebei leider denken te zijn,
ontstaat per afvuring maar één run.

### Schedules → APScheduler-jobs

De `schedules`-tabel is de bron van waarheid en de jobs worden daaruit afgeleid.
De leider reconcilieert na elke `NOTIFY schedules_changed` (die de API na elke wijziging
stuurt) en daarnaast elke `LAMPLIGHTER_SCHEDULER_SYNC_INTERVAL_S` (5s):

- job-id `schedule:<id>`, job-naam = fingerprint van cron, timezone en misfire_grace_s;
- nieuw of gewijzigd: `add_job(replace_existing=True)`. Ongewijzigde jobs blijven staan,
  zodat hun `next_run_time` behouden blijft (nodig voor misfire-detectie na een failover);
- uitgeschakeld of verwijderd: `remove_job`.

De job leidt `scheduled_for` af uit de trigger: het laatste fire-moment ≤ nu. Dat is
deterministisch, dus twee leiders komen op dezelfde waarde uit. `coalesce=True`: na
downtime vuurt een job één keer. Een afvuring buiten de `misfire_grace_s` levert een
`skipped` run met reden `missed: scheduler unavailable`.

De tabel `apscheduler_jobs` wordt via Alembic aangemaakt. De jobstore doet zelf
`create(checkfirst=True)`, en dat is dan een no-op.

### Cron-semantiek

Cron-expressies hebben 5 velden en worden geïnterpreteerd zoals Vixie cron:

- weekdag 0 en 7 = zondag, 1 = maandag. APScheduler's `from_crontab` gebruikt
  0 = maandag, daarom vertalen we het veld naar namen;
- dag-van-de-maand én weekdag tegelijk beperken wordt geweigerd. Cron combineert die met
  OF, APScheduler met EN;
- DST, uur-veld met wildcard of stap (`*`, `*/2`): de job vuurt elk werkelijk uur, dus in
  het dubbele herfstuur twee keer;
- DST, vast uur (`30 2 * * *`): de job vuurt één keer per dag. In het voorjaar vuurt een
  niet-bestaande tijd direct na de sprong.

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

Per template houdt de worker tijdens de run een sessie-lock vast:
`pg_try_advisory_lock(0x5450, template_id)` op een eigen connectie. Een xact-lock volstaat
niet, want een run beslaat meerdere transacties. De policy wordt bij het aanmaken
van de run gekopieerd naar `runs.overlap_policy`:

- `skip`: de nieuwe run krijgt status `skipped` met een reden. De scheduler controleert
  dit al bij het aanmaken (template-lock in `pg_locks` of een eerdere run in de queue).
  De worker controleert het nog eens bij het claimen.
- `queue`: de run blijft `queued` tot de lock vrij is. De claim slaat hem over en pakt de
  volgende kandidaat, zodat andere templates niet blokkeren.
- Handmatige runs gedragen zich als `queue`.

Leader-lock en template-locks gebruiken de vorm met twee int4-waarden (namespace, id):
`0x5343` voor de scheduler en `0x5450` voor templates.

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
misfire_grace_s (default 60), extra_vars_override (jsonb), created_at, updated_at

**runs**
id, template_id, schedule_id (nullable, `ON DELETE SET NULL`), scheduled_for (nullable),
overlap_policy, triggered_by (`schedule` | `user:<sub>`),
status, extra_vars (jsonb, effectief), limit (effectief), commit_sha, worker_id, created_at,
started_at, finished_at, cancel_requested_at, rc, status_reason,
stats (jsonb: ok/changed/failed/unreachable/skipped/rescued/ignored per host)

`extra_vars` en `limit` zijn de effectieve launch-parameters (template samengevoegd met
de overrides uit `/launch`). `cancel_requested_at` wordt gezet door `/cancel` bij een
lopende run; de worker pikt dat op via `cancel_callback`.

**run_events**
id (bigserial), run_id, seq, event, host, task, created_at, stdout, data (jsonb, gefilterd)

**users**
id, source (`local` | `oidc`), username (bij OIDC: `sub`), display_name, email,
password_hash (argon2id, alleen lokaal), roles (alleen lokaal), disabled,
failed_logins, locked_until, created_at, last_login_at

**sessions** (UI)
id, token_hash (sha256 van het cookie-id), user_id, csrf_token, oidc_roles (snapshot bij
login), created_at, last_seen_at, expires_at

**api_tokens** (lokale gebruikers)
id, user_id, name, token_hash (sha256), prefix, created_at, expires_at, last_used_at,
revoked_at

**audit_log**
id, at, actor (`user:local:<naam>` | `user:oidc:<sub>` | `cli`), action, object_type,
object_id, details (jsonb: veldnamen, geen waarden of secrets), ip

**notifications** (outbox voor webhooks)
id, run_id, target (fingerprint van de webhook-URL, nooit de URL zelf), event,
status (`pending` | `sent` | `failed`), attempts, next_attempt_at, last_error,
created_at, sent_at

### Run-statussen

`queued` → `running` → `successful` | `failed` | `error` | `timeout` | `canceled`
`queued` → `skipped` | `canceled`

## Uitvoering van een run

1. Claim de run (zie Queue) en neem de overlap-lock.
2. Clone of fetch de repo in de cache (`LAMPLIGHTER_REPO_CACHE_DIR/<project_id>`), check de
   branch-HEAD uit in een per-run worktree en sla `commit_sha` op.
3. Maak de private data dir onder `LAMPLIGHTER_RUNTIME_DIR/<run_id>` aan. De worktree staat in
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
(zelfde CRUD voor inventories, credentials, templates, schedules;
 een schedule heeft daarnaast een berekend veld `next_run_at`)

POST  /api/v1/templates/{id}/launch     body: extra_vars, limit (optioneel)
GET   /api/v1/runs                      filters: template_id, status, since
GET   /api/v1/runs/{id}
GET   /api/v1/runs/{id}/events          paginated
GET   /api/v1/runs/{id}/stream          SSE, live events
POST  /api/v1/runs/{id}/cancel

GET   /healthz                          liveness
GET   /readyz                           db-connectie
GET   /api/v1/me                        eigen identiteit en rollen
GET/POST/DELETE /api/v1/tokens          eigen API-tokens (lokale gebruikers)
GET/POST/PATCH  /api/v1/users           gebruikersbeheer (admin)
PUT   /api/v1/users/{id}/password       wachtwoord resetten (admin)
GET   /api/v1/audit                     audit log (admin)

GET   /metrics                          Prometheus (fase 3; open, zie Open punten)

GET   /ui/...                           server-rendered UI (Jinja2 + htmx)
```

## Configuratie (env, prefix `LAMPLIGHTER_`)

| Variabele               | Default                        |
|-------------------------|--------------------------------|
| `LAMPLIGHTER_DATABASE_URL`    | —                              |
| `LAMPLIGHTER_RUNTIME_DIR`     | `/run/lamplighter`               |
| `LAMPLIGHTER_REPO_CACHE_DIR`  | `/var/cache/lamplighter/repos`   |
| `LAMPLIGHTER_WORKER_ID`       | hostname                       |
| `LAMPLIGHTER_POLL_INTERVAL_S` | `2`                            |
| `LAMPLIGHTER_LOG_LEVEL`       | `INFO`                         |
| `LAMPLIGHTER_API_HOST`        | `0.0.0.0`                      |
| `LAMPLIGHTER_API_PORT`        | `8000`                         |
| `LAMPLIGHTER_ANSIBLE_HOST_KEY_CHECKING` | `true`               |
| `LAMPLIGHTER_SCHEDULER_LOCK_RETRY_S` | `10`                    |
| `LAMPLIGHTER_SCHEDULER_SYNC_INTERVAL_S` | `5`                  |
| `LAMPLIGHTER_PUBLIC_URL`      | `http://localhost:8000`        |
| `LAMPLIGHTER_WEBHOOK_OPENBAO_PATH` | — (leeg = geen webhooks) |
| `LAMPLIGHTER_WEBHOOK_CACHE_S` | `60`                           |
| `LAMPLIGHTER_AUTH_LOCAL_ENABLED` | `true`                      |
| `LAMPLIGHTER_SESSION_COOKIE_SECURE` | `true` (dev: `false`)    |
| `LAMPLIGHTER_SESSION_IDLE_S`  | `28800` (8 uur)                |
| `LAMPLIGHTER_SESSION_MAX_S`   | `86400` (24 uur)               |
| `LAMPLIGHTER_LOGIN_MAX_FAILURES` | `5`                         |
| `LAMPLIGHTER_LOGIN_LOCKOUT_S` | `900`                          |
| `LAMPLIGHTER_OIDC_DISCOVERY_URL` | — (default: van issuer)     |
| `LAMPLIGHTER_OIDC_CLIENT_ID`  | `lamplighter`            |
| `LAMPLIGHTER_OIDC_CLIENT_SECRET` | —                           |
| `LAMPLIGHTER_OPENBAO_ADDR`    | — (worker en scheduler)        |
| `LAMPLIGHTER_OPENBAO_ROLE_ID` | — (AppRole per rol)            |
| `LAMPLIGHTER_OPENBAO_SECRET_ID` | —                            |
| `LAMPLIGHTER_OPENBAO_KV_MOUNT` | `secret`                      |
| `LAMPLIGHTER_OPENBAO_CA_CERT` | — (systeem-CA's)               |
| `LAMPLIGHTER_OIDC_ISSUER`     | — (leeg = alleen lokale login) |
| `LAMPLIGHTER_OIDC_AUDIENCE`   | — (default: client-id)         |
| `LAMPLIGHTER_TRUSTED_PROXIES` | `127.0.0.1` (IP's/CIDR's, komma-gescheiden) |
| `LAMPLIGHTER_METRICS_TOKEN`   | — (leeg = `/metrics` open)     |
| `LAMPLIGHTER_RETENTION_EVENTS_DAYS` | `30` (0 = nooit)         |
| `LAMPLIGHTER_RETENTION_RUNS_DAYS`   | `180` (0 = nooit)        |
| `LAMPLIGHTER_RETENTION_AUDIT_DAYS`  | `365` (0 = nooit)        |
| `LAMPLIGHTER_RETENTION_TOKENS_DAYS` | `30` (0 = nooit)         |
| `LAMPLIGHTER_RETENTION_HOUR_UTC`    | `3`                      |
| `LAMPLIGHTER_REAPER_GRACE_S`  | `120`                          |
| `LAMPLIGHTER_REAPER_INTERVAL_S` | `60`                         |

## Dev-omgeving (`compose.dev.yml`)

- `postgres`: Postgres 18 (image `postgres:18.6`). Het volume hoort op `/var/lib/postgresql`;
  vanaf 18 staat PGDATA in `/var/lib/postgresql/18/docker`. Een upgrade vanaf een ouder
  major-versie gaat via `pg_dump`/restore of `pg_upgrade`, niet door alleen het image te wisselen.
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
  `LAMPLIGHTER_DEV_SECRETS_DIR/<openbao_path>/<openbao_key>`. `scripts/dev-keys.sh` genereert
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

## Fase 3 — UI en observability

_Volgorde omgedraaid t.o.v. het oorspronkelijke plan: de UI eerst, omdat die voorlopig
alleen in dev draait. Auth-naden (`current_user`) zitten er vanaf fase 3 al in._

**Scope:** een frontend (runs-overzicht, live log-view, templates en schedules
beheren), `/metrics` met Prometheus (run-duur-histogram, runs per status per template,
queue-diepte, laatste succesvolle run per schedule) en webhook-notificaties bij
`failed`, `error` en `timeout`.

**Uitwerking**
- **UI:** Engelstalig, server-rendered met Jinja2 en htmx (2.0.11, vendored in `app/ui/static`, geen
  Node-toolchain). Onder `/ui`. Formulieren valideren met dezelfde pydantic-schema's als
  de API.
- **Beheer in de UI:** runs, templates, schedules, projecten, inventories en credentials
  (alleen referenties naar OpenBao; de UI vraagt nooit om secretwaarden). Voor admins
  ook gebruikers en de audit log.
- **SSE:** `GET /api/v1/runs/{id}/stream` stuurt `run_event` (id = seq), `status` en
  `end`. De server pollt `run_events` elke 0,5s. Herverbinden gaat verder vanaf
  `Last-Event-ID`. In de UI via `EventSource`; in fase 4 werkt dat met een sessiecookie
  zonder aanpassingen.
- **Metrics:** een eigen collector berekent bij elke scrape alles uit Postgres. Zo zijn
  de waarden gelijk over api, scheduler en worker-replicas en altijd actueel.
- **Webhooks:** een outbox-tabel `notifications`, gevuld in dezelfde transactie als het
  afronden van de run. De scheduler-leider verstuurt elke 5s (`SKIP LOCKED`) met backoff
  5s → 10s → … max 10 min, en geeft na 10 pogingen op (`failed`). Optioneel een
  HMAC-SHA256-signatuur in `X-Lamplighter-Signature`.
- **Auth-naad:** `current_user()` geeft in fase 3 een anonieme admin terug. Routes en
  templates checken `Principal.can(action)`.

**Acceptatiecriteria**
- Het live log in de UI volgt een lopende run via SSE.
- De metrics zijn scrapebaar en worden bijgewerkt bij statuswijzigingen.
- De webhook stuurt JSON met run-id, template, status en een link. Retry met backoff.

## Fase 4 — Secrets en auth

_Geleverd in twee delen: **4a** auth (lokale gebruikers, API-tokens, Keycloak, RBAC,
audit, CSRF) en **4b** OpenBao (credentials, git-tokens, webhook-URL's)._

**Uitwerking 4a**
- **Identiteiten:** lokale gebruikers (rollen in de DB, argon2id, lockout na 5 fouten)
  en Keycloak-gebruikers (rollen uit de token, client roles van `lamplighter`;
  aangemaakt bij de eerste login). Beide bronnen zijn apart aan en uit te zetten.
  `triggered_by` is `user:local:<naam>` of `user:oidc:<sub>`.
- **UI-sessies:** een server-side sessie in de DB met een cookie (`HttpOnly`,
  `SameSite=Lax`, `Secure`), een idle-timeout van 8 uur en maximaal 24 uur. CSRF gaat
  via een token per sessie (formulierveld of `X-CSRF-Token`) plus een Origin-check.
  `EventSource` stuurt de cookie mee.
- **OIDC-login:** authorization code met PKCE, server-side (confidential client).
  `state`, `nonce` en de verifier staan in een kortlevende cookie. Het id_token en het
  access token worden gevalideerd via JWKS. Zonder rol wordt de login geweigerd.
- **API:** accepteert de sessiecookie (met CSRF), een lokale API-token
  (`Bearer lamplighter_…`, alleen de hash in de DB, met vervaldatum) of een Keycloak-JWT
  (`iss`, `aud` en `exp` gecontroleerd). Zonder geldige authenticatie volgt 401, zonder
  de juiste rol 403.
- **Rollen:** `viewer` (lezen), `operator` (plus launch en cancel), `admin` (plus
  configuratie, gebruikersbeheer en audit).
- **Audit:** vastgelegd in dezelfde transactie als de wijziging.
- **CLI:** `python -m app create-user <naam> --role admin` (wachtwoord via prompt of
  stdin) en `python -m app create-token <naam> --name <x>`.
- **Dev:** Keycloak 26.7.4 met een realm-import. Het client-secret en de
  testwachtwoorden worden door `scripts/dev-keys.sh` in `.dev/` gegenereerd.

**Uitwerking 4b**
- **OpenBao-client** (hvac): AppRole-login. Vóór elke read wordt de token vernieuwd
  zodra minder dan een derde van de TTL over is. Lukt vernieuwen niet, of is de max-TTL
  bereikt, dan volgt een nieuwe login; bij een 403 één keer opnieuw. Er wordt uit KV v2
  gelezen. Foutmeldingen bevatten alleen pad en key.
- **Padconventie en policies** (least privilege):

  | Pad (mount `secret`) | Inhoud | Leesbaar voor |
  |---|---|---|
  | `ssh/<naam>` | SSH-key onder `openbao_key` | worker |
  | `vault/<naam>` | vault-wachtwoord onder `openbao_key` | worker |
  | `git/<naam>` | token onder `openbao_key`, optioneel `username` (default `x-access-token`) | worker |
  | `webhooks/<naam>` | `urls` (JSON-lijst), optioneel `hmac_secret` | scheduler |

  De API leest geen secrets.
- **Git via https:** `projects.credential_id` verwijst naar een `git_token`-credential.
  Clone en fetch gaan via een askpass-script met bestanden (0600) in de private data dir.
  De token komt niet in de URL, de procesargumenten, de omgeving of de config van de
  cache-repo, en `credential.helper` staat leeg.
- **Webhooks:** de worker zet bij een fout-status één outbox-rij met `target='*'` en
  kent geen webhook-secrets. De scheduler-leider leest de config uit OpenBao (gecachet
  `LAMPLIGHTER_WEBHOOK_CACHE_S`) en splitst `*` uit naar één rij per URL-fingerprint. Is
  OpenBao niet bereikbaar, dan blijft de `*`-rij staan.
- **Dev:** OpenBao 2.7.0 in dev-mode met `openbao-init` (policies, AppRoles met TTL 60s
  en max 300s, dev-secrets). Daarnaast een `git-http`-container (git http-backend met
  basic-auth). `scripts/it-secret-scan.sh` doorzoekt de containerlogs op alle
  dev-secrets.

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

## Fase 5 — Hardening en productie

**Scope:** een productie-`compose.yml`, CI-pipeline (lint, test, build, push naar
Harbor), een Ansible-rol `deploy/roles/lamplighter`, een retentie-job voor
`run_events` en oude runs, een backup van Postgres (`pg_dump`) en TLS via een reverse
proxy.

_Geleverd in delen: **5a** hardening van de app; **5b** productie en deploy, met eerst
de CI (5b-1) en daarna de Ansible-rol, de productie-compose en de backup (5b-2)._

**Keuzes 5b**
- **Doelplatform:** containers via Docker Compose op een VM. Welk OS maakt niet uit, en
  Docker Engine met de compose-plugin is de standaard. Een Helm chart volgt in fase 6.
- **TLS:** de bestaande nginx op het systeem termineert TLS; de app zelf doet geen TLS.
  Er komt een voorbeeldconfig in `docs/deploy/nginx.conf`, met buffering uit voor SSE,
  de `X-Forwarded-*`-headers, een rate limit op de login en `/metrics` beperkt tot
  monitoring. `LAMPLIGHTER_TRUSTED_PROXIES` wijst naar het adres waarvandaan nginx
  binnenkomt; bij Docker-port-forwarding is dat meestal de bridge-gateway, niet
  `127.0.0.1`.
- **Postgres:** in de compose op dezelfde host.
- **Registry:** GHCR (`ghcr.io/<owner>/lamplighter`).

**Uitwerking 5b-1 (CI)**, GitHub Actions in `.github/workflows/ci.yml`, met de actions op
commit-SHA vastgezet:
- `lint`: ruff en mypy.
- `unit`: pytest `tests/unit`.
- `integration`: `compose.dev.yml` met Docker op de runner, met `dev-keys.sh`, de
  integratietests, de trage tests, `it-failover.sh` en `it-secret-scan.sh`. Bij een fout
  worden de containerlogs als artefact bewaard.
- `image`: alleen bij een push naar `main` of een `v*`-tag, en alleen na groene tests.
  Bouwt target `runtime` en pusht naar GHCR met als tags de volledige git-SHA, `latest`
  (op `main`) en de semver (bij een tag).
- De host-scripts kiezen de runtime via `$CONTAINER` (standaard Podman als die er is,
  anders Docker; zie `scripts/lib.sh`).

**Uitwerking 5a**
- **Retentie:** een dagelijkse interne job van de scheduler-leider
  (`LAMPLIGHTER_RETENTION_HOUR_UTC`), met een eigen advisory lock. Wat weg mag:
  - events van afgeronde runs
  - afgeronde runs, met cascade naar events en notificaties
  - de audit log
  - ingetrokken of verlopen API-tokens

  Alle termijnen zijn instelbaar, en `0` betekent nooit. Er wordt in batches van 5.000
  verwijderd. Lopende en wachtende runs blijven altijd staan.
- **Metrics na retentie:** vóór het verwijderen worden de tellingen per template en status
  (runs, duur, buckets) opgeteld in `run_stats_archive`, in dezelfde transactie.
  `lamplighter_runs_total` en het duur-histogram zijn live plus archief, dus Prometheus
  ziet geen counter-reset.
- **Reaper:** een run op `running` zonder databaseverbinding met
  `application_name = worker:<worker_id>` wordt na `LAMPLIGHTER_REAPER_GRACE_S`
  `error`, met de reden "worker lost" en een notificatie. Alle connecties van een rol
  hebben daarvoor `application_name = <rol>:<id>`.
- **Lock-connectie van de worker:** de `cancel_callback` controleert elke 5s de
  overlap-lock. Na een verbroken verbinding neemt de worker hem opnieuw. Lukt dat niet,
  omdat een andere run hem heeft, dan wordt de run `error` met de reden "overlap lock
  lost".
- **Proxy:** uvicorn met `proxy_headers` en `forwarded_allow_ips =
  LAMPLIGHTER_TRUSTED_PROXIES`. Audit en lockout zien zo het echte client-IP, en een
  vervalste `X-Forwarded-For` van een onbekende client wordt genegeerd.
- **`/metrics`:** met `LAMPLIGHTER_METRICS_TOKEN` is `Authorization: Bearer <token>`
  verplicht.
- **Host keys:**
  - Een `known_hosts`-credential (`ssh/<naam>`, key `known_hosts`) op een template dwingt
    strikte checking af: `StrictHostKeyChecking=yes` met een eigen `UserKnownHostsFile`.
  - Zonder `known_hosts` geldt `LAMPLIGHTER_ANSIBLE_HOST_KEY_CHECKING`. Staat die aan,
    dan wordt de run meteen geweigerd.
  - Elke run krijgt een eigen SSH-`ControlPath` (`ANSIBLE_SSH_CONTROL_PATH_DIR` in de
    private data dir). Anders zou een run via `ControlPersist` een masterverbinding van
    een andere run hergebruiken, en daarmee de host-key-controle omzeilen. De test voor
    een verkeerde host key liet dat zien.
- **Statische bestanden:** URL's krijgen een inhoudsversie (`?v=<hash>`), zodat browsers
  na een update niet uit hun cache blijven laden.

**Acceptatiecriteria**
- Een verse LXC/VM komt met één playbook-run van de rol volledig op, met een groene
  healthcheck.
- Een image-update via `-e image_tag=<sha>` onderbreekt geen lopende run.
- Retentie en backup draaien als ingeplande taak en zijn getest met restore.

---

## Fase 6 — Kubernetes (Helm)

**Scope:** een Helm chart als alternatief voor Docker Compose: deployments voor api,
scheduler en worker (met `terminationGracePeriodSeconds` voor lopende runs), de
migrate-job als hook, een PodDisruptionBudget, secrets via de OpenBao-integratie van het
cluster, en Postgres extern of via een operator.

---

## Open punten

- **SSE schaalt per thread:** elke open stream houdt een thread uit de threadpool bezet
  en pollt de database. Voor dev en kleine schaal is dat prima. Bij meer kijkers:
  `LISTEN/NOTIFY` op nieuwe events of een async generator (fase 5).
- **Uitloggen bij Keycloak:** de UI verwijdert alleen de eigen sessie. De SSO-sessie in
  Keycloak blijft bestaan, dus "Inloggen met Keycloak" logt meteen weer in. Oplossing:
  RP-initiated logout via het `end_session_endpoint` met `id_token_hint`.
- **Rolwijzigingen in Keycloak** gelden pas bij de volgende login: de sessie bewaart een
  snapshot, en die blijft maximaal 24 uur geldig. Bearer-tokens volgen direct, want ze
  leven maar 5 minuten.
- **`/healthz` en `/readyz` zijn open** (bedoeld voor load balancer en healthchecks).
  `/metrics` heeft sinds 5a een optioneel scrape-token; afschermen via de proxy komt in 5b.
- **Lockout per gebruikersnaam:** voorkomt brute force op één account. Een aanvaller kan
  daarmee wel een account tijdelijk blokkeren. Een rate limit per IP komt er in fase 5
  bij, via de proxy.
- **UI op smalle schermen:** de tabellen zijn voor desktop gemaakt en scrollen op een
  telefoon niet netjes.
- **Remote processen bij cancel/timeout:** ansible-runner stopt het lokale
  ansible-proces; een lopend commando op de target (bv. `sleep`) loopt daar door.
- **Secret-id's van AppRoles** staan nu als env-bestand op de host. Beter: response
  wrapping of een kortlevende secret-id per deploy, uitgedeeld door de Ansible-rol
  (fase 5).
- **Dev-OpenBao is in-memory:** herstart je alleen `openbao`, dan zijn de secrets weg
  tot `openbao-init` opnieuw draait (`podman compose up -d`).
