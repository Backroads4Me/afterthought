"""Notes waiting on the user, and settling them (BUILD-PLAN F42)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml

from aft import evidence, lint, notes, pagewrite
from aft import vault as vaultmod
from aft.cli import main
from aft.config import Config
from aft.dream import apply, rules
from aft.readers import ASSISTANT, USER, Entry
from aft.registry import Registry
from conftest import run_git

NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
OVERVIEW = "projects/widget/overview.md"

BODY = """## State

- The daemon listens on 8123. [observed] 2026-09-20 [^s1]

> [!conflict] 2026-10-05
> Newer evidence: The daemon listens on 9000. ([^s2])

- The widget is round. [observed] 2026-09-20 [^s1]
"""


def entry(id: str, day: int, role: str, text: str) -> Entry:
    return Entry(
        id=id, time=datetime(2026, 10, day, 9, tzinfo=UTC), role=role, text=text, is_error=False
    )


@pytest.fixture
def widget(initialized: Config) -> Config:
    """A project whose overview holds one line under a conflict note. The line's quote and
    the note's quote both sit in the archive."""
    registry = Registry.load(initialized.registry_path)
    project = registry.add("widget", "Widget")
    registry.save()
    evidence.store(
        initialized,
        agent="c",
        session_id="1",
        project_id=project.id,
        entries=[entry("e1", 1, ASSISTANT, "it listens on 8123 and is round")],
    )
    evidence.store(
        initialized,
        agent="c",
        session_id="2",
        project_id=project.id,
        entries=[entry("e1", 5, USER, "the daemon now listens on 9000")],
    )
    write_overview(
        initialized,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "listens on 9000"},
        ],
    )
    return initialized


def write_overview(config: Config, project_id: str, sources: list[dict], body: str = BODY):
    front = {
        "type": "Project",
        "title": "Widget",
        "description": "The widget project.",
        "sources": sources,
        "aft": {"project": project_id},
    }
    path = config.vault / OVERVIEW
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(front, sort_keys=False).strip()}\n---\n\n{body}")
    vaultmod.commit(config.vault, [OVERVIEW], "seed overview")


def page_text(config: Config) -> str:
    return (config.vault / OVERVIEW).read_text()


def the_note(config: Config) -> notes.Note:
    [note] = notes.open_notes(config, "widget")
    return note


def test_open_notes_give_the_line_the_note_and_the_quotes_behind_both(widget):
    note = the_note(widget)
    assert note.page == OVERVIEW
    assert note.statement == "The daemon listens on 8123."
    assert note.note_type == "conflict"
    assert note.note_date == "2026-10-05"
    assert note.note_text == ["Newer evidence: The daemon listens on 9000. ([^s2])"]
    assert [(s.id, s.verified, s.yours) for s in note.line_sources] == [("s1", True, False)]
    [source] = note.note_sources
    assert (source.id, source.quote, source.date, source.verified, source.yours) == (
        "s2",
        "listens on 9000",
        "2026-10-05",
        True,
        True,
    )
    assert page_text(widget).splitlines()[note.line - 1].startswith("- The daemon listens on 8123.")


def test_count_reads_the_project_and_the_shared_pages_only(widget):
    assert notes.count(widget, "widget") == 1
    assert notes.count(widget, None) == 0
    for shared in ("global/preferences.md", "references/spec/power.md"):
        page = widget.vault / shared
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(BODY.replace("[^s", "[^g"))
    other = widget.vault / "projects/other/nested/page.md"
    other.parent.mkdir(parents=True)
    other.write_text(BODY)
    assert notes.count(widget, None) == 2
    assert notes.count(widget, "widget") == 3
    assert notes.count(widget, "other") == 3
    assert notes.count(widget, None, everywhere=True) == 4


def test_every_note_needs_you_lists_is_one_aft_notes_lists(widget):
    """The notes a user is asked about are the notes reports/needs-you.md holds."""
    from aft.dream import report

    nested = widget.vault / "references/spec/power.md"
    nested.parent.mkdir(parents=True)
    nested.write_text(BODY)
    needs_you = report.regenerate_needs_you(widget.vault).read_text()
    listed = notes.open_notes(widget, None, everywhere=True)
    assert sorted(n.page for n in listed) == [OVERVIEW, "references/spec/power.md"]
    for note in listed:
        assert note.page in needs_you


def test_notes_from_an_excluded_directory_ask_the_shared_pages_only(widget, tmp_path, capsys):
    """A directory capture excludes is no project to `aft notes`, as to every other
    resolver call (DESIGN 7, F49): the phantom slug its name would settle must not name
    the pages a session of that directory once filed."""
    excluded = tmp_path / "scratch" / "gizmo"
    excluded.mkdir(parents=True)
    (widget.home / "config.toml").write_text(
        f'vault = "{widget.vault}"\nhost = "{widget.host}"\n'
        f'[capture]\nexclude_paths = ["{tmp_path / "scratch"}"]\n'
    )
    # The pages a session of the excluded directory once filed under its name stand in
    # the vault, with a note of their own.
    evidence.store(
        widget,
        agent="c",
        session_id="9",
        project_id=None,
        entries=[entry("e1", 2, USER, "the gizmo daemon listens on 8123 and is round")],
    )
    front = {
        "type": "Project",
        "title": "Gizmo",
        "description": "The gizmo project.",
        "sources": [{"id": "s1", "resource": "aft://session/c/9#e1", "quote": "listens on 8123"}],
        "aft": {},
    }
    phantom = widget.vault / "projects/gizmo/overview.md"
    phantom.parent.mkdir(parents=True)
    phantom.write_text(f"---\n{yaml.safe_dump(front, sort_keys=False).strip()}\n---\n\n{BODY}")
    vaultmod.commit(widget.vault, ["projects/gizmo/overview.md"], "seed gizmo page")
    assert main(["notes", str(excluded)]) == 0
    assert capsys.readouterr().out.startswith("aft notes: nothing is waiting")
    # The same pages answer when they are asked for by name.
    assert main(["notes", "--all"]) == 0
    assert "gizmo" in capsys.readouterr().out


def test_keep_closes_the_note_and_leaves_the_line(widget):
    note = the_note(widget)
    before = page_text(widget)
    settled = notes.settle(widget, note.where, notes.KEEP, now=NOW)

    text = page_text(widget)
    assert "[!conflict]" not in text
    assert "- The daemon listens on 8123. [observed] 2026-09-20 [^s1]" in text
    assert text.count("\n") < before.count("\n")
    archive = (widget.vault / "archive" / OVERVIEW).read_text()
    assert "> [!conflict] 2026-10-05" in archive
    assert "> Newer evidence: The daemon listens on 9000. ([^s2])" in archive
    assert notes.open_notes(widget, "widget") == []
    assert settled.message.startswith(f"aft: settle {note.where} (keep): The daemon listens")
    assert run_git(["log", "-1", "--format=%s"], widget.vault) == settled.message
    assert settled.citation is None
    assert run_git(["status", "--porcelain", "projects", "archive", "reports"], widget.vault) == ""
    needs_you = (widget.vault / "reports/needs-you.md").read_text()
    assert "8123" not in needs_you


def test_withdraw_strikes_the_line_today(widget):
    notes.settle(widget, the_note(widget).where, notes.WITHDRAW, now=NOW)
    text = page_text(widget)
    assert "- ~~The daemon listens on 8123." in text
    assert "withdrawn 2026-10-06" in text
    assert "[!conflict]" not in text


def test_replace_rests_on_the_line_s_and_the_note_s_quotes(widget):
    notes.settle(
        widget, the_note(widget).where, notes.REPLACE, "The daemon  listens on 9000.", now=NOW
    )
    day = run_git(["show", "-s", "--format=%cs", "HEAD~1"], widget.vault).strip()
    lines = page_text(widget).splitlines()
    new = lines.index(f"- The daemon listens on 9000. [stated] {day} [^s1] [^s2] [^s3]")
    assert lines[new + 1].startswith("- ~~The daemon listens on 8123.")
    assert lines[new + 1].endswith("superseded 2026-10-05 by [^s2]")


def test_a_replacement_cites_the_settle_as_its_own_source(widget):
    """T43: the settle commit is itself the user's edit, so the replacement cites it as
    its own newest source: a second commit adds ``aft://edit/<sha>`` of the settle's,
    quoted with the statement, and the line takes the commit's day as its date."""
    note = the_note(widget)
    settled = notes.settle(
        widget, note.where, notes.REPLACE, "The daemon listens on 9000.", now=NOW
    )
    first = run_git(["rev-parse", "HEAD~1"], widget.vault).strip()
    subjects = run_git(["log", "--format=%s"], widget.vault).splitlines()
    assert subjects[:2] == [
        f"aft: cite settle {first[:7]} on {note.where}",
        f"aft: settle {note.where} (replace): The daemon listens on 8123.",
    ]
    assert (settled.message, settled.citation) == (subjects[1], subjects[0])
    day = run_git(["show", "-s", "--format=%cs", first], widget.vault).strip()
    text = page_text(widget)
    assert f"- The daemon listens on 9000. [stated] {day} [^s1] [^s2] [^s3]" in text
    assert "superseded 2026-10-05 by [^s2]" in text
    doc = pagewrite.PageDoc(text, widget.vault / OVERVIEW)
    assert doc.sources["s3"]["resource"] == f"aft://edit/{first}"
    assert doc.sources["s3"]["quote"] == "The daemon listens on 9000."
    verified, origin, ev_date, yours = apply._lookup_evidence(
        widget, f"aft://edit/{first}", "The daemon listens on 9000."
    )
    assert (verified, origin, ev_date, yours) == (True, rules.PRIMARY, day, False)
    assert lint.lint_vault(widget.vault) == []


def test_reverting_a_replacement_undoes_both_commits_newest_first(widget):
    """T43: a replacement is two commits, so reverting them newest first restores the page
    and brings the note back."""
    note = the_note(widget)
    before = page_text(widget)
    notes.settle(widget, note.where, notes.REPLACE, "The daemon listens on 9000.", now=NOW)
    newest, first = run_git(["rev-parse", "HEAD", "HEAD~1"], widget.vault).split()
    run_git(["revert", "--no-edit", newest, first], widget.vault)
    assert page_text(widget) == before
    assert notes.count(widget, "widget") == 1


def test_a_failed_citation_leaves_the_settle_commit_standing(widget, monkeypatch):
    """T43: a citation that cannot be made leaves the settle commit as it stands, with the
    replacement line it wrote still valid."""

    def refuse(*args, **kwargs):
        raise RuntimeError("the citation write failed")

    monkeypatch.setattr(notes, "_cite_settle", refuse)
    note = the_note(widget)
    settled = notes.settle(
        widget, note.where, notes.REPLACE, "The daemon listens on 9000.", now=NOW
    )
    assert settled.citation is None
    subjects = run_git(["log", "--format=%s"], widget.vault).splitlines()
    assert subjects[0].startswith(f"aft: settle {note.where} (replace):")
    assert not any(s.startswith("aft: cite settle") for s in subjects)
    assert "- The daemon listens on 9000. [stated] 2026-10-05 [^s1] [^s2]" in page_text(widget)
    assert lint.lint_vault(widget.vault) == []


@pytest.mark.parametrize(
    ("statement", "word"),
    [
        ("The daemon listens on 4500.", "4500"),
        ("Per Rando the daemon listens on 9000.", "Rando"),
    ],
)
def test_replace_with_a_word_the_quotes_do_not_say_is_refused(widget, statement, word):
    """R2 holds for a replacement: a name or number neither the line's quotes nor the
    note's say refuses the settle, and nothing is written and nothing is committed (T41,
    T45)."""
    note = the_note(widget)
    before = page_text(widget)
    head = run_git(["rev-parse", "HEAD"], widget.vault)
    with pytest.raises(notes.NoteError, match=f"{word} that none of the line's or the note's"):
        notes.settle(widget, note.where, notes.REPLACE, statement, now=NOW)
    assert page_text(widget) == before
    assert not (widget.vault / "archive" / OVERVIEW).exists()
    assert run_git(["rev-parse", "HEAD"], widget.vault) == head


def test_replace_is_observed_when_no_quote_is_the_user_s(widget):
    project = Registry.load(widget.registry_path).by_slug("widget")
    evidence.store(
        widget,
        agent="c",
        session_id="3",
        project_id=project.id,
        entries=[entry("e1", 5, ASSISTANT, "the port is 9000 now")],
    )
    write_overview(
        widget,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/3#e1", "quote": "the port is 9000"},
        ],
    )
    notes.settle(widget, the_note(widget).where, notes.REPLACE, "Port 9000.", now=NOW)
    day = run_git(["show", "-s", "--format=%cs", "HEAD~1"], widget.vault).strip()
    assert f"- Port 9000. [observed] {day} [^s1] [^s2] [^s3]" in page_text(widget)


def test_replace_without_a_verified_quote_is_refused_and_changes_nothing(widget):
    project = Registry.load(widget.registry_path).by_slug("widget")
    write_overview(
        widget,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "words never said"},
        ],
    )
    before = page_text(widget)
    head = run_git(["rev-parse", "HEAD"], widget.vault)
    with pytest.raises(notes.NoteError, match="no quote that still verifies"):
        notes.settle(widget, the_note(widget).where, notes.REPLACE, "Port 9000.", now=NOW)
    assert page_text(widget) == before
    assert not (widget.vault / "archive" / OVERVIEW).exists()
    assert run_git(["rev-parse", "HEAD"], widget.vault) == head


def test_a_name_only_the_line_s_quote_says_can_rest_in_a_replacement(widget):
    """T45: a replacement that corrects one detail of the line may keep a name only the
    line's own quote holds, and it cites the line's sources and the note's, line first.
    The struck line links to the note's source, the evidence that replaced it."""
    note = the_note(widget)
    notes.settle(
        widget, note.where, notes.REPLACE, "The daemon listens on 9000, not 8123.", now=NOW
    )
    day = run_git(["show", "-s", "--format=%cs", "HEAD~1"], widget.vault).strip()
    lines = page_text(widget).splitlines()
    new = lines.index(f"- The daemon listens on 9000, not 8123. [stated] {day} [^s1] [^s2] [^s3]")
    assert lines[new + 1].startswith("- ~~The daemon listens on 8123.")
    assert lines[new + 1].endswith("superseded 2026-10-05 by [^s2]")
    assert lint.lint_vault(widget.vault) == []


def test_a_worker_session_s_line_quote_does_not_ground_a_replacement(widget):
    """T45: only a line quote that could carry a long-term line grounds a replacement, so
    a worker session's quote under the line holds none of its names."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    evidence.store(
        widget,
        agent="c",
        session_id="w",
        project_id=project.id,
        entries=[entry("e1", 1, "worker", "it listens on 8123")],
    )
    write_overview(
        widget,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/w#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "listens on 9000"},
        ],
    )
    [source] = the_note(widget).line_sources
    assert (source.verified, source.origin) == (True, "worker")
    before = page_text(widget)
    head = run_git(["rev-parse", "HEAD"], widget.vault)
    with pytest.raises(notes.NoteError, match="8123 that none of the line's or the note's"):
        notes.settle(
            widget,
            the_note(widget).where,
            notes.REPLACE,
            "The daemon listens on 9000, not 8123.",
            now=NOW,
        )
    assert page_text(widget) == before
    assert run_git(["rev-parse", "HEAD"], widget.vault) == head


def test_a_note_without_a_verified_quote_is_refused_whatever_the_line_s_quotes_say(widget):
    """T45: a replacement needs new evidence from the note, so a note none of whose quotes
    verifies is refused even when the line's own quotes hold every name of it."""
    project = Registry.load(widget.registry_path).by_slug("widget")
    write_overview(
        widget,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "words never said"},
        ],
    )
    before = page_text(widget)
    head = run_git(["rev-parse", "HEAD"], widget.vault)
    with pytest.raises(notes.NoteError, match="no quote that still verifies"):
        notes.settle(widget, the_note(widget).where, notes.REPLACE, "Port 8123.", now=NOW)
    assert page_text(widget) == before
    assert not (widget.vault / "archive" / OVERVIEW).exists()
    assert run_git(["rev-parse", "HEAD"], widget.vault) == head


@pytest.mark.parametrize(
    ("where", "choice", "statement", "error"),
    [
        (f"{OVERVIEW}:3", notes.KEEP, None, "no note under line 3"),
        ("projects/widget/missing.md:1", notes.KEEP, None, "no page"),
        (OVERVIEW, notes.KEEP, None, "is not <page>:<line>"),
        ("reports/needs-you.md:3", notes.KEEP, None, "is not a fact page"),
        ("projects/../reports/x.md:3", notes.KEEP, None, "is not a fact page"),
        (f"{OVERVIEW}:1", "drop", None, "unknown choice"),
        (f"{OVERVIEW}:1", notes.REPLACE, " ", "--replace takes the new statement"),
        (f"{OVERVIEW}:1", notes.KEEP, "text", "--replace takes the new statement"),
    ],
)
def test_settle_refuses_what_it_cannot_do(widget, where, choice, statement, error):
    with pytest.raises(notes.NoteError, match=error):
        notes.settle(widget, where, choice, statement, now=NOW)


def test_the_commands_list_and_settle(widget, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("AFT_HOME", str(widget.home))
    assert main(["notes", "--all"]) == 0
    out = capsys.readouterr().out
    where = the_note(widget).where
    assert f"[1] {where} (conflict, 2026-10-05)" in out
    assert "Memory says: The daemon listens on 8123." in out
    assert "listens on 9000" in out

    assert main(["notes", "--all", "--json"]) == 0
    [listed] = json.loads(capsys.readouterr().out)
    assert listed["where"] == where
    assert listed["note_sources"][0]["yours"] is True

    assert main(["settle", where, "--keep"]) == 0
    assert "aft: settle" in capsys.readouterr().out
    assert main(["notes", "--all"]) == 0
    assert "nothing is waiting on the user" in capsys.readouterr().out

    assert main(["settle", where, "--keep"]) == 2
    assert "no note under line" in capsys.readouterr().err


def test_a_refused_statement_puts_the_archive_back(widget):
    """Closing a note writes the archive at once. A statement the writer refuses comes
    after that, and the settle still leaves no trace."""
    before = page_text(widget)
    head = run_git(["rev-parse", "HEAD"], widget.vault)
    with pytest.raises(notes.NoteError, match="cannot settle"):
        notes.settle(widget, the_note(widget).where, notes.REPLACE, "Port ~~9000~~.", now=NOW)
    assert page_text(widget) == before
    assert not (widget.vault / "archive" / OVERVIEW).exists()
    assert run_git(["rev-parse", "HEAD"], widget.vault) == head
    assert run_git(["status", "--porcelain", "projects", "archive"], widget.vault) == ""

    notes.settle(widget, the_note(widget).where, notes.KEEP, now=NOW)
    archive = (widget.vault / "archive" / OVERVIEW).read_text()
    assert archive.count("[!conflict]") == 1


def test_a_refused_statement_keeps_an_archive_that_was_there(widget):
    archive = widget.vault / "archive" / OVERVIEW
    write_overview(
        widget,
        Registry.load(widget.registry_path).by_slug("widget").id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "listens on 9000"},
        ],
        body=BODY + "\n- The lid is blue. [observed] 2026-09-20 [^s1]\n\n"
        "> [!question] 2026-10-05\n> Is it green now? ([^s2])\n",
    )
    first, second = notes.open_notes(widget, "widget")
    notes.settle(widget, second.where, notes.KEEP, now=NOW)
    kept = archive.read_bytes()
    with pytest.raises(notes.NoteError, match="cannot settle"):
        notes.settle(widget, first.where, notes.REPLACE, "Port ~~9000~~.", now=NOW)
    assert archive.read_bytes() == kept


def test_a_worker_session_s_quote_cannot_carry_a_replacement(widget):
    project = Registry.load(widget.registry_path).by_slug("widget")
    evidence.store(
        widget,
        agent="c",
        session_id="w",
        project_id=project.id,
        entries=[entry("e1", 5, "worker", "the port is 9000 now")],
    )
    write_overview(
        widget,
        project.id,
        [
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "listens on 8123"},
            {"id": "s2", "resource": "aft://session/c/w#e1", "quote": "the port is 9000"},
        ],
    )
    [source] = the_note(widget).note_sources
    assert (source.verified, source.origin) == (True, "worker")
    with pytest.raises(notes.NoteError, match="no quote that still verifies"):
        notes.settle(widget, the_note(widget).where, notes.REPLACE, "Port 9000.", now=NOW)
