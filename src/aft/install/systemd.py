"""Install and uninstall afterthought's systemd user service and timer units (BUILD-PLAN M2.9).

A systemd user timer triggers `aft work` periodically (every `dream.interval_minutes`,
defaulting to 30 minutes) to process pending capture queues, generate digests, and dream.
Because a systemd user service does not inherit the user's interactive shell environment,
the service file loads an optional `EnvironmentFile=-%h/.afterthought/env`, where the
model API key variable named in `model.api_key_env` can be configured.

Capture hooks may also trigger `aft-work.service` early via:
`systemctl --user start --no-block aft-work.service`, rate-limited to at most once
every 5 minutes.
"""

from __future__ import annotations

import difflib
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aft.config import Config, load

SERVICE_NAME = "aft-work.service"
TIMER_NAME = "aft-work.timer"
UNIT_NAMES = (SERVICE_NAME, TIMER_NAME)
DEFAULT_UNIT_DIR = Path(".config/systemd/user")
EARLY_START_MINUTES = 5
RATE_LIMIT_FILE = "last-work-trigger"


class InstallError(Exception):
    """The systemd unit directory cannot be written, or aft was not found,
    and nothing was changed.
    """


@dataclass(frozen=True)
class Change:
    """What one install or uninstall did, or would have done under ``--dry-run``."""

    changed: bool
    units: tuple[Path, ...]
    diff: str = ""
    preview: str = ""


def unit_dir(path: Path | None = None) -> Path:
    """The user systemd unit directory."""
    if path is not None:
        return path
    return Path.home() / DEFAULT_UNIT_DIR


def resolve_aft_path(explicit: Path | str | None = None) -> Path:
    """Find the absolute path to the aft executable, refusing if not found."""
    if explicit is not None:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file() or not os.access(p, os.X_OK):
            raise InstallError(f"aft executable not found or not executable: {explicit}")
        return p
    found = shutil.which("aft")
    if not found:
        raise InstallError("aft executable not found in PATH")
    return Path(found).resolve()


def format_interval(interval_minutes: int) -> str:
    """Format an interval in minutes for systemd OnUnitActiveSec timer specification."""
    if interval_minutes <= 0:
        raise ValueError(f"interval_minutes must be positive, got {interval_minutes}")
    return f"{interval_minutes}min"


def service_content(aft_path: Path | str | None = None) -> str:
    """The content of aft-work.service."""
    executable = resolve_aft_path(aft_path)
    return f"""\
[Unit]
Description=Afterthought background work

[Service]
Type=oneshot
EnvironmentFile=-%h/.afterthought/env
ExecStart={executable} work
"""


def timer_content(interval_minutes: int = 30) -> str:
    """The content of aft-work.timer for the given interval in minutes."""
    interval_spec = format_interval(interval_minutes)
    return f"""\
[Unit]
Description=Afterthought background work timer

[Timer]
OnBootSec=5min
OnUnitActiveSec={interval_spec}

[Install]
WantedBy=timers.target
"""


def install(
    target_dir: Path | None = None,
    config: Config | None = None,
    *,
    dry_run: bool = False,
    aft_path: Path | str | None = None,
) -> Change:
    """Write aft-work.service and aft-work.timer into target_dir."""
    directory = unit_dir(target_dir)
    cfg = config if config is not None else load()
    interval = cfg.settings.dream.interval_minutes

    desired_service = service_content(aft_path)
    desired_timer = timer_content(interval)

    service_path = directory / SERVICE_NAME
    timer_path = directory / TIMER_NAME

    existing_service = service_path.read_text(encoding="utf-8") if service_path.is_file() else None
    existing_timer = timer_path.read_text(encoding="utf-8") if timer_path.is_file() else None

    if existing_service == desired_service and existing_timer == desired_timer:
        return Change(changed=False, units=(service_path, timer_path))

    previews: list[str] = []
    for path, old, new in [
        (service_path, existing_service, desired_service),
        (timer_path, existing_timer, desired_timer),
    ]:
        if old != new:
            if old is None:
                previews.append(f"+++ {path}\n{new}")
            else:
                diff = "".join(
                    difflib.unified_diff(
                        old.splitlines(keepends=True),
                        new.splitlines(keepends=True),
                        fromfile=str(path),
                        tofile=str(path),
                    )
                )
                previews.append(diff)

    preview_text = "\n".join(previews)

    if dry_run:
        return Change(
            changed=True,
            units=(service_path, timer_path),
            diff=preview_text,
            preview=preview_text,
        )

    try:
        directory.mkdir(parents=True, exist_ok=True)
        service_path.write_text(desired_service, encoding="utf-8")
        timer_path.write_text(desired_timer, encoding="utf-8")
    except OSError as exc:
        raise InstallError(f"cannot write {directory}: {exc}") from exc

    return Change(
        changed=True,
        units=(service_path, timer_path),
        diff=preview_text,
        preview=preview_text,
    )


def uninstall(
    target_dir: Path | None = None,
    *,
    dry_run: bool = False,
) -> Change:
    """Remove afterthought's systemd user service and timer units."""
    directory = unit_dir(target_dir)
    service_path = directory / SERVICE_NAME
    timer_path = directory / TIMER_NAME

    existing = tuple(p for p in (service_path, timer_path) if p.is_file() or p.is_symlink())
    if not existing:
        return Change(changed=False, units=())

    preview_text = f"would remove {', '.join(str(p) for p in existing)}"

    if dry_run:
        return Change(
            changed=True,
            units=existing,
            preview=preview_text,
        )

    removed: list[Path] = []
    try:
        for p in existing:
            p.unlink()
            removed.append(p)
    except OSError as exc:
        raise InstallError(f"cannot remove units in {directory}: {exc}") from exc

    return Change(
        changed=True,
        units=tuple(removed),
        preview=f"removed {', '.join(str(p) for p in removed)}",
    )


def _default_runner(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, capture_output=True, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


def trigger_early(
    config: Config,
    *,
    now: datetime | None = None,
    runner: Callable[[list[str]], Any] | None = None,
) -> bool:
    """Start aft-work.service early if at least 5 minutes have elapsed since the last trigger."""
    current_time = now if now is not None else datetime.now(UTC)
    trigger_file = config.home / RATE_LIMIT_FILE

    if trigger_file.is_file():
        try:
            content = trigger_file.read_text(encoding="utf-8").strip()
            last_time = datetime.fromisoformat(content)
            if current_time - last_time < timedelta(minutes=EARLY_START_MINUTES):
                return False
        except (ValueError, OSError):
            pass

    cmd = ["systemctl", "--user", "start", "--no-block", SERVICE_NAME]
    try:
        config.home.mkdir(parents=True, exist_ok=True)
        trigger_file.write_text(current_time.isoformat() + "\n", encoding="utf-8")
        run_func = runner if runner is not None else _default_runner
        run_func(cmd)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
