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

Three rules hold the user's own configuration safe:

* A merge touches only what afterthought owns, and it owns where its command line is: an
  entry is found by that line whether it sits directly in one of our three event lists or
  nested in a matcher group. Other people's hook entries keep their position and their
  order, and uninstall removes command hooks and nothing else: every key of the user's
  keeps its place, a matcher group keeps its matcher once our hook leaves it, and an event
  list or the ``hooks`` object that uninstall empties stays empty rather than going away.
  The file is read, edited in place, and written back with its key order intact.
* A write goes to the file the settings path points at, so a symbolic link the user made
  there - into a version-controlled settings file, say - survives and keeps being read.
* No settings file changes before a backup of its text is safe beside it, carrying that
  file's own mode, and nothing changes at all to a file whose text afterthought cannot read
  as JSON. `install`, `uninstall`, `_read`, `_backup` and `_write` each state their rule
  where a reader of a write will find it.
"""

from __future__ import annotations

import difflib
import itertools
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
    after the elements already there: an entry is never inserted before, sorted among, or
    substituted for a hook someone else wrote. An event that already runs our command is
    left exactly as it is, whether that command sits directly in the event list or inside
    a matcher group, so installing twice changes nothing on the second run, and an entry
    the user edited after installing - its timeout, its matcher - stays as they set it.
    The command line is the whole identity of an entry, which is what makes our own
    entries findable again without a marker field of afterthought's in the user's file.
    """
    document, original = _read(settings)
    hooks = _hooks(document)

    added = False
    for event in HOOK_EVENTS:
        groups = _groups(hooks, event)
        if all(_remove(element, event) is element for element in groups):
            groups.append(_group(event))
            added = True

    return _commit(settings, original, document, changed=added, dry_run=dry_run)


def uninstall(settings: Path, *, dry_run: bool = False) -> Change:
    """Remove exactly afterthought's own hook entries from ``settings``.

    Only the three events afterthought installs are read, and within them only the command
    hooks running ``aft hook claude <event>``, wherever one sits: directly in the event
    list, or nested in a matcher group. Nothing else is deleted and nothing is reordered:
    an element that is not ours keeps its keys and its place, an event keeps its key, the
    ``hooks`` object keeps its key. A container stays where it is even when our hook was
    the last thing in it, so an emptied event list is left as an empty list and an emptied
    ``hooks`` object as an empty object, which Claude Code runs as nothing. Only an
    element left holding nothing but an empty ``hooks`` list goes, because it holds no key
    of anyone's. Telling our own containers from the user's would mean remembering what
    was installed; this rule needs no history to be right.
    """
    document, original = _read(settings)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return Change(changed=False, backup=None, diff="")

    removed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        for element in groups:
            left = _remove(element, event)
            if left is not element:
                removed = True
            if left is not _DROPPED:
                kept.append(left)
        groups[:] = kept

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


def _is_ours(element: object, event: str) -> bool:
    """Whether one command object is afterthought's own entry for this event.

    The command line is the whole identity of an entry, and that shape is looked for wherever
    it can sit: directly in an event list, which is how a person editing the file by hand
    tends to leave it, or nested in a matcher group, which is how an install writes it.
    """
    return isinstance(element, dict) and element.get("command") == hook_command(event)


_DROPPED = object()


def _remove(element: object, event: str) -> object:
    """One element of an event list with afterthought's own entry taken out of it.

    The element itself comes back when it is not ours and holds none of ours, which is how
    install and uninstall tell an element they rewrote from one they left alone. The
    ``_DROPPED`` sentinel, compared by identity, comes back when nothing of anyone's is
    sitting in the list, or a group left holding nothing but an empty ``hooks`` list. A group
    with a matcher, or any other key of its own, keeps that key even once our hook is gone,
    and an element that is not an object at all - a string, a number, a JSON null - comes
    back untouched: uninstall takes out afterthought's own command hooks, never a shape of
    someone else's.
    """
    if _is_ours(element, event):
        return _DROPPED
    if not isinstance(element, dict):
        return element
    hooks = element.get("hooks")
    if not isinstance(hooks, list):
        return element
    kept = [hook for hook in hooks if not _is_ours(hook, event)]
    if len(kept) == len(hooks):
        return element
    if not kept and set(element) == {"hooks"}:
        return _DROPPED
    stripped = dict(element)
    stripped["hooks"] = kept
    return stripped


def _read(settings: Path) -> tuple[dict, str | None]:
    """The document in ``settings`` and the text it came from; no file reads as no settings.

    BUILD-PLAN M1.3 requires an invalid settings file to abort without writing, and this is
    the one place that decides what invalid means: a file that cannot be opened at all,
    text that is not UTF-8, text that is not JSON, a JSON value that is not an object, a
    ``hooks`` key that is not an object, and a value where one of afterthought's three
    events lives that is not a list. Each is refused rather than overwritten, because a
    file afterthought cannot read is a file it cannot show a diff against, and it is
    somebody's working configuration. Text with nothing in it is the exception: an empty
    file is a file with no settings yet, not a broken one.
    """
    if not settings.exists():
        return {}, None
    try:
        original = settings.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{settings} is not UTF-8 text ({exc}); nothing was written") from exc
    except OSError as exc:
        raise InstallError(f"cannot read {settings} ({exc}); nothing was written") from exc
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


def _target(settings: Path) -> Path:
    """The file the settings path points at.

    ``os.replace`` would unlink a symbolic link the user made at that path and put a
    plain file where the link was, silently taking the linked file - usually the one in
    their dotfiles - out of use. Every read, backup and write then goes to the same
    file the link names, and the link itself is left alone.
    """
    if not settings.is_symlink():
        return settings
    return Path(os.path.realpath(settings))


def _commit(
    settings: Path, original: str | None, document: dict, *, changed: bool, dry_run: bool
) -> Change:
    """Report the change, and write it unless nothing changed or this is a dry run.

    Nothing is written when nothing changed, so a second install leaves the file's bytes
    untouched and no backup behind. An unchanged merge is not reformatted either, which
    keeps a hand-written settings file hand-written. What is written goes to the file the
    settings path points at, so a symbolic link the user made there keeps working.
    """
    if not changed:
        return Change(changed=False, backup=None, diff="")
    target = _target(settings)
    after = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    diff = "\n".join(
        difflib.unified_diff(
            (original or "").splitlines(),
            after.splitlines(),
            fromfile=f"{target} (before)",
            tofile=f"{target} (after)",
            lineterm="",
        )
    )
    if dry_run:
        return Change(changed=True, backup=None, diff=diff)
    return Change(changed=True, backup=_write(target, after, original), diff=diff)


def _backup(settings: Path, original: str | None) -> Path | None:
    """Copy the text a write is about to replace into a file beside it. This is the backup
    rule, where a reader of a write meets it: settings.json changes only once its previous
    text is safe next to it.

    The copy is named for the tool that made it and stamped with the UTC second, with a
    bumped suffix whenever that name is already taken, so two writes in one second leave
    two backups and one install never overwrites another's backup. It is created private
    and then given the mode of the file it copies: settings.json routinely holds API keys,
    and the backup holds their exact text, so a file the user keeps closed cannot leave a
    copy anyone can read. A file with no text of its own has nothing to keep, so no backup
    is written to claim an empty settings file was lost.
    """
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    mode = stat.S_IMODE(settings.stat().st_mode)
    for suffix in itertools.count():
        tail = "" if suffix == 0 else f"-{suffix}"
        backup = settings.with_name(f"{settings.name}.aft-{stamp}{tail}.bak")
        try:
            handle = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(original)
        os.chmod(backup, mode)
        return backup


def _new_file_mode() -> int:
    """The mode a plain write gives a file that does not exist yet: the create default.

    Reading the umask is the only way to ask what that write would produce, and setting it
    back happens before anything else does.
    """
    umask = os.umask(0)
    os.umask(umask)
    return 0o666 & ~umask


def _write(settings: Path, text: str, original: str | None) -> Path | None:
    """Put the new text in place in one step, and return the backup of the text it replaces.

    A half-written settings file costs the user their whole Claude Code configuration, so
    the new text goes to a temporary file in the same directory and ``os.replace`` moves it
    over the old one, keeping that file's permissions. A file afterthought creates - one
    not there yet, or the target of a dangling link - takes the mode a plain write would
    have given it. The previous text is backed up only once the new one is staged and the
    replace is next, so a refusal never leaves behind a copy of text that was never
    superseded. A directory afterthought cannot write in is refused the same way a file it
    cannot read is.
    """
    try:
        settings.parent.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(settings.stat().st_mode) if settings.exists() else _new_file_mode()
        handle, temporary = tempfile.mkstemp(dir=settings.parent, prefix=f"{settings.name}.aft-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                output.write(text)
            os.chmod(temporary, mode)
            backup = _backup(settings, original)
            os.replace(temporary, settings)
        except OSError:
            os.unlink(temporary)
            raise
    except OSError as exc:
        raise InstallError(f"cannot write {settings} ({exc}); it was not changed") from exc
    return backup
