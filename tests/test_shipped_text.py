"""F6: the text a person installs with names no one.

Shipped text is what reaches a stranger's machine: the prompts and schemas the model is
asked with, the skill an agent follows, the text aft writes (marks, reports, unit files)
and the comments a contributor reads while changing it. A name in any of it makes the tool
look like it belongs to one person, and in a prompt it is worse than cosmetic: the model
sees turns labelled ``USER``, not a name, so a name there matches nothing.

The identities below are the ones this text has carried. An identity belongs here, in the
test that refuses it, and nowhere a person installs.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SHIPPED_DIRS = ("src", "integrations", "scripts", "docs")
SHIPPED_FILES = ("README.md", "pyproject.toml")
TEXT_SUFFIXES = {".py", ".md", ".json", ".ts", ".toml"}

NAMES = ("Ted", "firstmate", "Backroads4Me", "RV-C", "Victron")


def shipped_text() -> list[Path]:
    """Every text file an install carries, and nothing else.

    ``planning/`` is out: the design and the plan are this project's own record and may
    name whoever they like, and rule 2 of the build reserves them to lead. ``tests/`` is
    out because a test quotes the world it builds.
    """
    found = [REPO / name for name in SHIPPED_FILES]
    for root in SHIPPED_DIRS:
        paths = (REPO / root).rglob("*")
        found += [p for p in paths if p.is_file() and p.suffix in TEXT_SUFFIXES]
    return found


def test_shipped_text_names_no_one() -> None:
    """No person, account or one user's project appears in anything an install carries."""
    hits: list[str] = []
    for path in shipped_text():
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            for name in NAMES:
                if re.search(rf"\b{re.escape(name)}\b", line):
                    hits.append(f"{path.relative_to(REPO)}:{number} names {name!r}")
    assert hits == [], "shipped text names someone (F6): " + "; ".join(hits)
