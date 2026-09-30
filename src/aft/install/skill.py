"""Install and uninstall afterthought's library import skill (BUILD-PLAN M7.5, DESIGN 11).

Afterthought ships the import procedure as a skill for Claude Code and pi,
installed by their adapters (`aft install claude` and `aft install pi`).
The skill drives only the `aft library` commands.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SKILL_NAME = "library-import"
SKILL_FILE = "SKILL.md"

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
    """The skill file shipped with the package in the checkout or installed wheel."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / SOURCE
        if candidate.is_file():
            return candidate
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


def install(skills_dir: Path, *, dry_run: bool = False) -> SkillChange:
    """Install the library-import skill into ``skills_dir``."""
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
    """Remove the library-import skill from ``skills_dir``."""
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
