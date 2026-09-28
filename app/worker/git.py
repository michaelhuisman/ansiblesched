"""Repo cache per project (bare clone) and a detached worktree per run."""

import fcntl
import logging
import shlex
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from app.worker.credentials import GitAuth

log = logging.getLogger(__name__)

GIT_TIMEOUT_S = 300


class GitError(Exception):
    pass


def _subcommand(args: tuple[str, ...]) -> str:
    rest = list(args)
    while rest[:1] == ["-c"]:
        rest = rest[2:]
    return rest[0] if rest else "?"


def _git(*args: str, cwd: Path | None = None) -> str:
    # GIT_TERMINAL_PROMPT=0 is set in the image; stdin closed so git never waits.
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
        # Only the subcommand, no arguments: a URL can contain a token.
        raise GitError(f"git {_subcommand(args)} failed: {exc.stderr.strip()[-500:]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {_subcommand(args)} timed out after {GIT_TIMEOUT_S}s") from exc
    return result.stdout.strip()


def askpass_config(auth: GitAuth, auth_dir: Path) -> list[str]:
    """Write an askpass script that reads username and token from files.

    That keeps the token out of the URL, the process arguments, the environment and the
    config of the bare repo. `auth_dir` is in the private data dir (tmpfs) and is removed
    after the run. Returns the `-c` options for git.
    """
    auth_dir.mkdir(mode=0o700)
    username, token = auth_dir / "username", auth_dir / "token"
    for path, value in ((username, auth.username), (token, auth.token)):
        path.touch(mode=0o600)
        path.write_text(value)
    script = auth_dir / "askpass.sh"
    script.touch(mode=0o700)
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  Username*) cat {shlex.quote(str(username))} ;;\n"
        f"  *) cat {shlex.quote(str(token))} ;;\n"
        "esac\n"
    )
    # empty credential.helper: never a helper that stores the token anywhere.
    return ["-c", f"core.askPass={script}", "-c", "credential.helper="]


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

    def checkout(
        self,
        project_id: int,
        git_url: str,
        branch: str,
        dest: Path,
        *,
        auth: GitAuth | None = None,
        auth_dir: Path | None = None,
    ) -> str:
        """Fetch the branch and check out its HEAD as a detached worktree in `dest`.

        With `auth`, clone and fetch go through an askpass script in `auth_dir`.
        Returns the commit SHA.
        """
        _validate_ref(branch)
        remote_opts: list[str] = []
        if auth is not None:
            if auth_dir is None:
                raise GitError("auth_dir is required with auth")
            remote_opts = askpass_config(auth, auth_dir)
        with self._locked(project_id) as repo:
            if not repo.exists():
                log.info("cloning repository", extra={"project_id": project_id})
                _git(*remote_opts, "clone", "--bare", "--", git_url, str(repo))
            else:
                _git("remote", "set-url", "origin", git_url, cwd=repo)
            _git(
                *remote_opts,
                "fetch",
                "--prune",
                "origin",
                "+refs/heads/*:refs/heads/*",
                cwd=repo,
            )
            sha = _git("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", cwd=repo)
            _git("worktree", "add", "--detach", "--", str(dest), sha, cwd=repo)
        return sha

    def prune(self, project_id: int) -> None:
        """Clean up worktree metadata of removed worktrees."""
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
