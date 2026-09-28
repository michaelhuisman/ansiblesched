#!/usr/bin/env sh
# Builds a bare git repo from a directory of playbooks (dev/tests).
#   build-fixture-repo.sh <source-dir> <target.git>
set -eu
src=$1
dest=$2
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$src"/. "$work"/
cd "$work"
git init -q -b main
git add -A
GIT_AUTHOR_NAME=fixture GIT_AUTHOR_EMAIL=fixture@example.invalid \
GIT_COMMITTER_NAME=fixture GIT_COMMITTER_EMAIL=fixture@example.invalid \
    git commit -q -m "fixture playbooks"
rm -rf "$dest"
git clone -q --bare "$work" "$dest"
echo "fixture repo at $dest ($(git -C "$dest" rev-parse HEAD))"
