"""Install and uninstall afterthought's omp hook module.

omp (Oh My Pi) discovers hook modules from two subdirectories of its agent directory,
``hooks/pre`` and ``hooks/post``; a file sitting directly in ``hooks`` is never found (omp's
``hooks.md``, "Discovery"). Afterthought's lands at ``hooks/pre/afterthought.ts``, a copy of
``integrations/omp/afterthought.ts``. A module discovered there is loaded as an extension
module and handed omp's ``ExtensionAPI``, which is what lets it answer
``before_agent_start`` with a system prompt of its own.

omp resolves that agent directory as its configuration docs describe
(``config-usage.md``, "Profiles"; ``environment-variables.md``):

* ``OMP_PROFILE`` names the profile and wins over ``PI_PROFILE`` even when it is set to an
  empty value; empty, whitespace and ``default`` all mean the default profile.
* A named profile puts the agent directory at ``~/.omp/profiles/<name>/agent`` and ignores
  ``PI_CODING_AGENT_DIR`` - a profile never sees the default profile's agent configuration.
* In the default profile, ``PI_CODING_AGENT_DIR`` moves the agent directory, and
  ``~/.omp/agent`` is where it stands otherwise.

Unlike pi's extension, which owns a directory of its own, this one file sits in a directory
omp shares with every other hook module. Two rules hold everything that is not
afterthought's:

* A write touches one file, ``afterthought.ts``, and the rescue copies named after it.
  Sibling hook modules are never read, reordered or removed, and neither ``hooks/pre`` nor
  ``hooks`` is ever removed, whether or not this installer created it. The rescue copies end
  in ``.bak`` rather than ``.ts`` or ``.js`` for the same reason the installer's temporary
  files do: omp would load a second hook module from any other name.
* A write is preceded by a backup of the text it replaces, and a path that holds the wrong
  kind of thing - a file where a directory belongs, a directory where the module belongs, a
  link of anybody's standing in place of either - is refused with nothing written. The path
  of afterthought's own file is judged as itself, never as what a link behind it points at.

Every function here takes the hooks directory as an argument so a test can point it somewhere
temporary; ``hooks_dir`` is the default the command line uses.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

AGENT = "omp"

# The subdirectory of omp's hooks directory that holds a module running before the agent, and
# the name afterthought's own module carries there.
HOOK_PHASE = "pre"
HOOK_FILE = "afterthought.ts"

# The hook module's path in the repository that holds this package.
SOURCE = Path("integrations") / "omp" / "afterthought.ts"

# The name a rescue copy carries: the file whose text it holds, this tool's own mark, and the
# UTC second it was made. An uninstall reads the mark back to tell its own copies from a thing
# of somebody else's standing in the same directory, so both ends name it here. The suffix
# keeps a copy out of omp's ``*.ts`` and ``*.js`` scan.
BACKUP_PREFIX = f"{HOOK_FILE}.aft-"
BACKUP_SUFFIX = ".bak"


class InstallError(Exception):
    """The hook module is not one afterthought can install, or sits where it cannot write."""


@dataclass(frozen=True)
class Change:
    """What one install or uninstall did, or would have done under ``--dry-run``.

    ``changed`` is False when the path already held exactly what was asked for, in which case
    nothing was written or removed, no copy was named and no diff was drawn. ``backup`` is
    where the text this change replaces or removes is kept, or would be kept under
    ``--dry-run``; None when there is no text worth a copy. ``diff`` is the unified diff of
    the hook module's lines before and after, empty when its text changes no line. An
    uninstall names the paths it takes away in ``removed``; an install replaces text and takes
    nothing away, so its ``removed`` is empty.
    """

    changed: bool
    backup: Path | None
    diff: str
    removed: tuple[Path, ...] = ()


def agent_dir() -> Path:
    """The agent directory omp reads for the current user, profiles and all.

    The module docstring says where each of the three answers comes from. ``OMP_PROFILE`` is
    read as present rather than as truthy, because omp treats it as set even when it is
    empty and falls back to ``PI_PROFILE`` only when it is absent altogether.
    """
    profile = os.environ.get("OMP_PROFILE")
    if profile is None:
        profile = os.environ.get("PI_PROFILE", "")
    profile = profile.strip()
    if profile and profile != "default":
        return Path.home() / ".omp" / "profiles" / profile / "agent"
    agent = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(agent).expanduser() if agent else Path.home() / ".omp" / "agent"


def hooks_dir() -> Path:
    """The hooks directory omp scans for the current user."""
    return agent_dir() / "hooks"


def target_path(hooks: Path) -> Path:
    """The one file afterthought owns inside omp's hooks directory."""
    return hooks / HOOK_PHASE / HOOK_FILE


def hook_source() -> Path:
    """The hook module this package ships, in the installed package or the checkout."""
    installed = Path(__file__).resolve().parents[1] / SOURCE
    if installed.is_file():
        return installed
    checkout = Path(__file__).resolve().parents[3] / SOURCE
    if checkout.is_file():
        return checkout
    raise InstallError(f"no {SOURCE} beside the aft package; run from a checkout")


def install(hooks: Path, *, dry_run: bool = False) -> Change:
    """Copy afterthought's hook module into ``hooks/pre``.

    Only afterthought's own file is written, and writing it happens once: a target that
    already holds the shipped text is left byte for byte, so installing twice changes nothing
    on the second run and no backup of an unchanged file is kept. A target holding anything
    else - an older afterthought hook, or text someone wrote there by hand - is backed up and
    replaced, because this is the one path afterthought owns in omp's configuration.
    """
    try:
        target = _target(hooks)
        text = hook_source().read_text(encoding="utf-8")
        original = _text(target)
        if original == text:
            return Change(changed=False, backup=None, diff="")
        diff = _diff(target, original, text)
        backup = _backup(target, original, dry_run=dry_run)
        if dry_run:
            return Change(changed=True, backup=backup, diff=diff)
        _write(target, text)
        return Change(changed=True, backup=backup, diff=diff)
    except OSError as exc:
        raise InstallError(f"cannot install into {hooks}: {exc}") from exc


def uninstall(hooks: Path, *, dry_run: bool = False) -> Change:
    """Remove exactly afterthought's own hook module from ``hooks/pre``.

    The one file the installer creates goes, backed up first when it holds text other than
    the module afterthought ships - the copy beside it is there for text that exists nowhere
    else, and a file that matches the shipped one needs no copy to survive it. The rescue
    copies of earlier runs, stamped with this installer's own mark, hold text of writes it
    replaced and are its own to take back, so they go with the file they were made beside -
    and a run that has to keep text of somebody's own leaves the whole footprint where it was.

    No directory is ever removed. ``hooks/pre`` is omp's own, shared with every other hook
    module, so an uninstall that emptied it would be taking away a container afterthought did
    not create. ``Change.removed`` names the paths it actually took - and so says nothing when
    there was nothing of afterthought's there to take.
    """
    try:
        target = _target(hooks)
        container = target.parent
        original = _text(target)
        # Text of somebody's own gets a copy beside the file it removes, so whether an
        # uninstall will leave such a copy behind decides whether the earlier copies go too.
        keep = original if original and original != _shipped() else None
        copies = _own_copies(container) if original is not None and keep is None else []
        removed: list[Path] = []
        if original is not None:
            removed.append(target)
        removed += copies
        changed = bool(removed)
        diff = _diff(target, original, "")
        backup = _backup(target, keep, dry_run=dry_run)
        if dry_run:
            return Change(changed=changed, backup=backup, diff=diff, removed=tuple(removed))
        if target in removed:
            target.unlink()
        for copy in copies:
            copy.unlink()
        return Change(changed=changed, backup=backup, diff=diff, removed=tuple(removed))
    except OSError as exc:
        raise InstallError(f"cannot uninstall from {hooks}: {exc}") from exc


def _own_copies(container: Path) -> list[Path]:
    """The rescue copies this installer stamped inside ``container``, oldest first; none where
    there is no directory to hold them.

    A thing of somebody else's under such a name - a directory, or a link they made - is
    judged as itself and keeps its place. ``uninstall`` says what it does with the copies.
    """
    if not container.is_dir():
        return []
    return sorted(
        entry
        for entry in container.iterdir()
        if entry.name.startswith(BACKUP_PREFIX)
        and entry.name.endswith(BACKUP_SUFFIX)
        and stat.S_ISREG(_own_mode(entry) or 0)
    )


def _target(hooks: Path) -> Path:
    """The file afterthought owns, once the paths leading to it are the right kind of thing.

    A path that exists as the wrong kind of thing is refused rather than worked around. Both
    ``hooks`` and ``hooks/pre`` are omp's own in the general case, and flattening one to make
    a write succeed would take away a container afterthought did not create.

    Afterthought's own file is judged as itself, by ``lstat``, never as whatever a link behind
    it points at. A symlink there is a person's own arrangement - a module whose dotfiles are
    not on this machine - and writing or unlinking would take their link away, with no text of
    it to back up and no removal to report.
    """
    if hooks.exists() and not hooks.is_dir():
        raise InstallError(f"{hooks} is not a directory; nothing was written")
    container = hooks / HOOK_PHASE
    container_mode = _own_mode(container)
    if container_mode is not None and not stat.S_ISDIR(container_mode):
        raise InstallError(f"{container} is not a directory; nothing was written")
    target = container / HOOK_FILE
    target_mode = _own_mode(target)
    if target_mode is not None and not stat.S_ISREG(target_mode):
        raise InstallError(f"{target} is not a file; nothing was written")
    return target


def _own_mode(path: Path) -> int | None:
    """The mode of ``path`` itself, a link un-followed; None where nothing stands."""
    try:
        return path.lstat().st_mode
    except OSError:
        return None


def _text(target: Path) -> str | None:
    """The text in ``target``; no file reads as nothing installed yet.

    Text that is not UTF-8 is refused rather than replaced: it is somebody's hook module, and
    a write here would take away text this tool cannot read.
    """
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{target} is not UTF-8 text ({exc}); nothing was written") from exc


def _shipped() -> str | None:
    """The text of the hook module this package installs, or None when it cannot be found.

    Only an uninstall needs the answer when there is no answer to give: it then keeps a copy
    of whatever it is removing, because it cannot tell that text from the one it wrote.
    """
    try:
        return hook_source().read_text(encoding="utf-8")
    except InstallError:
        return None


def _diff(target: Path, before: str | None, after: str) -> str:
    """What the change does to the hook module's lines, for ``--dry-run`` to print.

    A file that does not exist yet has no text to diff against, so it goes in as one block of
    additions, which is what installing it means. Each line of the result is terminated once,
    so what the dry run prints is a patch a tool could apply.

    Lines, not bytes, are the unit: text that differs only by a final newline or a CRLF ending
    changes no line, so its diff is empty, and the command's own line about the write or the
    removal is what says a change is coming.
    """
    return "\n".join(
        difflib.unified_diff(
            (before or "").splitlines(),
            after.splitlines(),
            fromfile=f"{target} (before)",
            tofile=f"{target} (after)",
            lineterm="",
        )
    )


def _backup(target: Path, original: str | None, *, dry_run: bool) -> Path | None:
    """Name the file that keeps the text this change replaces or removes, and copy the text
    into it unless the change is only being previewed. This is the backup rule, where a reader
    of a write meets it: afterthought's hook module changes only once its previous text is on
    the device in a file beside it, and a preview says that copy is coming without making it.

    The copy is named for the tool that made it and stamped with the UTC second, so one
    install never overwrites another's backup. A file with no text of its own has nothing to
    keep, so no backup is named to claim a file was lost that was never there.
    """
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}")
    if not dry_run:
        with open(backup, "w", encoding="utf-8") as copy:
            copy.write(original)
            copy.flush()
            os.fsync(copy.fileno())
    return backup


def _write(target: Path, text: str) -> None:
    """Put the hook module in place in one step, so a crash leaves the old file or the new one.

    The text goes to a temporary file in the same directory and ``os.replace`` moves it over
    the old one, with its bytes fsynced before that rename so the published name never stands
    over text the device has not taken. The temporary name ends in neither ``.ts`` nor ``.js``,
    so omp cannot load it as a hook module of its own while it stands. The new file keeps the
    permissions of the one it replaces.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f"{target.name}.aft-", suffix=BACKUP_SUFFIX
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError:
        os.unlink(temporary)
        raise
