"""Sessions of no project (BUILD-PLAN F35; DESIGN 7).

A session whose directory belongs to no project is digested and filed as unfiled. Dreaming
takes only its global facts into memory: its project facts have no project page, stay in
the digest, and are reported.
"""

from __future__ import annotations

from aft import digest, evidence, lint
from aft.dream import apply, schedule
from aft.readers import USER, Entry
from aft.registry import Registry
from fakes.model_server import Reply
from test_apply import NOW, TODAY, config_with_fake, fake, reply, write_digest

__all__ = ["fake"]  # the fixture, imported so pytest finds it here


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
    fake.script = [
        Reply(
            content={
                "summary": "Talked about punctuation.",
                "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
                "facts": [],
            }
        )
    ]
    wrote = digest.redigest(cfg, "loose-1", now=NOW)
    assert wrote.path.startswith(f"digests/{digest.UNFILED}/")


def test_a_session_in_a_directory_of_no_project_counts_as_unfiled_activity(
    initialized, home, fake, tmp_path, monkeypatch
):
    """The quiet period holds for unfiled sessions too, so a dream never takes up a session
    that is still talking."""
    from aft import queue

    cfg = config_with_fake(home, initialized.vault, fake)
    nowhere = (tmp_path / "loose").resolve()
    nowhere.mkdir()
    monkeypatch.setattr(queue, "last_events", lambda config: {nowhere: NOW})
    assert schedule.last_activity(cfg) == {digest.UNFILED: NOW}
