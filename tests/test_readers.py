"""Transcript readers, over fixtures written in the shape of real Claude Code and pi files."""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

from aft import evidence, readers
from aft import filter as filtermod

FIXTURES = Path(__file__).parent / "fixtures" / "transcripts"
CLAUDE = readers.CLAUDE
PI = readers.PI

# The pi session header of pi-session.jsonl: bookkeeping, never conversation.
PI_HEADER_ID = "7be69a6f-a36a-507b-b4b7-6cc754237d29"
# The text-less tool result closing claude-resumed.jsonl, in the id form the
# Claude reader gives the result of a user message.
CLAUDE_EMPTY_RESULT_ID = "38624bec-7c7e-5ca1-8d56-9f959db90177#toolu_5013a588fd9a51beb3d7ebb9"

# A user turn the CLI tagged as though it were one of its own notices.
MARKED_USER_TURN = {
    "parentUuid": "9016e5a5-35e8-502c-ada5-9f305dcb1235",
    "isSidechain": False,
    "promptId": "6c5feec8-b407-5da0-8a22-a970fbcd2b2e",
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "text", "text": "did the schedule doc land?"},
            {
                "type": "tool_result",
                "content": "schedule doc missing",
                "is_error": True,
                "tool_use_id": "toolu_380fffc5950b58f6974ef308",
            },
        ],
    },
    "uuid": "d59a4bb6-be78-52de-92a5-2019e732fc1f",
    "timestamp": "2026-09-28T06:57:06.713Z",
    "sourceToolAssistantUUID": "9016e5a5-35e8-502c-ada5-9f305dcb1235",
    "sessionId": "f3572571-122b-59d2-901e-3a3be1c5cd0a",
    "version": "2.1.280",
    "isApiErrorMessage": True,
}


def read(agent: str, fixture: str, after: str | None = None) -> list[readers.Entry]:
    return list(readers.read(agent, FIXTURES / fixture, after))


def agent_of(fixture: str) -> str:
    return CLAUDE if fixture.startswith("claude") else PI


def ids(entries) -> list[str]:
    return [entry.id for entry in entries]


DROP = object()  # a field to take out of the record rather than set


def patch_record(fixture: str, record_id: str, tmp_path, name: str, **fields) -> Path:
    """A copy of `fixture` with record `record_id` given `fields`, DROP removing one."""
    records = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
    for record in records:
        if record.get("id") != record_id:
            continue
        for field, value in fields.items():
            if value is DROP:
                record.pop(field, None)
            else:
                record[field] = value
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
    return path


def parts_holding(record: dict, container: str) -> list[dict]:
    """The parts of one record that hold the fields an entry is built from."""
    if container == "record":
        return [record]
    message = record.get("message")
    if not isinstance(message, dict):
        return []
    if container == "message":
        return [message]
    return [
        block
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == container
    ]


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


def test_a_user_turn_the_cli_marked_is_still_read(tmp_path):
    # The notices to skip are the ones Claude Code writes on its assistant entries, so a
    # marker on a user turn does not unsay what the user asked or hide the tool result
    # that came back inside the same message.
    path = tmp_path / "claude-marked-user-turn.jsonl"
    path.write_text(json.dumps(MARKED_USER_TURN) + "\n")

    entries = list(readers.read(CLAUDE, path))

    assert [(e.id, e.role) for e in entries] == [
        ("d59a4bb6-be78-52de-92a5-2019e732fc1f", "user"),
        ("d59a4bb6-be78-52de-92a5-2019e732fc1f#toolu_380fffc5950b58f6974ef308", "tool"),
    ]
    assert [e.text for e in entries] == [
        "did the schedule doc land?",
        "schedule doc missing",
    ]
    assert [e.is_error for e in entries] == [False, True]


def test_a_marked_user_turn_that_cannot_be_read_is_reported(tmp_path):
    # The guard on a message's content covers content, not one side of the conversation,
    # so a user turn the CLI marked is still reported when none of it can be built.
    path = tmp_path / "claude-marked-user-turn-unreadable.jsonl"
    turn = {**MARKED_USER_TURN, "message": {"role": "user", "body": "retry"}}
    path.write_text(json.dumps(turn) + "\n")

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(CLAUDE, path))


def test_a_sidechain_record_is_not_read(tmp_path):
    # A sidechain record is a subagent's conversation, whose user messages are the main
    # agent's prompts, not the person's words, so neither side of it becomes an entry.
    records = [
        {
            "type": "user",
            "uuid": "main-user",
            "timestamp": "2026-09-28T06:57:06.713Z",
            "message": {"role": "user", "content": "schedule the valves"},
        },
        {
            "type": "user",
            "isSidechain": True,
            "uuid": "side-user",
            "timestamp": "2026-09-28T06:58:06.713Z",
            "message": {"role": "user", "content": "check the valve specs"},
        },
        {
            "type": "assistant",
            "isSidechain": True,
            "uuid": "side-assistant",
            "timestamp": "2026-09-28T06:59:06.713Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "The spec says 5 bar"}],
            },
        },
        {
            "type": "assistant",
            "uuid": "main-assistant",
            "timestamp": "2026-09-28T07:00:06.713Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Set the valves to 5 bar"}],
            },
        },
    ]
    path = tmp_path / "claude-sidechain.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    entries = list(readers.read(CLAUDE, path))

    assert [(e.id, e.role, e.text) for e in entries] == [
        ("main-user", "user", "schedule the valves"),
        ("main-assistant", "assistant", "Set the valves to 5 bar"),
    ]


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
    # One rule in both readers, so pi drops the same kind of result while the read still
    # yields the turn beside it.
    turn = {
        "type": "message",
        "id": "11223344",
        "parentId": None,
        "timestamp": "2026-09-27T04:59:00.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Running it now"}]},
    }
    result = {
        "type": "message",
        "id": "0a0b0c0d",
        "parentId": "11223344",
        "timestamp": "2026-09-27T05:00:00.000Z",
        "message": {"role": "toolResult", "content": [], "isError": True},
    }
    empty_result = tmp_path / "pi-empty-result.jsonl"
    empty_result.write_text("\n".join(json.dumps(r) for r in (turn, result)) + "\n")

    assert ids(readers.read(PI, empty_result)) == ["11223344"]


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


def test_a_parent_the_file_lacks_ends_the_branch_and_says_so(tmp_path, caplog):
    # A write that failed partway leaves 80343c73 as a fragment the parser drops, with
    # the records after it still naming it as their parent. The branch ends at the
    # break rather than guessing past it into whatever else the file holds.
    lines = (FIXTURES / "pi-resumed.jsonl").read_text().splitlines()
    path = tmp_path / "pi-torn-parent.jsonl"
    path.write_text(
        "\n".join(
            '{"type":"message","id":"80343c73","parentId":"997aac76","mess'
            if json.loads(line)["id"] == "80343c73"
            else line
            for line in lines
        )
        + "\n"
    )

    with caplog.at_level(logging.WARNING):
        entries = list(readers.read(PI, path))

    assert ids(entries) == ["2f255160"]
    warning = next(record for record in caplog.records if record.levelno == logging.WARNING)
    assert warning.name == "aft.readers.pi"
    text = warning.getMessage()
    assert str(path) in text
    assert "80343c73" in text
    assert "5 message records" in text


def test_a_new_root_opens_a_branch_and_warns_nothing(tmp_path, caplog):
    # pi writes a null parentId when /tree moves the conversation to the root. What
    # sits above that new root is an abandoned branch, so the short read is correct and
    # nothing is missing.
    lines = (FIXTURES / "pi-session.jsonl").read_text().splitlines()
    root = {
        "type": "message",
        "id": "aaaa0001",
        "parentId": None,
        "timestamp": "2026-09-27T09:00:00.000Z",
        "message": {"role": "user", "content": "back to the first question"},
    }
    reply = {
        "type": "message",
        "id": "aaaa0002",
        "parentId": "aaaa0001",
        "timestamp": "2026-09-27T09:00:30.000Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "Sure."}]},
    }
    path = tmp_path / "pi-new-root.jsonl"
    path.write_text("\n".join([*lines, json.dumps(root), json.dumps(reply)]) + "\n")

    with caplog.at_level(logging.WARNING):
        entries = list(readers.read(PI, path))

    assert ids(entries) == ["aaaa0001", "aaaa0002"]
    assert not caplog.records


def test_entry_ids_are_unique_within_a_session():
    for path in sorted(FIXTURES.glob("*.jsonl")):
        seen = ids(read(agent_of(path.name), path.name))
        assert seen == sorted(set(seen), key=seen.index), path.name


def test_a_line_the_agent_left_half_written_is_skipped(tmp_path):
    lines = (FIXTURES / "pi-session.jsonl").read_text().splitlines()
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([*lines, '{"type":"message","id":"9999"', "not json at all"]) + "\n")
    assert ids(readers.read(PI, path)) == ids(read(PI, "pi-session.jsonl"))


def test_a_break_over_a_whole_conversation_reads_short_not_unreadable(tmp_path, caplog):
    # The branch holds only a system message, so nothing is yielded, while the real
    # conversation of the file sits behind the break. No record is unreadable and each
    # names its type, so this is a short read with a warning, not a changed format.
    lines = (FIXTURES / "pi-session.jsonl").read_text().splitlines()
    orphan = {
        "type": "message",
        "id": "cccc0001",
        "parentId": "dddd9999",
        "timestamp": "2026-09-27T09:00:00.000Z",
        "message": {"role": "system", "content": "you are a coding agent"},
    }
    path = tmp_path / "pi-break-above-conversation.jsonl"
    path.write_text("\n".join([*lines, json.dumps(orphan)]) + "\n")

    with caplog.at_level(logging.WARNING):
        entries = list(readers.read(PI, path))

    assert entries == []
    assert any("dddd9999" in record.getMessage() for record in caplog.records)


def test_damage_to_a_record_off_the_branch_leaves_the_read_alone(tmp_path, caplog):
    # 2bef3173 is the user turn as it stood before /tree re-submitted it, so it sits on
    # the branch the walk dropped. Whether a record is part of the conversation this
    # read hands over is what decides it, and a dropped record is not.
    path = patch_record(
        "pi-resumed.jsonl", "2bef3173", tmp_path, "pi-dead-branch.jsonl", timestamp=DROP
    )

    with caplog.at_level(logging.WARNING):
        entries = list(readers.read(PI, path))

    assert ids(entries) == ["8538c366", "b0f7bd00", "17f1b5d4", "2f255160"]
    assert not caplog.records


def test_damage_to_a_record_the_branch_kept_is_reported(tmp_path):
    # The same field gone from 2f255160, which the walk did keep: the conversation being
    # handed over holds a record no entry can be built from, which is a changed format.
    path = patch_record(
        "pi-resumed.jsonl", "2f255160", tmp_path, "pi-live-branch.jsonl", timestamp=DROP
    )

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(PI, path))


def test_a_record_id_that_is_not_a_name_is_reported(tmp_path):
    # pi names every record with a string id, so an id the walk cannot place on any
    # branch is not one it dropped. Capture is told to retry the reader error, not a
    # type error it was never given.
    path = patch_record(
        "pi-session.jsonl", "61afebc7", tmp_path, "pi-id-not-a-name.jsonl", id={"value": "a2"}
    )

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(PI, path))


def test_a_file_of_nothing_the_reader_reads_is_reported_rather_than_empty(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"event": "session_start", "at": 1}\nnot json at all\n')

    for agent in (CLAUDE, PI):
        with pytest.raises(readers.UnrecognizedTranscript) as failure:
            list(readers.read(agent, path))
        assert str(path) in str(failure.value)


def test_a_session_file_nothing_written_to_yet_is_simply_empty(tmp_path):
    for name, text in (("empty.jsonl", ""), ("blank lines.jsonl", "\n \n\t\n")):
        path = tmp_path / name
        path.write_text(text)
        for agent in (CLAUDE, PI):
            assert list(readers.read(agent, path)) == []


def test_a_session_of_only_bookkeeping_before_the_first_turn_is_empty(tmp_path):
    # Both agents write records before anyone has said anything: Claude Code its queue
    # and title entries, pi its header, model change and the system prompt holding the
    # instructions. None of them is conversation the reader failed to read, so the
    # session is empty and not reported.
    claude_only = [
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "sessionId": "0dcbf5a2-1b06-5f0e-9cbb-0af0e0d0a991",
            "timestamp": "2026-09-27T05:00:00.000Z",
            "content": "wake up",
        },
        {"type": "custom-title", "customTitle": "Watering scheduler", "sessionId": "0dcbf5a2"},
        {"type": "mode", "mode": "normal", "sessionId": "0dcbf5a2"},
    ]
    pi_only = [
        {
            "type": "session",
            "id": "7be69a6f-a36a-507b-b4b7-6cc754237d29",
            "timestamp": "2026-09-27T04:59:00.000Z",
            "cwd": "/home/dev/projects/garden",
            "version": 3,
        },
        {
            "type": "model_change",
            "id": "5e33d511",
            "parentId": None,
            "timestamp": "2026-09-27T04:59:01.000Z",
            "provider": "mock",
            "modelId": "mock-coder",
        },
        {
            "type": "message",
            "id": "c354ef8c",
            "parentId": "5e33d511",
            "timestamp": "2026-09-27T04:59:02.000Z",
            "message": {"role": "system", "content": "You are a coding agent."},
        },
    ]

    for agent, records in ((CLAUDE, claude_only), (PI, pi_only)):
        path = tmp_path / f"{agent}-bookkeeping.jsonl"
        path.write_text("\n".join(json.dumps(record) for record in records) + "\n")
        assert list(readers.read(agent, path)) == []


def test_a_renamed_pi_field_is_reported_not_read_as_empty(tmp_path):
    # pi names a record's time `timestamp` and the reader reads no entry without it, so
    # a transcript that renamed the field still holds its conversation unreadably.
    text = (FIXTURES / "pi-compaction.jsonl").read_text()
    records = [json.loads(line) for line in text.splitlines()]
    for record in records:
        record["ts"] = record.pop("timestamp")
    path = tmp_path / "pi-renamed-timestamp.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(PI, path))


def test_a_renamed_claude_field_is_reported_not_read_as_empty(tmp_path):
    # The Claude reader identifies an entry by the record's uuid, so a transcript that
    # renamed that field yields nothing while holding a whole conversation.
    text = (FIXTURES / "claude-session.jsonl").read_text()
    records = [json.loads(line) for line in text.splitlines()]
    for record in records:
        if "uuid" in record:
            record["identifier"] = record.pop("uuid")
    path = tmp_path / "claude-renamed-uuid.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(CLAUDE, path))


@pytest.mark.parametrize(
    "fixture, container, field",
    [
        ("claude-session.jsonl", "message", "content"),
        ("claude-session.jsonl", "tool_result", "tool_use_id"),
        ("pi-session.jsonl", "message", "content"),
        ("pi-session.jsonl", "message", "role"),
        ("pi-session.jsonl", "record", "id"),
    ],
)
def test_a_renamed_field_an_entry_is_built_from_is_reported(tmp_path, fixture, container, field):
    # An entry is built from the fields of the message and of the blocks inside it, so a
    # transcript that renamed one still holds its conversation, unreadably.
    records = [json.loads(line) for line in (FIXTURES / fixture).read_text().splitlines()]
    for record in records:
        for part in parts_holding(record, container):
            if field in part:
                part["renamed_" + field] = part.pop(field)
    path = tmp_path / f"{fixture}-{container}-{field}-renamed.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")

    with pytest.raises(readers.UnrecognizedTranscript):
        list(readers.read(agent_of(fixture), path))


def test_a_well_formed_session_reads_without_a_warning(caplog):
    # A broken parent chain is damage, not an ordinary shape, so every fixture - each
    # one a transcript in the form its agent really writes - must read back silent.
    with caplog.at_level(logging.WARNING):
        for fixture in sorted(FIXTURES.glob("*.jsonl")):
            read(agent_of(fixture.name), fixture.name)

    assert not caplog.records


def test_an_unknown_agent_is_reported_at_once():
    with pytest.raises(readers.ReaderError):
        readers.read("cursor", FIXTURES / "pi-session.jsonl")


def test_an_entry_is_a_frozen_record_of_the_pinned_fields():
    names = [f.name for f in dataclasses.fields(readers.Entry)]
    assert names == ["id", "time", "role", "text", "is_error"]
    entry = read(CLAUDE, "claude-session.jsonl")[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.text = "changed"


def test_claude_slash_command_prompt_renders_as_typed(tmp_path):
    # F45: The Claude Code reader renders a slash-command prompt as the text typed, /<name> <args>.
    records = [
        # The exact Claude Code prompt text that failed
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-message>aft</command-message>\n"
                    "<command-name>/aft</command-name>\n"
                    "<command-args>off</command-args>"
                ),
            },
        },
        # The exact on prompt
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-message>aft</command-message>\n"
                    "<command-name>/aft</command-name>\n"
                    "<command-args>on</command-args>"
                ),
            },
        },
        # Command without arguments
        {
            "type": "user",
            "uuid": "u3",
            "timestamp": "2026-10-03T10:02:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-name>/clear</command-name>\n"
                    "            <command-message>clear</command-message>\n"
                    "            <command-args></command-args>"
                ),
            },
        },
        # Command with other arguments
        {
            "type": "user",
            "uuid": "u4",
            "timestamp": "2026-10-03T10:03:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-name>/plan</command-name>\n"
                    "<command-args>rewrite scheduler</command-args>"
                ),
            },
        },
        # Text typed alongside command is not dropped
        {
            "type": "user",
            "uuid": "u5",
            "timestamp": "2026-10-03T10:04:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "before command\n"
                    "<command-name>/aft</command-name>\n"
                    "<command-args>off</command-args>\n"
                    "after command"
                ),
            },
        },
        # Ordinary prompt is byte-identical
        {
            "type": "user",
            "uuid": "u6",
            "timestamp": "2026-10-03T10:05:00.000Z",
            "message": {
                "role": "user",
                "content": "ordinary prompt without slash command",
            },
        },
    ]
    path = tmp_path / "claude-slash-commands.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    entries = list(readers.read(CLAUDE, path))
    assert [e.text for e in entries] == [
        "/aft off",
        "/aft on",
        "/clear",
        "/plan rewrite scheduler",
        "before command\n/aft off\nafter command",
        "ordinary prompt without slash command",
    ]


def test_pi_skill_call_renders_without_skill_body(tmp_path):
    # F45: The pi reader renders a skill call as /<name> <args>, without the skill body.
    records = [
        # Skill call for off
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/home/user/.agents/skills/aft/SKILL.md">\n'
                    "References are relative to /home/user/.agents/skills/aft.\n\n"
                    "# aft\nTurn capture on or off.\n"
                    "</skill>\n\n"
                    "off"
                ),
            },
        },
        # Skill call for on
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/home/user/.agents/skills/aft/SKILL.md">\n'
                    "References are relative to /home/user/.agents/skills/aft.\n\n"
                    "# aft\nTurn capture on or off.\n"
                    "</skill>\n\n"
                    "on"
                ),
            },
        },
        # Skill call without arguments
        {
            "type": "message",
            "id": "m3",
            "parentId": "m2",
            "timestamp": "2026-10-03T10:02:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/home/user/.agents/skills/aft/SKILL.md">\n'
                    "References are relative to /home/user/.agents/skills/aft.\n\n"
                    "# aft\nTurn capture on or off.\n"
                    "</skill>"
                ),
            },
        },
        # Skill call with other arguments
        {
            "type": "message",
            "id": "m4",
            "parentId": "m3",
            "timestamp": "2026-10-03T10:03:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="summarize" location="/path/to/SKILL.md">\n'
                    "References are relative to /path/to.\n\n"
                    "# summarize\nSummary skill\n"
                    "</skill>\n\n"
                    "src/aft/filter.py"
                ),
            },
        },
        # Multiline args alongside skill call
        {
            "type": "message",
            "id": "m5",
            "parentId": "m4",
            "timestamp": "2026-10-03T10:04:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/path/to/SKILL.md">\n'
                    "body\n"
                    "</skill>\n\n"
                    "off\nand also keep this line"
                ),
            },
        },
        # Ordinary prompt is byte-identical
        {
            "type": "message",
            "id": "m6",
            "parentId": "m5",
            "timestamp": "2026-10-03T10:05:00.000Z",
            "message": {
                "role": "user",
                "content": "ordinary prompt without skill",
            },
        },
        # Ordinary prompt mentioning <skill name="foo"> mid-sentence is byte-identical
        {
            "type": "message",
            "id": "m7",
            "parentId": "m6",
            "timestamp": "2026-10-03T10:06:00.000Z",
            "message": {
                "role": "user",
                "content": 'referencing <skill name="foo">bar</skill> in text',
            },
        },
    ]
    path = tmp_path / "pi-skill-calls.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    entries = list(readers.read(PI, path))
    assert [e.text for e in entries] == [
        "/aft off",
        "/aft on",
        "/aft",
        "/summarize src/aft/filter.py",
        "/aft off\nand also keep this line",
        "ordinary prompt without skill",
        'referencing <skill name="foo">bar</skill> in text',
    ]


def test_claude_slash_markers_open_and_close_span(tmp_path, cfg):
    # F45: Exact Claude Code markup for off and for on opens and closes a span.
    records = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {"role": "user", "content": "the plan"},
        },
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-message>aft</command-message>\n"
                    "<command-name>/aft</command-name>\n"
                    "<command-args>off</command-args>"
                ),
            },
        },
        {
            "type": "user",
            "uuid": "u3",
            "timestamp": "2026-10-03T10:02:00.000Z",
            "message": {"role": "user", "content": "the staging host is bellwether"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-10-03T10:02:05.000Z",
            "message": {"role": "assistant", "content": "noted"},
        },
        {
            "type": "user",
            "uuid": "u4",
            "timestamp": "2026-10-03T10:03:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-message>aft</command-message>\n"
                    "<command-name>/aft</command-name>\n"
                    "<command-args>on</command-args>"
                ),
            },
        },
        {
            "type": "assistant",
            "uuid": "a2",
            "timestamp": "2026-10-03T10:03:05.000Z",
            "message": {"role": "assistant", "content": "back to it"},
        },
    ]
    path = tmp_path / "claude-markers.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    entries = list(readers.read(CLAUDE, path))
    kept, off = filtermod.filter_entries(entries, cfg)
    assert not off
    assert [e.text for e in kept] == ["the plan", "back to it"]


def test_pi_skill_markers_open_and_close_span(tmp_path, cfg):
    # F45: Exact pi skill call for off and for on opens and closes a span.
    records = [
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {"role": "user", "content": "the plan"},
        },
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/home/user/.agents/skills/aft/SKILL.md">\n'
                    "References are relative to /home/user/.agents/skills/aft.\n\n"
                    "# aft\nControl capture.\n"
                    "</skill>\n\n"
                    "off"
                ),
            },
        },
        {
            "type": "message",
            "id": "m3",
            "parentId": "m2",
            "timestamp": "2026-10-03T10:02:00.000Z",
            "message": {"role": "user", "content": "the staging host is bellwether"},
        },
        {
            "type": "message",
            "id": "m4",
            "parentId": "m3",
            "timestamp": "2026-10-03T10:02:05.000Z",
            "message": {"role": "assistant", "content": "noted"},
        },
        {
            "type": "message",
            "id": "m5",
            "parentId": "m4",
            "timestamp": "2026-10-03T10:03:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/home/user/.agents/skills/aft/SKILL.md">\n'
                    "References are relative to /home/user/.agents/skills/aft.\n\n"
                    "# aft\nControl capture.\n"
                    "</skill>\n\n"
                    "on"
                ),
            },
        },
        {
            "type": "message",
            "id": "m6",
            "parentId": "m5",
            "timestamp": "2026-10-03T10:03:05.000Z",
            "message": {"role": "assistant", "content": "back to it"},
        },
    ]
    path = tmp_path / "pi-markers.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")

    entries = list(readers.read(PI, path))
    kept, off = filtermod.filter_entries(entries, cfg)
    assert not off
    assert [e.text for e in kept] == ["the plan", "back to it"]


def test_slash_aft_offline_and_other_args_do_not_start_a_span(tmp_path, cfg):
    # F45: /aft offline and a slash command with other arguments do not start a span.
    claude_records = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-name>/aft</command-name>\n<command-args>offline</command-args>"
                ),
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-name>/plan</command-name>\n<command-args>write docs</command-args>"
                ),
            },
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-10-03T10:01:05.000Z",
            "message": {"role": "assistant", "content": "working on docs"},
        },
    ]
    c_path = tmp_path / "claude-offline.jsonl"
    c_path.write_text("\n".join(json.dumps(r) for r in claude_records) + "\n")
    c_entries = list(readers.read(CLAUDE, c_path))
    c_kept, c_off = filtermod.filter_entries(c_entries, cfg)
    assert not c_off
    assert [e.text for e in c_kept] == [
        "/aft offline",
        "/plan write docs",
        "working on docs",
    ]

    pi_records = [
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="aft" location="/path/to/SKILL.md">\nbody\n</skill>\n\noffline'
                ),
            },
        },
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "timestamp": "2026-10-03T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="summarize" location="/path/to/SKILL.md">\n'
                    "body\n"
                    "</skill>\n\n"
                    "README.md"
                ),
            },
        },
        {
            "type": "message",
            "id": "m3",
            "parentId": "m2",
            "timestamp": "2026-10-03T10:01:05.000Z",
            "message": {"role": "assistant", "content": "summarized"},
        },
    ]
    p_path = tmp_path / "pi-offline.jsonl"
    p_path.write_text("\n".join(json.dumps(r) for r in pi_records) + "\n")
    p_entries = list(readers.read(PI, p_path))
    p_kept, p_off = filtermod.filter_entries(p_entries, cfg)
    assert not p_off
    assert [e.text for e in p_kept] == [
        "/aft offline",
        "/summarize README.md",
        "summarized",
    ]


def test_archived_slash_command_prompt_reads_as_slash_name_args(tmp_path, cfg):
    # F45: An archived slash-command prompt reads as /<name> <args>.
    c_records = [
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    "<command-name>/plan</command-name>\n"
                    "<command-args>rewrite scheduler</command-args>"
                ),
            },
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "timestamp": "2026-10-03T10:00:05.000Z",
            "message": {"role": "assistant", "content": "planning rewrite"},
        },
    ]
    c_path = tmp_path / "claude-archived-cmd.jsonl"
    c_path.write_text("\n".join(json.dumps(r) for r in c_records) + "\n")
    c_entries = list(readers.read(CLAUDE, c_path))
    c_kept, _ = filtermod.filter_entries(c_entries, cfg)
    evidence.store(cfg, "claude", "sess-c", None, c_kept)

    archived_c = evidence.entries(cfg, "sess-c")
    assert archived_c[0].text == "/plan rewrite scheduler"

    p_records = [
        {
            "type": "message",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-10-03T10:00:00.000Z",
            "message": {
                "role": "user",
                "content": (
                    '<skill name="summarize" location="/path/to/SKILL.md">\n'
                    "body\n"
                    "</skill>\n\n"
                    "src/aft/filter.py"
                ),
            },
        },
        {
            "type": "message",
            "id": "m2",
            "parentId": "m1",
            "timestamp": "2026-10-03T10:00:05.000Z",
            "message": {"role": "assistant", "content": "summarized"},
        },
    ]
    p_path = tmp_path / "pi-archived-cmd.jsonl"
    p_path.write_text("\n".join(json.dumps(r) for r in p_records) + "\n")
    p_entries = list(readers.read(PI, p_path))
    p_kept, _ = filtermod.filter_entries(p_entries, cfg)
    evidence.store(cfg, "pi", "sess-p", None, p_kept)

    archived_p = evidence.entries(cfg, "sess-p")
    assert archived_p[0].text == "/summarize src/aft/filter.py"
