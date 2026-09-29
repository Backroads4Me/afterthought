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
  kind of thing - a file where a directory belongs, a directory where the extension belongs -
  is refused with nothing written.
"""

from __future__ import annotations

import contextlib
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

    ``changed`` is False when the target already held exactly what was asked for, in which
    case nothing was written and no backup was taken. ``diff`` is the unified diff of the
    extension file's text before and after, and is empty when nothing changed.
    """

    changed: bool
    backup: Path | None
    diff: str


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
        if dry_run:
            return Change(changed=True, backup=None, diff=diff)
        backup = _backup(target, original)
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
    where it stood.
    """

    try:
        target = _target(extensions)
        container = target.parent
        original = _text(target)
        if original is None and not _is_our_empty_dir(container):
            return Change(changed=False, backup=None, diff="")
        diff = _diff(target, original, "")
        if dry_run:
            return Change(changed=True, backup=None, diff=diff)
        backup = _backup(target, None if original == _shipped() else original)
        if target.exists():
            target.unlink()
        if _is_our_empty_dir(container):
            # The extension file is what uninstalling means. A container that will
            # not go away - one symlinked to a directory of somebody's own, say - is
            # theirs to keep, and leaving it does not undo the removal above.
            with contextlib.suppress(OSError):
                container.rmdir()
        return Change(changed=True, backup=backup, diff=diff)
    except OSError as exc:
        raise InstallError(f"cannot uninstall from {extensions}: {exc}") from exc


def _is_our_empty_dir(container: Path) -> bool:
    """True when ``container`` exists as a directory holding nothing.

    An empty one is afterthought's own - the installer created it and nobody else has
    put anything in it - so removing it is ours to do, and there is nothing left of
    afterthought's in it to remove. Anything else standing in the way, including a
    directory still holding somebody else's files, is not ours to touch.
    """
    return container.is_dir() and not any(container.iterdir())


def _target(extensions: Path) -> Path:
    """The file afterthought owns, once the paths leading to it are the right kind of thing.

    A path that exists as the wrong kind of thing is refused rather than worked around. The
    extensions directory and afterthought's own directory are somebody else's in the general
    case - pi's install put one there, or a user's hand did - and flattening either to make a
    write succeed would be the uninstall's exact failure: removing a container afterthought
    did not create.
    """
    container = extensions / EXTENSION_DIR
    for path in (extensions, container):
        if path.exists() and not path.is_dir():
            raise InstallError(f"{path} is not a directory; nothing was written")
    target = container / EXTENSION_FILE
    if target.exists() and not target.is_file():
        raise InstallError(f"{target} is not a file; nothing was written")
    return target


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
    """What the change does to the extension file's text, for ``--dry-run`` to print.

    A file that does not exist yet has no text to diff against, so it goes in as one block of
    additions, which is what installing it means.
    """
    return "\n".join(
        difflib.unified_diff(
            (before or "").splitlines(),
            after.splitlines(),
            fromfile=f"{target} (before)",
            tofile=f"{target} (after)",
        )
    )


def _backup(target: Path, original: str | None) -> Path | None:
    """Copy the text a change is about to replace or remove into a file beside it. This is
    the backup rule, where a reader of a write meets it: afterthought's extension file
    changes only once its previous text is safe next to it.

    The copy is named for the tool that made it and stamped with the UTC second, so one
    install never overwrites another's backup. A file with no text of its own has nothing to
    keep, so no backup is written to claim a file was lost that was never there.
    """
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}.aft-{stamp}.bak")
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
