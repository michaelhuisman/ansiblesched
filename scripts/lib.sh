# Gedeelde helpers voor de host-scripts. Container-runtime: $CONTAINER, anders podman als
# die er is (lokaal), anders docker (CI).
if [ -z "${CONTAINER:-}" ]; then
    if command -v podman >/dev/null 2>&1; then CONTAINER=podman; else CONTAINER=docker; fi
fi
COMPOSE="$CONTAINER compose -f compose.dev.yml"
