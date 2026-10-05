"""The turns pipeline (BUILD-PLAN F55): narrow questions over a compacted input.

Each test digests the shared two-exchange transcript through the fake endpoint, which
answers the pipeline's calls in the order it makes them: the summary, what the user
settled per turn, what the session observed, and, when two or more items stand, what a
later item replaced.
"""

# ruff: noqa: F811 - the tests take the shared session fixture by name
from __future__ import annotations

from datetime import UTC, date, datetime

from aft import config as configmod
from aft import digest
from aft.config import Config
from aft.readers import ASSISTANT, TOOL, USER, Entry
from test_digest import (
    ASSISTANT_ANSWER,
    NOW,
    SECOND_ANSWER,
    SECOND_PROMPT,
    USER_PROMPT,
    Session,
    claim,
    record,
    serve,
    session,  # noqa: F401 - the fixture the tests below take
    write_records,
)


def turns_config(cfg: Config) -> Config:
    """The served config with the turns pipeline chosen, re-read from disk."""
    path = cfg.home / configmod.CONFIG_NAME
    path.write_text(
        path.read_text(encoding="utf-8").replace('pipeline = "base"', 'pipeline = "turns"'),
        encoding="utf-8",
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
    assert first.startswith(
        f"[#1] TURN {NOW:%Y-%m-%d %A}\nBEFORE: {digest.NO_REPLY}\nERRORS:\n(none)\nUSER: "
    )
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
    assert any("neither the user's turn" in d.reason for d in wrote.dropped)


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
        "ProjectionLab"
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
                "The hook writes one row to SQLite and returns.",
                "Give the retry its own setting",
                proposal="the hook writes one row and returns",
            )
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any(d.reason.endswith("holds: SQLite") for d in wrote.dropped)


def test_a_statement_naming_the_agents_words_without_a_proposal_is_refused(session: Session):
    """Words the session holds elsewhere do not ground [stated]: only the user's turn does,
    or, for an approval, the proposal it cites (DESIGN R2)."""
    wrote = run(
        session,
        summary(),
        settled(
            item("e1", "decision", "Capture runs in PostgreSQL.", "Capture runs at session end.")
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any(d.reason.endswith("holds: PostgreSQL") for d in wrote.dropped)


def test_a_quoted_question_is_never_stated(session: Session):
    """BUILD-PLAN F52: a quote holding a question mark is never [stated]."""
    wrote = run(
        session,
        summary(),
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end?")
        ),
        {"items": []},
    )
    assert not wrote.facts
    assert any("the quote is a question" in d.reason for d in wrote.dropped)


def test_each_settled_label_maps_to_its_kind(session: Session):
    wrote = run(
        session,
        summary(),
        settled(
            item("e1", "rule", "Capture runs at session end.", "Capture runs at session end."),
            item(
                "e4",
                "preference",
                "The retry has its own setting.",
                "Give the retry its own setting",
                scope="global",
            ),
        ),
        {"items": []},
    )
    assert sorted((f.entry, f.kind, f.scope) for f in wrote.facts) == [
        ("e1", "decision", "project"),
        ("e4", "preference", "global"),
    ]


def test_an_observed_placeholder_quote_is_dropped(session: Session):
    wrote = run(session, summary(), settled(), {"items": [seen("The hook returns.", "...", "e2")]})
    assert not wrote.facts


def test_turn_batches_hold_the_count_and_the_token_budget():
    blocks = [f"[#{n}] TURN\nUSER: {'x' * 30}" for n in range(1, 21)]
    assert [len(b) for b in digest.turn_batches(blocks, 10_000)] == [digest.TURN_BATCH, 5]
    small = digest.turn_batches(blocks, digest.tokens("\n\n".join(blocks[:3])))
    assert [len(b) for b in small][:2] == [3, 3]
    assert [len(b) for b in digest.turn_batches(["x" * 100], 1)] == [1]


def test_best_sentence_cites_nothing_when_two_sentences_match_equally():
    text = "Option A: keep the hook small. Option B: keep the hook fast."
    assert digest.best_sentence(text, "keep the hook") == ""


def test_a_name_from_the_answered_message_cites_its_sentence():
    """A reply's statement may take a name from the message it answers only with that
    message's sentence cited beside it (DESIGN R2)."""
    before = "The sounds come from the theme. Switch voxtype to the Default theme? Or keep it."
    statement = "The sounds use the Default theme of Voxtype."
    assert digest.ungrounded(statement, "change to default") == ["Voxtype"]
    assert (
        digest.grounding_sentence(before, statement, "change to default")
        == "Switch voxtype to the Default theme?"
    )
    assert digest.grounding_sentence(before, "Use the Classic theme.", "ok") == ""


def test_a_quote_inside_a_paste_or_command_block_is_not_the_users_words():
    """DESIGN 6, R2: a paste and a command block are never [stated]; a block with no closing
    tag runs to the end of the turn."""
    text = (
        "Fix it now.\n"
        '<pasted_content id="d88a">\nYes, fix finding 3 on a new branch.\n'
        '</pasted_content id="d88a">\nand use port 8008'
    )
    assert not digest.own_words(text, "fix finding 3")
    assert digest.own_words(text, "use   port 8008")
    assert digest.own_words(text, "Fix it now.")
    assert not digest.own_words("<bash-input>systemctl stop x</bash-input>", "systemctl stop x")
    assert not digest.own_words('<pasted_content id="a">\nno close here', "no close")
    assert not digest.own_words(
        "<command-name>/goal</command-name>\n<command-args>never push to main", "never push"
    )


def test_turn_blocks_show_date_and_weekday():
    """BUILD-PLAN F59: each turn block header shows the turn's date and weekday."""
    when = datetime(2026, 10, 5, 14, 30, tzinfo=UTC)
    part = [Entry("e1", when, USER, "pause it until tomorrow", False)]
    blocks, _, _, _ = digest.turn_blocks(part)
    assert len(blocks) == 1
    assert blocks[0].startswith("[#1] TURN 2026-10-05 Monday\n")


def test_relative_dates_compute_for_each_relative_form():
    """BUILD-PLAN F59: relative dates computed from relative expressions and the turn's time.

    Covers: today, tomorrow, yesterday, weekday name with or without 'this' or 'next',
    'in N days' and 'in N weeks'.
    """
    # 2026-10-05 is a Monday
    when = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)

    # today
    assert date(2026, 10, 5) in digest.relative_dates("do it today", when)

    # tomorrow
    assert date(2026, 10, 6) in digest.relative_dates("pause it until tomorrow", when)

    # yesterday
    assert date(2026, 10, 4) in digest.relative_dates("it failed yesterday", when)

    # bare weekday
    assert date(2026, 10, 7) in digest.relative_dates("let's meet Wednesday", when)
    assert date(2026, 10, 9) in digest.relative_dates("let's meet Friday", when)

    # this <weekday>
    assert date(2026, 10, 9) in digest.relative_dates("pause until this Friday", when)

    # next <weekday>
    next_fri = digest.relative_dates("pause until next Friday", when)
    assert date(2026, 10, 9) in next_fri or date(2026, 10, 16) in next_fri

    # in N days
    assert date(2026, 10, 8) in digest.relative_dates("pause it in 3 days", when)
    assert date(2026, 10, 8) in digest.relative_dates("pause it in three days", when)
    assert date(2026, 10, 6) in digest.relative_dates("pause it in 1 day", when)
    assert date(2026, 10, 6) in digest.relative_dates("pause it in a day", when)
    assert date(2026, 10, 7) in digest.relative_dates("pause it in a couple of days", when)

    # in N weeks
    assert date(2026, 10, 19) in digest.relative_dates("pause it in two weeks", when)
    assert date(2026, 10, 19) in digest.relative_dates("pause it in 2 weeks", when)
    assert date(2026, 10, 12) in digest.relative_dates("pause it in a week", when)
    assert date(2026, 10, 12) in digest.relative_dates("pause it in one week", when)


def test_ungrounded_accepts_iso_and_october_style_computed_dates():
    """BUILD-PLAN F59, R2: statement date is grounded when computed from turn's relative
    expression, accepting ISO (2026-10-06) and 'October 6' style."""
    when = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    said = "pause it until tomorrow"

    # ISO format (2026-10-06)
    assert digest.ungrounded("The backup is paused until 2026-10-06.", said, when) == []

    # October 6 style
    assert digest.ungrounded("The backup is paused until October 6.", said, when) == []
    assert digest.ungrounded("The backup is paused until October 6, 2026.", said, when) == []
    assert digest.ungrounded("The backup is paused until October 6th.", said, when) == []


def test_ungrounded_refuses_date_not_implied_or_unsupported_format():
    """BUILD-PLAN F59: a date the turn does not imply is refused; formats other than ISO
    and 'October 6' style stay ungrounded."""
    when = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    said = "pause it until tomorrow"

    # Date not implied (tomorrow is 2026-10-06, not 2026-10-07)
    assert digest.ungrounded("The backup is paused until 2026-10-07.", said, when) == ["2026-10-07"]
    assert digest.ungrounded("The backup is paused until October 7.", said, when) == [
        "October",
        "7",
    ]

    # Unsupported date format (stays ungrounded)
    assert digest.ungrounded("The backup is paused until Tuesday.", said, when) == ["Tuesday"]
    assert digest.ungrounded("The backup is paused until 10/06/2026.", said, when) == ["10/06/2026"]

    # Turn without relative date cannot ground a date
    assert digest.ungrounded(
        "The backup is paused until 2026-10-06.", "pause the backup", when
    ) == ["2026-10-06"]

    # Without when, relative date is not computed
    assert digest.ungrounded("The backup is paused until 2026-10-06.", said) == ["2026-10-06"]


def test_pause_it_until_tomorrow_on_monday_turn_becomes_tuesday_date_and_is_kept(session: Session):
    """BUILD-PLAN F59: 'pause it until tomorrow' on a Monday turn becomes Tuesday's date
    and is kept."""
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", "pause it until tomorrow", when=monday),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real", when=monday),
        record("e3", "user", SECOND_PROMPT, when=monday),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real", when=monday),
    )
    stmt = "The backup is paused until 2026-10-06."
    wrote = run(
        session,
        summary(),
        settled(item("e1", "decision", stmt, "pause it until tomorrow")),
        {"items": []},
    )
    assert [f.statement for f in wrote.facts] == [stmt]


def test_pause_it_until_tomorrow_with_october_style_date_is_kept(session: Session):
    """BUILD-PLAN F59: 'pause it until tomorrow' on a Monday turn is kept with October 6 style."""
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", "pause it until tomorrow", when=monday),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real", when=monday),
        record("e3", "user", SECOND_PROMPT, when=monday),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real", when=monday),
    )
    stmt = "The backup is paused until October 6."
    wrote = run(
        session,
        summary(),
        settled(item("e1", "decision", stmt, "pause it until tomorrow")),
        {"items": []},
    )
    assert [f.statement for f in wrote.facts] == [stmt]


def test_date_not_implied_by_turn_is_refused(session: Session):
    """BUILD-PLAN F59: a date the turn does not imply is refused."""
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", "pause it until tomorrow", when=monday),
        record("e2", "assistant", ASSISTANT_ANSWER, model="claude-real", when=monday),
        record("e3", "user", SECOND_PROMPT, when=monday),
        record("e4", "assistant", SECOND_ANSWER, model="claude-real", when=monday),
    )
    unimplied = "The backup is paused until 2026-10-07."
    wrote = run(
        session,
        summary(),
        settled(item("e1", "decision", unimplied, "pause it until tomorrow")),
        {"items": []},
    )
    assert not wrote.facts
    assert any("2026-10-07" in d.reason for d in wrote.dropped)
