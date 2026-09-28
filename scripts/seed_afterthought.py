#!/usr/bin/env python3
"""Write afterthought's own overview page into a vault, from its design document.

Each fact below is hand-written from ``planning/DESIGN.md`` and carries the quote it came
from. Before the page is written, every quote is checked against the section of the design
document it cites, so a design edit that drops a quoted line fails the seed instead of
writing a page whose evidence no longer exists, and ``aft``'s own lint checks the rendered page
in a scratch directory, so a page that breaks the page grammar never reaches the vault
(design section 6, BUILD-PLAN M1.5).

Usage: uv run python scripts/seed_afterthought.py <vault>
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

import yaml

from aft import page as pagemod
from aft import vault
from aft.config import ConfigError, load
from aft.gitutil import GitError
from aft.lint import lint_file
from aft.registry import Registry, RegistryError

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN = REPO_ROOT / "planning" / "DESIGN.md"
DESIGN_RESOURCE = "file:planning/DESIGN.md"
SLUG = "afterthought"
REL_PATH = f"projects/{SLUG}/overview.md"
DESCRIPTION = "Shared, evidence-backed memory for coding agents, kept as OKF markdown in git."
COMMIT_MESSAGE = f"aft: seed the {SLUG} overview page"

_HEADING = re.compile(r"^#{1,6} (?P<heading>.+?)\s*$")
_FENCE = re.compile(r"^\s*```")


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class SeedFact:
    section: str
    """The kind section of the page this fact lands in."""

    statement: str
    heading: str
    """The heading in DESIGN.md that the quote is copied from."""

    quote: str


FACTS: tuple[SeedFact, ...] = (
    SeedFact(
        "Decisions",
        "Capture and injection run from agent lifecycle hooks, not from the working model "
        "choosing to use memory.",
        "3. Design principles",
        "Capture and injection run from agent lifecycle hooks.",
    ),
    SeedFact(
        "Decisions",
        "Files are the truth: every index is a cache that can be deleted and rebuilt from them.",
        "3. Design principles",
        "Files are the truth.",
    ),
    SeedFact(
        "Decisions",
        "Evidence travels with the claim: each fact carries a verbatim quote from its source, "
        "checked by a script.",
        "3. Design principles",
        "Evidence travels with the claim.",
    ),
    SeedFact(
        "Decisions",
        "Superseded facts are struck through and dated, and deletion happens only when you ask "
        "for it.",
        "3. Design principles",
        "Deletion happens only when you ask for it.",
    ),
    SeedFact(
        "Decisions",
        "Afterthought follows only its own rules and instructions; anything it imports is read "
        "as evidence and checked.",
        "3. Design principles",
        "Afterthought follows only its own rules and instructions.",
    ),
    SeedFact(
        "Decisions",
        "Rule R1: every fact line carries one source tag and at least one source with a verified "
        "quote.",
        "The rules",
        "Every fact line carries one source tag and at least one source with a verified quote.",
    ),
    SeedFact(
        "Decisions",
        "Rule R9: model output is schema-constrained JSON, and only the script edits files.",
        "The rules",
        "Model output is schema-constrained JSON. Only the script edits files.",
    ),
    SeedFact(
        "Facts",
        "Afterthought gives coding agents one shared, durable memory.",
        "Afterthought: design and build plan",
        "Afterthought gives coding agents one shared, durable memory.",
    ),
    SeedFact(
        "Facts",
        "Agents read memory automatically at session start and search it on demand.",
        "Afterthought: design and build plan",
        "Agents read it automatically at session start and search it on demand.",
    ),
    SeedFact(
        "Facts",
        "Agents do not use memory unless forced, so no part of the system may depend on the "
        "working model deciding to use it.",
        "1. The problem",
        "Agents do not use memory unless forced.",
    ),
    SeedFact(
        "Facts",
        "One OpenAI-compatible endpoint, set in the home directory's config.toml, serves every "
        "model call.",
        "13. The model and its load",
        "One OpenAI-compatible endpoint, set in `~/.afterthought/config.toml`.",
    ),
    SeedFact(
        "Facts",
        "`aft inject` reads files only, uses no model, and targets under 150 ms.",
        "Injection at session start",
        "`aft inject` reads files only, uses no model, and targets under 150 ms.",
    ),
    SeedFact(
        "Facts",
        "`aft search` queries a SQLite FTS5 index ranked with BM25.",
        "Search",
        "`aft search` queries a SQLite FTS5 index with BM25 ranking.",
    ),
    SeedFact(
        "Facts",
        "Every page is an OKF v0.2 concept file, with afterthought's own fields under one `aft` "
        "key.",
        "Page format",
        "Every page is an OKF v0.2 concept file.",
    ),
    SeedFact(
        "Facts",
        "A page's sections are the kinds of fact it holds, and candidates sit in their own "
        "section until they pass the gate.",
        "Kinds",
        "Candidates sit in their own section until they pass the gate.",
    ),
    SeedFact(
        "Facts",
        "Where a fact came from and where it stands are separate: the parser reads state from "
        "the markdown.",
        "Fact state",
        "Where a fact came from and where it stands are separate.",
    ),
    SeedFact(
        "Facts",
        "Each project has a stable id separate from its slug, so a rename rewrites nothing else.",
        "7. Project identity",
        "Each project has a stable id separate from its slug, so a rename rewrites nothing else.",
    ),
    SeedFact(
        "Facts",
        "Reports describe memory; they are never memory.",
        "6. The memory vault",
        "Reports describe memory; they are never memory.",
    ),
    SeedFact(
        "Facts",
        "Forgetting removes content from everything afterthought controls, but not from the "
        "vault's git history.",
        "12. Forgetting and privacy",
        "Forgetting removes content from everything afterthought controls.",
    ),
)


def sections(design: str) -> dict[str, list[str]]:
    """Map each heading to the raw text under it, up to the next heading of the document.

    A heading inside a fenced code block is that block's content, not a section of the
    document, so it neither ends the body before it nor becomes a heading of its own. A name
    used more than once maps to every body written under it, so a citation to it is reported
    as ambiguous instead of silently resolving to the last one.
    """
    found: dict[str, list[str]] = {}
    heading: str | None = None
    body: list[str] = []
    fenced = False
    for line in design.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            match = _HEADING.match(line)
            if match is not None:
                if heading is not None:
                    found.setdefault(heading, []).append("\n".join(body))
                heading = match.group("heading")
                body = []
                continue
        if heading is not None:
            body.append(line)
    if heading is not None:
        found.setdefault(heading, []).append("\n".join(body))
    return found


def check_quotes(facts: tuple[SeedFact, ...], design: str) -> None:
    """Fail unless every fact names a kind section of a page and its quote appears verbatim
    under the one heading it cites."""
    found = sections(design)
    for fact in facts:
        if fact.section not in pagemod.KIND_SECTIONS:
            raise SeedError(
                f"{fact.statement!r} would land in {fact.section!r}; a page's sections are "
                + ", ".join(pagemod.KIND_SECTIONS)
            )
        bodies = found.get(fact.heading)
        if bodies is None:
            raise SeedError(f"{DESIGN_RESOURCE} has no heading {fact.heading!r}")
        if len(bodies) > 1:
            raise SeedError(
                f"{DESIGN_RESOURCE} has {len(bodies)} headings {fact.heading!r}, so the quote "
                f"for {fact.statement!r} does not name one of them"
            )
        if fact.quote not in bodies[0]:
            raise SeedError(f"quote is not verbatim under {fact.heading!r}: {fact.quote!r}")


def render(facts: tuple[SeedFact, ...], project_id: str, observed: str) -> str:
    """Render the overview page: sources in the frontmatter, one fact line each."""
    sources = [
        {
            "id": f"s{index}",
            "resource": f"{DESIGN_RESOURCE}#{fact.heading}",
            "quote": fact.quote,
        }
        for index, fact in enumerate(facts, start=1)
    ]
    frontmatter = {
        "type": "Project",
        "title": SLUG,
        "description": DESCRIPTION,
        "tags": ["memory", "agents"],
        "sources": sources,
        "aft": {"project": project_id},
    }
    statements: dict[str, list[str]] = {section: [] for section in pagemod.KIND_SECTIONS}
    for index, fact in enumerate(facts, start=1):
        statements[fact.section].append(f"- {fact.statement} [observed] {observed} [^s{index}]")
    lines: list[str] = []
    for section, entries in statements.items():
        if not entries:
            continue
        lines += [f"## {section}", "", *entries, ""]
    body = "\n".join(lines).rstrip("\n") + "\n"
    front = yaml.safe_dump(frontmatter, sort_keys=False, width=10_000, allow_unicode=True)
    return f"---\n{front}---\n\n{body}"


def lint_page(text: str, registry: Registry) -> list[str]:
    """Lint the rendered page in a scratch directory, so a page that breaks the grammar
    never reaches the vault's working tree."""
    rel = Path(REL_PATH)
    with tempfile.TemporaryDirectory() as scratch:
        page = Path(scratch) / rel
        page.parent.mkdir(parents=True)
        page.write_text(text)
        return [str(finding) for finding in lint_file(Path(scratch), rel, registry)]


def seed(vault_path: Path) -> bool:
    """Write and commit the seed page into ``vault_path``. Returns whether it committed."""
    check_quotes(FACTS, DESIGN.read_text())
    config = replace(load(), vault=vault_path)
    vault.require(config)
    with vault.locked(config):
        registry = Registry.load(config.registry_path)
        project = registry.by_slug(SLUG)
        if project is None:
            raise SeedError(
                f"{config.registry_path} does not register {SLUG}; "
                f"run `aft project add ~/src/{SLUG}` first"
            )
        text = render(FACTS, project.id, date.today().isoformat())
        findings = lint_page(text, registry)
        if findings:
            raise SeedError("the seeded page does not lint: " + "; ".join(findings))
        path = config.vault / REL_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return vault.commit(config.vault, [REL_PATH], COMMIT_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_afterthought.py",
        description=f"Seed afterthought's overview page from {DESIGN_RESOURCE}.",
    )
    parser.add_argument("vault", help="the vault to write into, such as ~/afterthought")
    args = parser.parse_args(argv)
    vault_path = Path(args.vault).expanduser().resolve()
    try:
        committed = seed(vault_path)
    except (SeedError, ConfigError, GitError, RegistryError, vault.VaultError, OSError) as exc:
        print(f"seed_afterthought: {exc}", file=sys.stderr)
        return 2
    state = "committed" if committed else "unchanged"
    print(f"seeded {REL_PATH} into {vault_path} ({len(FACTS)} facts, {state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
