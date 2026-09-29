"""Install and uninstall afterthought's pi extension (BUILD-PLAN M1.4).

pi loads user extensions from ``<agent-dir>/extensions``, where the agent directory defaults
to ``~/.pi/agent`` and moves when ``PI_CODING_AGENT_DIR`` names another one (pi's own
configuration reference, "Agent directory"). A directory under ``extensions`` is one
extension whose entry point is ``index.ts``, so afterthought's lands at
``extensions/afterthought/index.ts``, a copy of ``integrations/pi/afterthought.ts``.

The directory is the whole of afterthought's footprint in pi's configuration: it is named for
the tool, which is what lets an uninstall recognize what it created without a marker field of
afterthought's in pi's own files. Every function here takes the extensions directory as an
argument so a test can point it somewhere temporary; ``extensions_dir`` is the default the
command line uses.

Two rules hold everything that is not afterthought's:

* A write touches one file inside a directory named after this tool. Sibling extensions are
  never read, entered, reordered or removed, and the extensions directory itself is never
  removed, whether or not this installer created it.
* A write is preceded by a backup of the text it replaces, and a path that holds the wrong
  kind of thing - a file where a directory belongs, a directory where the extension belongs,
  a link of anybody's standing in place of either - is refused with nothing written. The two
  paths this installer creates and removes are judged as themselves, never as what a link
  behind them points at.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

AGENT = "pi"

# afterthought's own directory under pi's extensions, and the entry point pi reads inside it.
# The repository spells the extension ``afterthought.ts``; pi takes a directory's entry point
# as ``index.ts``, which is the name it installs under.
EXTENSION_DIR = "afterthought"
EXTENSION_FILE = "index.ts"

# The extension's path in the repository that holds this package.
SOURCE = Path("integrations") / "pi" / "afterthought.ts"


class InstallError(Exception):
    """The extension is not one afterthought can install, or sits where it cannot write."""


@dataclass(frozen=True)
class Change:
    """What one install or uninstall did, or would have done under ``--dry-run``.

    ``changed`` is False when the paths already held exactly what was asked for, in which
    case nothing was written or removed, no copy was named and no diff was drawn. ``backup``
    is where the text this change replaces or removes is kept, or would be kept under
    ``--dry-run``; None when there is no text worth a copy. ``diff`` is the unified diff of
    the extension file's lines before and after, empty when its text changes no line - a
    difference only in line endings is such a case, and a command that changes something says
    so in its own line whatever the diff holds. An uninstall names the paths it takes away in
    ``removed`` rather than leaving them to be inferred from a diff, and an install replaces
    text and takes nothing away, so its ``removed`` is empty.
    """

    changed: bool
    backup: Path | None
    diff: str
    removed: tuple[Path, ...] = ()


def extensions_dir() -> Path:
    """The extensions directory pi loads for the current user.

    Pi resolves its agent directory from ``PI_CODING_AGENT_DIR`` first and ``~/.pi/agent``
    otherwise (pi's configuration reference, "Agent directory"), and an empty value is no
    directory at all, so the two spellings agree on where the default stands.
    """
    agent_dir = os.environ.get("PI_CODING_AGENT_DIR")
    base = Path(agent_dir).expanduser() if agent_dir else Path.home() / ".pi" / "agent"
    return base / "extensions"


def target_path(extensions: Path) -> Path:
    """The one file afterthought owns inside pi's extensions directory."""
    return extensions / EXTENSION_DIR / EXTENSION_FILE


def extension_source() -> Path:
    """The extension this package ships, in the checkout that holds it.

    ``integrations/`` sits beside ``src/`` rather than inside the package, so the checkout is
    found by walking up from this file: install, then aft, then src, then the root. A package
    installed without that tree cannot say which extension it is installing, so it says so
    instead of guessing.
    """
    source = Path(__file__).resolve().parents[3] / SOURCE
    if not source.is_file():
        raise InstallError(f"no {SOURCE} beside the aft package; run from a checkout")
    return source


def install(extensions: Path, *, dry_run: bool = False) -> Change:
    """Copy afterthought's extension into ``extensions``, in a directory of its own.

    Only afterthought's own file is written, and writing it happens once: a target that
    already holds the shipped text is left byte for byte, so installing twice changes nothing
    on the second run and no backup of an unchanged file is kept. A target holding anything
    else - an older afterthought extension, or text someone wrote there by hand - is backed up
    and replaced, because this is the one path afterthought owns in pi's configuration.
    """
    try:
        target = _target(extensions)
        text = extension_source().read_text(encoding="utf-8")
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
        raise InstallError(f"cannot install into {extensions}: {exc}") from exc


def uninstall(extensions: Path, *, dry_run: bool = False) -> Change:
    """Remove exactly afterthought's own extension from ``extensions``.

    The one file the installer creates goes, backed up first when it holds text other than the
    extension afterthought ships - the copy beside it is there for text that exists nowhere
    else, and a file that matches the shipped one needs no copy to survive it. With nothing of
    anyone else's left inside, the directory of our own goes too; a directory still holding
    anything keeps every byte of it, the extensions directory is never removed, and a sibling
    extension is never looked at, so an uninstall leaves whatever it did not install standing
    where it stood. ``Change.removed`` names the paths it actually took - and so says nothing
    when there was nothing of afterthought's there to take.
    """

    try:
        target = _target(extensions)
        container = target.parent
        original = _text(target)
        # Text of somebody's own gets a copy beside the file it removes, so whether an
        # uninstall will leave such a copy behind decides whether our own directory can go
        # with the file or has to stay holding it.
        keep = original if original and original != _shipped() else None
        # What the uninstall takes away, named before any of it happens so the dry run, the
        # report and the filesystem end up describing the same act: afterthought's own file
        # when one stands there, and the directory of our own once that file has left behind
        # nothing of anybody else's.
        removed: list[Path] = []
        if original is not None:
            removed.append(target)
        if keep is None and _is_ours_to_remove(container, target):
            removed.append(container)
        changed = bool(removed)
        diff = _diff(target, original, "")
        backup = _backup(target, keep, dry_run=dry_run)
        if dry_run:
            return Change(changed=changed, backup=backup, diff=diff, removed=tuple(removed))
        if target in removed:
            target.unlink()
        if container in removed:
            container.rmdir()
        return Change(changed=changed, backup=backup, diff=diff, removed=tuple(removed))
    except OSError as exc:
        raise InstallError(f"cannot uninstall from {extensions}: {exc}") from exc


def _is_ours_to_remove(container: Path, target: Path) -> bool:
    """True when ``container`` is a directory holding nothing but afterthought's own file.

    Empty, or holding only the entry point the uninstall is taking away with it, the
    directory is afterthought's own - it created it and nobody else has put anything in it -
    so it goes. A directory holding anything else keeps every byte of it, and where there is
    no directory at all there is nothing to remove.
    """
    return container.is_dir() and all(entry == target for entry in container.iterdir())


def _target(extensions: Path) -> Path:
    """The file afterthought owns, once the paths leading to it are the right kind of thing.

    A path that exists as the wrong kind of thing is refused rather than worked around. The
    extensions directory is somebody else's in the general case - pi's install put one there,
    or a user's hand did - and flattening it to make a write succeed would be the uninstall's
    exact failure: removing a container afterthought did not create.

    Afterthought's own two paths are judged as themselves, by ``lstat``, never as whatever a
    link behind them points at. A symlink at either one is a person's own arrangement - an
    ``index.ts`` whose dotfiles are not on this machine, a directory of theirs standing in
    afterthought's name - and writing or unlinking there would take their link away, with no
    text of it to back up and no removal to report.
    """
    container = extensions / EXTENSION_DIR
    if extensions.exists() and not extensions.is_dir():
        raise InstallError(f"{extensions} is not a directory; nothing was written")
    container_mode = _own_mode(container)
    if container_mode is not None and not stat.S_ISDIR(container_mode):
        raise InstallError(f"{container} is not a directory; nothing was written")
    target = container / EXTENSION_FILE
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

    Text that is not UTF-8 is refused rather than replaced: it is somebody's extension, and
    a write here would take away text this tool cannot read.
    """
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{target} is not UTF-8 text ({exc}); nothing was written") from exc


def _shipped() -> str | None:
    """The text of the extension this package installs, or None when it cannot be found.

    Only an uninstall needs the answer when there is no answer to give: it then keeps a copy of
    whatever it is removing, because it cannot tell that text from the one it wrote.
    """
    try:
        return extension_source().read_text(encoding="utf-8")
    except InstallError:
        return None


def _diff(target: Path, before: str | None, after: str) -> str:
    """What the change does to the extension file's lines, for ``--dry-run`` to print.

    A file that does not exist yet has no text to diff against, so it goes in as one block of
    additions, which is what installing it means. Each line of the result is terminated once,
    so what the dry run prints is a patch a tool could apply rather than a patch with blank
    lines sprinkled through it.

    Lines, not bytes, are the unit: text that differs only by a final newline or a CRLF
    ending changes no line, so its diff is empty, and the command's own line about the write
    or the removal is what says a change is coming.
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
    into it unless the change is only being previewed. This is the backup rule, where a
    reader of a write meets it: afterthought's extension file changes only once its previous
    text is safe next to it, and a preview says that copy is coming without making it.

    The copy is named for the tool that made it and stamped with the UTC second, so one
    install never overwrites another's backup. A file with no text of its own has nothing to
    keep, so no backup is named to claim a file was lost that was never there.
    """
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}.aft-{stamp}.bak")
    if not dry_run:
        backup.write_text(original, encoding="utf-8")
    return backup


def _write(target: Path, text: str) -> None:
    """Put the extension in place in one step, so a crash leaves the old file or the new one.

    The text goes to a temporary file in the same directory and ``os.replace`` moves it over
    the old one; the new file keeps the permissions of the one it replaces, and an extension
    file pi has already loaded keeps looking like the file it expected.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.aft-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError:
        os.unlink(temporary)
        raise
