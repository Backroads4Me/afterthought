"""Tests for recall on each prompt (BUILD-PLAN F87)."""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from aft import config as configmod
from aft import recall
from aft.adapters.claude import parse_claude
from aft.adapters.pi import parse_pi
from aft.cli import main
from aft.events import PROMPT, AdapterError
from aft.registry import Registry, path_key
from conftest import make_repo

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)

# The query these pages answer: three content words, so the bar is "all three in the row".
PROMPT_TEXT = "how do we deploy the release to staging"
QUERY_WORDS = ["deploy", "release", "staging"]

OVERVIEW = """---
type: Project
title: Widget
description: Everything known about the widget.
sources: []
aft:
  project: p_widget
---

# Widget

## Facts

- Deploy the release to staging with scripts/deploy.sh [stated] 2026-10-01 [^s1]
- Rollbacks run scripts/rollback.sh from the staging host [observed] 2026-10-02 [^s1]
"""

# Four lines the query matches, so a max_hits cut has something to cut; the second page's
# line holds all three query words too.
NOTES = """---
type: Project
title: Widget notes
description: Working notes for the widget.
sources: []
aft:
  project: p_widget
---

# Widget notes

## State

- Deploy the release candidate to staging nightly [observed] 2026-10-03 [^s1]
- The staging release checklist asks for deploy signoff [observed] 2026-10-04 [^s1]
- Deploy notes: the staging release cutoff is Friday [observed] 2026-10-05 [^s1]
- The release staging area is swept clean nightly [observed] 2026-10-06 [^s1]
"""

# One line below the bar: it holds two of the query's three content words, so it never
# shows however well it ranks.
THIN = """---
type: Project
title: Widget thin
description: A page whose only match is below the bar.
sources: []
aft:
  project: p_widget
---

# Widget thin

## Facts

- The release staging area is swept clean nightly [observed] 2026-10-06 [^s1]
"""


@pytest.fixture
def workspace(initialized, tmp_path) -> tuple[Path, Path]:
    """A registered project at ``cwd`` whose vault pages answer the recall prompt."""
    cwd = make_repo(tmp_path / "widget-work")
    registry = Registry.load(initialized.registry_path)
    project = registry.add("widget", "Widget")
    registry.add_path(project, path_key(initialized.host, cwd))
    registry.save()
    for rel, text in (
        ("projects/widget/overview.md", OVERVIEW),
        ("projects/widget/notes.md", NOTES),
    ):
        page = initialized.vault / rel
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(text, encoding="utf-8")
    return cwd, initialized.vault


def run_hook(monkeypatch, capsys, agent: str, event: str, stdin_text: str) -> tuple[int, str, str]:
    """`aft hook <agent> <event>` with `stdin_text` on stdin, as the agent would run it."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    code = main(["hook", agent, event])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def prompt_payload(cwd: Path, prompt: str, agent: str = "claude") -> dict:
    """One agent's prompt payload, field for field as the adapter reads it."""
    name = "UserPromptSubmit" if agent == "claude" else "before_agent_start"
    return {
        "session_id": "s-recall-1",
        "transcript_path": str(cwd / "transcript.jsonl"),
        "cwd": str(cwd),
        "hook_event_name": name,
        "prompt": prompt,
    }


def files_under(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# --- the query ----------------------------------------------------------------------


def test_query_words_drops_pasted_content_fences_and_markers():
    prompt = (
        "off the record how do we deploy the release to staging\n"
        "<pasted_content>\nQZXJ pasted noise nobody asked about\n</pasted_content>\n"
        "```python\nprint('fence noise QZXJ')\n```\n"
        "aft on please"
    )
    words = recall.query_words(prompt)
    assert words == ["deploy", "release", "staging", "please"]
    assert "QZXJ" not in " ".join(words)


def test_query_words_cut_to_the_first_32():
    words = recall.query_words(" ".join(f"w{number}" for number in range(40)))
    assert len(words) == 32
    assert words[0] == "w0" and words[-1] == "w31"


def test_query_words_lose_the_punctuation_around_them():
    """A word with punctuation around it is the word itself, so the bar can count it."""
    words = recall.query_words('how do we "deploy" the release, to staging?')
    assert words == QUERY_WORDS


def test_a_prompt_ending_in_punctuation_still_recalls(workspace, cfg):
    cwd, _ = workspace
    assert "release candidate" in recall.recall(f"{PROMPT_TEXT}?", cwd, cfg)


def test_a_short_start_block_line_does_not_hide_a_row_that_contains_it():
    """A one-word block line ("none", a title) is not matched inside a longer row."""
    hit = SimpleNamespace(snippet="Deploy the release candidate to staging nightly", description="")
    assert not recall._held_by_block(hit, ["staging"])
    assert recall._held_by_block(hit, ["deploy the release candidate to staging nightly"])


# --- the parsers ---------------------------------------------------------------------


def test_claude_user_prompt_submit_parses_to_a_prompt_event(tmp_path):
    payload = prompt_payload(tmp_path, PROMPT_TEXT)
    event = parse_claude("UserPromptSubmit", payload, NOW)
    assert event.kind == PROMPT
    assert event.prompt == PROMPT_TEXT
    assert event.detail is None
    assert event.session_id == "s-recall-1"


def test_claude_user_prompt_submit_without_a_prompt_is_refused(tmp_path):
    payload = prompt_payload(tmp_path, PROMPT_TEXT)
    del payload["prompt"]
    with pytest.raises(AdapterError, match="prompt"):
        parse_claude("UserPromptSubmit", payload, NOW)


def test_pi_before_agent_start_parses_to_a_prompt_event(tmp_path):
    payload = prompt_payload(tmp_path, PROMPT_TEXT, agent="pi")
    event = parse_pi("before_agent_start", payload, NOW)
    assert event.kind == PROMPT
    assert event.prompt == PROMPT_TEXT
    assert event.detail is None


# --- recall ---------------------------------------------------------------------------


def test_a_matching_prompt_recalls_at_most_max_hits_within_max_chars(workspace, cfg):
    cwd, _ = workspace
    block = recall.recall(PROMPT_TEXT, cwd, cfg)
    assert block.startswith(recall.HEADING)
    lines = block.split("\n")
    assert 1 < len(lines) <= 1 + cfg.settings.recall.max_hits
    assert all(line.startswith("- ") for line in lines[1:])
    assert len(block) <= cfg.settings.recall.max_chars


def test_a_hit_below_the_bar_is_dropped(initialized, tmp_path):
    """A row holding fewer than three of the query's content words never shows."""
    cwd = tmp_path / "thin-work"
    cwd.mkdir()
    page = initialized.vault / "projects/thin/notes.md"
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text(THIN, encoding="utf-8")
    assert recall.recall(PROMPT_TEXT, cwd, initialized) == ""


def test_a_short_prompt_recalls_nothing(workspace, cfg):
    cwd, _ = workspace
    assert recall.recall("do it", cwd, cfg) == ""
    assert recall.recall("yes please", cwd, cfg) == ""


def test_a_line_in_the_start_block_is_not_repeated(workspace, cfg):
    """The overview line the block already holds is dropped; the other page's line shows."""
    cwd, _ = workspace
    block = recall.recall(PROMPT_TEXT, cwd, cfg)
    assert "scripts/deploy.sh" not in block
    assert "release candidate" in block


def test_whole_lines_only_within_max_chars(workspace, initialized, tmp_path, monkeypatch):
    """A line that does not fit is dropped whole; no line is ever cut mid-line."""
    cwd, _ = workspace
    (initialized.home / "config.toml").write_text(
        f'vault = "{initialized.vault}"\nhost = "testhost"\n\n[recall]\nmax_chars = 200\n'
    )
    cfg = configmod.load()
    block = recall.recall(PROMPT_TEXT, cwd, cfg)
    assert len(block) <= 200
    for line in block.split("\n")[1:]:
        assert len(line) < 200


def test_recall_disabled_gives_nothing(workspace, initialized):
    cwd, _ = workspace
    (initialized.home / "config.toml").write_text(
        f'vault = "{initialized.vault}"\nhost = "testhost"\n\n[recall]\nenabled = false\n'
    )
    cfg = configmod.load()
    assert recall.recall(PROMPT_TEXT, cwd, cfg) == ""


# --- the hook -------------------------------------------------------------------------


def test_the_claude_hook_prints_the_json_shape_only_when_there_is_a_block(
    workspace, monkeypatch, capsys
):
    cwd, _ = workspace
    code, out, err = run_hook(
        monkeypatch,
        capsys,
        "claude",
        "UserPromptSubmit",
        json.dumps(prompt_payload(cwd, PROMPT_TEXT)),
    )
    assert (code, err) == (0, "")
    block = recall.recall(PROMPT_TEXT, cwd, configmod.load())
    assert (
        out
        == json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": block,
                }
            }
        )
        + "\n"
    )

    code, out, _ = run_hook(
        monkeypatch, capsys, "claude", "UserPromptSubmit", json.dumps(prompt_payload(cwd, "do it"))
    )
    assert (code, out) == (0, "")


def test_the_pi_hook_prints_the_plain_block(workspace, monkeypatch, capsys):
    cwd, _ = workspace
    code, out, err = run_hook(
        monkeypatch,
        capsys,
        "pi",
        "before_agent_start",
        json.dumps(prompt_payload(cwd, PROMPT_TEXT, agent="pi")),
    )
    assert (code, err) == (0, "")
    block = recall.recall(PROMPT_TEXT, cwd, configmod.load())
    assert out == block + "\n"


def test_a_failure_prints_nothing_exits_0_and_logs_no_prompt(workspace, home, monkeypatch, capsys):
    cwd, _ = workspace
    token = "QZXJ-SECRET-PROMPT-TOKEN"

    def boom(config, query, **kwargs):
        raise RuntimeError(f"fts5: syntax error near {query!r} ({token})")

    monkeypatch.setattr("aft.search.search", boom)
    code, out, err = run_hook(
        monkeypatch,
        capsys,
        "claude",
        "UserPromptSubmit",
        json.dumps(prompt_payload(cwd, f"{PROMPT_TEXT} {token}")),
    )
    assert (code, out, err) == (0, "", "")
    log = (home / "logs" / "hooks.log").read_text()
    assert "RecallError" in log
    assert token not in log


def test_recall_disabled_hook_prints_nothing(workspace, initialized, monkeypatch, capsys):
    cwd, _ = workspace
    (initialized.home / "config.toml").write_text(
        f'vault = "{initialized.vault}"\nhost = "testhost"\n\n[recall]\nenabled = false\n'
    )
    code, out, err = run_hook(
        monkeypatch,
        capsys,
        "claude",
        "UserPromptSubmit",
        json.dumps(prompt_payload(cwd, PROMPT_TEXT)),
    )
    assert (code, out, err) == (0, "", "")


def test_the_prompt_is_written_nowhere(workspace, initialized, monkeypatch, capsys):
    """After a recall hook run, the prompt's words are in no file of the home or the vault."""
    cwd, _ = workspace
    token = "QZXJ-UNIQUE-PROMPT-TOKEN"
    code, out, err = run_hook(
        monkeypatch,
        capsys,
        "claude",
        "UserPromptSubmit",
        json.dumps(prompt_payload(cwd, f"{PROMPT_TEXT} {token}")),
    )
    assert (code, err) == (0, "")
    assert token not in out
    for root in (initialized.home, initialized.vault):
        for path in files_under(root):
            assert token.encode() not in path.read_bytes(), path


# --- configuration ---------------------------------------------------------------------


def test_config_rejects_max_hits_zero(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(f'vault = "{tmp_path / "vault"}"\n\n[recall]\nmax_hits = 0\n')
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    with pytest.raises(configmod.ConfigError, match="recall.max_hits must be positive"):
        configmod.load()


def test_config_rejects_a_non_bool_enabled(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        f'vault = "{tmp_path / "vault"}"\n\n[recall]\nenabled = "yes"\n'
    )
    monkeypatch.setenv(configmod.HOME_ENV, str(home))
    with pytest.raises(configmod.ConfigError, match="recall.enabled must be true or false"):
        configmod.load()


def test_config_defaults_match_the_plan(cfg):
    settings = cfg.settings.recall
    assert (settings.enabled, settings.max_hits, settings.max_chars, settings.rerank) == (
        True,
        3,
        1500,
        False,
    )


# --- the installer ---------------------------------------------------------------------


def test_install_claude_adds_and_uninstall_removes_the_user_prompt_submit_group(tmp_path):
    from aft.install import claude as claude_install

    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")

    claude_install.install(settings)
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert hooks["UserPromptSubmit"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aft hook claude UserPromptSubmit",
                    "timeout": 5,
                }
            ]
        }
    ]

    claude_install.uninstall(settings)
    hooks = json.loads(settings.read_text(encoding="utf-8"))["hooks"]
    assert "UserPromptSubmit" not in hooks
