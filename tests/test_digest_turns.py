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
from aft import digest, queue, work
from aft.config import Config, Settings
from aft.model import ATTEMPTS
from aft.readers import ASSISTANT, TOOL, USER, Entry
from fakes.model_server import FakeEndpoint, Reply
from test_digest import (
    ASSISTANT_ANSWER,
    NOW,
    SECOND_ANSWER,
    SECOND_PROMPT,
    SESSION,
    USER_PROMPT,
    Session,
    claim,
    event,
    pages,
    record,
    rows,
    serve,
    session,  # noqa: F401 - the fixture the tests below take
    tool_record,
    watermark,
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
        {"items": [seen("The file was written.", "written", "e3#t1")]},
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("observed", "e3#t1")]


def test_an_observed_item_the_successful_output_backs_stays_observed(session: Session):
    """F78: the turns pipeline's observed check reads the part's raw entries too, so a
    report of what a successful command printed stays [observed] though the filter kept
    none of that output."""
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        tool_record("e2", "t1", "release 2.4.1 built against vllm 0.6.3", is_error=False),
        record(
            "e3",
            "assistant",
            "The release is 2.4.1, built against vllm 0.6.3.",
            model="claude-real",
        ),
        record("e4", "user", SECOND_PROMPT),
        record("e5", "assistant", SECOND_ANSWER, model="claude-real"),
    )
    wrote = run(
        session,
        summary(),
        settled(),
        {
            "items": [
                seen(
                    "The release is 2.4.1, built against vllm 0.6.3.",
                    "The release is 2.4.1",
                    "e3",
                )
            ]
        },
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("observed", "e3")]


def test_an_assistant_claim_without_tool_backing_becomes_inferred(session: Session):
    wrote = run(
        session,
        summary(),
        settled(),
        {"items": [seen("The hook writes one row and returns.", "the hook writes one row", "e2")]},
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("inferred", "e2")]


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


def test_the_turns_ask_after_an_answer_with_no_quotes_is_guided(session: Session):
    """The retry that follows a turns answer whose every quote was missing carries the
    guidance to quote the sentence that holds the claim (finding T17)."""
    fake, cfg = serve(
        session.cfg,
        summary(),
        settled(
            item("e1", "decision", "Capture runs at session end.", "the moon is made of cheese"),
            item(
                "e4",
                "decision",
                "A slow endpoint is not retried forever.",
                "the endpoint never came back",
            ),
        ),
        settled(
            item("e1", "decision", "Capture runs at session end.", "Capture runs at session end.")
        ),
        {"items": []},
    )
    cfg = turns_config(cfg)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert digest.QUOTE_RETRY_GUIDANCE not in fake.requests[1]["messages"][1]["content"]
    assert digest.QUOTE_RETRY_GUIDANCE in fake.requests[2]["messages"][1]["content"]
    assert [f.statement for f in wrote.facts] == ["Capture runs at session end."]


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
    expression, accepting ISO (2026-10-06), 'October 6', and '6 October' styles."""
    when = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    said = "pause it until tomorrow"

    # ISO format (2026-10-06)
    assert digest.ungrounded("The backup is paused until 2026-10-06.", said, when) == []

    # October 6 style
    assert digest.ungrounded("The backup is paused until October 6.", said, when) == []
    assert digest.ungrounded("The backup is paused until October 6, 2026.", said, when) == []
    assert digest.ungrounded("The backup is paused until October 6th.", said, when) == []

    # Lowercase month names
    assert digest.ungrounded("The backup is paused until october 6.", said, when) == []
    assert digest.ungrounded("The backup is paused until october 6th.", said, when) == []
    assert digest.ungrounded("The backup is paused until october 6, 2026.", said, when) == []

    # Day-first format (6 October)
    assert digest.ungrounded("The backup is paused until 6 October.", said, when) == []
    assert digest.ungrounded("The backup is paused until 6th October.", said, when) == []
    assert digest.ungrounded("The backup is paused until 6th of October.", said, when) == []
    assert digest.ungrounded("The backup is paused until 6 October 2026.", said, when) == []
    assert digest.ungrounded("The backup is paused until 6th of October, 2026.", said, when) == []
    assert digest.ungrounded("The backup is paused until 6 october.", said, when) == []


def test_ungrounded_refuses_date_not_implied_or_unsupported_format():
    """BUILD-PLAN F59: a date the turn does not imply is refused; formats other than ISO
    and month/day styles stay ungrounded."""
    when = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    said = "pause it until tomorrow"

    # Date not implied (tomorrow is 2026-10-06, not 2026-10-07)
    assert digest.ungrounded("The backup is paused until 2026-10-07.", said, when) == ["2026-10-07"]
    assert digest.ungrounded("The backup is paused until October 7.", said, when) == [
        "October",
        "7",
    ]
    assert digest.ungrounded("The backup is paused until 7 October.", said, when) == [
        "7",
        "October",
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


# The item check (BUILD-PLAN F56): after the final state, every [stated] item left is asked
# back once, whether the user asserted it and whether it still stands. The check is off
# unless the config turns it on, and its answers follow the job's other calls in order.


def check(reason: str = "The user's own words, and nothing later took it back.") -> dict:
    """A verifier answer that keeps the item: yes to both questions."""
    return {"asserted": True, "in_effect": True, "reason": reason}


def verified_config(cfg: Config, model: str = "") -> Config:
    """The turns config with the item check turned on, re-read from disk.

    The setting is a config.toml line like every other, appended the way turns_config
    chooses the pipeline. An empty model name leaves the check on the [model] endpoint.
    """
    path = cfg.home / configmod.CONFIG_NAME
    added = 'pipeline = "turns"\nverify_items = true\n'
    if model:
        added += f'verifier_model = "{model}"\n'
    path.write_text(
        path.read_text(encoding="utf-8").replace('pipeline = "turns"\n', added),
        encoding="utf-8",
    )
    return configmod.load()


def run_checked(
    session: Session, *answers: object, model: str = ""
) -> tuple[digest.Digest, FakeEndpoint]:
    """The digest of the shared session with the item check on, and the fake that served it."""
    fake, cfg = serve(session.cfg, *answers)
    cfg = verified_config(turns_config(cfg), model)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    return wrote, fake


SETTLED = item("e1", "decision", "Capture runs at session end.", "Capture runs at session end.")


def test_an_item_the_verifier_confirms_is_kept(session: Session):
    wrote, fake = run_checked(session, summary(), settled(SETTLED), {"items": []}, check())
    assert [(f.statement, f.source) for f in wrote.facts] == [
        ("Capture runs at session end.", "stated")
    ]
    assert wrote.dropped == ()
    # The check is the job's last call: the summary, the turns, the observed, then one
    # short call over the entries around the item's own.
    assert len(fake.requests) == 4
    sent = fake.requests[-1]["messages"][1]["content"]
    assert "[decision] Capture runs at session end." in sent
    assert "[#1] USER ITEM" in sent and "Capture runs at session end." in sent
    assert "[#2] ASSISTANT" in sent and ASSISTANT_ANSWER in sent
    # Two entries after the item's own: the second user turn is in, the last one is not.
    assert "[#4] USER" in sent
    assert "[#5]" not in sent, "the window holds the item's entry and two on each side"


def test_an_item_the_verifier_finds_not_asserted_is_dropped(session: Session):
    wrote, _ = run_checked(
        session,
        summary(),
        settled(SETTLED),
        {"items": []},
        {"asserted": False, "in_effect": True, "reason": "The words were a question."},
    )
    assert not wrote.facts
    assert [(d.statement, d.reason) for d in wrote.dropped] == [
        ("Capture runs at session end.", "the verifier found it not asserted")
    ]


def test_an_item_the_verifier_finds_no_longer_in_effect_is_dropped(session: Session):
    wrote, _ = run_checked(
        session,
        summary(),
        settled(SETTLED),
        {"items": []},
        {"asserted": True, "in_effect": False, "reason": "A later turn reversed it."},
    )
    assert not wrote.facts
    assert [(d.statement, d.reason) for d in wrote.dropped] == [
        ("Capture runs at session end.", "the verifier found it no longer in effect")
    ]


def test_the_check_makes_no_call_when_the_setting_is_off(session: Session):
    fake, cfg = serve(session.cfg, summary(), settled(SETTLED), {"items": []}, check())
    cfg = turns_config(cfg)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert [(f.statement, f.source) for f in wrote.facts] == [
        ("Capture runs at session end.", "stated")
    ]
    assert len(fake.requests) == 3, "the check asked nothing: the setting is off"


def test_the_verifier_model_reaches_the_check_call(session: Session):
    wrote, fake = run_checked(
        session, summary(), settled(SETTLED), {"items": []}, check(), model="check-model"
    )
    assert [(f.statement, f.source) for f in wrote.facts] == [
        ("Capture runs at session end.", "stated")
    ]
    assert [r["model"] for r in fake.requests[:-1]] == ["fake-model", "fake-model", "fake-model"]
    assert fake.requests[-1]["model"] == "check-model"
    asked = fake.requests[-1]["messages"][0]["content"]
    assert "did the user assert this" in asked and "still in effect" in asked


def test_the_check_never_asks_about_an_observed_item(session: Session):
    wrote, fake = run_checked(
        session,
        summary(),
        settled(SETTLED),
        {"items": [seen("The file was written.", "written", "e3#t1")]},
        {"replacements": []},
        check(),
    )
    assert [(f.source, f.entry) for f in wrote.facts] == [("stated", "e1"), ("observed", "e3#t1")]
    # Summary, turns, observed, final state, and one check call: the observed item is
    # never asked about.
    assert len(fake.requests) == 5
    assert "The file was written." not in fake.requests[-1]["messages"][1]["content"]


def test_each_stated_item_gets_its_own_call_after_the_final_state(session: Session):
    again = item(
        "e4",
        "fact",
        "A slow endpoint is not retried forever.",
        "a slow endpoint is not retried forever",
    )
    wrote, fake = run_checked(
        session,
        summary(),
        settled(SETTLED, again),
        {"items": []},
        {"replacements": []},
        {"asserted": False, "in_effect": True, "reason": "Those were the agent's words."},
        check(),
    )
    # Two [stated] items: the final state is asked first, then each item in session order.
    assert len(fake.requests) == 6
    assert fake.requests[-2]["messages"][1]["content"].startswith("[decision] Capture runs")
    assert fake.requests[-1]["messages"][1]["content"].startswith("[fact] A slow endpoint")
    assert [(f.statement, f.source, f.entry) for f in wrote.facts] == [
        ("A slow endpoint is not retried forever.", "stated", "e4")
    ]
    assert [(d.statement, d.reason) for d in wrote.dropped] == [
        ("Capture runs at session end.", "the verifier found it not asserted")
    ]


def test_a_refused_check_drops_the_item_and_the_job_still_stands(session: Session):
    wrote, _ = run_checked(
        session,
        summary(),
        settled(SETTLED),
        {"items": []},
        {"asserted": "yes", "in_effect": True, "reason": "The user asked, never settled."},
        {"asserted": "yes", "in_effect": True, "reason": "The user asked, never settled."},
    )
    assert not wrote.facts
    reasons = [d.reason for d in wrote.dropped if d.statement]
    assert len(reasons) == 1
    assert reasons[0].startswith(
        "the verifier refused: the answer failed its schema: $.asserted should be boolean"
    )


def verified_two_endpoint_config(cfg: Config, verifier: FakeEndpoint, key_env: str = "") -> Config:
    """The turns config with the item check on a second endpoint, re-read from disk.

    The [model] table keeps a key name and an extra_body field of its own, the kind a main
    endpoint needs and a second one must not inherit.
    """
    path = cfg.home / configmod.CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'name = "fake-model"\n',
        'name = "fake-model"\napi_key_env = "MAIN_KEY"\nextra_body = { top_p = 0.5 }\n',
    )
    text = text.replace(
        'pipeline = "base"\n',
        'pipeline = "turns"\nverify_items = true\n'
        f'verifier_endpoint = "{verifier.endpoint}"\n'
        + (f'verifier_api_key_env = "{key_env}"\n' if key_env else ""),
    )
    path.write_text(text, encoding="utf-8")
    return configmod.load()


def run_on_two_endpoints(
    session: Session,
    cfg: Config,
    verifier: FakeEndpoint,
    key_env: str = "",
) -> digest.Digest:
    """The digest of the shared session with the item check on the verifier's endpoint."""
    try:
        cfg = verified_two_endpoint_config(cfg, verifier, key_env)
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        verifier.stop()
    assert wrote is not None
    return wrote


def test_a_verifier_endpoint_gets_no_main_key_and_no_extra_body(session, monkeypatch):
    """The check's endpoint is its own service: no [model] key and none of the [model]
    table's extra_body fields reach it (BUILD-PLAN F56)."""
    monkeypatch.setenv("MAIN_KEY", "main-secret")
    main, cfg = serve(session.cfg, summary(), settled(SETTLED), {"items": []})
    verifier = FakeEndpoint([check()])
    verifier.start()
    try:
        wrote = run_on_two_endpoints(session, cfg, verifier)
    finally:
        main.stop()
    assert [(f.statement, f.source) for f in wrote.facts] == [
        ("Capture runs at session end.", "stated")
    ]
    # The three turns calls went to the [model] endpoint, the check to the verifier's.
    assert len(main.requests) == 3
    assert len(verifier.requests) == 1
    sent = verifier.requests[0]
    assert sent["model"] == "fake-model"
    assert "top_p" not in sent, "the [model] table's extra_body stays with its own endpoint"
    assert "Authorization" not in verifier.headers[0], "an empty key name sends no key"
    assert main.headers[0]["authorization"] == "Bearer main-secret"


def test_a_verifier_endpoint_sends_the_key_its_own_setting_names(session, monkeypatch):
    """digest.verifier_api_key_env names the key the check's endpoint takes, and the
    [model] key stays with the [model] endpoint (BUILD-PLAN F56)."""
    monkeypatch.setenv("MAIN_KEY", "main-secret")
    monkeypatch.setenv("CHECK_KEY", "check-secret")
    main, cfg = serve(session.cfg, summary(), settled(SETTLED), {"items": []})
    verifier = FakeEndpoint([check()])
    verifier.start()
    try:
        run_on_two_endpoints(session, cfg, verifier, key_env="CHECK_KEY")
    finally:
        main.stop()
    assert verifier.headers[0]["authorization"] == "Bearer check-secret"
    assert main.headers[0]["authorization"] == "Bearer main-secret"


def transcript_entries(*made: tuple[str, str, str]) -> list[Entry]:
    """The entries a window test reads, in order: (id, role, text) for each."""
    return [Entry(id, NOW, role, text, False) for id, role, text in made]


def stated_fact(
    entry_id: str, statement: str, quote: str, proposal: tuple[str, str] | None = None
) -> digest.Fact:
    """One [stated] fact the check is asked about, quoting its own entry."""
    return digest.Fact(
        statement=statement,
        kind="decision",
        source="stated",
        quote=quote,
        entry=entry_id,
        scope="project",
        date="2025-01-01",
        proposal=proposal,
    )


def check_sends(tmp_path, fact: digest.Fact, entries: list[Entry], monkeypatch) -> str:
    """The input the item check sends for this fact, with the answer scripted to yes."""
    sent: list[str] = []

    def answer(config, prompt, text, schema):
        sent.append(text)
        return check()

    monkeypatch.setattr(digest, "_model_answer", answer)
    kept, dropped = digest._verified_items(
        Config(home=tmp_path, vault=tmp_path, host="testhost", settings=Settings()),
        [fact],
        entries,
    )
    assert kept == [fact] and not dropped
    return sent[0]


def test_the_window_counts_past_tool_entries(tmp_path, monkeypatch):
    """The check reads the two USER or ASSISTANT entries on either side of the item's own,
    counted past the TOOL entries between them (BUILD-PLAN F56)."""
    entries = transcript_entries(
        ("e1", USER, "Capture runs at session end."),
        ("e2", ASSISTANT, "On it.\n```python\nrow = write_row()\n```\nDone."),
        ("e3", TOOL, "boom: no such file"),
        ("e4", TOOL, "boom: no such file"),
        ("e5", USER, "Scrap that, the retry gets its own setting."),
        ("e6", ASSISTANT, "The retry got its own setting."),
    )
    fact = stated_fact("e1", "Capture runs at session end.", "Capture runs at session end.")
    sent = check_sends(tmp_path, fact, entries, monkeypatch)
    assert "[#1] USER ITEM" in sent and "[#2] ASSISTANT" in sent
    # Two entries after the item's own, reached past the tool entries: the reversal is in,
    # the entry after it is not, and no tool output is shown.
    assert "Scrap that" in sent
    assert "The retry got its own setting" not in sent
    assert "boom" not in sent
    # Each entry in the compacted form the turns calls show: the code is named, not shown.
    assert "On it." in sent and "write_row" not in sent


def test_an_approvals_proposal_entry_joins_the_window(tmp_path, monkeypatch):
    """The entry an approved item's proposal came from is in the window even when it lies
    beyond the two USER or ASSISTANT entries on either side (BUILD-PLAN F56)."""
    entries = transcript_entries(
        ("e1", USER, "What about the retry?"),
        (
            "e2",
            ASSISTANT,
            "Give the retry its own setting, so a slow endpoint is not retried forever."
            " Or keep it?",
        ),
        ("e3", USER, "Anyway, the theme stays as it is."),
        ("e4", ASSISTANT, "Noted, the theme stays."),
        ("e5", USER, "Yes, give the retry its own setting."),
        ("e6", ASSISTANT, "Done."),
    )
    fact = stated_fact(
        "e5",
        "The retry gets its own setting.",
        "Yes, give the retry its own setting",
        proposal=("e2", "Give the retry its own setting"),
    )
    sent = check_sends(tmp_path, fact, entries, monkeypatch)
    assert "Give the retry its own setting, so a slow endpoint" in sent
    assert "[#2] ASSISTANT" in sent
    # Without the proposal, the window is the two entries on either side of the approval,
    # and the proposal's message is beyond them.
    plain = stated_fact(
        "e5", "The retry gets its own setting.", "Yes, give the retry its own setting"
    )
    sent = check_sends(tmp_path, plain, entries, monkeypatch)
    assert "Give the retry its own setting, so a slow endpoint" not in sent


def test_the_window_is_cut_to_the_input_budget(tmp_path, monkeypatch):
    """The check's input stays inside MAX_CHUNK_TOKENS less its prompt: the outer entries
    leave first, the item's own stands (BUILD-PLAN F56, DESIGN 13)."""
    entries = transcript_entries(
        ("e1", USER, "Capture runs at session end."),
        ("e2", ASSISTANT, "On it. The hook writes one row and returns."),
        ("e3", TOOL, "boom"),
        ("e4", USER, "Scrap that, use the env var."),
    )
    fact = stated_fact("e1", "Capture runs at session end.", "Capture runs at session end.")
    monkeypatch.setattr(digest, "MAX_CHUNK_TOKENS", 1)
    sent = check_sends(tmp_path, fact, entries, monkeypatch)
    assert sent.count("[#") == 1 and "[#1] USER ITEM" in sent
    assert "On it." not in sent and "Scrap that" not in sent


def test_a_verifier_error_leaves_the_job_as_any_model_error_does(session, monkeypatch):
    """A ModelError from the check's call is any model error (DESIGN 14): the run stops
    with the job queued for later, its page unwritten and its watermark unmoved."""
    monkeypatch.setenv("MAIN_KEY", "main-secret")
    main, cfg = serve(session.cfg, summary(), settled(SETTLED), {"items": []})
    verifier = FakeEndpoint([Reply(status=500)])
    verifier.start()
    try:
        cfg = verified_two_endpoint_config(cfg, verifier)
        queue.enqueue(cfg, event(session.transcript, session.cwd))
        report = work.run(cfg)
    finally:
        verifier.stop()
        main.stop()
    assert report.stopped == work.STOP_UNREACHABLE
    assert report.digested == 0
    assert pages(cfg) == []
    assert watermark(cfg, SESSION) is None
    assert rows(cfg, queue.FAILED) == []
    # The job stays claimed only by its lease, so a later run reads the same chunk again.
    assert rows(cfg, queue.RUNNING) == [SESSION]
    # The three turns calls went through; the check's call did not survive its endpoint.
    assert len(main.requests) == 3
    assert len(verifier.requests) == ATTEMPTS


# ---------------------------------------------------------------------------
# F57: what the working agent points at


def test_extract_pointer_recognizes_memory_footer_and_look_here_markers():
    """BUILD-PLAN F57: extract Memory: footers and bare look-here markers."""
    # Memory footer with bullet lines
    text1 = 'Updated port.\n\nMemory:\n- "use port 8008"\n- "keep tests fast"'
    assert digest.extract_pointer(text1) == 'Memory:\n- "use port 8008"\n- "keep tests fast"'

    # Memory footer inline
    text2 = 'Done.\n\nMemory: "use port 8008"'
    assert digest.extract_pointer(text2) == 'Memory: "use port 8008"'

    # Bare look-here HTML comment
    text3 = "Configured settings.\n<!-- look-here -->"
    assert digest.extract_pointer(text3) == "look-here"

    # Bare look-here bracketed
    text4 = "Configured settings.\n[look-here]"
    assert digest.extract_pointer(text4) == "look-here"

    # Bare look-here plain
    text5 = "Configured settings.\nlook-here"
    assert digest.extract_pointer(text5) == "look-here"

    # Bare look-here uppercase
    text6 = "Configured settings.\n<!-- LOOK-HERE -->"
    assert digest.extract_pointer(text6) == "look-here"

    # Ordinary response with no pointer
    text7 = "Running tests now."
    assert digest.extract_pointer(text7) is None

    # Text marked skippable is not a decision pointer
    text8 = "Verbose logs.\n<!-- skip -->"
    assert digest.extract_pointer(text8) is None


def test_turn_blocks_show_memory_footer_and_look_here_pointers():
    """BUILD-PLAN F57: turn blocks show POINTER with footer, marker, or (none)."""

    def entry(eid: str, role: str, text: str) -> Entry:
        return Entry(eid, NOW.astimezone(UTC), role, text, False)

    part = [
        entry("e1", USER, "use port 8008"),
        entry("e2", ASSISTANT, 'Configured port 8008.\n\nMemory:\n- "use port 8008"'),
        entry("e3", USER, "now run tests"),
        entry("e4", ASSISTANT, "Tests passed.\n<!-- look-here -->"),
        entry("e5", USER, "wrap up"),
        entry("e6", ASSISTANT, "All finished."),
    ]
    blocks, _, _, _ = digest.turn_blocks(part)
    assert len(blocks) == 3

    assert 'POINTER: Memory:\n- "use port 8008"' in blocks[0]
    assert "POINTER: look-here" in blocks[1]
    assert "POINTER: (none)" in blocks[2]


def test_turn_with_memory_footer_extracts_stated_fact_quoting_user(session: Session):
    """BUILD-PLAN F57: a turn whose response has a Memory: footer is kept as [stated]."""
    user_turn = "We should use port 8008 for the api."
    agent_reply = 'Switched to port 8008.\n\nMemory:\n- "use port 8008 for the api"'
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", user_turn),
        record("e2", "assistant", agent_reply),
        record("e3", "user", "run tests now"),
        record("e4", "assistant", "Tests passing."),
    )

    items = settled(
        item("e1", "decision", "Use port 8008 for the api.", "use port 8008 for the api")
    )
    wrote = run(session, summary(), items, {"items": []})
    assert len(wrote.facts) == 1
    assert wrote.facts[0].source == "stated"
    assert wrote.facts[0].quote == "use port 8008 for the api"
    assert wrote.facts[0].entry == "e1"


def test_pointer_is_pointer_only_fact_quoting_assistant_footer_is_refused(session: Session):
    """BUILD-PLAN F57: a stated fact quoting the assistant footer rather than the user
    is refused."""
    user_turn = "Use port 8008."
    agent_reply = 'Done.\n\nMemory:\n- "switch to port 8008"'
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", user_turn),
        record("e2", "assistant", agent_reply),
        record("e3", "user", "run tests now"),
        record("e4", "assistant", "Tests passing."),
    )

    # Model cited e2 (the assistant entry) rather than the user turn e1.
    items = settled(item("e2", "decision", "Switch to port 8008.", "switch to port 8008"))
    wrote = run(session, summary(), items, {"items": []})
    # Stated fact cannot cite assistant entry; dropped by verify.
    assert len(wrote.facts) == 0
    assert any("not one of the user's turns" in d.reason for d in wrote.dropped)


def test_skip_marker_is_never_removed_from_compacted_blocks():
    """BUILD-PLAN F57: text the agent marks skippable is never removed."""
    from aft import filter as filtermod

    entry = Entry("e1", NOW.astimezone(UTC), ASSISTANT, "Step 1\n<!-- skip -->\nStep 2", False)
    compacted = filtermod.compact(entry)
    assert "Step 1" in compacted
    assert "Step 2" in compacted
    assert "<!-- skip -->" in compacted


def test_the_turns_prompt_includes_the_users_own_life():
    """BUILD-PLAN F73: a turn's fact covers what the user states about themselves, a
    preference covers personal tastes, and a fact about the user's own life is global."""
    words = " ".join(digest.turns_prompt_text().split())
    assert (
        "or about themselves: interests, hobbies and skills they are learning, activities "
        "and events they took part in, things they own or bought, plans, people in their "
        "life, places, and quantities and dates (\"I've restored three vintage radios since "
        'I started the hobby")' in words
    )
    assert 'including personal tastes ("I prefer matinee showings")' in words
    assert (
        "scope: global for a preference or a fact about the user's environment that holds "
        "for every project, and for a fact about the user's own life; otherwise project." in words
    )


def test_the_turns_prompt_reads_an_assistant_session():
    """BUILD-PLAN F73: the session is between the user and an AI assistant, often a
    coding agent."""
    words = " ".join(digest.turns_prompt_text().split())
    assert "an AI assistant, often a coding agent" in words


def test_the_turns_prompt_labels_a_yes_to_work_a_request():
    """BUILD-PLAN F88: a yes to a proposal is a decision only when what it approves stays
    true after the turn; a yes to doing a piece of work now is a request."""
    words = " ".join(digest.turns_prompt_text().split())
    assert (
        "A yes to a specific proposal in BEFORE is a decision too when what it approves stays"
        " true after the turn: a design choice, a setting, a rule or a plan the user adopts"
        ' ("yes, keep the cache in memory").' in words
    )
    assert (
        "A yes to doing a piece of work now (spec, build, start, merge, run, fix or commit"
        " something) is a request, as the same words without the proposal would be." in words
    )
    assert '"yes, build the importer and merge it"' in words


def test_the_turns_prompt_keeps_a_lasting_value_set_by_a_request():
    """BUILD-PLAN T47: a request that sets a lasting value also gives that value as its
    own item, labelled decision or fact, with a statement that says the value and not
    the order."""
    words = " ".join(digest.turns_prompt_text().split())
    assert (
        "A request that names a value a later session must know (where something lives,"
        " what something is called, what a setting is set to) also gives that value as its"
        " own item, labelled decision or fact, with a statement that says the value and not"
        ' the order ("The project lives at /srv/project"). That work was started, run,'
        " restarted, retried or finished is never such a value." in words
    )
