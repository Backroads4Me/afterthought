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

* A merge touches only what afterthought owns: the command hooks it writes, found by their
  command line inside the matcher groups of its three event lists. Other people's hook
  entries keep their position and their order, and uninstall removes command hooks and
  nothing else: every key of the user's keeps its place, a matcher group keeps its matcher
  once our hook leaves it, the ``hooks`` object an uninstall empties keeps its key as an
  empty object, and only an event list this very uninstall emptied of our last entry goes
  with it, so nothing of the user's is moved or removed. The file is read, edited in place,
  and written back with its key order intact.
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

from aft.install import skill as skill_install
from aft.redact import redact

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
    case nothing was written and no backup was taken. ``preview`` names the entries of ours
    the change adds or removes, each by its event and its own command line, and is empty
    when nothing changed. ``diff`` is the unified diff of the settings file's lines before
    and after, as `_diff` draws it, and is empty when nothing changed.
    """

    changed: bool
    backup: Path | None
    preview: str
    diff: str = ""


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
    left exactly as it is: an event's whole list is searched, at any depth, for the command
    line before anything is appended, so aft never writes a second copy of a command the
    event already runs - whether that command sits in the group an install wrote, in one the
    user moved it into, or in an element the user wrote by hand. Installing twice therefore
    changes nothing on the second run, and an entry the user edited after installing - its
    timeout, its matcher - stays as they set it.
    The command line is the whole identity of an entry, which is what makes our own
    entries findable again without a marker field of afterthought's in the user's file.
    """
    document, original = _read(settings)
    hooks = _hooks(document)

    entries = []
    for event in HOOK_EVENTS:
        groups = _groups(hooks, event)
        if not any(_runs(element, hook_command(event)) for element in groups):
            groups.append(_group(event))
            entries.append(event)

    return _commit(settings, original, document, entries=entries, adding=True, dry_run=dry_run)


def uninstall(settings: Path, *, dry_run: bool = False) -> Change:
    """Remove exactly afterthought's own hook entries from ``settings``.

    Only the three events afterthought installs are read, and within them only the command
    hooks running ``aft hook claude <event>`` inside a matcher group - the shape an install
    writes, plus whatever the user changed inside such a group. Nothing else is deleted and
    nothing is reordered: an element that is not ours keeps its keys and its place, and an
    event array that still holds one of them keeps its key. A command line of ours that
    someone wrote directly into an event list, one level above where an install writes,
    is theirs and stays.
    A container of aft's goes only when this very operation empties it and it then holds no
    key of anyone's: a group left holding nothing but an empty ``hooks`` list, and an event
    list whose last entry was our hook. An array this operation found empty, and one it
    leaves holding a hook of someone else's, keep their keys, so an event key is dropped only
    by the run that emptied it and an array the user left empty survives uninstall for the
    reason it looks empty. The ``hooks`` object itself is never dropped: an empty one costs
    Claude Code nothing, and choosing between the object an install created and one the file
    already had would need the history this rule does not have.
    """
    document, original = _read(settings)
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return Change(changed=False, backup=None, preview="")

    entries = []
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        kept = []
        removed = False
        for element in groups:
            left = _remove(element, event)
            if left is not element:
                removed = True
                entries.append(event)
            if left is not _DROPPED:
                kept.append(left)
        if removed and not kept:
            del hooks[event]
        else:
            groups[:] = kept

    return _commit(settings, original, document, entries=entries, adding=False, dry_run=dry_run)


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


def _is_ours(hook: object, event: str) -> bool:
    """Whether one command hook inside a matcher group is afterthought's own for this event.

    The command line is the whole identity of an entry, which is what makes a hook we wrote
    - or one the user has since edited inside that same group - findable again without a
    marker field of afterthought's in the user's file.
    """
    return isinstance(hook, dict) and hook.get("command") == hook_command(event)


def _runs(value: object, command: str) -> bool:
    """Whether one command line appears anywhere inside a value, at any depth.

    Install asks this of a whole event list, so a hook a person wrote by hand - aft's command
    as a bare element of the list, or nested inside a group of theirs at whatever depth they
    chose - counts as that event already running the command, and install leaves it alone
    rather than writing a second copy of it. Deletion does not use this: ``_remove``
    recognizes only the shape an install writes, so an element aft cannot attribute to itself
    survives uninstall.
    """
    if isinstance(value, str):
        return value == command
    if isinstance(value, dict):
        return any(_runs(nested, command) for nested in value.values())
    if isinstance(value, list):
        return any(_runs(nested, command) for nested in value)
    return False


_DROPPED = object()


def _remove(element: object, event: str) -> object:
    """One element of an event list with afterthought's own entry taken out of it.

    Only a group's own ``hooks`` list is searched for our command line, so every other
    element - a bare command hook someone wrote by hand, a string, a number, a JSON null -
    comes back as the element itself, which is how uninstall tells an element it rewrote from
    one it left alone. The ``_DROPPED`` sentinel, compared by identity,
    comes back for a group left holding nothing but an empty ``hooks`` list, the one shape
    that holds no key of anyone's. A group with a matcher, or any other key of its own,
    keeps that key even once our hook is gone.
    """
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
    the one place that decides what invalid means: a file that cannot be opened or even
    looked up - a settings directory the user cannot search, say - text that is not UTF-8,
    text that is not JSON, a JSON value that is not an object, a ``hooks`` key that is not an
    object, and a value where one of afterthought's three events lives that is not a list.
    Each is refused rather than overwritten, because a file afterthought cannot read is a
    file it cannot merge into, and it is somebody's working configuration. Text with nothing
    in it is the exception, and so is a name that holds no file at all: an absent settings
    file is one with no settings yet, not a broken one, and so is a name under a path whose
    parent is no directory - which the write then refuses as the write it is.
    """
    try:
        original = settings.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return {}, None
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
    settings: Path,
    original: str | None,
    document: dict,
    *,
    entries: list[str],
    adding: bool,
    dry_run: bool,
) -> Change:
    """Report the change, and write it unless nothing changed or this is a dry run.

    Nothing is written when nothing changed, so a second install leaves the file's bytes
    untouched and no backup behind. An unchanged merge is not reformatted either, which
    keeps a hand-written settings file hand-written. What is written goes to the file the
    settings path points at, so a symbolic link the user made there keeps working.

    ``--dry-run`` prints two things. The preview is built from ``entries``, the events this
    change adds or removes, and never from the file's text. The diff is drawn from the text
    the file holds and the text a write would put there, so it shows where the entries land
    and every other line the write would touch; `_diff` says what keeps the configuration
    the user left beside them out of it.
    """
    if not entries:
        return Change(changed=False, backup=None, preview="")
    target = _target(settings)
    verb, where = ("add", "to") if adding else ("remove", "from")
    timeout = f", each with a {HOOK_TIMEOUT_SECONDS} second timeout" if adding else ""
    lines = [f"{len(entries)} entries to {verb} {where} {target}{timeout}:"]
    lines += [f"  {event}: {hook_command(event)}" for event in entries]
    preview = "\n".join(lines)
    after = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    diff = _diff(target, original, after)
    if dry_run:
        return Change(changed=True, backup=None, preview=preview, diff=diff)
    backup = _write(target, after, original)
    return Change(changed=True, backup=backup, preview=preview, diff=diff)


def _diff(target: Path, before: str | None, after: str) -> str:
    """What a write does to the settings file's lines, for ``--dry-run`` to print.

    The diff is between the text the file holds and the text the write would put there, so
    it shows a reformatting of the file as faithfully as it shows afterthought's own
    entries. A file that does not exist yet goes in as one block of additions.

    Settings files routinely hold API keys, so the diff carries no context lines: a line
    appears only when the write changes it, and each hunk header says where it sits. A line
    of the user's own can still change - the last line before a new key gains a comma, and a
    file not laid out the way ``json.dumps`` lays it out is rewritten whole - so every line
    also goes through `aft.redact.redact` and a secret of a shape it knows prints as its
    marker.
    """
    lines = difflib.unified_diff(
        (before or "").splitlines(),
        after.splitlines(),
        fromfile=f"{target} (before)",
        tofile=f"{target} (after)",
        lineterm="",
        n=0,
    )
    return "\n".join(redact(line) for line in lines)


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


def _write(settings: Path, text: str, original: str | None) -> Path | None:
    """Put the new text in place in one step, and return the backup of the text it replaces.

    A half-written settings file costs the user their whole Claude Code configuration, so
    the new text goes to a temporary file in the same directory and ``os.replace`` moves it
    over the old one, keeping that file's permissions. A file afterthought creates - one not
    there yet, or the target of a dangling link - keeps the private mode its temporary
    file was created under. The previous text is backed up only once the new one is
    staged and the replace is next, so a refusal never leaves behind a copy of text that
    was never superseded, and the staged file is taken back whenever the replace did not
    happen. A directory afterthought cannot write in, and settings text that will not encode
    as UTF-8, are refused the same way a file it cannot read is.
    """
    try:
        settings.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=settings.parent, prefix=f"{settings.name}.aft-")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as output:
                output.write(text)
            if settings.exists():
                os.chmod(temporary, stat.S_IMODE(settings.stat().st_mode))
            backup = _backup(settings, original)
            os.replace(temporary, settings)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except (OSError, UnicodeEncodeError) as exc:
        raise InstallError(f"cannot write {settings} ({exc}); it was not changed") from exc
    return backup


def skills_dir(settings: Path | None = None) -> Path:
    """The skills directory Claude Code reads for the current user."""
    if settings is not None:
        return settings.parent / "skills"
    return Path.home() / ".claude" / "skills"


def skill_target(skills: Path) -> Path:
    """The one skill file afterthought installs in Claude Code's skills directory."""
    return skill_install.target_path(skills)


def install_skill(skills: Path, *, dry_run: bool = False) -> skill_install.SkillChange:
    """Install the aft-library-import skill into Claude Code's skills directory."""
    return skill_install.install(skills, dry_run=dry_run)


def check_skill_target(skills: Path) -> Path:
    """Validate that the skill target path and container are valid and readable."""
    return skill_install.check_target(skills)


def uninstall_skill(skills: Path, *, dry_run: bool = False) -> skill_install.SkillChange:
    """Remove the aft-library-import skill from Claude Code's skills directory."""
    return skill_install.uninstall(skills, dry_run=dry_run)
