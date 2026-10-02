"""Install and uninstall afterthought's skill, ``aft`` (BUILD-PLAN M7.5, DESIGN 11).

Afterthought ships one skill for Claude Code and pi, installed by their adapters (`aft
install claude` and `aft install pi`). Its ``SKILL.md`` maps what a user asks of memory to
the `aft` command that does it; ``library-import.md`` beside it holds the library import
procedure, which an agent reads only when asked to import documentation.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SKILL_NAME = "aft"
SKILL_FILE = "SKILL.md"
# Every file the skill ships, its SKILL.md first.
FILES = (SKILL_FILE, "library-import.md")

SOURCE_DIR = Path("integrations") / "skills" / SKILL_NAME
SOURCE = SOURCE_DIR / SKILL_FILE

# The names afterthought's skill was installed under before, each with the sha256 of every
# SKILL.md afterthought shipped under it. A copy that matches one is ours to remove; a copy
# that matches none is someone else's skill, or one edited by hand, and is left alone.
LEGACY = {
    "library-import": frozenset(
        {
            "00c93e86a7ce66b9170d0ae6876c63bb9ffaa8d6ab00f1295f72ed5186c1c323",
            "143169eb9f929dbad8a90615f47151ec63330d746740b4a0a76397eec7e7737c",
            "2309510bcc4a8f435719269a89836fd3f25ea83eaa2431aef8f33fa6c6c7c5fa",
            "3678abbfde72260e90034b67e0963b6356d9550d0d8364b39834bf1420352e3a",
            "794c9dfdb9f27a1d690a4471a0e1905a5352691ad6517b9189e2dc87ef8cb737",
            "80c12ad615a62d8d6ea2b7e2a8cec701e5896a0ca691e466e706a051f2c28b1a",
            "a3260210d8775f68a43d4304cd374105241c7efb2f5a3c5eb4761ca4264cf972",
            "e10586af1a1e9c75eda84f507b674435d81da4eb6484ece65092d7d781a2fd93",
        }
    ),
    "aft-library-import": frozenset(
        {"d865dc4d57f9a3f9e649407837881b7cdf57f3873d34acd02faa3bb6841e9660"}
    ),
}

# The sha256 of every version of each file afterthought shipped. An installed copy that
# matches one is ours and is replaced or removed without a backup; any other text is
# someone's own and is kept first. A test holds every file's current text to this list.
SHIPPED_SHA256 = {
    "SKILL.md": frozenset(
        {
            "89c53d21ee9d4a213882ab0c919720ffe37c2728f53a91f98bfb93526ca30849",
            "fe30cf9c9356880bd562b199d2a800b3ec5c99f713d0ec277a34777ece274a38",
            "65feb68b62ba3311c31ae765ca67740f9e98c978b1be61ca773a810f12e6a60b",
        }
    ),
    "library-import.md": frozenset(
        {"373576f3ed44da7aa988cf0baf7cba9291cc10b0912c4948c9f96b28800572f9"}
    ),
}

# A file holding someone's own text is kept beside it as <file>.aft-<time>.bak.
BACKUP_INFIX = ".aft-"
BACKUP_SUFFIX = ".bak"


class InstallError(Exception):
    """The skill cannot be installed or uninstalled."""


@dataclass(frozen=True)
class SkillChange:
    """What one skill install or uninstall did, or would have done under ``--dry-run``.

    ``changed`` is False when every file already held the shipped content (on install) or
    was already absent (on uninstall). ``target`` is the skill's SKILL.md. ``backup`` is
    where the first file holding someone's own text was preserved, and ``backups`` names
    every such copy. ``diff`` is the unified diff of every file's lines. ``removed`` names
    the paths removed on uninstall.
    """

    changed: bool
    backup: Path | None
    diff: str
    target: Path
    removed: tuple[Path, ...] = ()
    backups: tuple[Path, ...] = ()


def skill_source() -> Path:
    """The skill's SKILL.md shipped with the package, installed or in the checkout."""
    return _source(SKILL_FILE)


def _source(name: str) -> Path:
    installed = Path(__file__).resolve().parents[1] / SOURCE_DIR / name
    if installed.is_file():
        return installed
    return Path(__file__).resolve().parents[3] / SOURCE_DIR / name


def skill_text() -> str:
    """The shipped SKILL.md."""
    return file_text(SKILL_FILE)


def file_text(name: str) -> str:
    """One shipped file of the skill."""
    source = _source(name)
    if source.is_file():
        return source.read_text(encoding="utf-8")
    raise InstallError(f"no {SOURCE_DIR / name} beside the aft package; run from a checkout")


def target_path(skills_dir: Path) -> Path:
    """The skill's SKILL.md inside the skills directory."""
    return skills_dir / SKILL_NAME / SKILL_FILE


def retire_legacy(skills_dir: Path, *, dry_run: bool = False) -> list[str]:
    """Remove the skills afterthought installed under earlier names, and say so.

    Only a copy afterthought shipped is removed, with its directory when nothing else is in
    it; a skill of an old name with other content is kept and named, so a person can decide.
    Returns the lines to print, one per old name found.
    """
    lines: list[str] = []
    for name, shipped in LEGACY.items():
        container = skills_dir / name
        target = container / SKILL_FILE
        if not target.is_file() or target.is_symlink():
            continue
        try:
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise InstallError(f"cannot read {target}: {exc}") from exc
        if digest not in shipped:
            lines.append(f"kept {target}: it is not a copy afterthought shipped")
            continue
        if dry_run:
            lines.append(f"would remove the old skill {container}")
            continue
        try:
            target.unlink()
            if not any(container.iterdir()):
                container.rmdir()
        except OSError as exc:
            raise InstallError(f"cannot remove the old skill {container}: {exc}") from exc
        lines.append(f"removed the old skill {container}")
    return lines


def install(skills_dir: Path, *, dry_run: bool = False) -> SkillChange:
    """Install the aft skill into ``skills_dir``: every file it ships, a file holding
    someone's own text backed up before it is replaced."""
    try:
        targets = _targets(skills_dir)
        changed, diffs, backups = False, [], []
        for name, target in targets.items():
            text = file_text(name)
            original = _text(target)
            if original == text:
                continue
            changed = True
            diffs.append(_diff(target, original, text))
            backup = _backup(target, _own(name, original), dry_run=dry_run)
            if backup is not None:
                backups.append(backup)
            if not dry_run:
                _write(target, text)
        return SkillChange(
            changed=changed,
            backup=backups[0] if backups else None,
            diff="\n".join(d for d in diffs if d),
            target=targets[SKILL_FILE],
            backups=tuple(backups),
        )
    except OSError as exc:
        raise InstallError(f"cannot install skill into {skills_dir}: {exc}") from exc


def check_target(skills_dir: Path) -> Path:
    """Validate that the skill's paths and container are valid and readable."""
    try:
        targets = _targets(skills_dir)
        for name, target in targets.items():
            _text(target)
            file_text(name)
        return targets[SKILL_FILE]
    except OSError as exc:
        raise InstallError(f"cannot install skill into {skills_dir}: {exc}") from exc


def uninstall(skills_dir: Path, *, dry_run: bool = False) -> SkillChange:
    """Remove the aft skill from ``skills_dir``. A file holding someone's own text is backed
    up first, and the directory goes only when nothing else is left in it."""
    try:
        targets = _targets(skills_dir)
        container = targets[SKILL_FILE].parent
        removed: list[Path] = []
        diffs: list[str] = []
        backups: list[Path] = []
        for name, target in targets.items():
            original = _text(target)
            if original is None:
                continue
            removed.append(target)
            diffs.append(_diff(target, original, ""))
            backup = _backup(target, _own(name, original), dry_run=dry_run)
            if backup is not None:
                backups.append(backup)
        will_be_empty = container.is_dir() and not any(
            entry for entry in container.iterdir() if entry not in removed
        )
        if removed and will_be_empty:
            removed.append(container)
        if not dry_run:
            for path in removed:
                if path == container:
                    if container.is_dir() and not any(container.iterdir()):
                        container.rmdir()
                else:
                    path.unlink()
        return SkillChange(
            changed=bool(removed),
            backup=backups[0] if backups else None,
            diff="\n".join(d for d in diffs if d),
            target=targets[SKILL_FILE],
            removed=tuple(removed),
            backups=tuple(backups),
        )
    except OSError as exc:
        raise InstallError(f"cannot uninstall skill from {skills_dir}: {exc}") from exc


def _targets(skills_dir: Path) -> dict[str, Path]:
    """Each shipped file's path, SKILL.md first, validating the container's structure."""
    container = skills_dir / SKILL_NAME
    if skills_dir.exists() and not skills_dir.is_dir():
        raise InstallError(f"{skills_dir} is not a directory; nothing was written")
    container_mode = _own_mode(container)
    if container_mode is not None and not stat.S_ISDIR(container_mode):
        raise InstallError(f"{container} is not a directory; nothing was written")
    targets = {}
    for name in FILES:
        target = container / name
        target_mode = _own_mode(target)
        if target_mode is not None and not stat.S_ISREG(target_mode):
            raise InstallError(f"{target} is not a file; nothing was written")
        targets[name] = target
    return targets


def _own_mode(path: Path) -> int | None:
    try:
        return path.lstat().st_mode
    except OSError:
        return None


def _text(target: Path) -> str | None:
    if not target.exists():
        return None
    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise InstallError(f"{target} is not UTF-8 text ({exc}); nothing was written") from exc


def _own(name: str, text: str | None) -> str | None:
    """The text, when it is someone's own rather than a version afterthought shipped."""
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    shipped = SHIPPED_SHA256.get(name, frozenset())
    return None if digest in shipped or text == _shipped(name) else text


def _shipped(name: str) -> str | None:
    try:
        return file_text(name)
    except InstallError:
        return None


def _diff(target: Path, before: str | None, after: str) -> str:
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
    if not original:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = target.with_name(f"{target.name}{BACKUP_INFIX}{stamp}{BACKUP_SUFFIX}")
    if not dry_run:
        with open(backup, "w", encoding="utf-8") as copy:
            copy.write(original)
            copy.flush()
            os.fsync(copy.fileno())
    return backup


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
    handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=f"{target.name}.aft-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except OSError:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise
