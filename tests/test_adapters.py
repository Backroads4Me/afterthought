"""The adapters and the hook command: every fixture parses, and a bad hook stays silent."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aft import config as configmod
from aft.adapters import PARSERS
from aft.adapters.claude import parse_claude
from aft.adapters.pi import parse_pi
from aft.cli import HOOK_LOG_LINE_LIMIT, HOOK_TIMEOUT_SECONDS, main
from aft.events import AdapterError, SessionEvent

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"
NOW = datetime(2026, 6, 30, 14, 22, 8, 517000, tzinfo=UTC)

# The Claude fixtures are payloads captured from a real Claude Code run, so they carry
# that capture session's id, working directory and transcript path.
CLAUDE_ID = "39eaeb4c-e03d-4847-b937-ae5e79107d95"
CLAUDE_CWD = Path(
    "/tmp/claude-1000/-home-ted-src-afterthought"
    "/75ecdcb9-0baf-4177-85c5-d35ca9eb59b2/scratchpad/hookcap/work"
)
CLAUDE_TRANSCRIPT = Path(
    "/home/ted/.claude/projects"
    "/-tmp-claude-1000--home-ted-src-afterthought"
    "-75ecdcb9-0baf-4177-85c5-d35ca9eb59b2-scratchpad-hookcap-work"
    f"/{CLAUDE_ID}.jsonl"
)
PI_ID = "019f17a3-2b64-7a1f-9c2e-4f6d8a1b3c5d"
PI_CWD = Path("/home/ted/src/widget")
PI_SESSION_DIR = "/home/ted/.pi/agent/sessions/--home-ted-src-widget--"
PI_TRANSCRIPT = Path(f"{PI_SESSION_DIR}/2026-06-30T14-22-08-517Z_{PI_ID}.jsonl")

# fixture, agent, the event name that agent fires, the event afterthought must build
CASES = [
    (
        "claude-session-start.json",
        "claude",
        "SessionStart",
        SessionEvent(
            "claude", "session_start", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "startup"
        ),
    ),
    (
        "claude-session-start-resume.json",
        "claude",
        "SessionStart",
        SessionEvent(
            "claude", "session_start", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "resume"
        ),
    ),
    (
        "claude-session-start-compact.json",
        "claude",
        "SessionStart",
        SessionEvent(
            "claude", "session_start", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "compact"
        ),
    ),
    (
        "claude-pre-compact.json",
        "claude",
        "PreCompact",
        SessionEvent(
            "claude", "compaction", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "manual"
        ),
    ),
    (
        "claude-session-end.json",
        "claude",
        "SessionEnd",
        SessionEvent(
            "claude", "session_end", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "other"
        ),
    ),
    (
        "pi-session-start.json",
        "pi",
        "session_start",
        SessionEvent("pi", "session_start", PI_ID, PI_CWD, PI_TRANSCRIPT, NOW, "startup"),
    ),
    (
        "pi-session-before-compact.json",
        "pi",
        "session_before_compact",
        SessionEvent("pi", "compaction", PI_ID, PI_CWD, PI_TRANSCRIPT, NOW, "threshold"),
    ),
    (
        "pi-session-shutdown.json",
        "pi",
        "session_shutdown",
        SessionEvent("pi", "session_end", PI_ID, PI_CWD, PI_TRANSCRIPT, NOW, "quit"),
    ),
]

CASE_IDS = [case[0] for case in CASES]


def payload_text(name: str) -> str:
    return (FIXTURES / name).read_text()


def payload(name: str) -> dict:
    return json.loads(payload_text(name))


def run_hook(monkeypatch, capsys, agent: str, event: str, stdin_text: str) -> tuple[int, str, str]:
    """`aft hook <agent> <event>` with `stdin_text` on stdin, as the agent would run it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = main(["hook", agent, event])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def hook_log(home: Path) -> list[str]:
    log = home / "logs" / "hooks.log"
    return log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(("name", "agent", "hook_event", "expected"), CASES, ids=CASE_IDS)
def test_each_fixture_parses_to_the_expected_event(name, agent, hook_event, expected):
    assert PARSERS[agent](hook_event, payload(name), NOW) == expected


@pytest.mark.parametrize(
    ("name", "agent", "hook_event"), [case[:3] for case in CASES], ids=CASE_IDS
)
def test_the_hook_takes_every_fixture(home, monkeypatch, capsys, name, agent, hook_event):
    assert run_hook(monkeypatch, capsys, agent, hook_event, payload_text(name)) == (0, "", "")
    assert hook_log(home) == []


def test_claude_session_start_without_a_transcript_parses():
    without_transcript = payload("claude-session-start.json") | {"transcript_path": None}
    assert parse_claude("SessionStart", without_transcript, NOW).transcript_path is None


def test_claude_non_start_without_a_transcript_is_refused():
    without_transcript = payload("claude-session-end.json") | {"transcript_path": None}
    with pytest.raises(AdapterError, match="transcript_path"):
        parse_claude("SessionEnd", without_transcript, NOW)


def test_pi_before_its_session_is_written_has_no_transcript():
    without_transcript = payload("pi-session-start.json") | {"transcript_path": None}
    assert parse_pi("session_start", without_transcript, NOW).transcript_path is None


def test_an_optional_detail_of_the_wrong_type_reads_as_absent(home, monkeypatch, capsys):
    """Only the fields the plan requires may refuse an event, and detail is not one."""
    wrong_type = payload("claude-session-end.json") | {"reason": 2}

    assert parse_claude("SessionEnd", wrong_type, NOW).detail is None
    assert run_hook(monkeypatch, capsys, "claude", "SessionEnd", json.dumps(wrong_type)) == (
        0,
        "",
        "",
    )
    assert hook_log(home) == []


def test_pi_reporting_an_empty_session_file_has_no_transcript(home, monkeypatch, capsys):
    """A session file pi has not written yet reads as no transcript, not as a refused event."""
    empty = payload("pi-session-start.json") | {"transcript_path": ""}

    assert parse_pi("session_start", empty, NOW).transcript_path is None
    assert run_hook(monkeypatch, capsys, "pi", "session_start", json.dumps(empty)) == (0, "", "")
    assert hook_log(home) == []


def test_fields_the_parser_does_not_know_are_ignored():
    """A captured payload carries more than afterthought reads; extra fields are
    ignored, never a reason to refuse an event the payload does describe."""
    captured = payload("claude-session-start-resume.json")
    known = {key: captured[key] for key in ("session_id", "transcript_path", "cwd", "source")}
    assert set(captured) - set(known) == {
        "hook_event_name",
        "scratchpad_dir",
        "seconds_since_last_response",
        "context_tokens",
        "prompt_cache_likely_expired",
        "estimated_cache_write_usd",
    }
    expected = SessionEvent(
        "claude", "session_start", CLAUDE_ID, CLAUDE_CWD, CLAUDE_TRANSCRIPT, NOW, "resume"
    )

    assert parse_claude("SessionStart", captured, NOW) == expected
    assert parse_claude("SessionStart", known, NOW) == expected


def test_an_unknown_event_is_refused():
    with pytest.raises(AdapterError, match=r"unknown claude hook event 'SessionNotFound'"):
        parse_claude("SessionNotFound", payload("claude-session-start.json"), NOW)


def test_malformed_json_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "claude", "SessionStart", "{ not json") == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert "claude" in lines[0]
    assert "SessionStart" in lines[0]
    assert "JSONDecodeError" in lines[0]


def test_a_missing_field_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    stripped = payload("pi-session-shutdown.json") | {"session_id": ""}
    assert run_hook(monkeypatch, capsys, "pi", "session_shutdown", json.dumps(stripped)) == (
        0,
        "",
        "",
    )
    lines = hook_log(home)
    assert len(lines) == 1
    assert "session_id" in lines[0]


def test_an_unknown_event_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    text = payload_text("claude-session-start.json")
    assert run_hook(monkeypatch, capsys, "claude", "PostToolUse", text) == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert "unknown claude hook event 'PostToolUse'" in lines[0]


def test_the_failure_line_stays_one_bounded_line(home, monkeypatch, capsys):
    """Agent, event and the error's own words come from outside: one bounded line that
    still names the failure."""
    text = payload_text("claude-session-start.json")
    event = "SessionStart\n" + "x" * 600

    assert run_hook(monkeypatch, capsys, "claude", event, text) == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert len(lines[0]) <= HOOK_LOG_LINE_LIMIT
    assert "AdapterError" in lines[0]


def test_a_long_agent_argument_leaves_one_bounded_failure_line(home, monkeypatch, capsys):
    """Neither caller-supplied argument may push the cause out of the record."""
    text = payload_text("claude-session-start.json")
    agent = "codex" + "y" * 600

    assert run_hook(monkeypatch, capsys, agent, "SessionStart", text) == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert len(lines[0]) <= HOOK_LOG_LINE_LIMIT
    assert "AdapterError" in lines[0]
    assert "unknown agent" in lines[0]


def test_an_unknown_agent_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    text = payload_text("claude-session-start.json")
    assert run_hook(monkeypatch, capsys, "codex", "SessionStart", text) == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert "unknown agent 'codex'" in lines[0]


def test_a_payload_that_is_not_an_object_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "pi", "session_start", "[1, 2]") == (0, "", "")
    assert "not a JSON object" in hook_log(home)[0]


def test_the_hook_needs_no_config_and_no_vault(tmp_path, monkeypatch, capsys):
    """A hook can fire before `aft init` has run anywhere on the machine."""
    home = tmp_path / "never-initialized"
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    # With no config the vault defaults to ~/afterthought, so HOME moves too: the vault this
    # hook looks for is one under tmp_path that nobody created.
    monkeypatch.setenv("HOME", str(tmp_path))
    text = payload_text("pi-session-start.json")
    assert run_hook(monkeypatch, capsys, "pi", "session_start", text) == (0, "", "")
    assert not (home / "logs").exists()


def test_a_home_that_cannot_be_resolved_is_silent_and_makes_no_directories(
    tmp_path, monkeypatch, capsys
):
    """A container can run the hook with no home at all; that is still silence and 0."""
    monkeypatch.chdir(tmp_path)
    for name in (configmod.HOME_ENV, "HOME", "LOGNAME", "USER"):
        monkeypatch.delenv(name, raising=False)
    # A uid with no passwd entry, as a container running as a random user has.
    monkeypatch.setattr(os, "getuid", lambda: 999_999)

    assert run_hook(monkeypatch, capsys, "claude", "SessionStart", "{ not json") == (0, "", "")
    assert list(tmp_path.iterdir()) == []


# The whole hook gets one bound (HOOK_TIMEOUT_SECONDS, BUILD-PLAN M1.1), split up front
# between its two blocking steps. A test that consumes the full bound allows this much
# scheduling slack; a test of a single blocking step is asserted against the bound itself,
# so paying the bound once per step fails it.
HOOK_TIMING_SLACK_SECONDS = 1.0


class StalledStdin:
    """A pipe that never delivers a payload, which is how a hung caller would look."""

    def read(self, size: int = -1) -> str:
        time.sleep(10)
        return ""


def test_a_blocked_log_write_gives_up_inside_the_budget(home, monkeypatch, capsys):
    """The budget covers the last thing the hook does: the failure record itself."""
    record = home / "logs" / "hooks.log"
    (home / "logs").mkdir()
    os.mkfifo(record)

    def unblock_someday() -> None:
        time.sleep(10)
        record.open("r").close()

    threading.Thread(target=unblock_someday, daemon=True).start()

    started = time.monotonic()
    result = run_hook(monkeypatch, capsys, "claude", "SessionStart", "{ not json")
    elapsed = time.monotonic() - started

    assert result == (0, "", "")
    assert elapsed < HOOK_TIMEOUT_SECONDS


def test_a_stalled_stdin_still_writes_its_failure_line_inside_the_budget(home, monkeypatch, capsys):
    """The read spending its share cannot cost the failure line the remainder held back
    for it."""
    monkeypatch.setattr(sys, "stdin", StalledStdin())
    started = time.monotonic()
    code = main(["hook", "claude", "SessionStart"])
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert (code, captured.out, captured.err) == (0, "", "")
    assert elapsed < HOOK_TIMEOUT_SECONDS + HOOK_TIMING_SLACK_SECONDS


def test_a_busy_queue_on_session_end_returns_inside_the_budget(home, monkeypatch, capsys):
    """When another process holds the queue write lock, the hook returns within its 2-second
    budget, and logs the database locked failure (BUILD-PLAN F23, M1 review 6)."""
    from aft.queue import _connect

    conn = _connect(configmod.load())
    conn.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        code, out, err = run_hook(
            monkeypatch, capsys, "claude", "SessionEnd", payload_text("claude-session-end.json")
        )
        elapsed = time.monotonic() - started
    finally:
        conn.rollback()
        conn.close()

    assert (code, out, err) == (0, "", "")
    assert elapsed < HOOK_TIMEOUT_SECONDS, (
        f"took {elapsed:.2f} s against budget of {HOOK_TIMEOUT_SECONDS} s"
    )
    lines = hook_log(home)
    assert len(lines) == 1
    assert "OperationalError: database is locked" in lines[0]


def test_a_stalled_stdin_and_a_blocked_write_share_one_budget(home, monkeypatch, capsys):
    """Both blocking steps at once still fit the one bound the plan states: the budget is
    split between them up front, so it is not paid once per step."""
    record = home / "logs" / "hooks.log"
    (home / "logs").mkdir()
    os.mkfifo(record)
    # No reader ever opens the fifo, so only the write's own share can end the open.

    monkeypatch.setattr(sys, "stdin", StalledStdin())
    started = time.monotonic()
    code = main(["hook", "claude", "SessionStart"])
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert (code, captured.out, captured.err) == (0, "", "")
    assert elapsed < HOOK_TIMEOUT_SECONDS + HOOK_TIMING_SLACK_SECONDS


@pytest.mark.parametrize(
    ("name", "agent", "hook_event"),
    [
        ("claude-pre-compact.json", "claude", "PreCompact"),
        ("claude-session-end.json", "claude", "SessionEnd"),
        ("pi-session-shutdown.json", "pi", "session_shutdown"),
    ],
)
def test_a_capture_event_writes_one_queue_row(home, monkeypatch, capsys, name, agent, hook_event):
    """Compaction and session end are capture work: the hook leaves one pending row."""
    assert run_hook(monkeypatch, capsys, agent, hook_event, payload_text(name)) == (0, "", "")
    conn = sqlite3.connect(home / "queue.sqlite")
    try:
        rows = conn.execute("SELECT agent, status FROM jobs").fetchall()
    finally:
        conn.close()
    assert rows == [(agent, "pending")]


def test_a_session_start_prints_the_block_and_writes_no_queue_row(
    initialized, home, tmp_path, monkeypatch, capsys
):
    """A session start is injection's: it prints what `aft inject` prints for the event's
    directory, and capture hears nothing of it."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    assert main(["inject", str(cwd)]) == 0
    block = capsys.readouterr().out
    assert block

    text = json.dumps(payload("claude-session-start.json") | {"cwd": str(cwd)})
    assert run_hook(monkeypatch, capsys, "claude", "SessionStart", text) == (0, block, "")
    assert not (home / "queue.sqlite").exists()
    assert hook_log(home) == []


def test_session_end_hook_never_calls_systemd_with_isolated_home(home, monkeypatch, capsys):
    """When running a hook with an isolated AFT_HOME, SessionEnd never calls systemd
    (BUILD-PLAN F23)."""
    calls: list[list[str]] = []
    monkeypatch.setattr("aft.install.systemd._default_runner", lambda cmd: calls.append(cmd))
    assert run_hook(
        monkeypatch, capsys, "claude", "SessionEnd", payload_text("claude-session-end.json")
    ) == (0, "", "")
    assert calls == []
    assert not (home / "last-work-trigger").exists()
