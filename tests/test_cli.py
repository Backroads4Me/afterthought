from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from aft import model, vault
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


def test_model_cooldown_shows_status_and_clears(cfg, capsys):
    assert main(["model", "cooldown"]) == 0
    assert "No active model cooldown." in capsys.readouterr().out
    model.set_cooldown(cfg, "cannot reach http://endpoint: connection refused")
    capsys.readouterr()
    assert main(["model", "cooldown"]) == 0
    out = capsys.readouterr().out
    assert "connection refused" in out
    assert re.search(r"another \d+m\d\ds", out)
    assert main(["model", "cooldown", "--clear"]) == 0
    assert "aft model: cooldown cleared" in capsys.readouterr().out
    assert model.get_cooldown(cfg) is None
    assert main(["model", "cooldown", "--clear"]) == 0
    assert "No active model cooldown." in capsys.readouterr().out


def test_budget_reset_clears_the_model_cooldown(cfg, capsys):
    model.set_cooldown(cfg, "the endpoint answered 500")
    assert main(["budget", "reset"]) == 0
    out = capsys.readouterr().out
    assert "reset at" in out and "cooldown was cleared" in out
    assert model.get_cooldown(cfg) is None
    assert main(["budget", "reset"]) == 0
    assert "cooldown" not in capsys.readouterr().out


# Attaching a directory to an existing project (F70)


def test_project_add_to_attaches_a_path_and_its_remote(initialized, tmp_path, capsys):
    """`aft project add <path> --to <slug>` gives the project the directory, and its
    origin remote, without making a new project (F70)."""
    repo = make_repo(tmp_path / "widget", origin="git@github.com:ex/widget.git")
    assert main(["project", "add", str(repo)]) == 0
    capsys.readouterr()
    extra = make_repo(tmp_path / "extra", origin="git@github.com:ex/extra.git")

    assert main(["project", "add", str(extra), "--to", "widget"]) == 0

    registry = Registry.load(initialized.registry_path)
    project = registry.by_slug("widget")
    assert project.paths == [f"testhost:{repo.resolve()}", f"testhost:{extra.resolve()}"]
    assert project.remotes == ["github.com/ex/widget", "github.com/ex/extra"]
    assert capsys.readouterr().out == (
        f"attached testhost:{extra.resolve()} to widget ({project.id})\n"
    )
    assert run_git(["log", "-1", "--format=%s"], initialized.vault) == "aft: attach extra to widget"
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_add_to_is_idempotent_for_its_own_directory(initialized, tmp_path, capsys):
    """A directory the project already holds attaches again as a success, and changes
    nothing (F70)."""
    repo = make_repo(tmp_path / "widget")
    assert main(["project", "add", str(repo)]) == 0
    capsys.readouterr()
    log_before = run_git(["log", "--format=%H"], initialized.vault)

    assert main(["project", "add", str(repo), "--to", "widget"]) == 0

    registry = Registry.load(initialized.registry_path)
    project = registry.by_slug("widget")
    assert project.paths == [f"testhost:{repo.resolve()}"]
    assert capsys.readouterr().out == (
        f"attached testhost:{repo.resolve()} to widget ({project.id})\n"
    )
    assert run_git(["log", "--format=%H"], initialized.vault) == log_before
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_add_to_refuses_a_path_another_project_holds(initialized, tmp_path, capsys):
    """A directory another project holds is a merge's business, not a second claim (F70)."""
    first = make_repo(tmp_path / "first")
    second = make_repo(tmp_path / "second")
    assert main(["project", "add", str(first)]) == 0
    assert main(["project", "add", str(second)]) == 0
    capsys.readouterr()

    assert main(["project", "add", str(second), "--to", "first"]) == 2

    captured = capsys.readouterr()
    assert f"a project 'second' already exists for {second.resolve()}" in captured.err
    assert "`aft project merge` merges projects" in captured.err
    registry = Registry.load(initialized.registry_path)
    assert registry.by_slug("first").paths == [f"testhost:{first.resolve()}"]
    assert registry.by_slug("second").paths == [f"testhost:{second.resolve()}"]
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_add_to_refuses_an_unknown_project(initialized, tmp_path, capsys):
    """`--to` names a project that has to exist already (F70)."""
    repo = make_repo(tmp_path / "widget")

    assert main(["project", "add", str(repo), "--to", "nowhere"]) == 2

    assert "no project 'nowhere'" in capsys.readouterr().err
    assert Registry.load(initialized.registry_path).projects == []
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_add_refuses_slug_and_to_together(initialized, tmp_path):
    """A call either names a new project or joins an existing one, not both (F70)."""
    repo = make_repo(tmp_path / "widget")
    with pytest.raises(SystemExit) as excinfo:
        main(["project", "add", str(repo), "--slug", "widget", "--to", "widget"])
    assert excinfo.value.code == 2


def test_the_default_home_refuses_attaching_a_temporary_project(default_home, tmp_path, capsys):
    """With AFT_HOME unset the vault is the user's real one, so attaching a directory
    under the system temporary directory waits for --allow-temp, like registering one
    does (BUILD-PLAN F25, F70)."""
    home_repo = make_repo(tmp_path / "widget")
    assert main(["project", "add", str(home_repo)]) == 0
    capsys.readouterr()
    scratch = temp_repo("scratch")
    try:
        assert main(["project", "add", str(scratch), "--to", "widget"]) == 2
        captured = capsys.readouterr()
        assert "temporary directory" in captured.err
        assert "--allow-temp" in captured.err
        registry = Registry.load(load().registry_path)
        assert registry.by_slug("widget").paths == [f"testhost:{home_repo.resolve()}"]

        assert main(["project", "add", str(scratch), "--allow-temp", "--to", "widget"]) == 0
        registry = Registry.load(load().registry_path)
        assert f"testhost:{scratch.resolve()}" in registry.by_slug("widget").paths
    finally:
        shutil.rmtree(scratch)


# Merging one project into another (F70)


def _write_page(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_project_merge_moves_everything_into_the_destination(initialized, tmp_path, capsys):
    """A merge gives the destination the source's paths and remotes, digests, archive and
    pages, and the source leaves the registry (F70)."""
    alpha_repo = make_repo(tmp_path / "alpha", origin="git@github.com:ex/alpha.git")
    beta_repo = make_repo(tmp_path / "beta")
    assert main(["project", "add", str(alpha_repo)]) == 0
    assert main(["project", "add", str(beta_repo)]) == 0
    capsys.readouterr()
    vault_root = initialized.vault
    _write_page(
        vault_root / "projects" / "alpha" / "overview.md",
        "---\ntype: Project\ntitle: alpha\ndescription: The alpha project.\nsources: []\n"
        "---\n\n## Facts\n\n- alpha keeps its own fact [stated] 2026-10-01\n\n"
        "## Lessons\n\n- alpha learned alone [observed] 2026-10-02\n",
    )
    _write_page(
        vault_root / "projects" / "alpha" / "recent.md",
        "---\ntype: Project\ntitle: alpha\ndescription: The alpha project.\nsources: []\n"
        "---\n\n## State\n\n- alpha is mid-migration [observed] 2026-10-04\n",
    )
    _write_page(vault_root / "projects" / "alpha" / "notes.md", "alpha notes\n")
    _write_page(
        vault_root / "projects" / "beta" / "overview.md",
        "---\ntype: Project\ntitle: beta\ndescription: The beta project.\nsources: []\n"
        "---\n\n## Facts\n\n- beta keeps its own fact [stated] 2026-10-03\n",
    )
    _write_page(
        vault_root / "projects" / "beta" / "recent.md",
        "---\ntype: Project\ntitle: beta\ndescription: The beta project.\nsources: []\n"
        "---\n\n## State\n\n- beta is mid-migration [observed] 2026-10-05\n",
    )
    _write_page(vault_root / "projects" / "beta" / "notes.md", "beta notes\n")
    _write_page(vault_root / "digests" / "alpha" / "2026" / "10" / "digest.md", "alpha digest\n")
    _write_page(vault_root / "digests" / "beta" / "2026" / "09" / "digest.md", "beta digest\n")
    _write_page(vault_root / "archive" / "projects" / "alpha" / "overview.md", "alpha archive\n")
    _write_page(
        vault_root / "archive" / "alpha.md",
        "---\ntype: Project\ntitle: alpha\ndescription: The alpha project.\nsources: []\n"
        "---\n\n## Facts\n\n- old alpha fact [stated] 2026-09-01\n",
    )
    _write_page(
        vault_root / "archive" / "beta.md",
        "---\ntype: Project\ntitle: beta\ndescription: The beta project.\nsources: []\n"
        "---\n\n## Facts\n\n- old beta fact [stated] 2026-09-02\n",
    )
    vault.commit(vault_root, ["projects", "digests", "archive"], "seed the two projects")

    assert main(["project", "merge", "alpha", "beta"]) == 0

    assert capsys.readouterr().out == "merged alpha into beta\n"
    registry = Registry.load(initialized.registry_path)
    beta = registry.by_slug("beta")
    assert registry.by_slug("alpha") is None
    assert beta.paths == [f"testhost:{beta_repo.resolve()}", f"testhost:{alpha_repo.resolve()}"]
    assert beta.remotes == ["github.com/ex/alpha"]

    overview = (vault_root / "projects" / "beta" / "overview.md").read_text()
    assert overview.index("beta keeps its own fact") < overview.index("alpha keeps its own fact")
    assert overview.index("## Lessons") < overview.index("alpha learned alone")
    assert overview.index("## Facts") < overview.index("## Lessons")
    recent = (vault_root / "projects" / "beta" / "recent.md").read_text()
    assert recent.index("beta is mid-migration") < recent.index("alpha is mid-migration")
    assert (vault_root / "projects" / "beta" / "notes.md").read_text() == "beta notes\n"
    assert (vault_root / "projects" / "beta" / "notes-2.md").read_text() == "alpha notes\n"
    assert not (vault_root / "projects" / "alpha").exists()
    assert (
        vault_root / "digests" / "beta" / "2026" / "10" / "digest.md"
    ).read_text() == "alpha digest\n"
    assert (
        vault_root / "digests" / "beta" / "2026" / "09" / "digest.md"
    ).read_text() == "beta digest\n"
    assert not (vault_root / "digests" / "alpha").exists()
    assert (
        vault_root / "archive" / "projects" / "beta" / "overview.md"
    ).read_text() == "alpha archive\n"
    assert not (vault_root / "archive" / "projects" / "alpha").exists()
    legacy = (vault_root / "archive" / "beta.md").read_text()
    assert legacy.index("old beta fact") < legacy.index("old alpha fact")
    assert not (vault_root / "archive" / "alpha.md").exists()
    assert run_git(["log", "-1", "--format=%s"], vault_root) == "aft: merge alpha into beta"
    assert run_git(["status", "--porcelain"], vault_root) == ""


def test_project_merge_moves_pages_the_destination_lacks(initialized, tmp_path, capsys):
    """Pages, a legacy archive page and digests the destination has none of move over
    whole (F70)."""
    alpha_repo = make_repo(tmp_path / "alpha")
    beta_repo = make_repo(tmp_path / "beta")
    assert main(["project", "add", str(alpha_repo)]) == 0
    assert main(["project", "add", str(beta_repo)]) == 0
    capsys.readouterr()
    vault_root = initialized.vault
    alpha_overview = (
        "---\ntype: Project\ntitle: alpha\ndescription: The alpha project.\nsources: []\n"
        "---\n\n## Facts\n\n- alpha keeps its own fact [stated] 2026-10-01\n"
    )
    _write_page(vault_root / "projects" / "alpha" / "overview.md", alpha_overview)
    _write_page(
        vault_root / "archive" / "alpha.md",
        "---\ntype: Project\ntitle: alpha\ndescription: The alpha project.\nsources: []\n"
        "---\n\n## Facts\n\n- old alpha fact [stated] 2026-09-01\n",
    )
    _write_page(vault_root / "digests" / "alpha" / "2026" / "10" / "digest.md", "alpha digest\n")
    vault.commit(vault_root, ["projects", "digests", "archive"], "seed alpha alone")

    assert main(["project", "merge", "alpha", "beta"]) == 0

    assert (vault_root / "projects" / "beta" / "overview.md").read_text() == alpha_overview
    assert not (vault_root / "projects" / "alpha").exists()
    assert not (vault_root / "projects" / "beta" / "recent.md").exists()
    assert (
        (vault_root / "archive" / "beta.md")
        .read_text()
        .endswith("- old alpha fact [stated] 2026-09-01\n")
    )
    assert not (vault_root / "archive" / "alpha.md").exists()
    assert (
        vault_root / "digests" / "beta" / "2026" / "10" / "digest.md"
    ).read_text() == "alpha digest\n"
    assert not (vault_root / "digests" / "alpha").exists()
    assert run_git(["log", "-1", "--format=%s"], vault_root) == "aft: merge alpha into beta"
    assert run_git(["status", "--porcelain"], vault_root) == ""


def test_project_merge_with_no_vault_directories_keeps_the_registry_tidy(
    initialized, tmp_path, capsys
):
    """Two projects with no pages, digests or archive merge as a registry change alone:
    the destination's paths and remotes are duplicate-free, and no directories appear
    (F70)."""
    alpha_repo = make_repo(tmp_path / "alpha", origin="git@github.com:ex/shared.git")
    beta_repo = make_repo(tmp_path / "beta", origin="git@github.com:ex/shared.git")
    assert main(["project", "add", str(alpha_repo)]) == 0
    assert main(["project", "add", str(beta_repo)]) == 0
    capsys.readouterr()

    assert main(["project", "merge", "alpha", "beta"]) == 0

    assert capsys.readouterr().out == "merged alpha into beta\n"
    registry = Registry.load(initialized.registry_path)
    beta = registry.by_slug("beta")
    assert registry.by_slug("alpha") is None
    assert beta.paths == [f"testhost:{beta_repo.resolve()}", f"testhost:{alpha_repo.resolve()}"]
    assert beta.remotes == ["github.com/ex/shared"]
    assert not (initialized.vault / "projects" / "alpha").exists()
    assert not (initialized.vault / "projects" / "beta").exists()
    assert not (initialized.vault / "digests" / "beta").exists()
    assert run_git(["log", "-1", "--format=%s"], initialized.vault) == "aft: merge alpha into beta"
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_merge_refuses_the_same_project(initialized, tmp_path, capsys):
    make_repo(tmp_path / "alpha")
    assert main(["project", "add", str(tmp_path / "alpha")]) == 0
    capsys.readouterr()

    assert main(["project", "merge", "alpha", "alpha"]) == 2

    assert "cannot merge a project into itself" in capsys.readouterr().err
    assert Registry.load(initialized.registry_path).by_slug("alpha") is not None


def test_project_merge_refuses_unknown_projects(initialized, tmp_path, capsys):
    """Both names have to be registered, whichever side of the merge they sit on (F70)."""
    make_repo(tmp_path / "alpha")
    assert main(["project", "add", str(tmp_path / "alpha")]) == 0
    capsys.readouterr()

    assert main(["project", "merge", "delta", "alpha"]) == 2
    assert "no project 'delta'" in capsys.readouterr().err
    assert main(["project", "merge", "alpha", "delta"]) == 2
    assert "no project 'delta'" in capsys.readouterr().err
    registry = Registry.load(initialized.registry_path)
    assert registry.by_slug("alpha") is not None
    assert registry.by_slug("delta") is None
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_project_merge_does_not_copy_source_listing(initialized, tmp_path, capsys):
    """A merge does not copy the source's reserved listing files, and rewrites the
    destination's listing to name the pages it now holds (T50)."""
    alpha_repo = make_repo(tmp_path / "alpha")
    beta_repo = make_repo(tmp_path / "beta")
    assert main(["project", "add", str(alpha_repo)]) == 0
    assert main(["project", "add", str(beta_repo)]) == 0
    capsys.readouterr()
    vault_root = initialized.vault
    _write_page(
        vault_root / "projects" / "alpha" / "overview.md",
        "---\ntype: Project\ntitle: alpha\ndescription: Alpha.\nsources: []\n"
        "---\n\n## Facts\n\n- alpha fact [stated] 2026-10-01\n",
    )
    _write_page(
        vault_root / "projects" / "alpha" / "notes.md",
        "---\ntype: Project\ntitle: alpha\ndescription: Alpha notes.\nsources: []\n"
        "---\n\n## Facts\n\n- alpha note [stated] 2026-10-01\n",
    )
    _write_page(
        vault_root / "projects" / "alpha" / "index.md",
        "# alpha\n\n- overview.md\n- notes.md\n",
    )
    _write_page(vault_root / "projects" / "alpha" / "log.md", "alpha log\n")
    _write_page(
        vault_root / "projects" / "beta" / "overview.md",
        "---\ntype: Project\ntitle: beta\ndescription: Beta.\nsources: []\n"
        "---\n\n## Facts\n\n- beta fact [stated] 2026-10-02\n",
    )
    _write_page(
        vault_root / "projects" / "beta" / "index.md",
        "# beta\n\n- overview.md\n",
    )
    vault.commit(vault_root, ["projects"], "seed alpha and beta with listings")

    assert main(["project", "merge", "alpha", "beta"]) == 0
    assert capsys.readouterr().out == "merged alpha into beta\n"

    assert not (vault_root / "projects" / "alpha").exists()
    assert not (vault_root / "projects" / "beta" / "index-2.md").exists()
    assert not (vault_root / "projects" / "beta" / "log-2.md").exists()
    assert not (vault_root / "projects" / "beta" / "log.md").exists()
    beta_index = (vault_root / "projects" / "beta" / "index.md").read_text()
    assert "- overview.md" in beta_index
    assert "- notes.md" in beta_index
    assert "index-2.md" not in beta_index
    assert "log" not in beta_index
    assert run_git(["status", "--porcelain"], vault_root) == ""
