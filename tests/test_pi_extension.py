"""Tests for pi extension (integrations/pi/afterthought.ts) (BUILD-PLAN F24, M1 review 5, 9)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_TS = REPO_ROOT / "integrations" / "pi" / "afterthought.ts"


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_pi_extension_payload_construction_does_not_throw_when_session_methods_fail() -> None:
    """Building the payload cannot throw out of a handler, even if session methods throw
    (BUILD-PLAN F24, M1 review 9)."""
    script = f"""
import extension from "{EXTENSION_TS}";

const handlers = {{}};
const mockPi = {{
  on(event, handler) {{
    handlers[event] = handler;
  }},
}};

extension(mockPi);

const brokenSession = {{
  getSessionId() {{ throw new Error("broken getSessionId"); }},
  getCwd() {{ throw new Error("broken getCwd"); }},
  getSessionFile() {{ throw new Error("broken getSessionFile"); }},
}};

const events = ["session_start", "session_before_compact", "session_compact", "session_shutdown"];
for (const ev of events) {{
  await handlers[ev]({{ reason: "test" }}, {{ sessionManager: brokenSession }});
}}
console.log("OK");
"""
    res = subprocess.run(
        [NODE, "--experimental-strip-types", "-e", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
    assert "OK" in res.stdout


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_pi_extension_refreshes_block_after_compaction(tmp_path: Path) -> None:
    """After a compaction the pi extension asks for a fresh block and replaces the one
    it holds, as Claude Code's SessionStart does (BUILD-PLAN F24, M1 review 5)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    aft_stub = bin_dir / "aft"
    aft_stub.write_text(
        """#!/bin/sh
if [ "$3" = "session_start" ]; then
  echo "REFRESHED_MEMORY_BLOCK"
fi
exit 0
""",
        encoding="utf-8",
    )
    aft_stub.chmod(0o755)

    script = f"""
import extension from "{EXTENSION_TS}";

const handlers = {{}};
const mockPi = {{
  on(event, handler) {{
    handlers[event] = handler;
  }},
}};

extension(mockPi);

const session = {{
  getSessionId() {{ return "session-123"; }},
  getCwd() {{ return "/tmp"; }},
  getSessionFile() {{ return undefined; }},
}};

await handlers["session_compact"]({{ reason: "threshold" }}, {{ sessionManager: session }});

const event = {{ systemPromptOptions: {{ sections: {{}} }} }};
handlers["before_agent_start"](event);
console.log(JSON.stringify(event.systemPromptOptions.sections));
"""
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
    sections = json.loads(res.stdout)
    assert sections.get("afterthought") == "REFRESHED_MEMORY_BLOCK"
