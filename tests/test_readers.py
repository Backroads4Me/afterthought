"""Transcript readers, over fixtures written in the shape of real Claude Code and pi files."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aft import readers

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE = readers.CLAUDE
PI = readers.PI

# The pi session header of pi-session.jsonl: bookkeeping, never conversation.
PI_HEADER_ID = "7be69a6f-a36a-507b-b4b7-6cc754237d29"
# The text-less tool result closing claude-resumed.jsonl, in the id form the
# Claude reader gives the result of a user message.
CLAUDE_EMPTY_RESULT_ID = "38624bec-7c7e-5ca1-8d56-9f959db90177#toolu_5013a588fd9a51beb3d7ebb9"


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
    assert entries[0].text.startswith("I am trying to build a new watering scheduler")
    assert entries[-1].text.startswith("Now I'll write the schedule doc")
    assert entries[1].time < entries[2].time < entries[3].time


def test_claude_leaves_out_bookkeeping_and_prompts_meant_for_the_agent():
    entries = read(CLAUDE, "claude-session.jsonl")
    # The queue-operation and attachment entries and the isMeta prompt to the agent
    # hold no conversation; the tool_use-only assistant turn has no text of its own.
    assert not any("Valve contract" in e.text for e in entries)
    assert all(e.time is not None and e.time.tzinfo is not None for e in entries)


def test_claude_skips_the_notices_it_writes_in_place_of_an_answer():
    entries = read(CLAUDE, "claude-session.jsonl")
    texts = " ".join(entry.text for entry in entries)
    assert "Failed to authenticate" not in texts  # marked by isApiErrorMessage
    assert "No response requested" not in texts  # marked by the synthetic model
    assert len(entries) == 4  # neither notice becomes an entry


def test_claude_splits_a_tool_result_out_of_the_user_message_it_came_in():
    tool = next(e for e in read(CLAUDE, "claude-session.jsonl") if e.role == "tool")
    assert tool.id == "d59a4bb6-be78-52de-92a5-2019e732fc1f#toolu_380fffc5950b58f6974ef308"
    assert tool.is_error
    assert tool.text.startswith("Exit code 1")


def test_a_tool_result_that_returned_no_text_is_not_an_entry(tmp_path):
    # The last user message of claude-resumed.jsonl carries a tool_reference result
    # and nothing else: it contributes no entry.
    assert [e.role for e in read(CLAUDE, "claude-resumed.jsonl")] == ["user", "assistant", "user"]
    assert CLAUDE_EMPTY_RESULT_ID not in ids(read(CLAUDE, "claude-resumed.jsonl"))
    # One rule in both readers, so pi drops the same kind of result.
    empty_result = tmp_path / "pi-empty-result.jsonl"
    empty_result.write_text(
        json.dumps(
            {
                "type": "message",
                "id": "0a0b0c0d",
                "parentId": None,
                "timestamp": "2026-09-27T05:00:00.000Z",
                "message": {"role": "toolResult", "content": [], "isError": True},
            }
        )
        + "\n"
    )
    assert ids(readers.read(PI, empty_result)) == []


def test_claude_skips_the_summary_it_writes_on_compaction():
    entries = read(CLAUDE, "claude-compaction.jsonl")
    assert [e.role for e in entries] == ["user", "assistant", "user", "assistant"]
    assert not any("This session is being continued" in e.text for e in entries)
    assert entries[0].text.startswith("I want to have a brilliant and very gifted senior dev")
    assert entries[2].text == 'it says "Zone 2 recovery" where is that covered?'


def test_claude_reads_a_resumed_session_from_the_copied_history_on():
    entries = read(CLAUDE, "claude-resumed.jsonl")
    # The file starts with the entries of the session it resumed, under that session's
    # id, and continues with the new turns under its own.
    assert [e.id for e in entries] == [
        "397af9a8-1d1a-5081-90de-c9c9ad78e4b7",
        "155cf51f-1e0c-59f8-98f4-02e32406e3b6",
        "42b8a868-fe04-599d-a816-a580ee5c9422",
    ]
    assert entries[0].text == "can I use the image registry instead of the tarball?"
    assert entries[2].text.startswith("what if instead, I run the bus api in docker")


def test_after_an_entry_yields_only_what_follows_it():
    entries = read(CLAUDE, "claude-resumed.jsonl", after="397af9a8-1d1a-5081-90de-c9c9ad78e4b7")
    assert ids(entries) == [
        "155cf51f-1e0c-59f8-98f4-02e32406e3b6",
        "42b8a868-fe04-599d-a816-a580ee5c9422",
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
    assert entries[1].text == "I'll check the metrics cache service and the panel tooling."
    assert entries[6].text.startswith("Yes, the metrics cache is healthy")


def test_pi_skips_entries_that_are_not_conversation():
    entries = read(PI, "pi-session.jsonl")
    # The session header, the model and thinking-level changes, and the system
    # message holding the prompt are entries of the file, not of the conversation.
    skipped = {PI_HEADER_ID, "5e33d511", "2732d77a", "c354ef8c"}
    assert not skipped & set(ids(entries))


def test_pi_marks_a_tool_result_the_agent_flagged():
    entry = next(e for e in read(PI, "pi-session.jsonl") if e.id == "61afebc7")
    assert (entry.role, entry.is_error) == ("tool", True)
    assert "Command exited with code 128" in entry.text


def test_pi_leaves_out_the_compaction_and_keeps_the_entries_around_it():
    entries = read(PI, "pi-compaction.jsonl")
    assert ids(entries) == ["f0fb81cb", "74a62723", "cc152226"]
    assert not any("## Goal" in e.text for e in entries)


def test_pi_follows_the_active_branch_only():
    entries = read(PI, "pi-resumed.jsonl")
    assert ids(entries) == ["8538c366", "b0f7bd00", "17f1b5d4", "2f255160"]
    assert entries[2].text.startswith("did you see my earlier note?   is this")
    assert all("notes-rule-b-to-plan" not in e.text for e in entries)


def test_pi_continues_after_a_watermark():
    assert ids(read(PI, "pi-resumed.jsonl", after="17f1b5d4")) == ["2f255160"]


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
    entry = read(CLAUDE, "claude-session.jsonl")[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.text = "changed"
