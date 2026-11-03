"""Tests for the omp hook module (integrations/omp/afterthought.ts).

The module is run as it is, against a fake omp API and an ``aft`` of a few lines on PATH, so
what these tests read back is what omp would hand the model: the system prompt array a
``before_agent_start`` handler returns. The two things the real-session check cannot cover
live here - the notice path, which needs an interface a ``-p`` run does not have, and a second
turn, which is where appending to an array rather than replacing a named section could leave
two copies of the block in front of the model.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK_TS = REPO_ROOT / "integrations" / "omp" / "afterthought.ts"

# omp's own base system prompt, standing in for the blocks the turn is built on.
BASE = ["You are omp.", "Tools: read, write."]


def aft_stub(tmp_path: Path, body: str) -> Path:
    """An ``aft`` of a few lines on a PATH of its own, printing what a test wants it to."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "aft"
    stub.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    stub.chmod(0o755)
    return bin_dir


def run(tmp_path: Path, bin_dir: Path, script: str) -> dict:
    """Run one script against the hook module and return the JSON it printed."""
    assert NODE is not None, "node is not available"
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
    res = subprocess.run(
        [NODE, "--experimental-strip-types", "-e", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


PRELUDE = f"""
import hook from "{HOOK_TS}";

const handlers = {{}};
hook({{ on(event, handler) {{ handlers[event] = handler; }} }});

const notices = [];
const session = {{
  getSessionId() {{ return "session-123"; }},
  getCwd() {{ return "/tmp"; }},
  getSessionFile() {{ return undefined; }},
}};
const base = {json.dumps(BASE)};
"""


def session_start(tmp_path: Path, printed: str, *, has_ui: bool = True) -> dict:
    """Start an omp session against an `aft` that prints ``printed``, and report the system
    prompt the model is given and every notice the user is shown."""
    (tmp_path / "printed").write_text(printed, encoding="utf-8")
    bin_dir = aft_stub(tmp_path, f'cat "{tmp_path / "printed"}"\nexit 0')
    script = (
        PRELUDE
        + """
const ctx = {
  sessionManager: session,
  hasUI: HAS_UI,
  ui: { notify(message, type) { notices.push([message, type]); } },
};
await handlers["session_start"]({ type: "session_start" }, ctx);
const result = await handlers["before_agent_start"](
  { type: "before_agent_start", reason: "prompt", prompt: "", systemPrompt: base },
  ctx,
);
console.log(JSON.stringify({ systemPrompt: result?.systemPrompt ?? null, notices }));
""".replace("HAS_UI", "true" if has_ui else "false")
    )
    return run(tmp_path, bin_dir, script)


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_the_block_is_appended_to_omp_own_system_prompt(tmp_path: Path) -> None:
    """omp reads a replacement for the whole turn's prompt, so the handler has to give its
    own base back along with the block, or the model loses its instructions."""
    seen = session_start(tmp_path, "# Memory: Widget\n\n- A fact.")

    assert seen["systemPrompt"] == [
        *BASE,
        "<afterthought>\n# Memory: Widget\n\n- A fact.\n</afterthought>",
    ]
    assert seen["notices"] == []


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_the_user_is_shown_the_notice_for_notes_waiting(tmp_path: Path) -> None:
    """A session start with notes waiting prints JSON: the block goes to the model and the
    notice to the user."""
    printed = json.dumps({"block": "# Memory: Widget", "notice": "2 notes wait on you."})

    seen = session_start(tmp_path, printed)

    assert seen["systemPrompt"] == [*BASE, "<afterthought>\n# Memory: Widget\n</afterthought>"]
    assert seen["notices"] == [["2 notes wait on you.", "warning"]]


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_without_an_interface_the_model_still_gets_the_block(tmp_path: Path) -> None:
    printed = json.dumps({"block": "# Memory: Widget", "notice": "2 notes wait on you."})

    seen = session_start(tmp_path, printed, has_ui=False)

    assert seen["systemPrompt"] == [*BASE, "<afterthought>\n# Memory: Widget\n</afterthought>"]
    assert seen["notices"] == []


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_a_turn_that_already_carries_the_blocks_ends_with_one_copy_of_each(
    tmp_path: Path,
) -> None:
    """A system prompt is an array with no names to replace by, so the handler takes its own
    blocks off the base before it puts them back: a second turn, or a base rebuilt from a
    prompt this hook already answered, leaves one block and one recall."""
    bin_dir = aft_stub(
        tmp_path,
        """if [ "$3" = "session_start" ]; then
  echo "BLOCK"
else
  echo "RECALL"
fi
exit 0""",
    )
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
await handlers["session_start"]({ type: "session_start" }, ctx);
const prompt = (systemPrompt) =>
  handlers["before_agent_start"](
    { type: "before_agent_start", reason: "prompt", prompt: "ship it", systemPrompt },
    ctx,
  );
const first = await prompt(base);
const second = await prompt(first.systemPrompt);
console.log(JSON.stringify({ first: first.systemPrompt, second: second.systemPrompt }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    expected = [
        *BASE,
        "<afterthought>\nBLOCK\n</afterthought>",
        "<afterthought-recall>\nRECALL\n</afterthought-recall>",
    ]
    assert seen["first"] == expected
    assert seen["second"] == expected


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_an_empty_recall_clears_the_recall_block_and_keeps_the_memory_block(
    tmp_path: Path,
) -> None:
    once = tmp_path / "recall-once"
    bin_dir = aft_stub(
        tmp_path,
        f"""if [ "$3" = "session_start" ]; then
  echo "BLOCK"
elif [ -f "{once}" ]; then
  :
else
  touch "{once}"
  echo "RECALL"
fi
exit 0""",
    )
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
await handlers["session_start"]({ type: "session_start" }, ctx);
const prompt = (systemPrompt) =>
  handlers["before_agent_start"](
    { type: "before_agent_start", reason: "prompt", prompt: "ship it", systemPrompt },
    ctx,
  );
const first = await prompt(base);
const second = await prompt(first.systemPrompt);
console.log(JSON.stringify({ first: first.systemPrompt, second: second.systemPrompt }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    assert seen["first"] == [
        *BASE,
        "<afterthought>\nBLOCK\n</afterthought>",
        "<afterthought-recall>\nRECALL\n</afterthought-recall>",
    ]
    assert seen["second"] == [*BASE, "<afterthought>\nBLOCK\n</afterthought>"]


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_a_revived_subagent_asks_aft_for_nothing(tmp_path: Path) -> None:
    """``reason: "revive"`` carries an empty prompt and its result is discarded, so the
    handler neither runs a command nor returns a prompt of its own."""
    bin_dir = aft_stub(tmp_path, f'touch "{tmp_path / "ran"}"\nexit 0')
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
const result = await handlers["before_agent_start"](
  { type: "before_agent_start", reason: "revive", prompt: "", systemPrompt: base },
  ctx,
);
console.log(JSON.stringify({ result: result ?? null }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    assert seen == {"result": None}
    assert not (tmp_path / "ran").exists()


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_nothing_to_say_leaves_omp_own_prompt_untouched(tmp_path: Path) -> None:
    """With no block and no recall the handler returns nothing, rather than a replacement
    that happens to equal the base: omp then builds the turn from its own prompt."""
    bin_dir = aft_stub(tmp_path, "exit 0")
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
await handlers["session_start"]({ type: "session_start" }, ctx);
const result = await handlers["before_agent_start"](
  { type: "before_agent_start", reason: "prompt", prompt: "ship it", systemPrompt: base },
  ctx,
);
console.log(JSON.stringify({ result: result ?? null }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    assert seen == {"result": None}


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_the_hook_calls_aft_with_its_own_agent_name_and_payload(tmp_path: Path) -> None:
    """Every event reaches ``aft hook omp <event>`` with the payload src/aft/adapters/omp.py
    parses, so a capture event is filed under omp rather than pi."""
    log = tmp_path / "calls"
    bin_dir = aft_stub(
        tmp_path,
        f'printf "%s %s %s " "$1" "$2" "$3" >> "{log}"\n'
        f'cat >> "{log}"\n'
        f'printf "\\n" >> "{log}"\n'
        "exit 0",
    )
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false, cwd: "/home/ted/src" };
await handlers["session_start"]({ type: "session_start" }, ctx);
await handlers["before_agent_start"](
  { type: "before_agent_start", reason: "prompt", prompt: "ship it", systemPrompt: base },
  ctx,
);
await handlers["session_before_compact"]({ type: "session_before_compact" }, ctx);
await handlers["session_shutdown"]({ type: "session_shutdown" }, ctx);
console.log(JSON.stringify({}));
"""
    )
    run(tmp_path, bin_dir, script)

    calls = [line.split(" ", 3) for line in log.read_text(encoding="utf-8").splitlines()]
    assert [(agent, event) for _, agent, event, _ in calls] == [
        ("omp", "session_start"),
        ("omp", "before_agent_start"),
        ("omp", "session_before_compact"),
        ("omp", "session_shutdown"),
    ]
    payloads = [json.loads(body) for *_, body in calls]
    assert all(p["session_id"] == "session-123" for p in payloads)
    assert all(p["cwd"] == "/home/ted/src" for p in payloads)
    assert "transcript_path" not in payloads[0], "no session file yet is a field left out"
    assert not any("reason" in p for p in payloads), "omp's session events carry no reason"
    assert payloads[1]["prompt"] == "ship it"


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_the_block_is_refreshed_after_a_compaction(tmp_path: Path) -> None:
    bin_dir = aft_stub(
        tmp_path,
        """if [ "$3" = "session_start" ]; then
  echo "REFRESHED_MEMORY_BLOCK"
fi
exit 0""",
    )
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
await handlers["session_compact"]({ type: "session_compact" }, ctx);
const result = await handlers["before_agent_start"](
  { type: "before_agent_start", reason: "prompt", prompt: "", systemPrompt: base },
  ctx,
);
console.log(JSON.stringify({ systemPrompt: result?.systemPrompt ?? null }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    assert seen["systemPrompt"] == [
        *BASE,
        "<afterthought>\nREFRESHED_MEMORY_BLOCK\n</afterthought>",
    ]


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_a_session_method_that_throws_cannot_break_a_turn(tmp_path: Path) -> None:
    """Building the payload cannot throw out of a handler, and a turn whose session says
    nothing still reaches the model with omp's own prompt."""
    bin_dir = aft_stub(tmp_path, "exit 0")
    script = f"""
import hook from "{HOOK_TS}";

const handlers = {{}};
hook({{ on(event, handler) {{ handlers[event] = handler; }} }});

const base = {json.dumps(BASE)};
const ctx = {{
  sessionManager: {{
    getSessionId() {{ throw new Error("no session"); }},
    getCwd() {{ throw new Error("no cwd"); }},
    getSessionFile() {{ throw new Error("no file"); }},
  }},
  hasUI: false,
}};
await handlers["session_start"]({{ type: "session_start" }}, ctx);
await handlers["session_shutdown"]({{ type: "session_shutdown" }}, ctx);
const result = await handlers["before_agent_start"](
  {{ type: "before_agent_start", reason: "prompt", prompt: "ship it", systemPrompt: base }},
  ctx,
);
console.log(JSON.stringify({{ result: result ?? null }}));
"""
    seen = run(tmp_path, bin_dir, script)

    assert seen == {"result": None}


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_a_turn_with_no_prompt_sends_the_previous_turn_recall_nowhere(tmp_path: Path) -> None:
    """A turn whose prompt is empty - an image alone - has nothing to recall for, and omp's
    override lasts one turn, so nothing of the previous prompt's recall carries over."""
    bin_dir = aft_stub(
        tmp_path,
        """if [ "$3" = "session_start" ]; then
  echo "BLOCK"
else
  echo "RECALL"
fi
exit 0""",
    )
    script = (
        PRELUDE
        + """
const ctx = { sessionManager: session, hasUI: false };
await handlers["session_start"]({ type: "session_start" }, ctx);
const prompt = (text, systemPrompt) =>
  handlers["before_agent_start"](
    { type: "before_agent_start", reason: "prompt", prompt: text, systemPrompt },
    ctx,
  );
const first = await prompt("ship it", base);
const second = await prompt("", first.systemPrompt);
console.log(JSON.stringify({ first: first.systemPrompt, second: second.systemPrompt }));
"""
    )
    seen = run(tmp_path, bin_dir, script)

    assert seen["first"] == [
        *BASE,
        "<afterthought>\nBLOCK\n</afterthought>",
        "<afterthought-recall>\nRECALL\n</afterthought-recall>",
    ]
    assert seen["second"] == [*BASE, "<afterthought>\nBLOCK\n</afterthought>"]
