"""The user documentation keeps up with the code (BUILD-PLAN F33).

A setting or a command added without a word in the docs fails here, so the docs a user
reads never fall behind what `aft` does.
"""

from __future__ import annotations

import argparse
import dataclasses
import re
from pathlib import Path

from aft import cli, config

DOCS = Path(__file__).resolve().parents[1] / "docs"
README = Path(__file__).resolve().parents[1] / "README.md"


def setting_names(cls: type = config.Settings, prefix: str = "") -> list[str]:
    """Every setting `config.toml` accepts, as ``section.key`` or a top-level ``key``."""
    names = []
    for field in dataclasses.fields(cls):
        if dataclasses.is_dataclass(field.type) or (
            isinstance(field.type, str) and field.type.endswith("Config")
        ):
            names += setting_names(getattr(config, field.type), f"{field.name}.")
        else:
            names.append(f"{prefix}{field.name}")
    return names


def command_names(
    parser: argparse.ArgumentParser | None = None, prefix: str = "aft"
) -> list[list[str]]:
    """Every command and subcommand `aft` takes, each as the spellings a user may type:
    its name, then any aliases."""
    parser = parser or cli._parser()
    names: list[list[str]] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            spelled: dict[int, list[str]] = {}
            for name, sub in action.choices.items():
                spelled.setdefault(id(sub), []).append(f"{prefix} {name}")
            for spellings in spelled.values():
                names.append(spellings)
                sub = action.choices[spellings[0].rsplit(" ", 1)[1]]
                names += command_names(sub, spellings[0])
    return names


def test_every_setting_is_documented():
    text = (DOCS / "configuration.md").read_text()
    names = setting_names()
    assert "model.endpoint" in names and "dream.short_term_sessions" in names
    missing = [name for name in names if f"`{name}`" not in text]
    assert missing == [], f"settings with no entry in docs/configuration.md: {missing}"


def test_digest_pipeline_documented_default_is_turns():
    """L2: docs/configuration.md documents the default digest.pipeline as turns."""
    text = (DOCS / "configuration.md").read_text()
    assert "| `digest.pipeline` | `turns` |" in text


def test_every_command_is_documented():
    text = (DOCS / "commands.md").read_text()
    names = command_names()
    assert ["aft project add"] in names and ["aft library describe"] in names
    missing = [
        spellings[0] for spellings in names if not any(f"`{name}" in text for name in spellings)
    ]
    assert missing == [], f"commands with no entry in docs/commands.md: {missing}"


def test_the_readme_links_every_page_of_the_docs():
    readme = README.read_text()
    for page in sorted(DOCS.glob("*.md")):
        assert f"(docs/{page.name})" in readme, f"README.md does not link docs/{page.name}"


def test_links_between_the_docs_resolve():
    for page in [README, *sorted(DOCS.glob("*.md"))]:
        for target in re.findall(r"\]\(([^)#:]+\.md)\)", page.read_text()):
            assert (page.parent / target).resolve().exists(), f"{page.name} links {target}"
