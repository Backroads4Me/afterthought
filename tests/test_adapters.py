"""The adapters and the hook command: every fixture parses, and a bad hook stays silent."""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aft import config as configmod
from aft.adapters import PARSERS
from aft.adapters.claude import parse_claude
from aft.adapters.pi import parse_pi
from aft.cli import HOOK_TIMEOUT_SECONDS, main
from aft.events import AdapterError, SessionEvent

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"
NOW = datetime(2026, 6, 30, 14, 22, 8, 517000, tzinfo=UTC)

CWD = Path("/home/ted/src/widget")
CLAUDE_ID = "6b1c2f6f-6d1e-4f2a-9d3c-7f2f8c1e5a52"
CLAUDE_TRANSCRIPT = Path(f"/home/ted/.claude/projects/-home-ted-src-widget/{CLAUDE_ID}.jsonl")
PI_ID = "019f17a3-2b64-7a1f-9c2e-4f6d8a1b3c5d"
PI_SESSION_DIR = "/home/ted/.pi/agent/sessions/--home-ted-src-widget--"
PI_TRANSCRIPT = Path(f"{PI_SESSION_DIR}/2026-06-30T14-22-08-517Z_{PI_ID}.jsonl")

# fixture, agent, the event name that agent fires, the event afterthought must build
CASES = [
    (
        "claude-session-start.json",
        "claude",
        "SessionStart",
        SessionEvent("claude", "session_start", CLAUDE_ID, CWD, CLAUDE_TRANSCRIPT, NOW, "startup"),
    ),
    (
        "claude-session-start-compact.json",
        "claude",
        "SessionStart",
        SessionEvent("claude", "session_start", CLAUDE_ID, CWD, CLAUDE_TRANSCRIPT, NOW, "compact"),
    ),
    (
        "claude-pre-compact.json",
        "claude",
        "PreCompact",
        SessionEvent("claude", "compaction", CLAUDE_ID, CWD, CLAUDE_TRANSCRIPT, NOW, "auto"),
    ),
    (
        "claude-session-end.json",
        "claude",
        "SessionEnd",
        SessionEvent(
            "claude", "session_end", CLAUDE_ID, CWD, CLAUDE_TRANSCRIPT, NOW, "prompt_input_exit"
        ),
    ),
    (
        "pi-session-start.json",
        "pi",
        "session_start",
        SessionEvent("pi", "session_start", PI_ID, CWD, PI_TRANSCRIPT, NOW, "startup"),
    ),
    (
        "pi-session-before-compact.json",
        "pi",
        "session_before_compact",
        SessionEvent("pi", "compaction", PI_ID, CWD, PI_TRANSCRIPT, NOW, "threshold"),
    ),
    (
        "pi-session-shutdown.json",
        "pi",
        "session_shutdown",
        SessionEvent("pi", "session_end", PI_ID, CWD, PI_TRANSCRIPT, NOW, "quit"),
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


def test_a_payload_naming_another_event_is_refused():
    """The event on the command line and the one in the payload have to agree."""
    with pytest.raises(AdapterError, match="payload says"):
        parse_claude("SessionStart", payload("claude-pre-compact.json"), NOW)


def test_an_unknown_event_is_refused():
    with pytest.raises(AdapterError, match=r"unknown claude hook event 'SessionNotFound'"):
        parse_claude("SessionNotFound", payload("claude-session-start.json"), NOW)


def test_malformed_json_is_silent_and_logs_one_line(home, monkeypatch, capsys):
    assert run_hook(monkeypatch, capsys, "claude", "SessionStart", "{ not json") == (0, "", "")
    lines = hook_log(home)
    assert len(lines) == 1
    assert "claude SessionStart" in lines[0]
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
