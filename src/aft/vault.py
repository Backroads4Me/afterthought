"""The memory vault: creating it, locking it, and committing to it."""

from __future__ import annotations

import fcntl
import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aft.config import Config
from aft.gitutil import (
    DEFAULT_WRITE_TIMEOUT,
    GitError,
    clear_stale_index_lock,
    git,
)
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

PROFILE = """\
---
type: Profile
title: Profile
description: Who you are, your background and lasting context.
sources: []
---

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

OBSIDIAN_GRAPH = {
    "collapse-filter": False,
    "search": "",
    "local-search": "",
    "local-backend": "current",
    "showTags": True,
    "showAttachments": False,
    "hideUnresolved": False,
    "showOrphans": True,
    "showFilter": True,
    "linkStrength": 0.73,
    "linkDistance": 200,
    "repelStrength": 12.5,
    "centerStrength": 0.52,
    "colorGroups": [
        {"query": 'tag:"#preference" or line:"[stated]"', "color": {"a": 1, "rgb": 16753920}},
        {"query": 'tag:"#decision" or section:"Decisions"', "color": {"a": 1, "rgb": 65330}},
        {"query": 'tag:"#state" or section:"State"', "color": {"a": 1, "rgb": 3394815}},
        {"query": 'tag:"#lesson" or section:"Lessons"', "color": {"a": 1, "rgb": 16724787}},
        {"query": 'tag:"#procedure" or section:"Procedures"', "color": {"a": 1, "rgb": 10040319}},
        {"query": 'tag:"#fact" or section:"Facts"', "color": {"a": 1, "rgb": 4474111}},
        {"query": 'path:"projects/"', "color": {"a": 1, "rgb": 16777215}},
        {"query": 'path:"digests/"', "color": {"a": 1, "rgb": 8947848}},
    ],
}


# The block `aft` owns inside the vault's `.gitignore`: its own marker, then the rule it
# wrote. A line the owner added themselves is never touched.
LIBRARY_IGNORE_MARKER = "# library.commit = false"
LIBRARY_IGNORE_RULE = "library/"


def gitignore_text(commit_library: bool) -> str:
    lines = [
        "# Obsidian keeps per-device state here; only the shared settings are committed.",
        ".obsidian/*",
        "!.obsidian/app.json",
        "!.obsidian/graph.json",
    ]
    if not commit_library:
        lines += ["", LIBRARY_IGNORE_MARKER, LIBRARY_IGNORE_RULE]
    return "\n".join(lines) + "\n"


def maintain_library_ignore(vault: Path, commit_library: bool) -> bool:
    """Bring the vault's `.gitignore` in line with `library.commit`; report a change.

    `aft init` wrote that file once and the setting is read from `config.toml` on every run, so
    turning `library.commit` off afterwards used to leave `library/<name>/` untracked *and*
    unignored - and whatever git reports as changed is what the dream's checkpoint commit
    collects, so imported sources went in as human edits (M7 review 12).

    Only the block this program wrote is rewritten: a `library/` line the owner added is theirs.
    Turning the setting off also untracks what an earlier import committed - the files stay on
    disk and in history, and stop being committed on every later run.
    """
    path = vault / ".gitignore"
    lines = path.read_text().splitlines() if path.is_file() else []
    kept: list[str] = []
    position = 0
    while position < len(lines):
        if (
            lines[position] == LIBRARY_IGNORE_MARKER
            and position + 1 < len(lines)
            and lines[position + 1] == LIBRARY_IGNORE_RULE
        ):
            position += 2
            continue
        kept.append(lines[position])
        position += 1
    if not commit_library and LIBRARY_IGNORE_RULE not in kept:
        kept += ["", LIBRARY_IGNORE_MARKER, LIBRARY_IGNORE_RULE]
    text = "\n".join(kept) + "\n" if kept else ""
    changed = not path.is_file() or path.read_text() != text
    if changed:
        path.write_text(text)
    if not commit_library and git(["ls-files", "--", "library"], cwd=vault).stdout.split():
        git(["rm", "-r", "--cached", "--quiet", "--", "library"], cwd=vault)
    return changed


def initial_files(config: Config) -> dict[str, str]:
    return {
        "index.md": ROOT_INDEX,
        "log.md": LOG,
        "registry.yaml": f"version: {REGISTRY_VERSION}\nprojects: []\n",
        "global/preferences.md": PREFERENCES,
        "global/environment.md": ENVIRONMENT,
        "global/profile.md": PROFILE,
        "reports/needs-you.md": NEEDS_YOU,
        ".obsidian/app.json": json.dumps(OBSIDIAN_APP, indent=2) + "\n",
        ".obsidian/graph.json": json.dumps(OBSIDIAN_GRAPH, indent=2) + "\n",
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
    clear_stale_index_lock(vault)
    _git_write_with_lock_retry(["add", "-A", "--", *paths], vault)
    staged = git(["diff", "--cached", "--quiet"], cwd=vault, check=False)
    if staged.returncode == 0:
        return False
    try:
        _git_write_with_lock_retry(["commit", "-q", "-m", message], vault)
    except GitError as exc:
        raise VaultError(f"could not commit to the vault: {exc}") from exc
    return True


def _git_write_with_lock_retry(
    args: list[str],
    vault: Path,
    timeout: float | None = DEFAULT_WRITE_TIMEOUT,
) -> subprocess.CompletedProcess:
    try:
        return git(args, cwd=vault, timeout=timeout)
    except GitError as exc:
        if "index.lock" in str(exc) and clear_stale_index_lock(vault):
            return git(args, cwd=vault, timeout=timeout)
        raise


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
