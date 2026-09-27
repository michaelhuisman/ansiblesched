"""Repo-cache per project (bare clone) en een detached worktree per run."""

import fcntl
import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 300


class GitError(Exception):
    pass


def _git(*args: str, cwd: Path | None = None) -> str:
    # GIT_TERMINAL_PROMPT=0 staat in het image; stdin dicht zodat git nooit wacht.
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        # Alleen het commando zonder argumenten: een URL kan een token bevatten.
        raise GitError(f"git {args[0]} failed: {exc.stderr.strip()[-500:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    return result.stdout.strip()


def _validate_ref(branch: str) -> None:
    if not branch or branch.startswith("-"):
        raise GitError(f"invalid branch name: {branch!r}")
    _git("check-ref-format", "--branch", branch)


class RepoCache:
    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir

    def _repo(self, project_id: int) -> Path:
        return self.cache_dir / f"{project_id}.git"

    @contextmanager
    def _locked(self, project_id: int) -> Iterator[Path]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with (self.cache_dir / f"{project_id}.lock").open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield self._repo(project_id)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    def checkout(self, project_id: int, git_url: str, branch: str, dest: Path) -> str:
        """Fetch de branch en zet HEAD ervan als detached worktree in `dest`.

        Geeft de commit-SHA terug.
        """
        _validate_ref(branch)
        with self._locked(project_id) as repo:
            if not repo.exists():
                log.info("cloning repository", extra={"project_id": project_id})
                _git("clone", "--bare", "--", git_url, str(repo))
            else:
                _git("remote", "set-url", "origin", git_url, cwd=repo)
            _git("fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", cwd=repo)
            sha = _git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=repo)
            _git("worktree", "add", "--detach", "--", str(dest), sha, cwd=repo)
        return sha

    def prune(self, project_id: int) -> None:
        """Ruim worktree-metadata op van verwijderde worktrees."""
        if not self._repo(project_id).exists():
            return
        with self._locked(project_id) as repo:
            _git("worktree", "prune", cwd=repo)

    def prune_all(self) -> None:
        if not self.cache_dir.exists():
            return
        for repo in self.cache_dir.glob("*.git"):
            try:
                self.prune(int(repo.stem))
            except (ValueError, GitError):
                log.warning("could not prune repository", extra={"repo": repo.name})
