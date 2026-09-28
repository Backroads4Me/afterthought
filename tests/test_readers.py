"""Transcript readers, over fixtures hand-trimmed from real Claude Code and pi sessions."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aft import readers

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE = readers.CLAUDE
PI = readers.PI


def read(agent: str, fixture: str, after: str | None = None) -> list[readers.Entry]:
    return list(readers.read(agent, FIXTURES / fixture, after))


def agent_of(fixture: str) -> str:
    return CLAUDE if fixture.startswith("claude") else PI


def ids(entries) -> list[str]:
    return [entry.id for entry in entries]


def test_claude_session_reads_the_conversation_in_file_order():
    entries = read(CLAUDE, "claude-session.jsonl")
    assert [(e.role, e.is_error) for e in entries] == [
        ("user", False),
        ("assistant", False),
        ("tool", True),
        ("assistant", False),
    ]
    assert entries[0].text.startswith("I am trying to build a new memory system")
    assert entries[-1].text.startswith("Now I'll write the design doc")
    assert entries[1].time < entries[2].time < entries[3].time


def test_claude_leaves_out_bookkeeping_and_prompts_meant_for_the_agent():
    entries = read(CLAUDE, "claude-session.jsonl")
    # The queue-operation and attachment entries and the isMeta prompt to the agent
    # hold no conversation; the tool_use-only assistant turn has no text of its own.
    assert not any("Page contract" in e.text for e in entries)
    assert all(e.time is not None and e.time.tzinfo is not None for e in entries)


def test_claude_skips_the_notices_it_writes_in_place_of_an_answer():
    entries = read(CLAUDE, "claude-session.jsonl")
    texts = " ".join(entry.text for entry in entries)
    assert "Failed to authenticate" not in texts  # marked by isApiErrorMessage
    assert "No response requested" not in texts  # marked by the synthetic model
    assert len(entries) == 4  # neither notice becomes an entry


def test_claude_splits_a_tool_result_out_of_the_user_message_it_came_in():
    tool = next(e for e in read(CLAUDE, "claude-session.jsonl") if e.role == "tool")
    assert tool.id == "57c9f783-9ddd-428f-a7eb-6c68db63501a#toolu_019vUjvZgDiRQH22ficLzXAx"
    assert tool.is_error
    assert tool.text.startswith("Exit code 1")


def test_claude_skips_the_summary_it_writes_on_compaction():
    entries = read(CLAUDE, "claude-compaction.jsonl")
    assert [e.role for e in entries] == ["user", "assistant", "user", "assistant"]
    assert not any("This session is being continued" in e.text for e in entries)
    assert entries[0].text.startswith("I want to have a brilliant")
    assert entries[2].text == 'it says "Victron solar" where is that covered?'


def test_claude_reads_a_resumed_session_from_the_copied_history_on():
    entries = read(CLAUDE, "claude-resumed.jsonl")
    # The file starts with the entries of the session it resumed, under that session's
    # id, and continues with the new turns under its own.
    assert [e.role for e in entries] == ["user", "assistant", "user", "tool"]
    assert entries[0].text == "can I use proxmox backup server instead?"
    assert entries[2].text.startswith("what if instead, I run hermes in docker")
    assert entries[3].text == ""


def test_after_an_entry_yields_only_what_follows_it():
    entries = read(CLAUDE, "claude-resumed.jsonl", after="e0748fc4-f4e9-4189-a0c4-d30d9ab42162")
    assert ids(entries) == [
        "f992d93c-6e93-4e9d-bd2e-ebc328129161",
        "23626d10-0916-4824-899f-2ad51ad88586#toolu_01YANNoA1RJfSLNFpAZGyRLa",
    ]


def test_a_watermark_the_transcript_lacks_rereads_the_whole_session():
    whole = read(CLAUDE, "claude-session.jsonl")
    assert ids(read(CLAUDE, "claude-session.jsonl", after="gone-from-this-file")) == ids(whole)


def test_pi_session_reads_its_message_entries():
    entries = read(PI, "pi-session.jsonl")
    assert [e.role for e in entries] == [
        "user",
        "assistant",
        "tool",
        "tool",
        "tool",
        "tool",
        "assistant",
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    # An assistant turn contributes its text only: not the thinking block beside it,
    # and not a turn whose text is whitespace.
    assert entries[1].text == "I'll check the OpenViking service and MCP tooling."
    assert entries[6].text.startswith("Yes, OpenViking is healthy")


def test_pi_skips_entries_that_are_not_conversation():
    entries = read(PI, "pi-session.jsonl")
    skipped = {"01a0c23e-8c89-73de-a794-8cd0266d5889", "30b3a2d7", "7ded9b64", "56ac5145"}
    assert not skipped & set(ids(entries))
    lines = (FIXTURES / "pi-session.jsonl").read_text().splitlines()
    header = json.loads(lines[0])
    assert header["type"] == "session" and header["id"] not in set(ids(entries))


def test_pi_marks_a_tool_result_the_agent_flagged():
    entry = next(e for e in read(PI, "pi-session.jsonl") if e.id == "b7f75b0a")
    assert (entry.role, entry.is_error) == ("tool", True)
    assert "Command exited with code 128" in entry.text


def test_pi_leaves_out_the_compaction_and_keeps_the_entries_around_it():
    entries = read(PI, "pi-compaction.jsonl")
    assert ids(entries) == ["be9db84b", "a871a4f6", "8003fdfe"]
    assert not any("## Goal" in e.text for e in entries)


def test_pi_follows_the_active_branch_only():
    entries = read(PI, "pi-resumed.jsonl")
    assert ids(entries) == ["3a605e2d", "592c4b7b", "927d8ee7", "98c3174f"]
    assert entries[2].text.startswith("did you get my last message?   is this")
    assert all("## memory-rule-b-to-plan" not in e.text for e in entries)


def test_pi_continues_after_a_watermark():
    assert ids(read(PI, "pi-resumed.jsonl", after="927d8ee7")) == ["98c3174f"]


def test_entry_ids_are_unique_within_a_session():
    for path in sorted(FIXTURES.glob("*.jsonl")):
        seen = ids(read(agent_of(path.name), path.name))
        assert seen == sorted(set(seen), key=seen.index), path.name


def test_a_line_the_agent_left_half_written_is_skipped(tmp_path):
    lines = (FIXTURES / "pi-session.jsonl").read_text().splitlines()
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([*lines, '{"type":"message","id":"9999"', "not json at all"]) + "\n")
    assert ids(readers.read(PI, path)) == ids(read(PI, "pi-session.jsonl"))


def test_an_unknown_agent_is_reported_at_once():
    with pytest.raises(readers.ReaderError):
        readers.read("cursor", FIXTURES / "pi-session.jsonl")


def test_an_entry_is_a_frozen_record_of_the_pinned_fields():
    names = [f.name for f in dataclasses.fields(readers.Entry)]
    assert names == ["id", "time", "role", "text", "is_error"]
    assert readers.Entry.__dataclass_params__.frozen
