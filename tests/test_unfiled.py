"""Sessions of no project (BUILD-PLAN F35; DESIGN 7).

A session whose directory belongs to no project is digested and filed as unfiled. Dreaming
takes only its global facts into memory: its project facts have no project page, stay in
the digest, and are reported.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from aft import digest, evidence, lint, page
from aft.dream import apply, schedule
from aft.readers import USER, Entry
from aft.registry import Registry
from fakes.model_server import Reply
from test_apply import NOW, TODAY, config_with_fake, fake, reply, write_digest

__all__ = ["fake"]  # the fixture, imported so pytest finds it here

EMPTY_ANSWER = {
    "summary": "Talked about punctuation.",
    "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
    "facts": [],
}


def unfiled_digest(cfg) -> None:
    write_digest(
        cfg,
        digest.UNFILED,
        "2026-10-05-c-u1",
        facts=[
            {
                "statement": "The user wants plain hyphens, never em dashes.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
                "term": "long",
            },
            {
                "statement": "The inference stack runs vLLM on port 8080.",
                "kind": "state",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
                "scope": "project",
                "term": "long",
            },
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/u1#e1",
                "quote": "The user wants plain hyphens, never em dashes.",
            },
            {
                "id": "s2",
                "resource": "aft://session/c/u1#e2",
                "quote": "The inference stack runs vLLM on port 8080.",
            },
        ],
    )


def test_an_unfiled_digest_gives_memory_its_global_facts_only(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    unfiled_digest(cfg)
    fake.script = [reply("add")]
    rep = schedule.run(cfg, now=NOW).reports[digest.UNFILED]
    assert len(fake.requests) == 1, "a project fact of no project was put to the model"
    prefs = (cfg.vault / "global" / "preferences.md").read_text()
    assert "The user wants plain hyphens, never em dashes. [stated]" in prefs
    assert [(s.statement, s.reason) for s in rep.skips] == [
        ("The inference stack runs vLLM on port 8080.", apply.UNFILED_SKIP)
    ]
    assert Registry.load(cfg.registry_path).projects == []
    assert not (cfg.vault / "projects" / digest.UNFILED).exists()
    assert lint.lint_vault(cfg.vault) == []


def test_redigest_files_a_session_of_no_project_as_unfiled(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    entries = [
        Entry(id="e1", time=NOW, role=USER, text="Keep plain hyphens.", is_error=False),
        Entry(id="e2", time=NOW, role=USER, text="Thanks, that works.", is_error=False),
    ]
    evidence.store(cfg, agent="claude", session_id="loose-1", project_id=None, entries=entries)
    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "loose-1", now=NOW)
    assert wrote.path.startswith(f"digests/{digest.UNFILED}/")
    frontmatter = page.split_frontmatter((cfg.vault / wrote.path).read_text())[0]
    assert frontmatter["title"].startswith("Unfiled session ")
    assert "project" not in frontmatter["aft"]


def test_redigest_files_an_unregistered_repository_under_its_own_name(
    initialized, home, fake, tmp_path
):
    """A session in a repository the registry has not met has no project id in the
    archive, and its directory still names it."""
    from aft import queue
    from aft.events import SESSION_END, SessionEvent
    from conftest import make_repo

    cfg = config_with_fake(home, initialized.vault, fake)
    repo = make_repo(tmp_path / "gizmo")
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="repo-1",
            cwd=repo,
            transcript_path=tmp_path / "t.jsonl",
            time=NOW,
            detail=None,
        ),
    )
    entries = [
        Entry(id="e1", time=NOW, role=USER, text="Keep plain hyphens.", is_error=False),
        Entry(id="e2", time=NOW, role=USER, text="Thanks, that works.", is_error=False),
    ]
    evidence.store(cfg, agent="claude", session_id="repo-1", project_id=None, entries=entries)
    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "repo-1", now=NOW)
    assert wrote.path.startswith("digests/gizmo/")


def test_an_unfiled_dream_waits_while_an_unfiled_session_is_active(
    initialized, home, fake, monkeypatch
):
    """The quiet period holds for unfiled sessions too, so a dream never takes up a session
    that is still talking."""
    from aft import queue

    cfg = config_with_fake(home, initialized.vault, fake)
    cfg = config_with_fake(home, initialized.vault, fake)
    # The session of an unfiled digest ran in a directory that resolves to none, which is
    # one under the system temporary directory (DESIGN 7, F49).
    nowhere = (Path(tempfile.gettempdir()) / "loose").resolve()
    nowhere.mkdir(exist_ok=True)
    unfiled_digest(cfg)
    monkeypatch.setattr(queue, "last_events", lambda config: {nowhere: NOW})
    run = schedule.run(cfg, now=NOW)
    assert digest.UNFILED in run.active
    assert digest.UNFILED not in run.ran
    assert fake.requests == []
