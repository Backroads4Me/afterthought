from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from aft import vault
from aft.cli import main
from aft.config import load
from aft.registry import Registry
from conftest import make_repo, run_git


def test_init_creates_config_and_vault(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("AFT_HOME", str(home))
    (tmp_path / "vault").mkdir()
    home.mkdir()
    (home / "config.toml").write_text(f'vault = "{tmp_path / "vault"}"\n')
    assert main(["init"]) == 0
    cfg = load()
    assert vault.is_initialized(cfg.vault)
    assert run_git(["log", "--format=%s"], cfg.vault) == "aft: initialize vault"
    assert main(["init"]) == 0
    assert "already initialized" in capsys.readouterr().out


def test_init_writes_default_config(tmp_path, monkeypatch):
    monkeypatch.setenv("AFT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["init"]) == 0
    assert (tmp_path / "home" / "config.toml").exists()
    assert (tmp_path / "afterthought" / "index.md").exists()


def test_library_commit_setting_controls_gitignore(home, tmp_path):
    (home / "config.toml").write_text(
        f'vault = "{tmp_path / "vault"}"\n[library]\ncommit = false\n'
    )
    vault.init(load())
    assert "library/" in (tmp_path / "vault" / ".gitignore").read_text()


def test_project_add_key_and_lint(initialized, tmp_path, capsys):
    repo = make_repo(tmp_path / "widget", origin="git@github.com:ex/widget.git")
    assert main(["project", "add", str(repo)]) == 0
    capsys.readouterr()
    assert main(["key", str(repo), "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["kind"], out["slug"]) == ("path", "widget")
    capsys.readouterr()
    assert main(["lint"]) == 0
    pages = len(list(initialized.vault.rglob("*.md")))
    assert pages > 1
    assert capsys.readouterr().out == f"checked {pages} pages, no findings\n"
    projects = Registry.load(initialized.registry_path).projects
    assert projects[0].remotes == ["github.com/ex/widget"]


def test_key_records_alias_and_commits(initialized, tmp_path, capsys):
    origin = "git@github.com:ex/widget.git"
    make_repo(tmp_path / "widget", origin=origin)
    main(["project", "add", str(tmp_path / "widget")])
    clone = make_repo(tmp_path / "elsewhere" / "widget2", origin=origin)
    capsys.readouterr()
    assert main(["key", str(clone)]) == 0
    assert capsys.readouterr().out.startswith("widget\tremote\t")
    paths = Registry.load(initialized.registry_path).projects[0].paths
    assert f"testhost:{clone.resolve()}" in paths
    assert "aft: record testhost:" in run_git(["log", "-1", "--format=%s"], initialized.vault)


def test_key_unregistered_exits_nonzero(initialized, tmp_path, capsys):
    repo = make_repo(tmp_path / "loose")
    assert main(["key", str(repo)]) == 1
    assert capsys.readouterr().out.startswith("loose\tunregistered\t")


def test_rename_moves_directories(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    main(["project", "add", str(repo)])
    page = initialized.vault / "projects" / "widget" / "overview.md"
    page.parent.mkdir(parents=True)
    page.write_text("placeholder")
    digest = initialized.vault / "digests" / "widget" / "2026" / "09" / "digest.md"
    digest.parent.mkdir(parents=True)
    digest.write_text("digest placeholder")
    archive_proj = initialized.vault / "archive" / "projects" / "widget" / "overview.md"
    archive_proj.parent.mkdir(parents=True)
    archive_proj.write_text("archive proj placeholder")
    archive_legacy = initialized.vault / "archive" / "widget.md"
    archive_legacy.parent.mkdir(parents=True, exist_ok=True)
    archive_legacy.write_text("archive legacy placeholder")
    vault.commit(initialized.vault, ["projects", "digests", "archive"], "add files")
    assert main(["project", "rename", "widget", "gadget"]) == 0
    assert (initialized.vault / "projects" / "gadget" / "overview.md").exists()
    assert not (initialized.vault / "projects" / "widget").exists()
    assert (initialized.vault / "digests" / "gadget" / "2026" / "09" / "digest.md").exists()
    assert not (initialized.vault / "digests" / "widget").exists()
    assert (initialized.vault / "archive" / "projects" / "gadget" / "overview.md").exists()
    assert not (initialized.vault / "archive" / "projects" / "widget").exists()
    assert (initialized.vault / "archive" / "gadget.md").exists()
    assert not (initialized.vault / "archive" / "widget.md").exists()
    assert Registry.load(initialized.registry_path).by_slug("gadget") is not None
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_rename_rolls_back_when_directory_move_fails(initialized, tmp_path, monkeypatch, capsys):
    """When a filesystem rename fails, the registry and any already-moved directories roll back."""
    repo = make_repo(tmp_path / "widget")
    main(["project", "add", str(repo)])
    page = initialized.vault / "projects" / "widget" / "overview.md"
    page.parent.mkdir(parents=True)
    page.write_text("placeholder")
    digest = initialized.vault / "digests" / "widget" / "2026" / "09" / "digest.md"
    digest.parent.mkdir(parents=True)
    digest.write_text("digest placeholder")
    vault.commit(initialized.vault, ["projects", "digests"], "add files")

    real_rename = Path.rename

    def failing_rename(self, target):
        if "digests" in str(self):
            raise OSError("simulated disk error")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", failing_rename)
    ret = main(["project", "rename", "widget", "gadget"])
    assert ret == 2
    err = capsys.readouterr().err
    assert "cannot rename project directories" in err

    assert Registry.load(initialized.registry_path).by_slug("widget") is not None
    assert Registry.load(initialized.registry_path).by_slug("gadget") is None
    assert (initialized.vault / "projects" / "widget" / "overview.md").exists()
    assert not (initialized.vault / "projects" / "gadget").exists()


def test_forget_removes_registry_entry(initialized, tmp_path, capsys):
    main(["project", "add", str(make_repo(tmp_path / "widget"))])
    assert main(["project", "forget", "widget"]) == 0
    assert Registry.load(initialized.registry_path).projects == []


def test_errors_are_reported_not_raised(initialized, tmp_path, capsys):
    main(["project", "add", str(make_repo(tmp_path / "widget"))])
    assert main(["project", "add", str(tmp_path / "widget")]) == 2
    assert "already exists" in capsys.readouterr().err


def test_commands_need_a_vault(home, capsys):
    assert main(["lint"]) == 2
    assert "run `aft init` first" in capsys.readouterr().err


@pytest.fixture
def default_home(tmp_path, monkeypatch) -> Path:
    """An initialized default home: ``AFT_HOME`` unset and ``HOME`` moved under tmp_path, so
    the home aft falls back to, and the vault its config names, are both this test's own."""
    monkeypatch.delenv("AFT_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    home = tmp_path / ".afterthought"
    home.mkdir()
    (home / "config.toml").write_text(f'vault = "{tmp_path / "vault"}"\nhost = "testhost"\n')
    cfg = load()
    assert cfg.home == home
    vault.init(cfg)
    return home


def test_the_default_home_refuses_a_temporary_project(default_home, tmp_path, capsys):
    """With AFT_HOME unset the vault is the user's real one, and a project under the system
    temporary directory is a scratch run that lost its way (BUILD-PLAN F25)."""
    repo = make_repo(tmp_path / "widget")
    registry_before = load().registry_path.read_bytes()
    log_before = run_git(["log", "--format=%H"], load().vault)

    assert main(["project", "add", str(repo)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(repo) in captured.err
    assert "temporary directory" in captured.err
    assert "AFT_HOME" in captured.err
    assert "--allow-temp" in captured.err
    assert load().registry_path.read_bytes() == registry_before
    assert run_git(["log", "--format=%H"], load().vault) == log_before


def test_allow_temp_registers_a_temporary_project_in_the_default_home(
    default_home, tmp_path, capsys
):
    repo = make_repo(tmp_path / "widget")

    assert main(["project", "add", str(repo), "--allow-temp"]) == 0

    assert [p.slug for p in Registry.load(load().registry_path).projects] == ["widget"]


def test_a_chosen_home_registers_a_temporary_project_without_the_flag(
    initialized, tmp_path, monkeypatch, capsys
):
    """A run that set AFT_HOME chose its home, so tests and benchmarks are unaffected."""
    assert os.environ.get("AFT_HOME")
    repo = make_repo(tmp_path / "widget")
    assert tempfile.gettempdir() in str(repo.resolve())

    assert main(["project", "add", str(repo)]) == 0

    assert [p.slug for p in Registry.load(initialized.registry_path).projects] == ["widget"]
