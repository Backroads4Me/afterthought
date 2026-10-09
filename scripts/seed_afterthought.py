#!/usr/bin/env python3
"""Write afterthought's own overview page into a vault, from its design document.

Each fact below is hand-written from ``planning/DESIGN.md`` and carries the quote it came
from. Before the page is written, every quote is checked against the section of the design
document it cites, so a design edit that drops a quoted line fails the seed instead of
writing a page whose evidence no longer exists, and ``aft``'s own lint checks the rendered page
in a scratch directory, so a page that breaks the page grammar never reaches the vault
(design section 6, BUILD-PLAN M1.5).

The page is memory people edit, so an existing one is left alone unless the run says
``--overwrite``. The seed bootstraps a page once; after that the page is curated memory, so an
overwrite may only carry the page's own dates across. A fact whose statement and quote are
unchanged keeps the date the page already carried, because re-reading the same source is not new
evidence. Everything else the page holds that this run's facts do not write stops it instead: a
hand-written, reworded or refiled line, a candidate, a state mark on a seeded line, a line the
page parser rejects, an extra frontmatter key, tag or anchor, a source the page adds or rewrites.
Each is named in the error, so curating the page never means losing it to a re-seed
(BUILD-PLAN F1).

Usage: uv run python scripts/seed_afterthought.py [--overwrite] <vault>
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
        "Rule R1: every fact line carries one source tag and at least one source whose quote was "
        "verified against the archive when the digest was made, or is verified now.",
        "The rules",
        "Every fact line carries one source tag and at least one source whose quote was "
        "verified against the archive when the digest was made, or is verified now.",
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
        "One OpenAI-compatible endpoint, set in `~/.afterthought/config.toml`",
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
    used more than once maps to every body written under it, so a quote copied from any of them
    verifies instead of only from the last one.
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
    under the heading it cites."""
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
        if not any(fact.quote in body for body in bodies):
            raise SeedError(f"quote is not verbatim under {fact.heading!r}: {fact.quote!r}")


def frontmatter_of(facts: tuple[SeedFact, ...], project_id: str) -> dict:
    """The frontmatter the seed writes: one quoted source per fact, plus the page's identity."""
    return {
        "type": "Project",
        "title": SLUG,
        "description": DESCRIPTION,
        "tags": ["memory", "agents"],
        "sources": [
            {
                "id": f"s{index}",
                "resource": f"{DESIGN_RESOURCE}#{fact.heading}",
                "quote": fact.quote,
            }
            for index, fact in enumerate(facts, start=1)
        ],
        "aft": {"project": project_id},
    }


def sections_of(facts: tuple[SeedFact, ...]) -> list[str]:
    """The kind sections the seed writes, in the order it writes them."""
    used = {fact.section for fact in facts}
    return [section for section in pagemod.KIND_SECTIONS if section in used]


def _source_edits(found: list, want: list) -> list[str]:
    """Name each source an existing page adds, drops, or rewrites."""
    seeded = {entry["id"]: entry for entry in want}
    edits: list[str] = []
    seen: set[str] = set()
    for entry in found:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            edits.append("the page has a source that is not a mapping with an id")
            continue
        source_id = entry["id"]
        if source_id not in seeded:
            edits.append(f"the page adds the source {source_id}")
            continue
        seen.add(source_id)
        edits += [
            f"the page rewrites source {source_id}'s {field}"
            for field in ("resource", "quote")
            if entry.get(field) != seeded[source_id][field]
        ]
    dropped = [sid for sid in seeded if sid not in seen]
    return [*edits, *(f"the page drops the source {sid}" for sid in dropped)]


def _frontmatter_edits(key: str, found: object, want: object) -> list[str]:
    """Name how one frontmatter key of an existing page differs from what the seed writes."""
    if found == want:
        return []
    if key == "sources" and isinstance(found, list) and isinstance(want, list):
        return _source_edits(found, want)
    if key == "tags" and isinstance(found, list) and isinstance(want, list):
        return [
            *(f"the page adds the tag {tag!r}" for tag in found if tag not in want),
            *(f"the page drops the tag {tag!r}" for tag in want if tag not in found),
        ] or [f"the page rewrites the {key} list"]
    if key == "aft" and isinstance(found, dict) and isinstance(want, dict):
        return [
            *(f"the page adds aft.{name}" for name in found if name not in want),
            *(f"the page drops aft.{name}" for name in want if name not in found),
            *(
                f"the page rewrites aft.{name}"
                for name in want
                if name in found and found[name] != want[name]
            ),
        ] or [f"the page rewrites the {key} block"]
    return [f"the page rewrites the frontmatter's {key}"]


def curated_edits(
    page: pagemod.Page,
    facts: tuple[SeedFact, ...],
    project_id: str,
    quotes: dict[str, str],
) -> list[str]:
    """Name everything an existing page holds that this run's facts did not write (F1).

    A fact line is the seed's own only when its section, its statement and the quote of the
    source it cites are exactly one entry of ``facts``, which is what makes a reworded line, a
    hand-written line and a seeded fact filed under another kind section edits rather than facts
    the run may date afresh and write back over. The dates the page carries are the one thing it
    may own, since the carry reads them, and so is the order of its lines and sections.
    """
    want = frontmatter_of(facts, project_id)
    found = page.frontmatter if isinstance(page.frontmatter, dict) else {}
    edits: list[str] = []
    for key in found:
        if not isinstance(key, str):
            edits.append("the page has a frontmatter key that is not a name")
        elif key not in want:
            edits.append(f"the page adds the frontmatter key {key!r}")
    for key in want:
        if key not in found:
            edits.append(f"the page drops the frontmatter key {key!r}")
        else:
            edits += _frontmatter_edits(key, found[key], want[key])
    wanted = sections_of(facts)
    edits += [
        f"the page adds the section {section!r}"
        for section in page.sections
        if section not in wanted
    ]
    edits += [
        f"the page drops the section {section!r}"
        for section in wanted
        if section not in page.sections
    ]
    seeded = {
        (fact.section, fact.statement, fact.quote): f"s{index}"
        for index, fact in enumerate(facts, start=1)
    }
    filed = {(statement, quote): section for section, statement, quote in seeded}
    held: dict[tuple[str, str, str | None], int] = {}
    for fact in page.facts:
        at = f"line {fact.line}"
        if fact.state == pagemod.CANDIDATE:
            edits.append(f"{at} holds a candidate, which the seed never writes")
            continue
        quote = quotes.get(fact.refs[0]) if len(fact.refs) == 1 else None
        pair = (fact.section, fact.statement, quote)
        if pair in seeded:
            if pair in held:
                edits.append(f"{at} repeats the fact the seed holds at line {held[pair]}")
            else:
                held[pair] = fact.line
        if fact.state != pagemod.ACTIVE:
            edits.append(f"{at} is marked {fact.state}")
        elif fact.source != "observed":
            edits.append(f"{at} is tagged [{fact.source}]; the seed writes [observed]")
        if len(fact.refs) != 1:
            edits.append(f"{at} cites {len(fact.refs)} sources; the seed writes exactly one")
        elif pair not in seeded:
            if (fact.statement, quote) in filed:
                edits.append(f"{at} files the seed's fact under {fact.section!r}")
            else:
                edits.append(f"{at} holds wording the seed never wrote: {fact.statement!r}")
    edits += [
        f"the page drops the fact the seed cites as [^{source_id}]: {pair[1]!r}"
        for pair, source_id in seeded.items()
        if pair not in held
    ]
    return edits


def prior_page(
    path: Path, facts: tuple[SeedFact, ...], project_id: str
) -> dict[tuple[str, str], str]:
    """Read the dates an existing page carries, refusing a page the seed did not write whole.

    Dates are keyed by the statement and quote that make a fact the same fact. A line citing a
    source the page does not define with a usable quote is checked before the edits below, since
    there the run cannot tell an unchanged fact from a new one at all.
    """
    if not path.exists():
        return {}
    page = pagemod.parse(path.read_text(), path)
    if page.errors:
        raise SeedError(
            f"{path} is not a page the parser can read: "
            + "; ".join(f"line {number}: {message}" for number, message in page.errors)
        )
    quotes = {
        str(source["id"]): source["quote"]
        for source in (page.frontmatter or {}).get("sources") or []
        if isinstance(source, dict)
        and source.get("id") is not None
        and isinstance(source.get("quote"), str)
        and source["quote"].strip()
    }
    unreadable = sorted({ref for fact in page.facts for ref in fact.refs if ref not in quotes})
    if unreadable:
        raise SeedError(
            f"{path} cites {', '.join(unreadable)}, which its frontmatter does not define with "
            "a quote: repair the page, or remove it and seed fresh"
        )
    edits = curated_edits(page, facts, project_id, quotes)
    if edits:
        raise SeedError(
            f"{path} holds text the seed did not write: "
            + "; ".join(edits)
            + "; the seed writes a page once, so curate it by hand, or remove it and seed fresh"
        )
    return {(fact.statement, quotes[ref]): fact.date for fact in page.facts for ref in fact.refs}


def render(facts: tuple[SeedFact, ...], project_id: str, dates: dict[tuple[str, str], str]) -> str:
    """Render the overview page: sources in the frontmatter, one dated fact line per fact."""
    statements: dict[str, list[str]] = {section: [] for section in pagemod.KIND_SECTIONS}
    for index, fact in enumerate(facts, start=1):
        day = dates[(fact.statement, fact.quote)]
        statements[fact.section].append(f"- {fact.statement} [observed] {day} [^s{index}]")
    lines: list[str] = []
    for section in sections_of(facts):
        lines += [f"## {section}", "", *statements[section], ""]
    body = "\n".join(lines).rstrip("\n") + "\n"
    front = yaml.safe_dump(
        frontmatter_of(facts, project_id), sort_keys=False, width=10_000, allow_unicode=True
    )
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


def seed(vault_path: Path, overwrite: bool = False) -> bool:
    """Write and commit the seed page into ``vault_path``, or refuse to touch a curated one.

    Returns whether the run committed a change. An existing page is read for its dates first, and
    a page holding anything the seed did not write stops the run before anything is rendered
    (BUILD-PLAN F1).
    """
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
        path = config.vault / REL_PATH
        if path.exists() and not overwrite:
            raise SeedError(f"{path} already exists; pass --overwrite to replace it")
        today = date.today().isoformat()
        carried = prior_page(path, FACTS, project.id)
        dates = {
            (fact.statement, fact.quote): carried.get((fact.statement, fact.quote), today)
            for fact in FACTS
        }
        text = render(FACTS, project.id, dates)
        findings = lint_page(text, registry)
        if findings:
            raise SeedError("the seeded page does not lint: " + "; ".join(findings))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return vault.commit(config.vault, [REL_PATH], COMMIT_MESSAGE)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="seed_afterthought.py",
        description=f"Seed afterthought's overview page from {DESIGN_RESOURCE}.",
    )
    parser.add_argument("vault", help="the vault to write into, such as ~/afterthought")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help=f"replace {REL_PATH} where the vault holds the page this seed wrote and nothing else",
    )
    args = parser.parse_args(argv)
    vault_path = Path(args.vault).expanduser().resolve()
    try:
        committed = seed(vault_path, overwrite=args.overwrite)
    except (SeedError, ConfigError, GitError, RegistryError, vault.VaultError, OSError) as exc:
        print(f"seed_afterthought: {exc}", file=sys.stderr)
        return 2
    state = "committed" if committed else "unchanged"
    print(f"seeded {REL_PATH} into {vault_path} ({len(FACTS)} facts, {state})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
