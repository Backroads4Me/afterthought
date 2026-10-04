"""The turns pipeline (BUILD-PLAN F55): narrow questions over a compacted input.

Each test digests the shared two-exchange transcript through the fake endpoint, which
answers the pipeline's calls in the order it makes them: the summary, what the user
settled per turn, what the session observed, and, when two or more items stand, what a
later item replaced.
"""

# ruff: noqa: F811 - the tests take the shared session fixture by name
from __future__ import annotations

from datetime import UTC

from aft import config as configmod
from aft import digest
from aft.config import Config
from aft.readers import ASSISTANT, TOOL, USER, Entry
from test_digest import (
    ASSISTANT_ANSWER,
    NOW,
    SECOND_PROMPT,
    USER_PROMPT,
    Session,
    claim,
    serve,
    session,  # noqa: F401 - the fixture the tests below take
)


def turns_config(cfg: Config) -> Config:
    """The served config with the turns pipeline chosen, re-read from disk."""
    path = cfg.home / configmod.CONFIG_NAME
    path.write_text(
        path.read_text(encoding="utf-8") + '[digest]\npipeline = "turns"\n', encoding="utf-8"
    )
    return configmod.load()


def summary(text: str = "Settled where capture runs.") -> dict:
    return {
        "summary": text,
        "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
    }


def item(turn: str, label: str, statement: str, quote: str) -> dict:
    return {"turn": turn, "label": label, "statement": statement, "quote": quote}


def run(session: Session, *answers: object) -> digest.Digest:
    fake, cfg = serve(session.cfg, *answers)
    cfg = turns_config(cfg)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    return wrote


def test_a_settled_turn_becomes_a_stated_fact(session: Session):
    wrote = run(
        session,
        summary(),
        {
            "items": [
                item(
                    "e1", "decision", "Capture runs at session end.", "Capture runs at session end."
                )
            ]
        },
        {"items": []},
    )
    assert [(f.statement, f.source, f.entry) for f in wrote.facts] == [
        ("Capture runs at session end.", "stated", "e1")
    ]


def test_an_item_quoting_the_assistant_is_never_stated(session: Session):
    """The turn label must be a user's turn: the shared checks refuse [stated] elsewhere."""
    wrote = run(
        session,
        summary(),
        {"items": [item("e2", "decision", "The hook returns without a model.", ASSISTANT_ANSWER)]},
        {"items": []},
    )
    assert not wrote.facts
    assert any("not one of the user's turns" in d.reason for d in wrote.dropped)


def test_an_observed_item_is_kept_as_observed(session: Session):
    wrote = run(
        session,
        summary(),
        {"items": []},
        {
            "items": [
                {
                    "statement": "The hook writes one row and returns.",
                    "quote": "the hook writes one row",
                    "entry": "e2",
                }
            ]
        },
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("observed", "e2")]


def test_a_later_stated_item_replaces_an_earlier_one(session: Session):
    wrote = run(
        session,
        summary(),
        {
            "items": [
                item(
                    "e1", "decision", "Capture runs at session end.", "Capture runs at session end."
                ),
                item(
                    "e4",
                    "decision",
                    "The retry has its own setting.",
                    "Give the retry its own setting",
                ),
            ]
        },
        {"items": []},
        {"replacements": [{"later": "#2", "earlier": "#1"}]},
    )
    assert [f.statement for f in wrote.facts] == ["The retry has its own setting."]
    assert any(d.reason.startswith("replaced by a later item") for d in wrote.dropped)


def test_an_observed_item_replaces_nothing(session: Session):
    """Only the user's own words change what the user settled (BUILD-PLAN F55, restraint)."""
    wrote = run(
        session,
        summary(),
        {
            "items": [
                item(
                    "e1", "decision", "Capture runs at session end.", "Capture runs at session end."
                )
            ]
        },
        {
            "items": [
                {
                    "statement": "The hook writes one row and returns.",
                    "quote": "the hook writes one row",
                    "entry": "e2",
                }
            ]
        },
        {"replacements": [{"later": "#2", "earlier": "#1"}]},
    )
    assert len(wrote.facts) == 2


def test_turn_blocks_show_before_actions_and_the_users_words():
    def entry(eid: str, role: str, text: str, error: bool = False) -> Entry:
        return Entry(eid, NOW.astimezone(UTC), role, text, error)

    part = [
        entry("e1", USER, USER_PROMPT),
        entry("e2", ASSISTANT, ASSISTANT_ANSWER),
        entry("e3", TOOL, "Edit src/aft/digest.py\nsome output"),
        entry("e4", USER, SECOND_PROMPT),
    ]
    text = digest.turn_blocks(part)
    first, second = text.split("\n\n")
    assert first.startswith("TURN #1\nBEFORE: (start of session)\nACTIONS:\n(none)\nUSER: ")
    assert f"BEFORE: {ASSISTANT_ANSWER}" in second
    assert "ACTIONS:\nEdit src/aft/digest.py\nUSER: " in second
    assert "some output" not in second
