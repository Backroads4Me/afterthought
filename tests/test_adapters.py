"""The adapters and the hook command: every fixture parses, and a bad hook stays silent."""

from __future__ import annotations

import io
import json
import os
import sys
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


def test_claude_without_a_transcript_is_refused():
    without_transcript = payload("claude-session-start.json") | {"transcript_path": None}
    with pytest.raises(AdapterError, match="transcript_path"):
        parse_claude("SessionStart", without_transcript, NOW)


def test_pi_before_its_session_is_written_has_no_transcript():
    without_transcript = payload("pi-session-start.json") | {"transcript_path": None}
    assert parse_pi("session_start", without_transcript, NOW).transcript_path is None


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


class StalledStdin:
    """A pipe that never delivers a payload, which is how a hung caller would look."""

    def read(self, size: int = -1) -> str:
        time.sleep(10)
        return ""


def test_a_stalled_stdin_gives_up_inside_the_budget(home, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", StalledStdin())
    started = time.monotonic()
    code = main(["hook", "claude", "SessionStart"])
    elapsed = time.monotonic() - started
    captured = capsys.readouterr()

    assert code == 0
    assert (captured.out, captured.err) == ("", "")
    assert elapsed < HOOK_TIMEOUT_SECONDS + 2
    assert "TimeoutError" in hook_log(home)[0]
