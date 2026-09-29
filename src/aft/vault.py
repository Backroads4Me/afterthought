"""The memory vault: creating it, locking it, and committing to it."""

from __future__ import annotations

import fcntl
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aft.config import Config
from aft.gitutil import GitError, git
from aft.registry import VERSION as REGISTRY_VERSION


class VaultError(Exception):
    pass


ROOT_INDEX = """\
---
okf_version: "0.2"
---

# Afterthought memory

This vault is maintained by afterthought. Its index is regenerated on every dream run.
"""

LOG = """\
# Log

Each afterthought run that changes the vault appends one entry here.
"""

PREFERENCES = """\
---
type: Preferences
title: Preferences
description: Your preferences that apply in every project.
sources: []
---

## Preferences
"""

ENVIRONMENT = """\
---
type: Environment
title: Environment
description: The hosts, tools and services you use.
sources: []
---

## State

## Facts
"""

NEEDS_YOU = """\
---
type: Needs You
title: Needs you
description: Every open conflict and question, regenerated on every dream run.
---

# Needs you

Nothing is waiting on you.
"""

OBSIDIAN_APP = {"userIgnoreFilters": ["reports/"]}


def gitignore_text(commit_library: bool) -> str:
    lines = [
        "# Obsidian keeps per-device state here; only the shared settings are committed.",
        ".obsidian/*",
        "!.obsidian/app.json",
    ]
    if not commit_library:
        lines += ["", "# library.commit = false", "library/"]
    return "\n".join(lines) + "\n"


def initial_files(config: Config) -> dict[str, str]:
    return {
        "index.md": ROOT_INDEX,
        "log.md": LOG,
        "registry.yaml": f"version: {REGISTRY_VERSION}\nprojects: []\n",
        "global/preferences.md": PREFERENCES,
        "global/environment.md": ENVIRONMENT,
        "reports/needs-you.md": NEEDS_YOU,
        ".obsidian/app.json": json.dumps(OBSIDIAN_APP, indent=2) + "\n",
        ".gitignore": gitignore_text(config.settings.library.commit),
    }


def is_initialized(vault: Path) -> bool:
    return (vault / ".git").exists() and (vault / "index.md").exists()


def init(config: Config) -> list[str]:
    """Create the vault, or fill in whatever is missing. Returns the files created."""
    vault = config.vault
    vault.mkdir(parents=True, exist_ok=True)
    if not (vault / ".git").exists():
        git(["init", "-q", "-b", "main"], cwd=vault)
    created = []
    for rel, text in initial_files(config).items():
        path = vault / rel
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        created.append(rel)
    if created:
        commit(vault, created, "aft: initialize vault")
    return created


def commit(vault: Path, paths: list[str], message: str) -> bool:
    """Stage ``paths`` (which may include removals) and commit them. Returns whether a
    commit was made."""
    git(["add", "-A", "--", *paths], cwd=vault)
    staged = git(["diff", "--cached", "--quiet"], cwd=vault, check=False)
    if staged.returncode == 0:
        return False
    try:
        git(["commit", "-q", "-m", message], cwd=vault)
    except GitError as exc:
        raise VaultError(f"could not commit to the vault: {exc}") from exc
    return True


@contextmanager
def locked(config: Config) -> Iterator[None]:
    """Hold the vault's exclusive lock. Every writer to the vault takes it."""
    config.lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config.lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def require(config: Config) -> None:
    if not is_initialized(config.vault):
        raise VaultError(f"no vault at {config.vault}; run `aft init` first")


PROJECT_DIRS: tuple[str, ...] = ("projects", "digests", "archive")


def path_slug(rel: str | Path) -> str | None:
    """The project slug for a vault-relative path, or None for pages outside projects.

    Handles:
    - projects/<slug>/...
    - digests/<slug>/...
    - archive/projects/<slug>/... (ruling 7943f9a layout)
    - archive/digests/<slug>/...
    - archive/<slug>.md (legacy archive layout)
    """
    parts = Path(rel).parts
    if not parts:
        return None
    if parts[0] in ("projects", "digests"):
        return parts[1] if len(parts) > 1 else None
    if parts[0] == "archive":
        if len(parts) >= 3 and parts[1] in ("projects", "digests"):
            return parts[2]
        if len(parts) == 2 and parts[1].endswith(".md"):
            stem = Path(parts[1]).stem
            return stem if stem != "index" else None
    return None
