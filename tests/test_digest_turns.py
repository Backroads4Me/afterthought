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


def item(
    turn: str, label: str, statement: str, quote: str, scope: str = "project", proposal: str = ""
) -> dict:
    return {
        "turn": turn,
        "label": label,
        "statement": statement,
        "quote": quote,
        "scope": scope,
        "term": "long",
        "proposal": proposal,
    }


def settled(*items: dict) -> dict:
    """A turns answer: the items given, and none for each user turn they leave out, since
    every turn gets an item (a skipped turn is asked again)."""
    given = {i["turn"] for i in items}
    rest = [item(turn, "none", "", "") for turn in ("e1", "e4") if turn not in given]
    return {"items": [*items, *rest]}


def seen(statement: str, quote: str, entry: str) -> dict:
    return {"statement": statement, "quote": quote, "entry": entry, "term": "long"}


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
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end.")
        ),
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
        settled(item("e2", "decision", "The hook returns without a model.", ASSISTANT_ANSWER)),
        {"items": []},
    )
    assert not wrote.facts
    assert any("not one of the user's turns" in d.reason for d in wrote.dropped)


def test_an_observed_item_is_kept_as_observed(session: Session):
    wrote = run(
        session,
        summary(),
        settled(),
        {"items": [seen("The hook writes one row and returns.", "the hook writes one row", "e2")]},
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("observed", "e2")]


def test_a_later_stated_item_replaces_an_earlier_one(session: Session):
    wrote = run(
        session,
        summary(),
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end."),
            item(
                "e4",
                "decision",
                "The retry has its own setting.",
                "Give the retry its own setting",
            ),
        ),
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
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end.")
        ),
        {"items": [seen("The hook writes one row and returns.", "the hook writes one row", "e2")]},
        {"replacements": [{"later": "#2", "earlier": "#1"}]},
    )
    assert len(wrote.facts) == 2


def test_turn_blocks_show_before_errors_and_the_users_words():
    def entry(eid: str, role: str, text: str, error: bool = False) -> Entry:
        return Entry(eid, NOW.astimezone(UTC), role, text, error)

    part = [
        entry("e1", USER, USER_PROMPT),
        entry("e2", ASSISTANT, ASSISTANT_ANSWER),
        entry("e3", TOOL, "running tests\nok", False),
        entry("e4", TOOL, "FAILED tests/test_x.py", True),
        entry("e5", USER, SECOND_PROMPT),
        entry("e6", USER, "and keep it small"),
    ]
    blocks, after, _, answered = digest.turn_blocks(part)
    first, second, third = blocks
    assert first.startswith(f"[#1] TURN\nBEFORE: {digest.NO_REPLY}\nERRORS:\n(none)\nUSER: ")
    assert f"BEFORE: {ASSISTANT_ANSWER}" in second
    assert "ERRORS:\nFAILED tests/test_x.py\nUSER: " in second
    assert "running tests" not in second
    # A turn sent straight after another answers no agent message.
    assert f"BEFORE: {digest.NO_REPLY}" in third
    assert after == ""
    assert answered == {"e5": "e2"}


def test_a_part_opening_with_a_turn_carries_the_answer_before_it():
    def entry(eid: str, role: str, text: str) -> Entry:
        return Entry(eid, NOW.astimezone(UTC), role, text, False)

    _, after, after_id, _ = digest.turn_blocks(
        [entry("e1", USER, "go"), entry("e2", ASSISTANT, "Option B?")]
    )
    blocks, _, _, answered = digest.turn_blocks([entry("e3", USER, "yes")], after, after_id)
    assert "BEFORE: Option B?" in blocks[0]
    assert answered == {"e3": "e2"}


def test_a_statement_naming_words_the_user_did_not_say_is_refused(session: Session):
    """A yes to a proposal gives the user's word 'yes' and the assistant's content: the
    statement is not the user's own, so it is refused rather than written [stated]."""
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e1",
                "decision",
                "Capture uses PostgreSQL replication.",
                "Capture runs at session end.",
            )
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any("never used" in d.reason for d in wrote.dropped)


def test_later_means_later_in_the_session_not_in_the_list(session: Session):
    """Items listed out of session order are put back in it before anything is replaced."""
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e4",
                "decision",
                "The retry has its own setting.",
                "Give the retry its own setting",
            ),
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end."),
        ),
        {"items": []},
        # In session order e1 is #1 and e4 is #2: e4 replacing e1 is the only order allowed.
        {"replacements": [{"later": "#2", "earlier": "#1"}]},
    )
    assert [f.statement for f in wrote.facts] == ["The retry has its own setting."]


def test_grounding_reads_the_whole_turn_and_ignores_punctuation():
    said = "Should we switch the nightly backup from 2am to 4am? The 2am slot collides."
    assert digest.ungrounded("The 2am nightly backup slot collides.", said) == []
    assert digest.ungrounded("Folders go into one dir.", "move the folders into one dir") == []
    assert digest.ungrounded("Do the proposed ProjectionLab improvements.", "yes do 1 and 5") == [
        "proposed",
        "ProjectionLab",
        "improvements",
    ]


def test_an_approval_cites_the_users_words_and_the_proposal(session: Session):
    """A yes to a proposal is the user's decision, cited with both its sources (DESIGN R2)."""
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e4",
                "decision",
                "The hook writes one row and returns.",
                "Give the retry its own setting",
                proposal="the hook writes one row and returns",
            )
        ),
        {"items": []},
    )
    assert [(f.source, f.entry, f.proposal) for f in wrote.facts] == [
        ("stated", "e4", ("e2", "the hook writes one row and returns"))
    ]
    page = (session.cfg.vault / wrote.path).read_text(encoding="utf-8")
    assert "(the proposal approved)" in page
    assert "#e2" in page and "#e4" in page


def test_an_approval_of_words_the_agent_never_wrote_is_refused(session: Session):
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e4",
                "decision",
                "Capture moves to PostgreSQL.",
                "Give the retry its own setting",
                proposal="move capture to PostgreSQL",
            )
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any("not in the message the turn answers" in d.reason for d in wrote.dropped)


def test_an_approval_with_a_loose_copy_cites_the_sentence_it_approves(session: Session):
    """The model's proposal need not be verbatim: the answered message's best matching
    sentence is cited instead, word for word (BUILD-PLAN F55)."""
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e4",
                "decision",
                "The hook writes one row and returns without calling a model.",
                "Give the retry its own setting",
                proposal="hook should write a single row and return without calling a model",
            )
        ),
        {"items": []},
    )
    assert [f.proposal for f in wrote.facts] == [
        ("e2", "the hook writes one row and returns without calling a model.")
    ]


def test_best_sentence_needs_two_shared_words():
    text = "Two options. Option A: run firstmate inside Paseo. Option B: keep them apart."
    assert (
        digest.best_sentence(text, "firstmate runs inside Paseo") == "run firstmate inside Paseo."
    )
    assert digest.best_sentence(text, "something unrelated entirely") == ""


def test_a_turn_the_answer_skipped_is_asked_again(session: Session):
    """Every turn gets an item; a skipped one is asked again on its own (BUILD-PLAN F55)."""
    wrote = run(
        session,
        summary(),
        {"items": [item("e1", "none", "", "")]},
        {
            "items": [
                item(
                    "e4",
                    "decision",
                    "The retry has its own setting.",
                    "Give the retry its own setting",
                )
            ]
        },
        {"items": []},
    )
    assert [(f.statement, f.entry) for f in wrote.facts] == [
        ("The retry has its own setting.", "e4")
    ]


def test_a_refused_summary_still_keeps_what_the_user_settled(session: Session):
    """The turns and observed questions do not read the summary (BUILD-PLAN F55)."""
    wrote = run(
        session,
        summary("..."),
        summary("..."),
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end.")
        ),
        {"items": []},
    )
    assert [f.statement for f in wrote.facts] == ["Capture runs at session end."]
    assert any(d.reason.startswith("summary refused") for d in wrote.dropped)


def test_an_approval_names_only_words_of_the_turn_or_the_proposal(session: Session):
    """DESIGN R2: words found elsewhere in the session do not ground an approval."""
    wrote = run(
        session,
        summary(),
        settled(
            item(
                "e4",
                "decision",
                "The hook writes one row and returns, and capture runs at session end.",
                "Give the retry its own setting",
                proposal="the hook writes one row and returns",
            )
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any("never used: capture" in d.reason for d in wrote.dropped)
