"""Tests for systemd user service and timer installer (BUILD-PLAN M2.9)."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft.cli import main
from aft.config import Config, load
from aft.install import systemd as systemd_install
from aft.install.systemd import (
    EARLY_START_MINUTES,
    SERVICE_NAME,
    TIMER_NAME,
    format_interval,
    install,
    trigger_early,
    uninstall,
)


def test_format_interval() -> None:
    assert format_interval(15) == "15min"
    assert format_interval(30) == "30min"
    assert format_interval(60) == "60min"
    assert format_interval(120) == "120min"

    with pytest.raises(ValueError):
        format_interval(0)
    with pytest.raises(ValueError):
        format_interval(-5)


def test_unit_file_contents(tmp_path: Path, cfg: Config) -> None:
    change = install(tmp_path, config=cfg)
    assert change.changed is True

    service_file = tmp_path / SERVICE_NAME
    timer_file = tmp_path / TIMER_NAME
    assert service_file.is_file()
    assert timer_file.is_file()

    service_text = service_file.read_text(encoding="utf-8")
    assert "[Unit]" in service_text
    assert "Description=Afterthought background work" in service_text
    # The unit lands in the home of whoever runs aft, so it names no account or repository.
    assert "Documentation=" not in service_text
    assert "[Service]" in service_text
    assert "Type=oneshot" in service_text
    assert "EnvironmentFile=-%h/.afterthought/env" in service_text
    real_aft = shutil.which("aft")
    assert real_aft is not None
    assert f"ExecStart={Path(real_aft).resolve()} work" in service_text

    timer_text = timer_file.read_text(encoding="utf-8")
    assert "[Unit]" in timer_text
    assert "Description=Afterthought background work timer" in timer_text
    assert "Documentation=" not in timer_text
    assert "[Timer]" in timer_text
    assert "OnBootSec=5min" in timer_text
    assert "OnUnitActiveSec=30min" in timer_text
    assert "[Install]" in timer_text
    assert "WantedBy=timers.target" in timer_text


def test_custom_interval_minutes(tmp_path: Path, home: Path) -> None:
    (home / "config.toml").write_text(
        f'vault = "{tmp_path / "vault"}"\n[dream]\ninterval_minutes = 15\n'
    )
    cfg = load()
    install(tmp_path, config=cfg)

    timer_text = (tmp_path / TIMER_NAME).read_text(encoding="utf-8")
    assert "OnUnitActiveSec=15min" in timer_text


def test_idempotent_install(tmp_path: Path, cfg: Config) -> None:
    first = install(tmp_path, config=cfg)
    assert first.changed is True

    service_mtime = (tmp_path / SERVICE_NAME).stat().st_mtime_ns
    timer_mtime = (tmp_path / TIMER_NAME).stat().st_mtime_ns

    second = install(tmp_path, config=cfg)
    assert second.changed is False

    assert (tmp_path / SERVICE_NAME).stat().st_mtime_ns == service_mtime
    assert (tmp_path / TIMER_NAME).stat().st_mtime_ns == timer_mtime


def test_dry_run_install(tmp_path: Path, cfg: Config) -> None:
    change = install(tmp_path, config=cfg, dry_run=True)
    assert change.changed is True
    assert SERVICE_NAME in change.preview
    assert TIMER_NAME in change.preview
    assert not (tmp_path / SERVICE_NAME).exists()
    assert not (tmp_path / TIMER_NAME).exists()


def test_uninstall(tmp_path: Path, cfg: Config) -> None:
    install(tmp_path, config=cfg)
    assert (tmp_path / SERVICE_NAME).exists()
    assert (tmp_path / TIMER_NAME).exists()

    change = uninstall(tmp_path)
    assert change.changed is True
    assert not (tmp_path / SERVICE_NAME).exists()
    assert not (tmp_path / TIMER_NAME).exists()

    second = uninstall(tmp_path)
    assert second.changed is False


def test_dry_run_uninstall(tmp_path: Path, cfg: Config) -> None:
    install(tmp_path, config=cfg)
    change = uninstall(tmp_path, dry_run=True)
    assert change.changed is True
    assert (tmp_path / SERVICE_NAME).exists()
    assert (tmp_path / TIMER_NAME).exists()


def test_cli_install_timer(tmp_path: Path, home: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(systemd_install, "unit_dir", lambda path=None: tmp_path)

    # 1. Dry run
    code = main(["install", "timer", "--dry-run"])
    assert code == 0
    out = capsys.readouterr().out
    assert SERVICE_NAME in out
    assert not (tmp_path / SERVICE_NAME).exists()

    # 2. Install
    code = main(["install", "timer"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"installed {tmp_path / SERVICE_NAME}, {tmp_path / TIMER_NAME}" in out
    assert (tmp_path / SERVICE_NAME).exists()

    # 3. Idempotent install
    code = main(["install", "timer"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"afterthought timer units in {tmp_path} are already up to date" in out

    # 4. Uninstall
    code = main(["install", "timer", "--uninstall"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"removed {tmp_path / SERVICE_NAME}, {tmp_path / TIMER_NAME}" in out
    assert not (tmp_path / SERVICE_NAME).exists()

    # 5. Uninstall when already gone
    code = main(["install", "timer", "--uninstall"])
    assert code == 0
    out = capsys.readouterr().out
    assert f"no afterthought timer units to remove in {tmp_path}" in out


def test_install_refuses_when_aft_not_in_path(tmp_path: Path, cfg: Config, monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    with pytest.raises(systemd_install.InstallError, match="aft executable not found in PATH"):
        install(tmp_path, config=cfg)


def test_cli_install_refuses_when_aft_not_in_path(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(systemd_install, "unit_dir", lambda path=None: tmp_path)
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    code = main(["install", "timer"])
    assert code == 2
    err = capsys.readouterr().err
    assert "aft: aft executable not found in PATH" in err


def test_trigger_early_rate_limiting(cfg: Config) -> None:
    calls: list[list[str]] = []

    def mock_runner(cmd: list[str]) -> None:
        calls.append(cmd)

    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    # First trigger starts the service
    assert trigger_early(cfg, now=t0, runner=mock_runner) is True
    assert len(calls) == 1
    assert calls[0] == ["systemctl", "--user", "start", "--no-block", SERVICE_NAME]

    # Second trigger 2 minutes later is rate-limited
    t1 = t0 + timedelta(minutes=2)
    assert trigger_early(cfg, now=t1, runner=mock_runner) is False
    assert len(calls) == 1

    # Third trigger 5 minutes and 1 second later succeeds
    t2 = t0 + timedelta(minutes=EARLY_START_MINUTES, seconds=1)
    assert trigger_early(cfg, now=t2, runner=mock_runner) is True
    assert len(calls) == 2


def test_trigger_early_error_handling(cfg: Config) -> None:
    def failing_runner(cmd: list[str]) -> None:
        raise OSError("failed to connect to bus")

    t0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert trigger_early(cfg, now=t0, runner=failing_runner) is False


def test_install_error_on_unwritable_directory(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    unwritable = tmp_path / "blocked"
    unwritable.write_text("file blocking directory creation", encoding="utf-8")
    monkeypatch.setattr(systemd_install, "unit_dir", lambda path=None: unwritable)

    code = main(["install", "timer"])
    assert code == 2
    err = capsys.readouterr().err
    assert err.startswith("aft: cannot write")
    assert "Traceback" not in err


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not available")
def test_systemd_analyze_verify_timer(tmp_path: Path, cfg: Config) -> None:
    install(tmp_path, config=cfg)
    timer_path = tmp_path / TIMER_NAME
    service_path = tmp_path / SERVICE_NAME
    service_text = service_path.read_text(encoding="utf-8")
    real_aft = shutil.which("aft")
    assert real_aft is not None
    assert f"ExecStart={Path(real_aft).resolve()} work" in service_text

    result = subprocess.run(
        ["systemd-analyze", "verify", str(timer_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


# ---------------------------------------------------------------- enabling the timer (T7)


class Systemctl:
    """A stand-in for systemctl that records each command and can refuse one."""

    def __init__(self, refuse: str = "") -> None:
        self.ran: list[str] = []
        self.refuse = refuse

    def __call__(self, command: list[str]) -> tuple[bool, str]:
        said = " ".join(command)
        self.ran.append(said)
        if self.refuse and self.refuse in said:
            return False, "Failed to connect to bus"
        return True, ""


ENABLE = ["systemctl --user daemon-reload", f"systemctl --user enable --now {TIMER_NAME}"]
DISABLE = [f"systemctl --user disable --now {TIMER_NAME}", "systemctl --user daemon-reload"]


def test_install_enables_and_starts_the_timer(tmp_path: Path, cfg: Config) -> None:
    """Writing the units is half an install: a timer nobody enabled never fires."""
    systemctl = Systemctl()
    change = systemd_install.install(tmp_path, cfg, runner=systemctl)
    assert systemctl.ran == ENABLE
    assert change.active is True
    assert list(change.commands) == ENABLE


def test_install_enables_the_timer_even_when_the_units_are_up_to_date(
    tmp_path: Path, cfg: Config
) -> None:
    """Units written by an earlier install that never enabled them are still enabled."""
    systemd_install.install(tmp_path, cfg, runner=Systemctl())
    systemctl = Systemctl()
    change = systemd_install.install(tmp_path, cfg, runner=systemctl)
    assert change.changed is False
    assert systemctl.ran == ENABLE
    assert change.active is True


def test_a_dry_run_names_the_commands_and_runs_none(tmp_path: Path, cfg: Config) -> None:
    systemctl = Systemctl()
    change = systemd_install.install(tmp_path, cfg, dry_run=True, runner=systemctl)
    assert systemctl.ran == []
    assert list(change.commands) == ENABLE
    assert change.active is None


def test_a_refusal_from_systemd_is_reported_with_what_it_said(tmp_path: Path, cfg: Config) -> None:
    systemctl = Systemctl(refuse="enable")
    change = systemd_install.install(tmp_path, cfg, runner=systemctl)
    assert change.active is False
    assert "Failed to connect to bus" in change.problem
    assert (tmp_path / TIMER_NAME).exists(), "the units are still written"


def test_uninstall_disables_the_timer_before_removing_its_unit(tmp_path: Path, cfg: Config) -> None:
    """systemd cannot disable a unit it can no longer read, so the order matters."""
    systemd_install.install(tmp_path, cfg, runner=Systemctl())
    seen: list[bool] = []

    def systemctl(command: list[str]) -> tuple[bool, str]:
        seen.append((tmp_path / TIMER_NAME).exists())
        ran.append(" ".join(command))
        return True, ""

    ran: list[str] = []
    change = systemd_install.uninstall(tmp_path, runner=systemctl)
    assert ran == DISABLE
    assert seen == [True, False], "disable ran with the unit present, reload after it was gone"
    assert change.active is True


def test_a_directory_systemd_does_not_read_is_never_enabled(tmp_path: Path, cfg: Config) -> None:
    """With no runner given, units written outside the user unit directory run no systemctl:
    enabling them would act on whatever the real directory holds."""
    called: list[list[str]] = []

    def real(command: list[str]) -> tuple[bool, str]:
        called.append(command)
        return True, ""

    import aft.install.systemd as module

    original = module._systemctl
    module._systemctl = real
    try:
        change = systemd_install.install(tmp_path, cfg)
        gone = systemd_install.uninstall(tmp_path)
    finally:
        module._systemctl = original
    assert called == []
    assert change.active is None and gone.active is None


def test_cli_install_timer_says_what_it_did_about_the_timer(
    tmp_path: Path, home: Path, monkeypatch, capsys
) -> None:
    """The command reports the enable, names the commands under --dry-run, and exits 1 with
    the commands to run by hand when systemd refuses."""
    monkeypatch.setattr(systemd_install, "unit_dir", lambda path=None: tmp_path)
    monkeypatch.setattr(systemd_install, "_systemd_reads", lambda directory: True)
    systemctl = Systemctl()
    monkeypatch.setattr(systemd_install, "_systemctl", systemctl)

    assert main(["install", "timer", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert [f"would run: {command}" in out for command in ENABLE] == [True, True]
    assert systemctl.ran == []

    assert main(["install", "timer"]) == 0
    assert f"enabled and started {TIMER_NAME}" in capsys.readouterr().out
    assert systemctl.ran == ENABLE

    assert main(["install", "timer", "--uninstall"]) == 0
    assert f"disabled and stopped {TIMER_NAME}" in capsys.readouterr().out

    monkeypatch.setattr(systemd_install, "_systemctl", Systemctl(refuse="enable"))
    assert main(["install", "timer"]) == 1
    err = capsys.readouterr().err
    assert "Failed to connect to bus" in err
    assert f"systemctl --user enable --now {TIMER_NAME}" in err
