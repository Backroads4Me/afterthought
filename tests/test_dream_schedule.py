from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import queue, vault
from aft.cli import main
from aft.dream import schedule
from aft.events import SESSION_END, SessionEvent
from conftest import make_repo, run_git

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=2)


def digest(cfg, slug: str, name: str, at: str, consumed: str | None = None) -> str:
    rel = f"digests/{slug}/2026/09/{name}.md"
    aft = f"  consumed: {consumed}\n" if consumed else ""
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "---\ntype: Session Digest\ntitle: t\ndescription: d\n"
        f"generated: {{by: aft, at: '{at}'}}\naft:\n  project: p_1\n{aft}---\n"
    )
    vault.commit(cfg.vault, [rel], f"aft: digest {name}")
    return rel


def event(cwd: Path, at: datetime) -> SessionEvent:
    return SessionEvent(
        agent="claude",
        kind=SESSION_END,
        session_id="s1",
        cwd=cwd,
        transcript_path=None,
        time=at,
        detail=None,
    )


def age(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def head(cfg) -> str:
    return run_git(["rev-parse", "HEAD"], cfg.vault)


def subjects(cfg) -> list[str]:
    return run_git(["log", "--format=%s"], cfg.vault).splitlines()


def hand_edit(cfg, when: datetime) -> Path:
    page = cfg.vault / "global" / "preferences.md"
    page.write_text(page.read_text() + "\n- No RGB lighting.\n")
    age(page, when)
    return page


@pytest.fixture
def widget(initialized, tmp_path):
    repo = make_repo(tmp_path / "widget")
    assert main(["project", "add", str(repo)]) == 0
    return repo


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, cfg, slug, digests) -> None:
        self.calls.append((slug, [str(p.relative_to(cfg.vault)) for p in digests]))


def test_no_digests_no_commit(initialized):
    hand_edit(initialized, LONG_AGO)
    before = head(initialized)
    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result == schedule.Run()
    assert head(initialized) == before
    assert process.calls == []
    assert "preferences.md" in run_git(["status", "--porcelain"], initialized.vault)


def test_consumed_digests_are_not_waiting(initialized):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z", consumed="abc123")
    before = head(initialized)
    assert schedule.run(initialized, Recorder(), now=NOW) == schedule.Run()
    assert head(initialized) == before


def test_waiting_digests_in_session_order(initialized):
    later = digest(initialized, "widget", "2026-09-29-claude-b", "2026-09-29T09:00:00Z")
    first = digest(initialized, "widget", "2026-09-29-claude-a", "2026-09-29T08:00:00Z")
    part = digest(initialized, "widget", "2026-09-29-claude-a-2", "2026-09-29T09:00:00Z")
    digest(initialized, "widget", "done", "2026-09-29T07:00:00Z", consumed="abc123")
    other = digest(initialized, "gadget", "2026-09-29-pi-c", "2026-09-29T10:00:00Z")
    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result.ran == ["gadget", "widget"]
    assert process.calls == [("gadget", [other]), ("widget", [first, part, later])]


def test_recent_hand_edit_skips_the_project(widget, initialized):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    hand_edit(initialized, NOW - timedelta(minutes=5))
    before = head(initialized)
    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result.edited == ["widget"]
    assert result.ran == [] and result.checkpoint is None
    assert process.calls == []
    assert head(initialized) == before
    assert "preferences.md" in run_git(["status", "--porcelain"], initialized.vault)


def test_older_hand_edit_is_checkpointed_apart_from_the_dream_commit(widget, initialized):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    hand_edit(initialized, LONG_AGO)
    note = initialized.vault / "projects" / "widget" / "scratch.md"
    note.parent.mkdir(parents=True)
    note.write_text("typed by hand\n")
    age(note, LONG_AGO)

    def dream(cfg, slug, digests) -> None:
        page = cfg.vault / "log.md"
        page.write_text(page.read_text() + "\n- dreamed\n")
        vault.commit(cfg.vault, ["log.md"], f"aft: dream {slug}")

    result = schedule.run(initialized, dream, now=NOW)
    assert result.ran == ["widget"]
    assert subjects(initialized)[:2] == ["aft: dream widget", schedule.CHECKPOINT_MESSAGE]
    assert result.checkpoint == run_git(["rev-parse", "HEAD~1"], initialized.vault)
    changed = run_git(
        ["show", "--name-only", "--format=", result.checkpoint], initialized.vault
    ).splitlines()
    assert changed == ["global/preferences.md", "projects/widget/scratch.md"]
    dream_changed = run_git(["show", "--name-only", "--format=", "HEAD"], initialized.vault)
    assert dream_changed == "log.md"
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_clean_vault_makes_no_checkpoint(widget, initialized):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    before = head(initialized)
    result = schedule.run(initialized, Recorder(), now=NOW)
    assert result.ran == ["widget"] and result.checkpoint is None
    assert head(initialized) == before


def test_active_session_skips_only_its_project(widget, initialized, tmp_path):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    digest(initialized, "gadget", "b", "2026-09-29T08:00:00Z")
    gadget = make_repo(tmp_path / "gadget")
    assert main(["project", "add", str(gadget)]) == 0
    (widget / "src").mkdir()
    queue.enqueue(initialized, event(widget / "src", NOW - timedelta(minutes=3)))
    queue.enqueue(initialized, event(gadget, NOW - timedelta(minutes=20)))
    queue.enqueue(initialized, event(tmp_path / "removed-worktree", NOW))
    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result.active == ["widget"]
    assert result.ran == ["gadget"]
    assert [slug for slug, _ in process.calls] == ["gadget"]


def test_quiet_period_follows_the_config(widget, home, tmp_path):
    (home / "config.toml").write_text(
        f'vault = "{tmp_path / "vault"}"\nhost = "testhost"\n[dream]\nquiet_minutes = 60\n'
    )
    from aft.config import load

    cfg = load()
    digest(cfg, "widget", "a", "2026-09-29T08:00:00Z")
    queue.enqueue(cfg, event(widget, NOW - timedelta(minutes=30)))
    assert schedule.run(cfg, Recorder(), now=NOW).active == ["widget"]
    assert schedule.run(cfg, Recorder(), now=NOW + timedelta(minutes=31)).ran == ["widget"]


def test_run_holds_the_vault_lock(widget, initialized):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    held = []

    def probe(cfg, slug, digests) -> None:
        with open(cfg.lock_path, "w") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                held.append(slug)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)

    assert schedule.run(initialized, probe, now=NOW).ran == ["widget"]
    assert held == ["widget"]


def test_uncommitted_reports_deletions_and_renames(initialized):
    root = initialized.vault
    run_git(["mv", "log.md", "history.md"], root)
    (root / "global" / "environment.md").unlink()
    age(root / "global", LONG_AGO)
    (root / "new.md").write_text("x\n")
    changes = schedule.uncommitted(root)
    assert set(changes) == {"history.md", "log.md", "global/environment.md", "new.md"}
    assert abs(changes["global/environment.md"] - LONG_AGO) < timedelta(seconds=1)


def test_last_events_keeps_the_newest_per_directory(initialized, tmp_path):
    here, there = tmp_path / "a", tmp_path / "b"
    queue.enqueue(initialized, event(here, LONG_AGO))
    queue.enqueue(initialized, event(here, NOW))
    queue.enqueue(initialized, event(there, LONG_AGO))
    assert queue.last_events(initialized) == {here: NOW, there: LONG_AGO}


def test_cli_dream(widget, initialized, capsys):
    capsys.readouterr()
    assert main(["dream"]) == 0
    assert capsys.readouterr().out == "no digests waiting\n"
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    assert main(["dream"]) == 0
    assert capsys.readouterr().out == "widget: 1 digest(s) taken up\n"
