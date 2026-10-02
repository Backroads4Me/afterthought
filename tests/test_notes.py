"""Notes waiting on the user, and settling them (BUILD-PLAN F42)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
import yaml

from aft import evidence, notes
from aft import vault as vaultmod
from aft.cli import main
from aft.config import Config
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


def test_count_reads_the_project_and_global_pages_only(widget):
    assert notes.count(widget, "widget") == 1
    assert notes.count(widget, None) == 0
    global_page = widget.vault / "global/preferences.md"
    global_page.parent.mkdir(parents=True, exist_ok=True)
    global_page.write_text(BODY.replace("[^s", "[^g"))
    assert notes.count(widget, None) == 1
    assert notes.count(widget, "widget") == 2
    assert notes.count(widget, "elsewhere") == 1


def test_keep_closes_the_note_and_leaves_the_line(widget):
    note = the_note(widget)
    before = page_text(widget)
    message = notes.settle(widget, note.where, notes.KEEP, now=NOW)

    text = page_text(widget)
    assert "[!conflict]" not in text
    assert "- The daemon listens on 8123. [observed] 2026-09-20 [^s1]" in text
    assert text.count("\n") < before.count("\n")
    archive = (widget.vault / "archive" / OVERVIEW).read_text()
    assert "> [!conflict] 2026-10-05" in archive
    assert "> Newer evidence: The daemon listens on 9000. ([^s2])" in archive
    assert notes.open_notes(widget, "widget") == []
    assert message.startswith(f"aft: settle {note.where} (keep): The daemon listens")
    assert run_git(["log", "-1", "--format=%s"], widget.vault) == message
    assert run_git(["status", "--porcelain", "projects", "archive", "reports"], widget.vault) == ""
    needs_you = (widget.vault / "reports/needs-you.md").read_text()
    assert "8123" not in needs_you


def test_withdraw_strikes_the_line_today(widget):
    notes.settle(widget, the_note(widget).where, notes.WITHDRAW, now=NOW)
    text = page_text(widget)
    assert "- ~~The daemon listens on 8123." in text
    assert "withdrawn 2026-10-06" in text
    assert "[!conflict]" not in text


def test_replace_rests_on_the_note_s_own_quotes(widget):
    notes.settle(
        widget, the_note(widget).where, notes.REPLACE, "The daemon  listens on 9000.", now=NOW
    )
    lines = page_text(widget).splitlines()
    new = lines.index("- The daemon listens on 9000. [stated] 2026-10-05 [^s2]")
    assert lines[new + 1].startswith("- ~~The daemon listens on 8123.")
    assert lines[new + 1].endswith("superseded 2026-10-05 by [^s2]")


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
    assert "- Port 9000. [observed] 2026-10-05 [^s2]" in page_text(widget)


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


@pytest.mark.parametrize(
    ("where", "choice", "statement", "error"),
    [
        (f"{OVERVIEW}:3", notes.KEEP, None, "no note under line 3"),
        ("projects/widget/missing.md:1", notes.KEEP, None, "no page"),
        (OVERVIEW, notes.KEEP, None, "is not <page>:<line>"),
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
