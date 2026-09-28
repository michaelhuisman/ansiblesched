# CLAUDE.md — lamplighter

Applicatie die Ansible-playbooks op schema en on-demand uitvoert. Eén codebase en één
image met drie rollen: `api`, `scheduler` en `worker` (plus een one-shot `migrate`).
Namen: compose-project en image `lamplighter`, env prefix `LAMPLIGHTER_`, paden
`/run/lamplighter` en `/var/cache/lamplighter`. Het volledige ontwerp en de fasering staan in `docs/plan.md`. Lees dat bestand vóór je
aan een fase begint.

## Stack (vastgepind, niet afwijken zonder overleg)

- Python 3.12
- FastAPI + uvicorn (API)
- Jinja2 + htmx voor de UI (server-rendered; htmx vendored, geen Node-toolchain)
- prometheus-client (metrics), httpx (webhooks)
- SQLAlchemy 2.x, **synchrone** sessies, `psycopg` 3 als driver
- Alembic voor alle schemawijzigingen
- APScheduler 3.x met `SQLAlchemyJobStore` op Postgres
- ansible-core (vastgepinde versie in `requirements.txt`) + ansible-runner
- pydantic v2 + pydantic-settings voor config
- PyJWT (OIDC/JWKS), argon2-cffi (wachtwoorden lokale gebruikers)
- hvac voor OpenBao (vanaf fase 4)
- pytest, ruff (lint + format), mypy (strict op `app/`)
- Postgres 18
- Docker Compose voor dev en productie

## Structuur

```
app/
  __main__.py      # python -m app {api|scheduler|worker|migrate}
  api/             # FastAPI routers, schemas (pydantic)
  ui/              # server-rendered UI: Jinja2-templates, htmx (vendored) en statics
  scheduler/       # APScheduler setup, leader election, schedule -> run enqueue
  worker/          # queue consumer, ansible-runner wrapper, git checkout
  models/          # SQLAlchemy models
  services/        # businesslogica, los van API en worker
  core/            # config, db session, logging, (later) auth en openbao client
migrations/        # Alembic
tests/
  unit/
  integration/     # draait tegen compose.dev.yml
docs/plan.md
compose.yml        # productie
compose.dev.yml    # dev: postgres, ssh-target, keycloak, openbao, git-http, webhook-sink
```

## Commando's

Containers draaien met Podman (`podman compose`), niet met Docker Desktop. Tooling
draait in de `dev`-container (Python 3.12, broncode gemount); lokaal is geen 3.12 nodig.

```bash
# dev-omgeving (eenmalig: scripts/dev-keys.sh)
podman compose -f compose.dev.yml up -d --build --scale worker=2 --scale scheduler=2
# na een codewijziging: herstart volstaat (app/ en migrations/ zijn gemount)
podman compose -f compose.dev.yml restart api worker scheduler
# eerste lokale admin (wachtwoord via prompt)
podman compose -f compose.dev.yml run --rm dev python -m app create-user admin --role admin

# kwaliteit, in de dev-container
DEV="podman compose -f compose.dev.yml run --rm dev"
$DEV sh -c 'ruff check . && ruff format --check .'
$DEV mypy app
$DEV pytest tests/unit
$DEV pytest tests/integration     # vereist draaiende compose.dev.yml
$DEV pytest tests/integration -m "not slow"   # zonder de tests die minuten op cron wachten
scripts/it-failover.sh            # op de host: kill de scheduler-leider, check takeover
scripts/it-secret-scan.sh         # op de host: geen dev-secrets in de containerlogs
# scripts kiezen de runtime via $CONTAINER (podman als die er is, anders docker)

# migraties
$DEV alembic revision --autogenerate -m "<omschrijving>"
podman compose -f compose.dev.yml run --rm migrate
```

## CI

GitHub Actions (`.github/workflows/ci.yml`): lint, unit, integratie (compose.dev.yml
met Docker, inclusief de trage tests, failover en secret-scan) en het image naar GHCR
(`ghcr.io/<owner>/lamplighter`, alleen `main` en `v*`-tags). Actions pin je op commit-SHA.

## Conventies

- Volledige type hints. Geen `Any` zonder reden.
- Config uitsluitend via `app/core/config.py` (pydantic-settings, env prefix `LAMPLIGHTER_`).
  Nergens `os.environ` direct lezen.
- Businesslogica in `app/services/`. Routers en de worker-loop zijn dun.
- Elke schemawijziging krijgt een Alembic-migratie. Nooit `metadata.create_all()`
  buiten tests.
- Tijd altijd timezone-aware in UTC opslaan. Een schedule heeft een eigen tijdzoneveld.
- Logging via stdlib `logging` in JSON-formaat naar stdout. Geen `print`.
- UI-teksten zijn Engels; code-commentaar en docs zijn Nederlands.
- De worker voert ansible-runner uit zonder process isolation
  (`process_isolation=False`). Ansible draait direct in de worker-container.
- De private data dir van ansible-runner staat onder `LAMPLIGHTER_RUNTIME_DIR` (tmpfs) en
  wordt na elke run verwijderd, ook bij een exception.
- Queue-claims gaan via `SELECT ... FOR UPDATE SKIP LOCKED`. Overlap per template en
  leader election gaan via Postgres advisory locks. Geen extra infra (Redis e.d.).

## Veiligheidsregels tijdens het bouwen

- Nooit echte hosts, echte inventories of echte OpenBao aanspreken. Integratietests
  draaien uitsluitend tegen de `ssh-target` container uit `compose.dev.yml`.
- Geen secrets committen. Dev-keys worden gegenereerd door `scripts/dev-keys.sh`
  en staan in `.gitignore`.
- Secrets nooit in de database, logs of run_events. Credentials zijn referenties.
  ansible-runner events worden vóór opslag gefilterd op `no_log` en bekende
  secret-velden.

## Werkwijze

- Werk per fase uit `docs/plan.md`. Maak eerst een plan en wacht op akkoord.
- Een fase is klaar als alle acceptatiecriteria van die fase gehaald zijn en
  ruff, mypy en pytest (unit + integration) groen zijn.
- Houd wijzigingen binnen de scope van de fase. Signaleer zaken voor latere fases
  in `docs/plan.md` onder "Open punten" in plaats van ze direct te bouwen.
