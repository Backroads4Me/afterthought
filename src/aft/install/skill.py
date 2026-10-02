"""Install and uninstall afterthought's library import skill (BUILD-PLAN M7.5, DESIGN 11).

Afterthought ships the import procedure as a skill for Claude Code and pi,
installed by their adapters (`aft install claude` and `aft install pi`).
The skill drives only the `aft library` commands.
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

SKILL_NAME = "aft-library-import"
SKILL_FILE = "SKILL.md"

# The name the skill was installed under before its aft- prefix, and the sha256 of every
# SKILL.md afterthought shipped under it. A copy that matches one is ours to remove; a copy
# that matches none is someone else's skill, or one edited by hand, and is left alone.
LEGACY_NAME = "library-import"
LEGACY_SHA256 = frozenset(
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
)

SOURCE = Path("integrations") / "skills" / SKILL_NAME / SKILL_FILE

BACKUP_PREFIX = f"{SKILL_FILE}.aft-"
BACKUP_SUFFIX = ".bak"


class InstallError(Exception):
    """The skill cannot be installed or uninstalled."""


@dataclass(frozen=True)
class SkillChange:
    """What one skill install or uninstall did, or would have done under ``--dry-run``.

    ``changed`` is False when the skill already held the shipped content (on install)
    or was already absent (on uninstall). ``backup`` is where previous custom content
    is preserved. ``diff`` is the unified diff of the skill file lines.
    ``removed`` names the paths removed on uninstall.
    """

    changed: bool
    backup: Path | None
    diff: str
    target: Path
    removed: tuple[Path, ...] = ()


def skill_source() -> Path:
    """The skill file shipped with the package in the installed package or the checkout."""
    installed = Path(__file__).resolve().parents[1] / SOURCE
    if installed.is_file():
        return installed
    return Path(__file__).resolve().parents[3] / SOURCE


def skill_text() -> str:
    """Read the canonical skill markdown from the checkout source."""
    source = skill_source()
    if source.is_file():
        return source.read_text(encoding="utf-8")
    raise InstallError(f"no {SOURCE} beside the aft package; run from a checkout")


def target_path(skills_dir: Path) -> Path:
    """The target SKILL.md file inside the skills directory."""
    return skills_dir / SKILL_NAME / SKILL_FILE


def retire_legacy(skills_dir: Path, *, dry_run: bool = False) -> str | None:
    """Remove the skill afterthought installed before its aft- prefix, and say so.

    Only a copy afterthought shipped is removed, with its directory when nothing else is in
    it; a ``library-import`` skill with other content is kept and named, so a person can
    decide. Returns the line to print, or None when there is no old copy.
    """
    container = skills_dir / LEGACY_NAME
    target = container / SKILL_FILE
    if not target.is_file() or target.is_symlink():
        return None
    try:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except OSError as exc:
        raise InstallError(f"cannot read {target}: {exc}") from exc
    if digest not in LEGACY_SHA256:
        return f"kept {target}: it is not a copy afterthought shipped"
    if dry_run:
        return f"would remove the old skill {container}"
    try:
        target.unlink()
        if not any(container.iterdir()):
            container.rmdir()
    except OSError as exc:
        raise InstallError(f"cannot remove the old skill {container}: {exc}") from exc
    return f"removed the old skill {container}"


def install(skills_dir: Path, *, dry_run: bool = False) -> SkillChange:
    """Install the aft-library-import skill into ``skills_dir``."""
    try:
        target = _target(skills_dir)
        text = skill_text()
        original = _text(target)
        if original == text:
            return SkillChange(changed=False, backup=None, diff="", target=target)
        diff = _diff(target, original, text)
        backup = _backup(target, original, dry_run=dry_run)
        if dry_run:
            return SkillChange(changed=True, backup=backup, diff=diff, target=target)
        _write(target, text)
        return SkillChange(changed=True, backup=backup, diff=diff, target=target)
    except OSError as exc:
        raise InstallError(f"cannot install skill into {skills_dir}: {exc}") from exc


def check_target(skills_dir: Path) -> Path:
    """Validate that the skill target path and container are valid and readable."""
    try:
        target = _target(skills_dir)
        _text(target)
        skill_text()
        return target
    except OSError as exc:
        raise InstallError(f"cannot install skill into {skills_dir}: {exc}") from exc


def uninstall(skills_dir: Path, *, dry_run: bool = False) -> SkillChange:
    """Remove the aft-library-import skill from ``skills_dir``."""
    try:
        target = _target(skills_dir)
        container = target.parent
        original = _text(target)
        shipped = _shipped()
        keep = original if original and original != shipped else None

        removed: list[Path] = []
        if original is not None:
            removed.append(target)
        backup = _backup(target, keep, dry_run=dry_run)

        will_be_empty = container.is_dir() and not any(
            entry for entry in container.iterdir() if entry != target
        )
        if original is not None and will_be_empty:
            removed.append(container)

        changed = bool(removed)
        diff = _diff(target, original, "")
        if dry_run:
            return SkillChange(
                changed=changed,
                backup=backup,
                diff=diff,
                target=target,
                removed=tuple(removed),
            )
        if target in removed:
            target.unlink()
        if container in removed and container.is_dir() and not any(container.iterdir()):
            container.rmdir()
        return SkillChange(
            changed=changed,
            backup=backup,
            diff=diff,
            target=target,
            removed=tuple(removed),
        )
    except OSError as exc:
        raise InstallError(f"cannot uninstall skill from {skills_dir}: {exc}") from exc


def _own_copies(container: Path) -> list[Path]:
    """Rescue copies stamped inside container, oldest first."""
    if not container.is_dir():
        return []
    return sorted(
        entry
        for entry in container.iterdir()
        if entry.name.startswith(BACKUP_PREFIX)
        and entry.name.endswith(BACKUP_SUFFIX)
        and stat.S_ISREG(_own_mode(entry) or 0)
    )


def _is_ours_to_remove(container: Path, ours: list[Path]) -> bool:
    """True when container is a directory holding nothing but paths named in ours."""
    return container.is_dir() and all(entry in ours for entry in container.iterdir())


def _target(skills_dir: Path) -> Path:
    """The skill file path, validating container structure."""
    container = skills_dir / SKILL_NAME
    if skills_dir.exists() and not skills_dir.is_dir():
        raise InstallError(f"{skills_dir} is not a directory; nothing was written")
    container_mode = _own_mode(container)
    if container_mode is not None and not stat.S_ISDIR(container_mode):
        raise InstallError(f"{container} is not a directory; nothing was written")
    target = container / SKILL_FILE
    target_mode = _own_mode(target)
    if target_mode is not None and not stat.S_ISREG(target_mode):
        raise InstallError(f"{target} is not a file; nothing was written")
    return target


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


def _shipped() -> str | None:
    try:
        return skill_text()
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
    backup = target.with_name(f"{BACKUP_PREFIX}{stamp}{BACKUP_SUFFIX}")
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
