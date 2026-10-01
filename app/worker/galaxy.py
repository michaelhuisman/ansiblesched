"""Ansible collections from a project's `collections/requirements.yml`.

Installed with `ansible-galaxy` into a shared cache, one directory per unique set of
requirements (the hash of everything under `collections/` plus the ansible-core version).
Only the first run with a given set waits; workers never install the same set at the
same time (flock), and a half-finished install is never used (temp dir + rename).

The image's standard set lives in /usr/share/ansible/collections; a project's collections
come first on the search path, so a project can pin a different version.
"""

import fcntl
import hashlib
import logging
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path

log = logging.getLogger(__name__)

REQUIREMENTS = Path("collections") / "requirements.yml"
# ansible-core's default search path, after the project's collections.
DEFAULT_PATHS = "~/.ansible/collections:/usr/share/ansible/collections"
_OUTPUT_TAIL_LINES = 15
_STALE_TMP_S = 24 * 3600


class CollectionsError(Exception):
    """Installing the project's collections failed. The message may contain output of
    ansible-galaxy: mask secrets before storing it."""


@dataclass(frozen=True)
class Collections:
    path: Path
    cached: bool
    seconds: float

    def search_path(self) -> str:
        return f"{self.path}:{DEFAULT_PATHS}"


def _cache_key(collections_dir: Path) -> str:
    """Hash of all files under collections/ (requirements and local tarballs), without
    collections that are vendored in collections/ansible_collections."""
    digest = hashlib.sha256(f"ansible-core={version('ansible-core')}\n".encode())
    for file in sorted(p for p in collections_dir.rglob("*") if p.is_file()):
        rel = file.relative_to(collections_dir)
        if rel.parts[0] == "ansible_collections":
            continue
        digest.update(f"{rel.as_posix()}\n".encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()[:32]


class CollectionCache:
    def __init__(self, cache_dir: Path, timeout_s: int) -> None:
        self._dir = cache_dir
        self._timeout_s = timeout_s

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        locks = self._dir / ".locks"
        locks.mkdir(parents=True, exist_ok=True)
        with (locks / f"{key}.lock").open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def ensure(self, project_dir: Path) -> Collections | None:
        """Install the project's collections (or reuse the cache). None without a
        collections/requirements.yml."""
        requirements = project_dir / REQUIREMENTS
        if not requirements.is_file():
            return None
        key = _cache_key(requirements.parent)
        try:
            return self._ensure(requirements, project_dir, key)
        except OSError as exc:
            # E.g. a cache volume that is not writable for the worker user.
            raise CollectionsError(
                f"collection cache {self._dir} not usable: {exc.strerror or exc}"
            ) from None

    def _ensure(self, requirements: Path, project_dir: Path, key: str) -> Collections:
        final = self._dir / key
        started = time.monotonic()
        with self._locked(key):
            if final.is_dir():
                final.touch()  # last use, for prune()
                return Collections(final, cached=True, seconds=time.monotonic() - started)
            tmp = self._dir / f".tmp-{key}-{uuid.uuid4().hex[:8]}"
            try:
                self._install(requirements, project_dir, tmp)
                tmp.rename(final)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        seconds = time.monotonic() - started
        log.info("collections installed", extra={"key": key, "seconds": round(seconds, 1)})
        return Collections(final, cached=False, seconds=seconds)

    def _install(self, requirements: Path, project_dir: Path, dest: Path) -> None:
        cmd = [
            shutil.which("ansible-galaxy") or "ansible-galaxy",
            "collection",
            "install",
            "-r",
            str(requirements.relative_to(project_dir)),
            "-p",
            str(dest),
        ]
        try:
            # cwd = project: local `type: file` entries are relative to the repo root.
            result = subprocess.run(
                cmd,
                cwd=project_dir,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CollectionsError(
                f"collection install timed out after {self._timeout_s}s"
            ) from None
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip().splitlines()
            tail = "\n".join(output[-_OUTPUT_TAIL_LINES:])
            raise CollectionsError(f"collection install failed: {tail}")

    def prune(self, max_age_days: int) -> int:
        """Remove cache entries unused for `max_age_days` and stale temp dirs. A run
        touches its entry when it starts, so entries in use are recent."""
        if max_age_days <= 0 or not self._dir.is_dir():
            return 0
        now = time.time()
        removed = 0
        for entry in self._dir.iterdir():
            if not entry.is_dir() or entry.name == ".locks":
                continue
            limit = _STALE_TMP_S if entry.name.startswith(".tmp-") else max_age_days * 86400
            if now - entry.stat().st_mtime > limit:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
        return removed
