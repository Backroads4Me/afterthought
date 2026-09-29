"""When the dream runs, and on what (DESIGN 9, "A run", step 1).

``run`` is ``aft dream``. It exits at once when no digest is waiting; otherwise it takes
the vault lock and holds it for the whole run, so no digest, registry change or other
dream lands in the vault while this one reads and writes it.

A digest is waiting until the dream marks it consumed with ``aft.consumed`` in its
frontmatter (M4.5). A project with waiting digests runs only once its sessions have been
quiet for ``dream.quiet_minutes``, measured from the newest event the capture queue holds
for any directory that resolves to it.

Before any project runs, the vault's uncommitted changes are looked at, because the only
writer that leaves changes uncommitted is a person editing the vault by hand. If any of
them was modified within the quiet period, the person may still be typing, so no project
runs this time. Otherwise they are committed on their own as ``checkpoint: human edits``,
so a dream commit never carries a person's bytes and each can be reverted alone.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft import keys, queue, vault
from aft.config import Config
from aft.gitutil import git
from aft.page import split_frontmatter
from aft.registry import Registry

DIGESTS = "digests"
CHECKPOINT_MESSAGE = "checkpoint: human edits"

Process = Callable[[Config, str, Sequence[Path]], None]
"""What a run does with one project that is ready: its slug and its waiting digests, in
session order. The decisions, rules and apply steps (M4.3 to M4.5) fill this in."""


@dataclass
class Run:
    """What one ``aft dream`` did, for the command line and the report."""

    waiting: dict[str, list[Path]] = field(default_factory=dict)
    """Every project with waiting digests, by slug, each list in session order."""

    ran: list[str] = field(default_factory=list)
    active: list[str] = field(default_factory=list)
    """Projects skipped because a session was active within the quiet period."""

    edited: list[str] = field(default_factory=list)
    """Projects skipped because the vault holds a hand edit made within the quiet period."""

    checkpoint: str | None = None
    """The ``checkpoint: human edits`` commit this run made, if any."""


def run(config: Config, process: Process | None = None, now: datetime | None = None) -> Run:
    """Run the dream once: every quiet project with waiting digests, one at a time."""
    vault.require(config)
    if not waiting_digests(config.vault):
        return Run()
    with vault.locked(config):
        now = now or datetime.now(UTC)
        since = now - timedelta(minutes=config.settings.dream.quiet_minutes)
        # Read again under the lock: another run may have consumed them meanwhile.
        result = Run(waiting=waiting_digests(config.vault))
        activity = last_activity(config)
        for slug in result.waiting:
            last = activity.get(slug)
            if last is not None and last > since:
                result.active.append(slug)
        ready = [slug for slug in result.waiting if slug not in result.active]
        if not ready:
            return result
        changes = uncommitted(config.vault)
        if any(modified > since for modified in changes.values()):
            result.edited = ready
            return result
        if changes and vault.commit(config.vault, sorted(changes), CHECKPOINT_MESSAGE):
            result.checkpoint = git(["rev-parse", "HEAD"], cwd=config.vault).stdout.strip()
            tag_hand_edits(config, result.checkpoint)
        for slug in ready:
            if process is not None:
                process(config, slug, result.waiting[slug])
            result.ran.append(slug)
        return result


def tag_hand_edits(config: Config, checkpoint: str) -> None:
    """Tag the lines a person added in ``checkpoint`` as ``[stated]`` (DESIGN 9 step 1).

    Not yet written: which added lines count is a trust rule (R2) awaiting Ted's ruling,
    so this tags nothing and the checkpoint holds the person's bytes unchanged.
    """


def waiting_digests(vault_path: Path) -> dict[str, list[Path]]:
    """The digests no dream has consumed, by project slug, each in session order.

    A digest lives at ``digests/<slug>/<yyyy>/<mm>/<name>.md`` (DESIGN 6, M2.6), so the
    directory names its project. A digest whose frontmatter cannot be read carries no
    ``aft.consumed`` mark, so it is still waiting; the step that reads it reports it.
    Session order is the order the digests were written in (``generated.at``), then the
    path, which puts a session's ``-2`` part after its first.
    """
    root = vault_path / DIGESTS
    found: dict[str, list[tuple[str, Path]]] = {}
    for path in root.glob("*/*/*/*.md"):
        frontmatter = split_frontmatter(path.read_text(encoding="utf-8"))[0] or {}
        aft = frontmatter.get("aft")
        if isinstance(aft, dict) and aft.get("consumed"):
            continue
        slug = path.relative_to(root).parts[0]
        found.setdefault(slug, []).append((_written_at(frontmatter), path))
    return {
        slug: [path for _, path in sorted(digests, key=lambda d: (d[0], str(d[1])))]
        for slug, digests in sorted(found.items())
    }


def _written_at(frontmatter: dict) -> str:
    """A digest's ``generated.at`` as a sortable UTC string, empty when it has none.

    YAML reads an unquoted timestamp as a datetime and a quoted one as text, so both are
    brought to the one form M2.6 writes.
    """
    generated = frontmatter.get("generated")
    at = generated.get("at") if isinstance(generated, dict) else None
    if isinstance(at, datetime):
        at = at if at.tzinfo else at.replace(tzinfo=UTC)
        return f"{at.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ}"
    return at if isinstance(at, str) else ""


def last_activity(config: Config) -> dict[str, datetime]:
    """The newest session event per project slug, from the capture queue.

    Each directory is resolved the way the digest resolves it, so a session's events and
    its digests land under the same slug. The lookup records no alias: scheduling only
    reads the registry. A directory that no longer exists, such as a removed worktree,
    has no session left running in it and nothing to resolve, so it is passed over.
    """
    registry = Registry.load(config.registry_path)
    newest: dict[str, datetime] = {}
    for cwd, moment in queue.last_events(config).items():
        if not cwd.is_dir():
            continue
        slug = keys.resolve(cwd, registry, config.host).slug
        if slug and (slug not in newest or moment > newest[slug]):
            newest[slug] = moment
    return newest


def uncommitted(vault_path: Path) -> dict[str, datetime]:
    """Every path git reports as changed and not committed, with when it last changed.

    Untracked files count, ignored ones do not. A deleted path has no time of its own, so
    it takes the time of the nearest directory still standing above it, which a deletion
    updates. A rename is reported as both of its paths, so committing it stages both.
    """
    status = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=vault_path).stdout
    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in code or "C" in code:
            paths.append(fields[index])  # the source path follows as its own field
            index += 1
    return {path: _modified(vault_path, path) for path in paths}


def _modified(vault_path: Path, rel: str) -> datetime:
    target = vault_path / rel
    while not os.path.lexists(target) and target != vault_path:
        target = target.parent
    return datetime.fromtimestamp(os.lstat(target).st_mtime, UTC)
