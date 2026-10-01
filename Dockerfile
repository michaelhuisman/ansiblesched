# syntax=docker/dockerfile:1
# Stages: system (OS + runtime dependencies) -> dev-deps (+ tooling) -> dev (+ code, tests)
#                                            -> runtime (+ code; default target)
# Dependencies come before the code, so a code change does not trigger a pip install.
FROM docker.io/library/python:3.12-slim AS system

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIT_TERMINAL_PROMPT=0 \
    ANSIBLE_LOCAL_TEMP=/tmp/ansible-local \
    ANSIBLE_REMOTE_TEMP=/tmp/.ansible-remote

# `upgrade`: Debian security fixes (e.g. OpenSSL) often land before a new base image does;
# the Trivy gate in CI fails on fixable HIGH/CRITICAL findings.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends git openssh-client tini \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --home-dir /home/app --create-home app \
    && install -d -o app -g app -m 0700 /run/lamplighter \
    && install -d -o app -g app /var/cache/lamplighter/repos /var/cache/lamplighter/collections \
        /fixtures

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Standard set of collections (collections/requirements.yml), on ansible-core's default
# search path. Projects can add their own via collections/requirements.yml in their repo.
COPY collections/requirements.yml /tmp/collections-requirements.yml
RUN ansible-galaxy collection install -r /tmp/collections-requirements.yml \
        -p /usr/share/ansible/collections \
    && chmod -R a+rX /usr/share/ansible/collections \
    && rm -rf /tmp/collections-requirements.yml /tmp/ansible-local /root/.ansible
# (ansible-galaxy runs as root here; its temp dir in ANSIBLE_LOCAL_TEMP would otherwise
# stay behind root-owned and break every Ansible command of the app user.)

ENTRYPOINT ["tini", "--", "python", "-m", "app"]
CMD ["api"]

# Tooling (ruff, mypy, pytest) for the dev image.
FROM system AS dev-deps
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

# Dev image: compose.dev.yml also mounts the source code, so changes show up
# without a rebuild (a restart is enough).
FROM dev-deps AS dev
COPY alembic.ini pyproject.toml ./
COPY migrations ./migrations
COPY app ./app
COPY tests ./tests
# Files created in the dev container (e.g. Alembic revisions) can be 0600 on the host
# via the virtiofs mount; the image must be able to read them as 'app'.
RUN chmod -R a+rX /app
USER app

FROM system AS runtime
COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app
RUN chmod -R a+rX /app
USER app
