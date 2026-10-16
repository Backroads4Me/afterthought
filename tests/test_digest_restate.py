"""BUILD-PLAN F89: A fact refused for its names is restated once in its source's words.

R2's names check (digest.ungrounded) refuses a fact when its statement names a word
the source text does not hold. When digest.restate is on (default true), the refused
facts go to one repair call per chunk: the model restates each in the source's words,
and the restated fact passes through the full verify code again.
"""

# ruff: noqa: F811 - the tests take the shared session fixture by name
from __future__ import annotations

from datetime import UTC, datetime

from aft import config as configmod
from aft import digest
from aft.config import Config
from fakes.model_server import FakeEndpoint
from test_digest import (
    ASSISTANT_ANSWER,
    SECOND_ANSWER,
    SECOND_PROMPT,
    USER_PROMPT,
    Session,
    answer,
    claim,
    digest_once,
    frontmatter,
    record,
    said,
    serve,
    session,  # noqa: F401 - pytest fixture
    with_budget,
    write_records,
)
from test_digest_turns import item, settled, summary


def with_restate(cfg: Config, on: bool) -> Config:
    """The same config.toml with digest.restate set, re-read from disk."""
    path = cfg.home / configmod.CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    setting = f"restate = {str(on).lower()}"
    if "restate = " in text:
        lines = [
            setting if line.strip().startswith("restate =") else line for line in text.splitlines()
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    elif "[digest]" in text:
        path.write_text(text.rstrip("\n") + f"\n{setting}\n", encoding="utf-8")
    else:
        path.write_text(f"{text}\n[digest]\n{setting}\n", encoding="utf-8")
    return configmod.load()


def test_fact_refused_for_name_is_restated_and_kept_with_source_spelling(session: Session):
    """A fact refused for a name the source spells otherwise is restated and kept with the
    source's spelling (BUILD-PLAN F89)."""
    # e1 holds: "Store the digest at session end, not per turn. Capture runs at session end."
    # The statement says "PostgreSQL" which e1 does not hold; restate rewrites it to use e1's words.
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL.",
            quote="Capture runs at session end.",
            entry="e1",
        )
    )
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Capture runs at session end, not per turn.",
            }
        ]
    }
    wrote = digest_once(session, initial, restatement)
    assert [f.statement for f in wrote.facts] == ["Capture runs at session end, not per turn."]
    data = frontmatter(session.cfg, wrote.path)["aft"]
    assert "dropped" not in data or not data["dropped"]


def test_restated_fact_still_naming_unheld_word_is_dropped(session: Session):
    """A restated fact that still names an unheld word is dropped with its new reason
    (BUILD-PLAN F89)."""
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL.",
            quote="Capture runs at session end.",
            entry="e1",
        )
    )
    # The restated statement still introduces an ungrounded name "MySQL"
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Capture runs at session end in MySQL.",
            }
        ]
    }
    wrote = digest_once(session, initial, restatement)
    assert not wrote.facts
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert len(dropped) == 1
    assert dropped[0]["statement"] == "Capture runs at session end in MySQL."
    assert "holds: MySQL" in dropped[0]["reason"]


def test_restated_fact_whose_quote_is_a_paste_is_dropped(session: Session):
    """A restated fact whose quote is from a paste block is dropped by verify (BUILD-PLAN F89)."""
    pasted_prompt = (
        "<pasted_content>Database port is 5432.</pasted_content>\nPlease use port 5432 for tests."
    )
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", pasted_prompt),
        record("e2", "assistant", ASSISTANT_ANSWER),
        record("e3", "user", SECOND_PROMPT),
        record("e4", "assistant", SECOND_ANSWER),
    )
    # Quote comes from inside the paste block; statement names unheld "PostgreSQL"
    initial = answer(
        said(
            statement="Database runs on PostgreSQL port 5432.",
            quote="Database port is 5432.",
            entry="e1",
        )
    )
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Database port is 5432.",
            }
        ]
    }
    wrote = digest_once(session, initial, restatement)
    assert not wrote.facts
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert len(dropped) == 1
    assert "paste" in dropped[0]["reason"]


def test_restated_fact_whose_quote_is_a_question_is_dropped(session: Session):
    """A restated fact whose quote is a question is dropped by verify (BUILD-PLAN F89)."""
    question_prompt = "Should capture run at session end? Please verify."
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", question_prompt),
        record("e2", "assistant", ASSISTANT_ANSWER),
        record("e3", "user", SECOND_PROMPT),
        record("e4", "assistant", SECOND_ANSWER),
    )
    # The quote has a question mark; statement names unheld "PostgreSQL"
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL at session end.",
            quote="Should capture run at session end?",
            entry="e1",
        )
    )
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Capture runs at session end.",
            }
        ]
    }
    wrote = digest_once(session, initial, restatement)
    assert not wrote.facts
    dropped = frontmatter(session.cfg, wrote.path)["aft"]["dropped"]
    assert len(dropped) == 1
    assert "question" in dropped[0]["reason"]


def test_nothing_is_sent_when_no_fact_was_refused_for_names(session: Session):
    """Nothing is sent to restate when no fact was refused for its names (BUILD-PLAN F89)."""
    initial = answer(
        said(
            statement="Capture runs at session end.",
            quote="Capture runs at session end.",
            entry="e1",
        )
    )
    fake, cfg = serve(session.cfg, initial)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert len(wrote.facts) == 1
    # Only 1 model call was made (the initial digest); no restate call
    assert len(fake.requests) == 1


def test_spent_budget_skips_the_restate_call(session: Session):
    """A spent daily token budget skips the restate call (BUILD-PLAN F89)."""
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL.",
            quote="Capture runs at session end.",
            entry="e1",
        )
    )
    fake, cfg = serve(session.cfg, initial)
    cfg = with_budget(cfg, 100)
    usage_file = cfg.home / "usage.jsonl"
    usage_file.parent.mkdir(parents=True, exist_ok=True)
    with usage_file.open("a", encoding="utf-8") as f:
        import json

        f.write(
            json.dumps(
                {
                    "time": datetime.now(UTC).isoformat(),
                    "purpose": "work",
                    "input_tokens": 150,
                    "output_tokens": 50,
                }
            )
            + "\n"
        )
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert not wrote.facts
    # Only 1 model call was made (initial digest), restate was skipped due to budget
    assert len(fake.requests) == 1
    dropped = frontmatter(cfg, wrote.path)["aft"]["dropped"]
    assert any("PostgreSQL" in d["reason"] for d in dropped)


def test_setting_off_makes_no_call(session: Session):
    """When digest.restate is false, no restatement call is made (BUILD-PLAN F89)."""
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL.",
            quote="Capture runs at session end.",
            entry="e1",
        )
    )
    fake, cfg = serve(session.cfg, initial)
    cfg = with_restate(cfg, False)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert not wrote.facts
    assert len(fake.requests) == 1
    dropped = frontmatter(cfg, wrote.path)["aft"]["dropped"]
    assert any("PostgreSQL" in d["reason"] for d in dropped)


def test_turns_pipeline_restates_fact_refused_for_names(session: Session):
    """A fact refused in the turns pipeline for ungrounded names is restated and kept
    (BUILD-PLAN F89)."""
    cfg = session.cfg
    path = cfg.home / configmod.CONFIG_NAME
    path.write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n[digest]\npipeline = "turns"\n',
        encoding="utf-8",
    )
    initial_summary = summary("Session settled capture details.")
    # e1 holds: USER_PROMPT (capture runs at session end, not per turn)
    initial_settled = settled(
        item("e1", "decision", "Capture runs in PostgreSQL.", "Capture runs at session end.")
    )
    observed = {"items": []}
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Capture runs at session end, not per turn.",
            }
        ]
    }
    fake = FakeEndpoint([initial_summary, initial_settled, observed, restatement])
    fake.start()
    try:
        path.write_text(
            f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
            f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
            f'[digest]\npipeline = "turns"\n',
            encoding="utf-8",
        )
        cfg = configmod.load()
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert [f.statement for f in wrote.facts] == ["Capture runs at session end, not per turn."]
    data = frontmatter(cfg, wrote.path)["aft"]
    assert "dropped" not in data or not data["dropped"]


def test_observed_fact_refused_for_name_is_not_restated(session: Session):
    """Only [stated] facts are restated (BUILD-PLAN F92): an observed fact refused for an
    ungrounded name stays refused and no restate call is made."""
    # e2 holds: ASSISTANT_ANSWER ("Done: the hook writes one row and returns...")
    # Initial statement names unheld "PostgreSQL"
    initial = answer(
        said(
            statement="The hook writes rows into PostgreSQL.",
            quote="Done: the hook writes one row and returns without calling a model.",
            entry="e2",
            source="observed",
        )
    )
    fake, cfg = serve(session.cfg, initial)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert wrote.facts == ()
    assert any("PostgreSQL" in d.reason for d in wrote.dropped)
    # Only the digest call was made: the observed fact went to no restate call.
    assert len(fake.requests) == 1


def test_restated_approval_fact_preserves_proposal(session: Session):
    """A restated approval fact preserves its proposal attribute (BUILD-PLAN F89)."""
    cfg = session.cfg
    path = cfg.home / configmod.CONFIG_NAME
    initial_summary = summary("Settled the protocol choice.")
    # e2 proposed "use binary framing for packets"
    # e3 user says "yes use that"
    write_records(
        session.transcript,
        {"cwd": str(session.cwd)},
        record("e1", "user", USER_PROMPT),
        record("e2", "assistant", "I propose we use binary framing for packets."),
        record("e3", "user", "yes use that for the protocol"),
        record("e4", "assistant", SECOND_ANSWER),
    )
    initial_settled = settled(
        item(
            "e3",
            "decision",
            "Use Redis framing for the protocol.",
            "yes use that for the protocol",
            proposal="use binary framing for packets",
        )
    )
    observed = {"items": []}
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Use binary framing for the protocol.",
            }
        ]
    }
    fake = FakeEndpoint([initial_summary, initial_settled, observed, restatement])
    fake.start()
    try:
        path.write_text(
            f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
            f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
            f'[digest]\npipeline = "turns"\n',
            encoding="utf-8",
        )
        cfg = configmod.load()
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert len(wrote.facts) == 1
    assert wrote.facts[0].statement == "Use binary framing for the protocol."
    assert wrote.facts[0].proposal is not None
    assert wrote.facts[0].proposal[1] == "use binary framing for packets"


def test_multiple_refused_facts_in_one_chunk_handled_in_single_call(session: Session):
    """Multiple facts refused for names in one chunk go to one call; un-restateable items remain
    dropped (BUILD-PLAN F89)."""
    # Two facts with unheld names
    initial = answer(
        said(
            statement="Capture runs in PostgreSQL.",
            quote="Capture runs at session end.",
            entry="e1",
        ),
        said(
            statement="The hook uses Bluetooth for notifications.",
            quote="Store the digest at session end, not per turn.",
            entry="e1",
        ),
    )
    # The model restates #1 and gives empty statement for #2
    restatement = {
        "items": [
            {
                "item": "#1",
                "statement": "Capture runs at session end, not per turn.",
            },
            {
                "item": "#2",
                "statement": "",
            },
        ]
    }
    fake, cfg = serve(session.cfg, initial, restatement)
    try:
        wrote = digest.digest_job(cfg, claim(cfg, session.transcript, session.cwd))
    finally:
        fake.stop()
    assert wrote is not None
    assert [f.statement for f in wrote.facts] == ["Capture runs at session end, not per turn."]
    # Only 2 calls total: 1 initial digest + 1 restate call (containing both items)
    assert len(fake.requests) == 2
    dropped = frontmatter(cfg, wrote.path)["aft"]["dropped"]
    assert len(dropped) == 1
    assert dropped[0]["statement"] == "The hook uses Bluetooth for notifications."
    assert "Bluetooth" in dropped[0]["reason"]
