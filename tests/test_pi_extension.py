"""What afterthought's pi extension does (BUILD-PLAN M1.4).

The extension is TypeScript, so these tests drive it as a module: a fake ``pi`` collects the
handlers it registers, and a fake ``aft`` on ``PATH`` records the payload each event hands to
``aft hook pi <event>`` and prints the block the extension is meant to keep. Node loads the
``.ts`` file directly, which is how pi itself runs it.

Two limits here are deliberate. The block comes from the fake rather than from ``aft inject``
(M1.2), because what is under test is the plumbing that carries a block from a hook to a
prompt, not the block's contents; and the live check of BUILD-PLAN 6.1, a real pi session
reading real memory, belongs to the captain and is not run here.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "integrations" / "pi" / "afterthought.ts"
SESSION_ID = "019f17a3-2b64-7a1f-9c2e-4f6d8a1b3c5d"
SECTION = "afterthought"
EVENTS = ("session_start", "session_before_compact", "session_shutdown")
# A session file pi would report, in the synthetic style of tests/fixtures/hooks: nothing here
# writes a session, the payload only has to carry a path.
SESSION_FILE = "/sessions/afterthought-example/2026-06-30T14-22-08-517Z_example.jsonl"

FAKE_AFT = """#!/usr/bin/env python3
\"\"\"Stand-in for `aft hook pi <event>`: records what it was handed, prints what it is told.\"\"\"
import json
import os
import sys
import time

argv = sys.argv[1:]
raw = sys.stdin.read()
with open(os.environ["AFT_RECORD"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": raw}) + "\\n")

event = argv[-1] if argv else ""
sleeps = os.environ.get("AFT_SLEEP_EVENT", "")
if event in [name.strip() for name in sleeps.split(",") if name.strip()]:
    time.sleep(float(os.environ.get("AFT_SLEEP", "0")))

printed = os.environ.get("AFT_PRINT", "")
if event in [name.strip() for name in printed.split(",") if name.strip()]:
    print(f"MEMORY BLOCK for {event}")
"""

HARNESS = """
import { pathToFileURL } from "node:url"

const [extensionPath, sessionId, cwd, sessionFile] = process.argv.slice(2)
const { default: setup } = await import(pathToFileURL(extensionPath).href)

const handlers = new Map()
await setup({ on: (event, handler) => handlers.set(event, handler) })

// pi's session manager reports no session file at all until the session is first written.
const ctx = {
  sessionManager: {
    getSessionId: () => sessionId,
    getCwd: () => cwd,
    getSessionFile: () => (sessionFile === "" ? undefined : sessionFile),
  },
}

// One prompt state, holding a section of pi's own, as before_agent_start is handed it.
const systemPromptOptions = { cwd, sections: { cwd } }
const report = { registered: [...handlers.keys()], forceSystemPrompt: null }

await handlers.get("session_start")({ type: "session_start", reason: "startup" }, ctx)
report.startReturnedNothing =
  (await handlers.get("before_agent_start")(
    { type: "before_agent_start", prompt: "hello", systemPromptOptions },
    ctx,
  )) === undefined
report.afterStart = { ...systemPromptOptions.sections }
report.forceSystemPrompt = systemPromptOptions.forceSystemPrompt ?? null

await handlers.get("session_before_compact")(
  { type: "session_before_compact", reason: "manual" },
  ctx,
)
await handlers.get("before_agent_start")(
  { type: "before_agent_start", prompt: "again", systemPromptOptions },
  ctx,
)
report.afterCompact = { ...systemPromptOptions.sections }

const began = Date.now()
report.shutdownReturnedNothing =
  (await handlers.get("session_shutdown")({ type: "session_shutdown", reason: "quit" }, ctx)) ===
  undefined
report.shutdownMs = Date.now() - began

console.log("AFT_HARNESS_JSON:" + JSON.stringify(report))
"""


def node() -> list[str]:
    """The command that runs a ``.ts`` file directly, skipping the host that cannot."""
    exe = shutil.which("node")
    if exe is None:
        pytest.skip("the pi extension is TypeScript, and no node is on PATH to load it")
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True)
    major, minor = (int(part) for part in version.stdout.lstrip("v").split(".")[:2])
    if (major, minor) < (22, 6):
        pytest.skip(f"node {major}.{minor} cannot load a .ts file without a build step")
    return [exe] if major >= 23 else [exe, "--experimental-strip-types"]


@pytest.fixture
def harness(tmp_path) -> Callable[..., tuple[dict, list[dict]]]:
    """Run the extension against a fake pi and a fake ``aft``, reporting what each observed.

    ``print_for`` names the events whose hook prints a block, and ``sleep_for`` the events
    whose hook sleeps past the extension's own bound, so each test chooses which half of the
    contract it is looking at.
    """
    root = tmp_path / "pi-extension"
    root.mkdir()
    script = root / "harness.mjs"
    script.write_text(HARNESS, encoding="utf-8")
    aft = root / "aft"
    aft.write_text(FAKE_AFT, encoding="utf-8")
    aft.chmod(aft.stat().st_mode | stat.S_IEXEC)
    record = root / "calls.jsonl"

    def run(
        *,
        print_for: str = ",".join(EVENTS),
        sleep_for: str = "",
        sleep_seconds: float = 0.0,
        session_file: str = SESSION_FILE,
    ) -> tuple[dict, list[dict]]:
        env = {
            **os.environ,
            "PATH": f"{root}{os.pathsep}{os.environ['PATH']}",
            "AFT_RECORD": str(record),
            "AFT_PRINT": print_for,
            "AFT_SLEEP_EVENT": sleep_for,
            "AFT_SLEEP": str(sleep_seconds),
        }
        result = subprocess.run(
            [*node(), str(script), str(EXTENSION), SESSION_ID, str(tmp_path), session_file],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert result.returncode == 0, f"the harness failed:\n{result.stdout}\n{result.stderr}"
        lines = [
            line for line in result.stdout.splitlines() if line.startswith("AFT_HARNESS_JSON:")
        ]
        assert len(lines) == 1, f"expected one report:\n{result.stdout}\n{result.stderr}"
        report = json.loads(lines[0].removeprefix("AFT_HARNESS_JSON:"))
        calls = [json.loads(row) for row in record.read_text(encoding="utf-8").splitlines()]
        for call in calls:
            call["payload"] = json.loads(call.pop("stdin"))
        return report, calls

    return run


def payload_for(calls: list[dict], event: str) -> dict:
    """The payload of the one call made for one event."""
    matched = [call for call in calls if call["argv"] == ["hook", "pi", event]]
    assert len(matched) == 1, f"expected one call for {event}, got {calls}"
    return matched[0]["payload"]


def test_the_extension_hands_each_event_to_its_own_hook_command(harness):
    report, calls = harness()

    assert set(report["registered"]) == {
        "session_start",
        "before_agent_start",
        "session_before_compact",
        "session_shutdown",
    }
    assert [call["argv"] for call in calls] == [["hook", "pi", event] for event in EVENTS]


def test_session_start_sends_its_session_id_cwd_and_session_file(harness, tmp_path):
    _, calls = harness()

    assert payload_for(calls, "session_start") == {
        "session_id": SESSION_ID,
        "cwd": str(tmp_path),
        "transcript_path": SESSION_FILE,
        "hook_event_name": "session_start",
        "reason": "startup",
    }


def test_a_session_pi_has_not_written_yet_sends_no_session_file(harness):
    _, calls = harness(session_file="")

    assert "transcript_path" not in payload_for(calls, "session_start")


def test_the_kept_block_is_added_as_its_own_section(harness, tmp_path):
    report, _ = harness()

    assert report["afterStart"] == {
        "cwd": str(tmp_path),
        SECTION: "MEMORY BLOCK for session_start",
    }


def test_adding_the_block_replaces_nothing_of_the_hosts_own_prompt(harness):
    report, _ = harness()

    assert report["startReturnedNothing"] is True
    assert report["forceSystemPrompt"] is None


def test_compaction_refreshes_the_kept_block_without_duplicating_it(harness, tmp_path):
    report, calls = harness()

    assert payload_for(calls, "session_before_compact")["reason"] == "manual"
    assert report["afterCompact"] == {
        "cwd": str(tmp_path),
        SECTION: "MEMORY BLOCK for session_before_compact",
    }


def test_a_compaction_whose_hook_prints_nothing_keeps_the_block_in_hand(harness, tmp_path):
    report, calls = harness(print_for="session_start")

    assert payload_for(calls, "session_before_compact")["reason"] == "manual"
    assert (
        report["afterCompact"]
        == report["afterStart"]
        == {
            "cwd": str(tmp_path),
            SECTION: "MEMORY BLOCK for session_start",
        }
    )


def test_shutdown_waits_two_seconds_on_a_hook_that_ignores_them(harness, tmp_path):
    report, calls = harness(sleep_for="session_shutdown", sleep_seconds=5)

    assert payload_for(calls, "session_shutdown") == {
        "session_id": SESSION_ID,
        "cwd": str(tmp_path),
        "transcript_path": SESSION_FILE,
        "hook_event_name": "session_shutdown",
        "reason": "quit",
    }
    assert report["shutdownReturnedNothing"] is True
    assert report["shutdownMs"] < 3000, "the bound is 2000 ms, with room for a loaded runner"
