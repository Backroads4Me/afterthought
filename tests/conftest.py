"""Shared fixtures. Every test runs against a temporary home, vault and git identity, with
no proxy in the way, so nothing touches real memory, the network, or the user's git
configuration."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from aft import config as configmod
from aft import vault


@pytest.fixture(autouse=True, scope="session")
def system_temp(tmp_path_factory):
    """Move the system temporary directory under pytest's own (DESIGN 7, F49).

    F49 makes a plain directory a project, so the directories that still resolve to no
    project are the system temporary directory, ``~/Downloads`` and the ones capture
    excludes. Tests make those from the temporary directories they are given, so the system
    temp is moved into the test tree, where the run can reach it, and is set back after.
    """
    original = tempfile.tempdir
    moved = tmp_path_factory.mktemp("system-temp")
    tempfile.tempdir = str(moved)
    yield moved
    tempfile.tempdir = original


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def isolated_git(tmp_path_factory, monkeypatch):
    gitconfig = tmp_path_factory.mktemp("gitconfig") / "config"
    gitconfig.write_text(
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n"
        '[protocol "file"]\n\tallow = always\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def direct_connection(monkeypatch):
    """Nothing reaches the network: a loopback request would otherwise be sent through a
    proxy when the environment names one, taking it away from the fake endpoint."""
    proxies = ("http_proxy", "https_proxy", "all_proxy")
    for name in (*proxies, *(name.upper() for name in proxies)):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_systemd(monkeypatch):
    """Never invoke real systemd during tests."""
    monkeypatch.setattr("aft.install.systemd._default_runner", lambda cmd: None)
    monkeypatch.setattr("aft.install.systemd._systemctl", lambda cmd: (True, ""))


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(f'vault = "{tmp_path / "vault"}"\nhost = "testhost"\n')
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    return home


@pytest.fixture
def cfg(home) -> configmod.Config:
    return configmod.load()


@pytest.fixture
def initialized(cfg) -> configmod.Config:
    vault.init(cfg)
    return cfg


def make_repo(path: Path, origin: str | None = None) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    run_git(["init", "-q"], path)
    (path / "README").write_text(path.name)
    run_git(["add", "README"], path)
    run_git(["commit", "-q", "-m", "init"], path)
    if origin:
        run_git(["remote", "add", "origin", origin], path)
    return path
