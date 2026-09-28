# syntax=docker/dockerfile:1
# Stages: system (OS + runtime-dependencies) -> dev-deps (+ tooling) -> dev (+ code, tests)
#                                            -> runtime (+ code; default target)
# Dependencies staan vóór de code, zodat een codewijziging geen pip install triggert.
FROM docker.io/library/python:3.12-slim AS system

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIT_TERMINAL_PROMPT=0 \
    ANSIBLE_LOCAL_TEMP=/tmp/ansible-local \
    ANSIBLE_REMOTE_TEMP=/tmp/.ansible-remote

RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home app \
    && install -d -o app -g app -m 0700 /run/lamplighter \
    && install -d -o app -g app /var/cache/lamplighter/repos /fixtures

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

ENTRYPOINT ["tini", "--", "python", "-m", "app"]
CMD ["api"]

# Tooling (ruff, mypy, pytest) voor het dev-image.
FROM system AS dev-deps
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

# Dev-image: in compose.dev.yml wordt de broncode ook gemount, zodat wijzigingen
# zonder rebuild zichtbaar zijn (herstart volstaat).
FROM dev-deps AS dev
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests
# Bestanden die in de dev-container zijn aangemaakt (bv. Alembic-revisies) kunnen via de
# virtiofs-mount op de host 0600 zijn; het image moet ze als 'app' kunnen lezen.
RUN chmod -R a+rX /app
USER app

FROM system AS runtime
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
RUN chmod -R a+rX /app
USER app
