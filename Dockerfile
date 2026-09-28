# syntax=docker/dockerfile:1
FROM docker.io/library/python:3.12-slim AS base

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
    && install -d -o app -g app -m 0700 /run/scheduler \
    && install -d -o app -g app /var/cache/scheduler/repos /fixtures

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
# Bestanden die in de dev-container zijn aangemaakt (bv. Alembic-revisies) kunnen via de
# virtiofs-mount op de host 0600 zijn; het image moet ze als 'app' kunnen lezen.
RUN chmod -R a+rX /app

USER app
ENTRYPOINT ["tini", "--", "python", "-m", "app"]
CMD ["api"]

# Dev-image: tooling (ruff, mypy, pytest) en de tests. De broncode wordt in dev
# gemount, zodat wijzigingen zonder rebuild zichtbaar zijn.
FROM base AS dev
USER root
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY pyproject.toml ./
COPY tests ./tests
USER app
