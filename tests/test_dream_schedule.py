from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import page, pagewrite, queue, vault, work
from aft.cli import main
from aft.dream import apply, schedule
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


def test_a_run_with_nothing_waiting_writes_the_idle_and_gone_report(widget, initialized, tmp_path):
    """A dream run with nothing waiting still names the idle and the gone: a home whose
    every project is idle gets no digest to run, and that is the state the section exists
    to surface (DESIGN 7, F49). It is written once a day, as a run with work writes it."""
    queue.enqueue(
        initialized,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="s-widget",
            cwd=widget,
            transcript_path=None,
            time=NOW - timedelta(days=100),
            detail=None,
        ),
    )
    run = schedule.run(initialized, Recorder(), now=NOW)
    assert run.waiting == {} and run.ran == []
    report_path = initialized.vault / "reports" / "2026" / "09" / "2026-09-29.md"
    body = report_path.read_text()
    assert "## Idle and gone" in body
    assert "widget: no session since 2026-06-21" in body
    # The second quiet run adds nothing: the day's section stands.
    schedule.run(initialized, Recorder(), now=NOW)
    assert report_path.read_text() == body


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


def test_one_slugs_digests_run_once_per_directory_their_records_name(widget, initialized, tmp_path):
    """Two directories that once claimed one name hold one digest directory, but never one
    project: the dream decides each digest's project from its own record, so one slug's
    waiting digests run as two runs (DESIGN 7, F49)."""
    second = (tmp_path / "yard" / "widget").resolve()
    second.mkdir(parents=True)
    # A record that names a project's id (a session that started in a registered project)
    # and one that names only a path the registry has never met.
    assert queue.record_project(initialized, "claude", "s-w1", "p_1", "widget")
    assert queue.record_project(initialized, "claude", "s-w2", None, "widget", second)
    digest(initialized, "widget", "2026-09-29-claude-s-w1", "2026-09-29T08:00:00Z")
    digest(initialized, "widget", "2026-09-29-claude-s-w2", "2026-09-29T07:00:00Z")

    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)

    assert [slug for slug, _ in process.calls] == ["widget", "widget"]
    assert all(len(digests) == 1 for _, digests in process.calls)
    assert result.ran == ["widget", "widget"]
    assert result.taken == {"widget": 2}


def _split_slug_setup(initialized, tmp_path) -> None:
    """One project's slug holding digests of two directories' sessions, so its digests run
    as two groups (DESIGN 7, F49)."""
    second = (tmp_path / "yard" / "widget").resolve()
    second.mkdir(parents=True)
    assert queue.record_project(initialized, "claude", "s-w1", "p_1", "widget")
    assert queue.record_project(initialized, "claude", "s-w2", None, "widget", second)
    digest(initialized, "widget", "2026-09-29-claude-s-w1", "2026-09-29T08:00:00Z")
    digest(initialized, "widget", "2026-09-29-claude-s-w2", "2026-09-29T07:00:00Z")


def test_one_slugs_group_reports_come_out_together(widget, initialized, tmp_path):
    """One slug that runs as several groups keeps every group's work (DESIGN 7, F49): the
    run hands back one report per slug with all the groups' items named, and the commit the
    first group's run made is still the run's."""
    _split_slug_setup(initialized, tmp_path)

    def process(cfg, slug, digests):
        return apply.ProjectRunReport(
            project=slug,
            commit="sha123",
            added=[
                apply.AddedFact(
                    statement=f"fact from {digests[0].stem}",
                    kind="state",
                    source="observed",
                    date="2026-09-29",
                    refs=(),
                    path="projects/widget/overview.md",
                )
            ],
        )

    result = schedule.run(initialized, process, now=NOW)

    report = result.reports["widget"]
    assert [fact.statement for fact in report.added] == [
        "fact from 2026-09-29-claude-s-w2",
        "fact from 2026-09-29-claude-s-w1",
    ]
    assert report.commit == "sha123"
    assert result.taken == {"widget": 2}


def test_a_failing_group_consumes_nothing_and_its_error_reaches_the_report(
    widget, initialized, tmp_path
):
    """A slug whose groups split across directories counts only the digests the runs
    actually consumed (DESIGN 7, F49): a group whose run failed consumed none of its
    digests, so they wait again, and its error stands beside the group that went through."""
    _split_slug_setup(initialized, tmp_path)

    def process(cfg, slug, digests):
        if "s-w1" in digests[0].name:
            return apply.ProjectRunReport(project=slug, commit="sha123")
        raise apply.ApplyError("the model endpoint said no")

    result = schedule.run(initialized, process, now=NOW)

    assert result.taken == {"widget": 1}
    assert result.ran == ["widget"]
    assert result.failures["widget"] == "the model endpoint said no"
    report = result.reports["widget"]
    assert report.commit == "sha123"
    assert report.error == "the model endpoint said no"


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
    assert subjects(initialized)[:3] == [
        "aft: dream widget",
        f"aft: tag hand edits from {result.checkpoint}",
        schedule.CHECKPOINT_MESSAGE,
    ]
    assert result.checkpoint == run_git(["rev-parse", "HEAD~2"], initialized.vault)
    assert result.tagged == ["global/preferences.md"]
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


def test_maintenance_expires_each_project_in_its_own_commit(widget, initialized, tmp_path):
    """Two registered projects with spent short-term lines and nothing waiting: each
    project's expiry is its own commit, named for it, beside its own report section
    (BUILD-PLAN F47)."""
    gadget = make_repo(tmp_path / "gadget")
    assert main(["project", "add", str(gadget)]) == 0
    for slug in ("gadget", "widget"):
        recent = initialized.vault / "projects" / slug / "recent.md"
        recent.parent.mkdir(parents=True, exist_ok=True)
        recent.write_text(
            "---\ntype: Project\ntitle: t\ndescription: d\n"
            "sources:\n  - {id: s1, resource: 'aft://session/c/s0#e1', quote: q}\n"
            "aft:\n  project: p_1\n---\n\n"
            "## State\n\n- The rewrite is paused. [observed] 2026-08-01 [^s1]\n"
        )
    vault.commit(initialized.vault, ["projects"], "seed recents")
    run = schedule.run(initialized, Recorder(), now=NOW)
    assert run.expired == ["gadget", "widget"]
    assert subjects(initialized)[:4] == [
        "aft: report widget",
        "aft: expire short-term memory widget",
        "aft: report gadget",
        "aft: expire short-term memory gadget",
    ]
    for slug in ("gadget", "widget"):
        sha = run_git(
            ["log", "-n", "1", "--format=%H", f"--grep=aft: expire short-term memory {slug}"],
            initialized.vault,
        )
        changed = run_git(["show", "--name-only", "--format=", sha], initialized.vault).splitlines()
        assert changed == [f"projects/{slug}/recent.md"]


def test_maintenance_leaves_a_project_with_waiting_digests(widget, initialized):
    """A project with waiting digests is not maintenance: its own run expires its lines
    when it is applied, so an expired line beside a waiting digest stays for that run."""
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    recent = initialized.vault / "projects" / "widget" / "recent.md"
    recent.parent.mkdir(parents=True)
    recent.write_text(
        "---\ntype: Project\ntitle: t\ndescription: d\n"
        "sources:\n  - {id: s1, resource: 'aft://session/c/s0#e1', quote: q}\n"
        "aft:\n  project: p_1\n---\n\n"
        "## State\n\n- The rewrite is paused. [observed] 2026-08-01 [^s1]\n"
    )
    vault.commit(initialized.vault, ["projects/widget/recent.md"], "seed recent")
    result = schedule.run(initialized, Recorder(), now=NOW)
    assert result.ran == ["widget"]
    assert result.expired == []
    assert "The rewrite is paused." in recent.read_text()


def test_an_expiry_failure_is_reported_once_a_day(widget, initialized, monkeypatch):
    """A project whose short-term expiry keeps failing hands its error to the day's report
    once (T30): the failure stays in every run's result, and the day's page does not grow
    by one error section per run."""
    recent = initialized.vault / "projects" / "widget" / "recent.md"
    recent.parent.mkdir(parents=True)
    recent.write_text(
        "---\ntype: Project\ntitle: t\ndescription: d\n"
        "sources:\n  - {id: s1, resource: 'aft://session/c/s0#e1', quote: q}\n"
        "aft:\n  project: p_1\n---\n\n"
        "## State\n\n- The rewrite is paused. [observed] 2026-08-01 [^s1]\n"
    )
    vault.commit(initialized.vault, ["projects/widget/recent.md"], "seed recent")

    error = "the expiry said no"

    def failing_expiry(cfg, slug, now=None):
        raise RuntimeError(error)

    monkeypatch.setattr(apply, "expire_short_term", failing_expiry)

    first = schedule.run(initialized, Recorder(), now=NOW)
    assert first.failures == {"widget": error}
    report_path = initialized.vault / "reports" / "2026" / "09" / "2026-09-29.md"
    body = report_path.read_text()
    assert "# widget" in body
    assert f"- Error: {error}" in body

    # The second run of the day still records the failure, and writes nothing: the page
    # and the commit log stand as the first run left them.
    commits = subjects(initialized)
    second = schedule.run(initialized, Recorder(), now=NOW)
    assert second.failures == {"widget": error}
    assert report_path.read_text() == body
    assert subjects(initialized) == commits

    # A different error is news again, the day's page carrying one section for it.
    def other_failure(cfg, slug, now=None):
        raise RuntimeError("the expiry said something else")

    monkeypatch.setattr(apply, "expire_short_term", other_failure)
    third = schedule.run(initialized, Recorder(), now=NOW)
    assert third.failures == {"widget": "the expiry said something else"}
    grown = report_path.read_text()
    assert grown != body
    assert "- Error: the expiry said something else" in grown


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
    assert capsys.readouterr().out == "aft dream: no digests waiting\n"
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    assert main(["dream"]) == 0
    assert capsys.readouterr().out == "aft dream: widget: 1 digest(s) taken up\n"


def test_work_dreams_at_its_end(widget, initialized, capsys, monkeypatch):
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    monkeypatch.setattr(work.capture, "sweep", lambda config, moment: [])
    capsys.readouterr()
    assert main(["work"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "aft work: 0 swept, 0 digested, 0 with nothing to digest",
        "aft dream: widget: 1 digest(s) taken up",
    ]


PAGE = """\
---
type: Project
title: widget
description: The widget.
sources:
  - id: s1
    resource: aft://session/claude/abc#entry-1
    quote: "we use uv"
aft:
  project: p_1
---

## Decisions

- The build uses uv. [stated] 2026-09-01 [^s1]
- Old bare line typed before the checkpoint.

> [!question] 2026-09-02
> Is this still true?

## Candidates

- Maybe cache wheels. [inferred] 2026-09-01 [^s1] (signals 1/3, sessions 1/2)
"""


def seeded_page(initialized) -> Path:
    """The fixture page committed into the vault, with one digest waiting on it."""
    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True)
    overview.write_text(PAGE)
    (root / "projects" / "widget" / "index.md").write_text("---\ntype: Index\n---\n")
    vault.commit(root, ["projects"], "seed")
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    return overview


def tag_day(cfg, sha: str) -> str:
    """The UTC date of the checkpoint commit, which is the date its tags carry."""
    stamp = int(run_git(["show", "-s", "--format=%ct", sha], cfg.vault))
    return datetime.fromtimestamp(stamp, UTC).date().isoformat()


def test_checkpoint_tags_added_bare_bullets_stated(widget, initialized):
    root = initialized.vault
    overview = root / "projects" / "widget" / "overview.md"
    overview.parent.mkdir(parents=True)
    overview.write_text(PAGE)
    index = root / "projects" / "widget" / "index.md"
    index.write_text("---\ntype: Index\n---\n")
    vault.commit(root, ["projects"], "seed")
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")

    edited = (
        PAGE.replace(
            "- Old bare line typed before the checkpoint.\n",
            "- Old bare line typed before the checkpoint.\n"
            "- Releases are cut from main only.\n"
            "- Typed in full form. [observed] 2026-09-20 [^s1]\n",
        )
        .replace("> Is this still true?\n", "> Is this still true?\n> - yes, still true\n")
        .replace("(signals 1/3, sessions 1/2)\n", "(signals 1/3, sessions 1/2)\n- A hunch.\n")
    )
    overview.write_text(edited)
    index.write_text("---\ntype: Index\n---\n\n- hand line in an index\n")
    for path in (overview, index):
        age(path, LONG_AGO)

    result = schedule.run(initialized, Recorder(), now=NOW)
    sha = result.checkpoint
    assert run_git(["show", f"{sha}:projects/widget/overview.md"], root) + "\n" == edited
    assert result.tagged == ["projects/widget/overview.md"]
    assert subjects(initialized)[0] == f"aft: tag hand edits from {sha}"

    day = (
        datetime.fromtimestamp(int(run_git(["show", "-s", "--format=%ct", sha], root)), UTC)
        .date()
        .isoformat()
    )
    text = overview.read_text()
    assert f"- Releases are cut from main only. [stated] {day} [^s2]\n" in text
    assert "- Typed in full form. [observed] 2026-09-20 [^s1]\n" in text
    assert "- Old bare line typed before the checkpoint.\n" in text
    assert "> - yes, still true\n" in text
    assert "(signals 1/3, sessions 1/2)\n- A hunch.\n" in text
    assert (
        f"  - id: s2\n    resource: aft://edit/{sha}\n    title: Hand edit {day}\n"
        "    quote: Releases are cut from main only.\n"
    ) in text
    assert "- hand line in an index" in index.read_text()
    assert run_git(["status", "--porcelain"], root) == ""
    fact = next(f for f in page.parse(text, overview).facts if f.refs == ("s2",))
    assert (fact.statement, fact.source, fact.kind) == (
        "Releases are cut from main only.",
        "stated",
        "decision",
    )


def test_checkpoint_tags_a_bare_bullet_that_ends_in_a_date(widget, initialized):
    """A date with nothing to attach it to is a bare line, not a fact line half-typed."""
    overview = seeded_page(initialized)
    edited = overview.read_text().replace(
        "- The build uses uv. [stated] 2026-09-01 [^s1]\n",
        "- The build uses uv. [stated] 2026-09-01 [^s1]\n"
        "- The 3.13 wheel build broke on 2026-09-27\n"
        "- Cut releases by hand. [observed] 2026-09-20\n",
    )
    assert edited != overview.read_text()
    overview.write_text(edited)
    age(overview, LONG_AGO)

    result = schedule.run(initialized, Recorder(), now=NOW)
    day = tag_day(initialized, result.checkpoint)
    assert result.tagged == ["projects/widget/overview.md"]
    assert subjects(initialized)[0] == f"aft: tag hand edits from {result.checkpoint}"

    text = overview.read_text()
    assert f"- The 3.13 wheel build broke on 2026-09-27 [stated] {day} [^s2]\n" in text
    assert "- Cut releases by hand. [observed] 2026-09-20\n" in text
    assert (
        f"  - id: s2\n    resource: aft://edit/{result.checkpoint}\n"
        f"    title: Hand edit {day}\n    quote: The 3.13 wheel build broke on 2026-09-27\n"
    ) in text
    fact = next(f for f in page.parse(text, overview).facts if f.refs == ("s2",))
    assert (fact.statement, fact.source, fact.kind) == (
        "The 3.13 wheel build broke on 2026-09-27",
        "stated",
        "decision",
    )


def test_checkpoint_tags_a_bare_bullet_that_carries_its_own_note(widget, initialized):
    """The note under a hand-typed bullet marks the fact it becomes, and stops nothing tagged."""
    overview = seeded_page(initialized)
    edited = overview.read_text().replace(
        "## Candidates",
        "- We ship on Tuesdays.\n\n> [!question] 2026-09-25\n> Is that still the cadence?\n\n"
        "- Releases are cut from main only.\n\n## Candidates",
    )
    assert edited != overview.read_text()
    overview.write_text(edited)
    age(overview, LONG_AGO)

    result = schedule.run(initialized, Recorder(), now=NOW)
    day = tag_day(initialized, result.checkpoint)
    assert result.tagged == ["projects/widget/overview.md"]
    assert subjects(initialized)[0] == f"aft: tag hand edits from {result.checkpoint}"

    text = overview.read_text()
    assert f"- We ship on Tuesdays. [stated] {day} [^s2]\n" in text
    assert f"- Releases are cut from main only. [stated] {day} [^s3]\n" in text
    facts = {f.statement: f for f in page.parse(text, overview).facts}
    noted = facts["We ship on Tuesdays."]
    assert (noted.source, noted.state) == ("stated", page.NEEDS_YOU)
    assert noted.callouts[0].text == ["Is that still the cadence?"]
    assert facts["Releases are cut from main only."].state == page.ACTIVE
    assert [message for _, message in page.parse(text, overview).errors] == [
        "fact line must read '- <statement> [<source>] YYYY-MM-DD [^id]'",
        "a callout cannot mark a refused fact line",
    ]


def test_checkpoint_without_bare_bullets_makes_no_tag_commit(widget, initialized):
    root = initialized.vault
    digest(initialized, "widget", "a", "2026-09-29T08:00:00Z")
    page_path = root / "global" / "preferences.md"
    page_path.write_text(page_path.read_text().replace("description: Your", "description: My"))
    age(page_path, LONG_AGO)
    result = schedule.run(initialized, Recorder(), now=NOW)
    assert result.checkpoint and result.tagged == []
    assert subjects(initialized)[0] == schedule.CHECKPOINT_MESSAGE


def test_a_project_that_fails_stops_only_itself(initialized):
    """A failure in one project is that project's own: it lands in failures and in the day's
    report, and the other ready project still runs."""
    digest(initialized, "p_fail", "d_fail", "2026-09-29T08:00:00Z")
    digest(initialized, "p_ok", "d_ok", "2026-09-29T08:00:00Z")

    def failing_process(cfg, slug, digests):
        if slug == "p_fail":
            # A page writer refusal, which is neither of the two errors the dream expects.
            raise pagewrite.WriteError("a statement that will not fit one line")
        return schedule.ProjectRunReport(project=slug, commit="sha123")

    result = schedule.run(initialized, failing_process, now=NOW)

    assert "p_fail" not in result.ran
    assert result.failures["p_fail"] == "a statement that will not fit one line"
    assert result.reports["p_fail"].error == "a statement that will not fit one line"
    assert result.ran == ["p_ok"]
    assert result.reports["p_ok"].commit == "sha123"

    report_text = (initialized.vault / "reports" / "2026" / "09" / "2026-09-29.md").read_text()
    assert "# p_fail" in report_text
    assert "- Error: a statement that will not fit one line" in report_text
    assert "# p_ok" in report_text
    assert subjects(initialized)[0] == "aft: report p_ok"


def test_every_project_failing_still_reports_the_checkpoint_commits(initialized):
    """A run whose every project fails still names the checkpoint and the hand-edit tag
    commits in its report, so the Revert item is never lost (DESIGN 9 item 9)."""
    digest(initialized, "p_one", "d_one", "2026-09-29T08:00:00Z")
    hand_edit(initialized, LONG_AGO)

    def failing_process(cfg, slug, digests):
        raise apply.ApplyError("the model endpoint said no")

    result = schedule.run(initialized, failing_process, now=NOW)

    assert result.checkpoint and result.tagged and result.tag_commit
    assert list(result.failures) == ["p_one"]
    report_text = (initialized.vault / "reports" / "2026" / "09" / "2026-09-29.md").read_text()
    assert "- Error: the model endpoint said no" in report_text
    assert f"Separate checkpoint commit: git revert {result.checkpoint}" in report_text
    assert f"Separate hand edits tag commit: git revert {result.tag_commit}" in report_text


def test_a_recorded_directory_is_dated_under_the_name_its_record_settled(
    widget, initialized, tmp_path
):
    """A session's events are dated under the name its session-start record settled, not a
    fresh answer for its directory (DESIGN 7, F49): the digest of that session files under
    the record's name, so the dream's quiet check sees the sessions where the digests went,
    and a same-named directory that claimed the plain name first does not hide one."""
    from aft.registry import Registry

    claimed = tmp_path / "fieldwork" / "widgets"
    claimed.mkdir(parents=True)
    queue.record_project(
        initialized,
        "claude",
        "s0",
        None,
        "widgets",
        claimed,
        registry=Registry.load(initialized.registry_path),
    )
    # A second directory of the same name records after: its plain name is taken, so the
    # record settles its parent's name in front of it.
    loose = tmp_path / "loose" / "widgets"
    loose.mkdir(parents=True)
    queue.record_project(
        initialized,
        "claude",
        "s1",
        None,
        "widgets",
        loose,
        registry=Registry.load(initialized.registry_path),
    )
    record = queue.recorded_project(initialized, "claude", "s1")
    assert record.slug == "loose-widgets"

    digest(initialized, "loose-widgets", "c", "2026-09-29T08:00:00Z")
    moment = NOW - timedelta(minutes=3)
    queue.enqueue(initialized, event(loose, moment))
    assert schedule.last_activity(initialized).get("loose-widgets") == moment

    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result.active == ["loose-widgets"]
    assert process.calls == [], "a session recorded under one name hid behind a fresh one"


def test_a_session_below_the_root_dates_the_name_its_record_settled(widget, initialized, tmp_path):
    """A session that runs below the root its digests file under, in a linked worktree, or
    through a symlink is dated under the name its directory's session-start record settled
    (DESIGN 7, F49): the record's name is looked up by the resolved root, so a session one
    level deep holds back the right group instead of hiding behind a fresh answer."""
    from aft.registry import Registry

    claimer = tmp_path / "fieldwork" / "widgets"
    claimer.mkdir(parents=True)
    queue.record_project(
        initialized,
        "claude",
        "s0",
        None,
        "widgets",
        claimer,
        registry=Registry.load(initialized.registry_path),
    )
    # The directory the digest files under is a repository; another same-named directory
    # claimed the plain name first, so the record settled its parent's name.
    repo = make_repo(tmp_path / "elsewhere" / "widgets")
    queue.record_project(
        initialized,
        "claude",
        "s1",
        None,
        "widgets",
        repo,
        registry=Registry.load(initialized.registry_path),
    )
    assert queue.recorded_project(initialized, "claude", "s1").slug == "elsewhere-widgets"
    run_git(["worktree", "add", "--detach", str(tmp_path / "tree"), "HEAD"], cwd=repo)
    linked = tmp_path / "alias"
    linked.symlink_to(repo, target_is_directory=True)
    (repo / "src").mkdir()

    digest(initialized, "elsewhere-widgets", "c", "2026-09-29T08:00:00Z")
    moment = NOW - timedelta(minutes=3)
    queue.enqueue(initialized, event(repo / "src", moment))
    assert schedule.last_activity(initialized).get("elsewhere-widgets") == moment
    queue.enqueue(initialized, event(tmp_path / "tree", moment))
    assert schedule.last_activity(initialized).get("elsewhere-widgets") == moment
    queue.enqueue(initialized, event(linked, moment))
    assert schedule.last_activity(initialized).get("elsewhere-widgets") == moment

    process = Recorder()
    result = schedule.run(initialized, process, now=NOW)
    assert result.active == ["elsewhere-widgets"]
    assert process.calls == [], "a session below the root hid behind a fresh one"
