"""Install and uninstall afterthought's Claude Code hooks (BUILD-PLAN M1.3).

Claude Code reads its hooks from ``~/.claude/settings.json``. Its ``hooks`` object maps
an event name to a list of matcher groups, and each group holds the command hooks to run
when the event matches:

.. code-block:: json

    {"hooks": {"SessionStart": [{"matcher": "startup", "hooks": [
        {"type": "command", "command": "aft hook claude SessionStart", "timeout": 5}]}]}}

This module merges into that file the three entries BUILD-PLAN M1.3 names, one per event,
each running ``aft hook claude <event>`` - the command line the merged ``aft hook`` command
takes, with Claude Code's own event name as the argument ``adapters/claude.py`` reads. The
file's path is an argument of every function here so a test can point it at a temporary
file; `settings_path` is the default the command line uses.

Two rules hold the user's own configuration safe:

* A merge touches only what afterthought owns. Other people's hook entries keep their
  position and their order, and every key afterthought does not own keeps its place in the
  document, because the file is read, edited in place, and written back with its key order
  intact.
* Every write is preceded by a backup of the text it replaces, and nothing is written at
  all to a file whose text afterthought cannot read as JSON. `install`, `uninstall`,
  `_read` and `_backup` each state their rule where a reader of a write will find it.
"""

from __future__ import annotations

import difflib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

AGENT = "claude"

# The three events BUILD-PLAN M1.3 installs, in the order it lists them, which is the
# order they appear in the file when this module has to create them. Each key is Claude
# Code's own event name, the same key `adapters/claude.py` maps, so the installed command
# line and the parser that reads its payload share one spelling.
HOOK_EVENTS = ("SessionStart", "PreCompact", "SessionEnd")

# Claude Code's hook handler field `timeout` is a number of seconds (hooks reference, "Hook
# handler fields"); BUILD-PLAN M1.3 fixes 5 for each entry. The number matters most for
# SessionEnd, whose own default budget is shorter than the hook budget in `cli.py`, so an
# explicit timeout is what lets the command finish. It is written on a group of our own, so
# a longer budget never applies to a group afterthought did not add.
HOOK_TIMEOUT_SECONDS = 5


class InstallError(Exception):
    """The settings file is not one afterthought can merge into, and nothing was written."""


@dataclass(frozen=True)
class Change:
    """What one install or uninstall did, or would have done under ``--dry-run``.

    ``changed`` is False when the file already held exactly what was asked for, in which
    case nothing was written and no backup was taken. ``diff`` is the unified diff of the
    file's text before and after, and is empty when nothing changed.
    """

    changed: bool
    backup: Path | None
    diff: str


def settings_path() -> Path:
    """The settings file Claude Code reads for the current user."""
    return Path.home() / ".claude" / "settings.json"


def hook_command(event: str) -> str:
    """The one command line afterthought installs for one Claude Code event."""
    return f"aft hook {AGENT} {event}"


def install(settings: Path, *, dry_run: bool = False) -> Change:
    """Merge afterthought's three hook entries into ``settings``, creating the file.

    Every event that does not already run our command gets one group of our own, appended
    after the groups already there: an entry is never inserted before, sorted among, or
    substituted for a hook someone else wrote. An event that already runs our command is
    left exactly as it is, so installing twice changes nothing on the second run, and an
    entry the user edited after installing - its timeout, its matcher - stays as they set
    it. The command line is the whole identity of an entry, which is what makes our own
    entries findable again without a marker field of afterthought's in the user's file.
    """
    document, original = _read(settings)
    hooks = _hooks(document)

    added = False
    for event in HOOK_EVENTS:
        groups = _groups(hooks, event)
        if _count(groups, event) == 0:
            groups.append(_group(event))
            added = True

    return _commit(settings, original, document, changed=added, dry_run=dry_run)


def uninstall(settings: Path, *, dry_run: bool = False) -> Change:
    """Remove exactly afterthought's own hook entries from ``settings``.

    Only the three events afterthought installs are read, and within them only the command
    hooks running ``aft hook claude <event>``. A group left with no hooks of its own is
    dropped, since an empty group does nothing and the emptiness is ours; a group left with
    any hook in it keeps its position and its matcher. An event list emptied this way, and a
    ``hooks`` object emptied this way, are dropped too, so uninstalling what was just
    installed leaves no trace of the keys afterthought added.
    """
    document, original = _read(settings)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return Change(changed=False, backup=None, diff="")

    removed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list) or _count(groups, event) == 0:
            continue
        kept = [group for group in (_strip(group, event) for group in groups) if group is not None]
        removed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]

    if removed and not hooks:
        del document["hooks"]

    return _commit(settings, original, document, changed=removed, dry_run=dry_run)


def _group(event: str) -> dict:
    """One group holding afterthought's own hook for one event, and nothing else.

    The group carries no ``matcher``: an absent matcher matches every source, trigger or
    reason, which is what DESIGN 8 asks for, a ``SessionStart`` that fires after ``/clear``
    and after a compaction as well as on startup.
    """
    return {
        "hooks": [
            {
                "type": "command",
                "command": hook_command(event),
                "timeout": HOOK_TIMEOUT_SECONDS,
            }
        ]
    }


def _runs(hook: object, event: str) -> bool:
    """Whether one command hook is afterthought's own entry for this event."""
    return isinstance(hook, dict) and hook.get("command") == hook_command(event)


def _count(groups: list, event: str) -> int:
    """How many of afterthought's own command hooks a list of groups runs."""
    return sum(
        1
        for group in groups
        if isinstance(group, dict) and isinstance(group.get("hooks"), list)
        for hook in group["hooks"]
        if _runs(hook, event)
    )


def _strip(group: object, event: str) -> object | None:
    """A group with afterthought's own hook taken out of it, or None when nothing is left.

    A group that is not ours, or that cannot be read as a group, comes back untouched:
    uninstall removes command hooks, not shapes it does not recognize.
    """
    if not isinstance(group, dict):
        return group
    hooks = group.get("hooks")
    if not isinstance(hooks, list):
        return group
    kept = [hook for hook in hooks if not _runs(hook, event)]
    if len(kept) == len(hooks):
        return group
    if not kept:
        return None
    stripped = dict(group)
    stripped["hooks"] = kept
    return stripped


def _read(settings: Path) -> tuple[dict, str | None]:
    """The document in ``settings`` and the text it came from; no file reads as no settings.

    BUILD-PLAN M1.3 requires an invalid settings file to abort without writing, and this is
    the one place that decides what invalid means: text that is not UTF-8, text that is not
    JSON, a JSON value that is not an object, a ``hooks`` key that is not an object, and a
    value where one of afterthought's three events lives that is not a list. Each is refused
    rather than overwritten, because a file afterthought cannot read is a file it cannot
    show a diff against, and it is somebody's working configuration. Text with nothing in it
    is the exception: an empty file is a file with no settings yet, not a broken one.
    """
    if not settings.exists():
        return {}, None
    try:
        original = settings.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{settings} is not UTF-8 text ({exc}); nothing was written") from exc
    if not original.strip():
        return {}, original
    try:
        document = json.loads(original)
    except json.JSONDecodeError as exc:
        raise InstallError(f"{settings} is not valid JSON ({exc}); nothing was written") from exc
    if not isinstance(document, dict):
        raise InstallError(f"{settings} holds no JSON object; nothing was written")
    hooks = document.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise InstallError(f"{settings} has a hooks key that is not an object; no write")
        for event in HOOK_EVENTS:
            if event in hooks and not isinstance(hooks[event], list):
                raise InstallError(
                    f"{settings} has a {event} entry that is not an array; nothing was written"
                )
    return document, original


def _hooks(document: dict) -> dict:
    """The ``hooks`` object, created when the file has none yet."""
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        hooks = document["hooks"] = {}
    return hooks


def _groups(hooks: dict, event: str) -> list:
    """One event's list of matcher groups, created when that event has none yet."""
    groups = hooks.get(event)
    if not isinstance(groups, list):
        groups = hooks[event] = []
    return groups


def _commit(
    settings: Path, original: str | None, document: dict, *, changed: bool, dry_run: bool
) -> Change:
    """Report the change, and write it unless nothing changed or this is a dry run.

    Nothing is written when nothing changed, so a second install leaves the file's bytes
    untouched and no backup behind. An unchanged merge is not reformatted either, which
    keeps a hand-written settings file hand-written.
    """
    if not changed:
        return Change(changed=False, backup=None, diff="")
    after = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    diff = "\n".join(
        difflib.unified_diff(
            (original or "").splitlines(),
            after.splitlines(),
            fromfile=f"{settings} (before)",
            tofile=f"{settings} (after)",
        )
    )
    if dry_run:
        return Change(changed=True, backup=None, diff=diff)
    backup = _backup(settings, original)
    _write(settings, after)
    return Change(changed=True, backup=backup, diff=diff)


def _backup(settings: Path, original: str | None) -> Path | None:
    """Copy the text a write is about to replace into a file beside it. This is the backup
    rule, where a reader of a write meets it: settings.json changes only once its previous
    text is safe next to it.

    The copy is named for the tool that made it and stamped with the UTC second, so one
    install never overwrites another's backup and a user can find every version
    afterthought kept. A file with no text of its own has nothing to keep, so no backup is
    written to claim an empty settings file was lost.
    """
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = settings.with_name(f"{settings.name}.aft-{stamp}.bak")
    backup.write_text(original, encoding="utf-8")
    return backup


def _write(settings: Path, text: str) -> None:
    """Put the new text in place in one step, so a crash leaves the old file or the new one.

    A half-written settings file costs the user their whole Claude Code configuration, so
    the text goes to a temporary file in the same directory and ``os.replace`` moves it over
    the old one. The new file keeps the permissions of the one it replaces.
    """
    settings.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(settings.stat().st_mode) if settings.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=settings.parent, prefix=f"{settings.name}.aft-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, settings)
    except OSError:
        os.unlink(temporary)
        raise
