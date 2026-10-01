"""Collections cache (app/worker/galaxy.py) without ansible-galaxy or network."""

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from app.worker import galaxy
from app.worker.galaxy import CollectionCache, CollectionsError, _cache_key


def _project(tmp_path: Path, requirements: str = "collections: []\n") -> Path:
    project = tmp_path / "project"
    (project / "collections").mkdir(parents=True)
    (project / "collections" / "requirements.yml").write_text(requirements)
    return project


class FakeInstall:
    """Replaces CollectionCache._install: creates what ansible-galaxy would."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _requirements: Path, _project: Path, dest: Path) -> None:
        self.calls += 1
        (dest / "ansible_collections" / "ns" / "coll").mkdir(parents=True)


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[CollectionCache, FakeInstall]:
    fake = FakeInstall()
    c = CollectionCache(tmp_path / "cache", timeout_s=30)
    monkeypatch.setattr(c, "_install", fake)
    return c, fake


def test_no_requirements_means_nothing_to_do(
    tmp_path: Path, cache: tuple[CollectionCache, FakeInstall]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    assert cache[0].ensure(project) is None
    assert cache[1].calls == 0


def test_install_once_then_cached(
    tmp_path: Path, cache: tuple[CollectionCache, FakeInstall]
) -> None:
    c, fake = cache
    project = _project(tmp_path)
    first = c.ensure(project)
    second = c.ensure(project)
    assert first is not None
    assert second is not None
    assert (first.cached, second.cached) == (False, True)
    assert first.path == second.path
    assert (first.path / "ansible_collections" / "ns" / "coll").is_dir()
    assert fake.calls == 1
    # No temp dirs left behind.
    assert [p.name for p in (tmp_path / "cache").iterdir() if p.name.startswith(".tmp-")] == []


def test_failed_install_leaves_no_cache_entry(tmp_path: Path) -> None:
    c = CollectionCache(tmp_path / "cache", timeout_s=30)

    def broken(_r: Path, _p: Path, dest: Path) -> None:
        dest.mkdir(parents=True)
        raise CollectionsError("collection install failed: boom")

    c._install = broken  # type: ignore[method-assign]
    with pytest.raises(CollectionsError, match="boom"):
        c.ensure(_project(tmp_path))
    entries = [p.name for p in (tmp_path / "cache").iterdir() if p.name != ".locks"]
    assert entries == []


def test_cache_key_follows_requirements_and_local_files(tmp_path: Path) -> None:
    project = _project(tmp_path, "collections:\n  - name: a.b\n")
    coll = project / "collections"
    key = _cache_key(coll)
    (coll / "requirements.yml").write_text("collections:\n  - name: a.c\n")
    assert _cache_key(coll) != key
    key = _cache_key(coll)
    (coll / "local-1.0.0.tar.gz").write_bytes(b"v1")
    with_tarball = _cache_key(coll)
    assert with_tarball != key
    (coll / "local-1.0.0.tar.gz").write_bytes(b"v2")
    assert _cache_key(coll) != with_tarball


def test_cache_key_ignores_vendored_collections(tmp_path: Path) -> None:
    project = _project(tmp_path)
    coll = project / "collections"
    key = _cache_key(coll)
    vendored = coll / "ansible_collections" / "ns" / "x"
    vendored.mkdir(parents=True)
    (vendored / "plugin.py").write_text("x = 1\n")
    assert _cache_key(coll) == key


def test_search_path_puts_the_project_first(tmp_path: Path) -> None:
    result = galaxy.Collections(tmp_path / "abc", cached=True, seconds=0)
    assert result.search_path() == f"{tmp_path / 'abc'}:{galaxy.DEFAULT_PATHS}"
    assert result.search_path().endswith("/usr/share/ansible/collections")


def _completed(rc: int, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout="", stderr=stderr)


def test_install_runs_galaxy_in_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen["cmd"], seen["kwargs"] = cmd, kwargs
        return _completed(0)

    monkeypatch.setattr(galaxy.subprocess, "run", fake_run)
    project = _project(tmp_path)
    CollectionCache(tmp_path / "c", timeout_s=42)._install(
        project / galaxy.REQUIREMENTS, project, tmp_path / "dest"
    )
    assert seen["cmd"][1:] == [
        "collection",
        "install",
        "-r",
        "collections/requirements.yml",
        "-p",
        str(tmp_path / "dest"),
    ]
    assert seen["kwargs"]["cwd"] == project
    assert seen["kwargs"]["timeout"] == 42
    assert seen["kwargs"]["stdin"] == subprocess.DEVNULL


def test_install_failure_and_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path)
    c = CollectionCache(tmp_path / "c", timeout_s=5)
    lines = "\n".join(f"line {i}" for i in range(40))
    monkeypatch.setattr(galaxy.subprocess, "run", lambda *_a, **_k: _completed(1, lines))
    with pytest.raises(CollectionsError) as exc:
        c._install(project / galaxy.REQUIREMENTS, project, tmp_path / "d")
    assert "line 39" in str(exc.value)
    assert "line 0\n" not in str(exc.value)  # only the tail

    def timeout(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="ansible-galaxy", timeout=5)

    monkeypatch.setattr(galaxy.subprocess, "run", timeout)
    with pytest.raises(CollectionsError, match="timed out after 5s"):
        c._install(project / galaxy.REQUIREMENTS, project, tmp_path / "d")


def test_prune(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    old, recent, tmp_old, locks = root / "old", root / "recent", root / ".tmp-x-1", root / ".locks"
    for d in (old, recent, tmp_old, locks):
        d.mkdir(parents=True)
    long_ago = time.time() - 40 * 86400
    for d in (old, tmp_old, locks):
        os.utime(d, (long_ago, long_ago))
    removed = CollectionCache(root, timeout_s=5).prune(max_age_days=30)
    assert removed == 2
    assert sorted(p.name for p in root.iterdir()) == [".locks", "recent"]
    assert CollectionCache(root, timeout_s=5).prune(max_age_days=0) == 0


def test_unwritable_cache_gives_a_clear_error(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    cache_dir.chmod(0o500)
    try:
        with pytest.raises(CollectionsError, match="not usable"):
            CollectionCache(cache_dir, timeout_s=5).ensure(_project(tmp_path))
    finally:
        cache_dir.chmod(0o700)
