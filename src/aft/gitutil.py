"""Thin wrappers around the git command line."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_SECONDS = 10


class GitError(Exception):
    pass


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from exc
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}")
    return result


@dataclass(frozen=True)
class RepoInfo:
    toplevel: Path
    """The top of the working tree containing the path."""

    main: Path
    """The main checkout: the toplevel itself, unless this is a linked worktree."""

    origin: str | None
    """The raw URL of the ``origin`` remote, if any."""


def repo_info(path: Path) -> RepoInfo | None:
    """Describe the git working tree containing ``path``, or None outside one."""
    result = git(
        ["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-dir", "--git-common-dir"],
        cwd=path,
        check=False,
    )
    lines = result.stdout.splitlines()
    if result.returncode != 0 or len(lines) != 3:
        return None
    toplevel, git_dir, common_dir = (Path(os.path.realpath(line)) for line in lines)
    main = toplevel
    # A linked worktree has its own git dir inside the common one. A submodule's git dir
    # also lives elsewhere, but equals its common dir, so it is not mistaken for one.
    if git_dir != common_dir:
        main = _main_worktree(toplevel) or toplevel
    origin = git(["config", "--get", "remote.origin.url"], cwd=toplevel, check=False)
    url = origin.stdout.strip() if origin.returncode == 0 else ""
    return RepoInfo(toplevel=toplevel, main=main, origin=url or None)


def _main_worktree(toplevel: Path) -> Path | None:
    result = git(["worktree", "list", "--porcelain"], cwd=toplevel, check=False)
    if result.returncode != 0:
        return None
    first: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            break
        first.append(line)
    # The first entry is the main worktree. A bare repository has no main checkout.
    if not first or not first[0].startswith("worktree ") or "bare" in first:
        return None
    return Path(os.path.realpath(first[0].removeprefix("worktree ")))


_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?(?P<path>/.*)?$")


def normalize_remote(url: str) -> str:
    """Reduce a remote URL to a stable key, such as ``github.com/owner/repo``.

    Network remotes lose their scheme, user, port and ``.git`` suffix and are lowercased,
    so the https and ssh forms of one repository match. A local path becomes
    ``file:<absolute path>``.
    """
    url = url.strip()
    if url.startswith("file://"):
        return "file:" + _strip_git(os.path.realpath(url.removeprefix("file://")))
    if url.startswith("/"):
        return "file:" + _strip_git(os.path.realpath(url))
    match = _URL.match(url) or (None if "://" in url else _SCP_LIKE.match(url))
    if not match:
        return url.lower()
    path = (match.group("path") or "").strip("/")
    return f"{match.group('host').lower()}/{_strip_git(path).lower()}".rstrip("/")


def _strip_git(path: str) -> str:
    path = path.rstrip("/")
    return path.removesuffix(".git")
