from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aft import vault
from aft.cli import main
from aft.config import load
from aft.events import SESSION_END, SessionEvent
from aft.registry import Registry, path_key
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


def test_project_archive_and_unarchive(initialized, tmp_path, capsys, monkeypatch):
    """`aft project archive` dates the project and commits the registry; its block stops
    being injected and its pages answer only `aft search --archive`; `aft project
    unarchive` brings it back (DESIGN 10, F49). Both are the user's say-so, and nothing
    archives a project by itself."""
    monkeypatch.setenv("AFT_HOME", str(initialized.home))
    repo = make_repo(tmp_path / "widget")
    main(["project", "add", str(repo)])
    capsys.readouterr()

    assert main(["project", "archive", "widget"]) == 0
    registry = Registry.load(initialized.registry_path)
    widget = registry.by_slug("widget")
    assert widget is not None and widget.archived == date.today().isoformat()
    assert run_git(["log", "-1", "--format=%s"], initialized.vault) == "aft: archive widget"
    assert run_git(["status", "--porcelain"], initialized.vault) == ""
    assert main(["project", "unarchive", "widget"]) == 0
    registry = Registry.load(initialized.registry_path)
    widget = registry.by_slug("widget")
    assert widget is not None and widget.archived == ""
    assert run_git(["log", "-1", "--format=%s"], initialized.vault) == "aft: unarchive widget"

    capsys.readouterr()
    assert main(["project", "archive", "no-such-project"]) == 2
    assert "no project" in capsys.readouterr().err
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


def temp_repo(name: str) -> Path:
    """A repository under the system temporary directory, whatever run gave it."""
    repo = (Path(tempfile.gettempdir()) / name).resolve()
    shutil.rmtree(repo, ignore_errors=True)
    return make_repo(repo)


def test_the_default_home_refuses_a_temporary_project(default_home, capsys):
    """With AFT_HOME unset the vault is the user's real one, and a project under the system
    temporary directory is a scratch run that lost its way (BUILD-PLAN F25)."""
    repo = temp_repo("widget")
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
    shutil.rmtree(repo)


def test_allow_temp_registers_a_temporary_project_in_the_default_home(default_home):
    repo = temp_repo("widget")

    assert main(["project", "add", str(repo), "--allow-temp"]) == 0

    assert [p.slug for p in Registry.load(load().registry_path).projects] == ["widget"]
    shutil.rmtree(repo)


def test_a_chosen_home_registers_a_temporary_project_without_the_flag(
    initialized, monkeypatch, capsys
):
    """A run that set AFT_HOME chose its home, so tests and benchmarks are unaffected."""
    assert os.environ.get("AFT_HOME")
    repo = temp_repo("widget")
    assert tempfile.gettempdir() in str(repo.resolve())

    assert main(["project", "add", str(repo)]) == 0

    assert [p.slug for p in Registry.load(initialized.registry_path).projects] == ["widget"]
    shutil.rmtree(repo)


# A name an unregistered directory has claimed is not free for a project made by hand
# (DESIGN 7, F49)


def test_project_add_of_a_claimed_name_is_prefixed(initialized, tmp_path, capsys):
    """``aft project add`` with no name of its own settles one the way a session-start
    record does: the name another directory's records claim is stepped aside from, so two
    directories never share a project (DESIGN 7, F49)."""
    import aft.queue as queue

    claimed = (tmp_path / "b" / "foo").resolve()
    claimed.mkdir(parents=True)
    assert queue.record_project(initialized, "claude", "s-1", None, "foo", claimed)
    making = (tmp_path / "c" / "foo").resolve()
    making.mkdir(parents=True)

    assert main(["project", "add", str(making)]) == 0

    registry = Registry.load(initialized.registry_path)
    project = registry.by_path_key(path_key("testhost", making))
    assert project is not None and project.slug == "c-foo"
    assert "registered c-foo" in capsys.readouterr().out


def test_project_add_refuses_a_chosen_name_another_directory_claimed(initialized, tmp_path, capsys):
    """A name the person chose is refused when another directory's session records claim
    it, with the claim named (DESIGN 7, F49)."""
    import aft.queue as queue

    claimed = (tmp_path / "b" / "foo").resolve()
    claimed.mkdir(parents=True)
    assert queue.record_project(initialized, "claude", "s-1", None, "foo", claimed)
    making = (tmp_path / "c" / "foo").resolve()
    making.mkdir(parents=True)

    assert main(["project", "add", str(making), "--slug", "foo"]) == 2

    assert "claimed" in capsys.readouterr().err
    assert Registry.load(initialized.registry_path).projects == []


def test_project_add_takes_a_chosen_name_the_directory_claimed(initialized, tmp_path, capsys):
    """A name the directory's own session records hold is its own (DESIGN 7, F49):
    `aft project add <dir> --slug <name>` is refused only by another directory's claim, so
    the person can name the directory what its sessions already file it under."""
    import aft.queue as queue

    making = (tmp_path / "c" / "foo").resolve()
    making.mkdir(parents=True)
    assert queue.record_project(initialized, "claude", "s-1", None, "foo", making)

    assert main(["project", "add", str(making), "--slug", "foo"]) == 0

    registry = Registry.load(initialized.registry_path)
    project = registry.by_path_key(path_key("testhost", making))
    assert project is not None and project.slug == "foo"
    assert "registered foo" in capsys.readouterr().out


def test_project_rename_refuses_a_name_another_directory_claimed(initialized, tmp_path, capsys):
    """A rename may not take a name another path's session records claim (DESIGN 7, F49)."""
    import aft.queue as queue

    repo = make_repo(tmp_path / "widget")
    assert main(["project", "add", str(repo)]) == 0
    claimed = (tmp_path / "b" / "gizmo").resolve()
    claimed.mkdir(parents=True)
    assert queue.record_project(initialized, "claude", "s-1", None, "gizmo", claimed)

    assert main(["project", "rename", "widget", "gizmo"]) == 2

    assert "claimed" in capsys.readouterr().err
    assert Registry.load(initialized.registry_path).by_slug("widget") is not None


def hook_event(session_id: str, cwd: Path, at: datetime) -> SessionEvent:
    """One session-start-shaped event, so a test can put a session's directory in the queue."""
    return SessionEvent(
        agent="claude",
        kind=SESSION_END,
        session_id=session_id,
        cwd=cwd,
        transcript_path=None,
        time=at,
        detail=None,
    )


def test_project_add_adopts_the_pathless_record_of_the_directory_it_names(initialized, tmp_path):
    """A record an earlier build wrote with no path is the claim of the directory that
    resolves to its name: `aft project add` of that directory registers the project under
    the name and gives the record the path (DESIGN 7, F49)."""
    import aft.queue as queue

    plain = (tmp_path / "other" / "foo").resolve()
    plain.mkdir(parents=True)
    # The queue saw the old session running in that directory, so its record is this
    # directory's own claim and `aft project add` may adopt it.
    queue.enqueue(initialized, hook_event("s-old", plain, datetime(2026, 10, 1, 12, 0, tzinfo=UTC)))
    assert queue.record_project(initialized, "claude", "s-old", None, "foo")

    assert main(["project", "add", str(plain)]) == 0

    registry = Registry.load(initialized.registry_path)
    project = registry.by_path_key(path_key("testhost", plain))
    assert project is not None and project.slug == "foo"
    record = queue.recorded_project(initialized, "claude", "s-old")
    assert record is not None and record.path == str(plain)


# The unfiled backfill's command surface (DESIGN 7, F49)


def test_redigest_unfiled_dry_run_lists_sessions_and_writes_nothing(
    initialized, monkeypatch, capsys
):
    """The dry run names each session and what a rebuild would charge, and writes nothing
    (DESIGN 7, F49)."""
    import aft.digest

    monkeypatch.setattr(aft.digest, "unfiled_sessions", lambda config: ["u1", "u2"])
    monkeypatch.setattr(aft.digest, "stays_unfiled", lambda config, session_id: False)
    monkeypatch.setattr(aft.digest, "uncovered_unfiled", lambda config: [])
    monkeypatch.setattr(
        aft.digest,
        "redigest_estimate",
        lambda config, session_id: 1234 if session_id == "u1" else None,
    )
    assert main(["redigest", "--unfiled", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "u1\t~1234 tokens\nu2\tno archived entries\n"
        "1 session(s) would rebuild, ~1234 tokens; 0 staying unfiled; 0 cannot rebuild\n"
    )
    assert run_git(["log", "--format=%s"], initialized.vault) == "aft: initialize vault"


def test_redigest_unfiled_stops_at_the_day_budget(initialized, monkeypatch, capsys):
    """A day whose token budget is spent starts no rebuild; the sessions still waiting are
    said so, and the command leaves them for a day with room in it (DESIGN 13, F49)."""
    import aft.digest
    import aft.model

    monkeypatch.setattr(aft.digest, "unfiled_sessions", lambda config: ["u1", "u2"])
    monkeypatch.setattr(aft.digest, "stays_unfiled", lambda config, session_id: False)
    monkeypatch.setattr(aft.model, "tokens_used_today", lambda config, now: 2**40)
    rebuilt: list[str] = []
    monkeypatch.setattr(
        aft.digest,
        "redigest",
        lambda config, session_id, now=None, unfiled=False: rebuilt.append(session_id),
    )
    assert main(["redigest", "--unfiled"]) == 1
    out = capsys.readouterr().out
    assert "token budget" in out and "2 session(s) wait" in out
    assert rebuilt == []


def test_redigest_unfiled_stops_at_the_budget_between_sessions(initialized, monkeypatch, capsys):
    """A rebuild that spends the day's budget stops the backfill after itself, not before
    the next session: what ran is said, and the rest wait for a day with room (DESIGN 13,
    F49)."""
    import aft.digest
    import aft.model

    spent = iter([0, 2**40])
    monkeypatch.setattr(aft.digest, "unfiled_sessions", lambda config: ["u1", "u2"])
    monkeypatch.setattr(aft.digest, "stays_unfiled", lambda config, session_id: False)
    monkeypatch.setattr(aft.model, "tokens_used_today", lambda config, now: next(spent))
    rebuilt: list[str] = []
    monkeypatch.setattr(
        aft.digest,
        "redigest",
        lambda config, session_id, now=None, unfiled=False: (
            rebuilt.append(session_id)
            or aft.digest.Digest(
                path="digests/fieldwork/x.md", facts=(), dropped=(), watermark="e9"
            )
        ),
    )
    assert main(["redigest", "--unfiled"]) == 1
    out = capsys.readouterr().out
    assert "rebuilt digest at digests/fieldwork/x.md" in out
    assert "token budget" in out and "1 session(s) wait" in out
    assert rebuilt == ["u1"]


def test_redigest_unfiled_names_a_failing_session_and_carries_on(initialized, monkeypatch, capsys):
    """A session that refuses for its own reason is named and the backfill goes on to the
    rest (DESIGN 7, F49)."""
    import aft.digest

    monkeypatch.setattr(aft.digest, "unfiled_sessions", lambda config: ["u1", "u2"])
    monkeypatch.setattr(aft.digest, "stays_unfiled", lambda config, session_id: False)
    seen: list[str] = []

    def redigest(config, session_id, now=None, unfiled=False):
        seen.append(session_id)
        if session_id == "u1":
            raise aft.digest.DigestError("no archived evidence")
        return aft.digest.Digest(
            path="digests/fieldwork/2026/10/x.md", facts=(), dropped=(), watermark="e9"
        )

    monkeypatch.setattr(aft.digest, "redigest", redigest)
    assert main(["redigest", "--unfiled"]) == 1
    captured = capsys.readouterr()
    assert "u1: no archived evidence" in captured.err
    assert "rebuilt digest at digests/fieldwork/2026/10/x.md" in captured.out
    assert seen == ["u1", "u2"]


def test_redigest_refuses_a_session_id_with_unfiled(initialized, capsys):
    """``--unfiled`` is the whole backfill of DESIGN 7 (F49), and a session id names one
    rebuild; the two are not one command, so together they are a usage error."""
    assert main(["redigest", "u1", "--unfiled"]) == 2
    assert "give it without a session id" in capsys.readouterr().err


def test_redigest_without_a_session_id_prints_usage(initialized, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["redigest"])
    assert excinfo.value.code == 2
    assert "usage: aft redigest" in capsys.readouterr().err
