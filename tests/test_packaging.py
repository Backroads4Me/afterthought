"""Tests for package distribution and installation from wheels (BUILD-PLAN F24, M1 review 10)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_wheel_ships_integrations_and_installers_work(tmp_path: Path) -> None:
    """The wheel ships integrations/, so aft install claude and aft install pi work
    from an installed package without a source checkout (BUILD-PLAN F24)."""
    repo_root = Path(__file__).resolve().parents[1]
    dist_dir = tmp_path / "dist"
    dist_dir.mkdir()

    # 1. Build the wheel
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    wheels = list(dist_dir.glob("*.whl"))
    assert len(wheels) == 1, f"expected 1 wheel, got {wheels}"
    wheel = wheels[0]

    # 2. Create isolated virtualenv and install the wheel
    venv_dir = tmp_path / "venv"
    subprocess.run(["uv", "venv", str(venv_dir)], check=True, capture_output=True)
    python_bin = venv_dir / "bin" / "python"
    aft_bin = venv_dir / "bin" / "aft"
    subprocess.run(
        ["uv", "pip", "install", str(wheel), "--python", str(python_bin)],
        check=True,
        capture_output=True,
    )

    # 3. Verify aft install claude and aft install pi work from the installed package
    env = os.environ.copy()
    fake_home = tmp_path / "home"
    env["AFT_HOME"] = str(fake_home)
    fake_user_home = tmp_path / "user_home"
    env["HOME"] = str(fake_user_home)

    res_claude_dry = subprocess.run(
        [str(aft_bin), "install", "claude", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_claude_dry.returncode == 0, res_claude_dry.stderr
    assert "entries to add to" in res_claude_dry.stdout

    res_pi_dry = subprocess.run(
        [str(aft_bin), "install", "pi", "--dry-run"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_pi_dry.returncode == 0, res_pi_dry.stderr
    assert "afterthought/index.ts" in res_pi_dry.stdout

    # 4. Perform actual installation
    res_claude = subprocess.run(
        [str(aft_bin), "install", "claude"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_claude.returncode == 0, res_claude.stderr
    assert (fake_user_home / ".claude" / "settings.json").is_file()
    assert (fake_user_home / ".claude" / "skills" / "library-import" / "SKILL.md").is_file()

    res_pi = subprocess.run(
        [str(aft_bin), "install", "pi"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_pi.returncode == 0, res_pi.stderr
    assert (fake_user_home / ".pi" / "agent" / "extensions" / "afterthought" / "index.ts").is_file()
