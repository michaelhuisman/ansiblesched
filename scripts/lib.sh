# Shared helpers for the host scripts. Container runtime: $CONTAINER, otherwise podman if
# available (local), otherwise docker (CI).
if [ -z "${CONTAINER:-}" ]; then
    if command -v podman >/dev/null 2>&1; then CONTAINER=podman; else CONTAINER=docker; fi
fi
COMPOSE="$CONTAINER compose -f compose.dev.yml"
