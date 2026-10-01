#!/usr/bin/env sh
# Builds the bare git repos for dev and tests from tests/fixtures:
#   repo.git              playbooks (ping, fail, sleep, ...)
#   collections.git       a playbook plus collections/requirements.yml with a local test
#                         collection tarball (built here, no Galaxy access needed)
#   collections-bad.git   a requirements.yml that cannot be installed
#   build-fixtures.sh <tests/fixtures> <target-dir>
set -eu
src=$1
dest=$2
here=$(dirname "$0")
sh "$here/build-fixture-repo.sh" "$src/repo" "$dest/repo.git"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$src/collections-repo" "$work/collections-repo"
ansible-galaxy collection build "$src/collection-src" --output-path "$work/collections-repo/collections" >/dev/null
sh "$here/build-fixture-repo.sh" "$work/collections-repo" "$dest/collections.git"
sh "$here/build-fixture-repo.sh" "$src/collections-bad-repo" "$dest/collections-bad.git"
