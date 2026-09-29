"""Retrieval and the decision call (design 9, a run, steps 2 and 3).

For one verified fact from a waiting digest, ``related`` finds the ten existing lines most
like it with a keyword search, ``ask`` puts the fact and those lines, numbered ``L1`` to
``L10``, to the model for one decision, and ``decide`` does both and resolves the line the
decision names. What comes back is a ``Proposal``: the decision and its target, which
``Proposal.check`` hands to ``aft.dream.rules`` unchanged. Nothing here writes a file; the
model only judges meaning, and the rules and the page writer decide what changes (R9).

Where the lines come from:

* **The pages the run may edit.** A project fact searches ``projects/<slug>/``; a global
  fact searches those and ``global/`` as well (BUILD-PLAN M4.3). Digests, the archive and
  reports are never read: a run must never cite its own reports (design 6). Reference
  pages are outside M4.3's list: one carries ``aft.projects`` rather than one project, so
  no run owns it under R6 as the rules read a page's project.
* **The pages as they are now, not the search index.** A run applies earlier facts before
  it decides later ones (design 9), so a line added a moment ago has to be found, and the
  index is only brought up to date after the run commits. An index row is also a whole
  section, and the decision names one line. So the lines are ranked here, one row per line,
  with the same SQLite FTS5 BM25 ranking ``aft.index`` uses, in a table held in memory.
* **Lines that stand, and candidates.** Active lines, lines under an open note (they are
  still injected, design 6's fact state) and candidates. Superseded, withdrawn and stale
  lines are not memory, and the rules give no authority over them.

A decision that names a line needs one that was shown. A label past the end of the list,
or none on an action that edits a line, leaves the proposal without a target, and
``rules.check`` refuses it as the schema failure it is, so the report names it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from aft import model, page
from aft.config import Config
from aft.dream import rules

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PROMPT_PATH = PROMPTS / "decide.md"
SCHEMA_PATH = PROMPTS / "decide.schema.json"

LIMIT = 10
"""How many existing lines one decision sees (BUILD-PLAN M4.3)."""

MAX_TOKENS = 256
"""The answer is three short fields, the longest a 200-character reason."""

RETRIEVED = (page.ACTIVE, page.CONFLICT, page.NEEDS_YOU, page.CANDIDATE)
"""The line states a decision may name: the ones that stand as memory, and candidates."""

TARGETED = ("confirm", "supersede", "conflict")
"""The actions that name an existing line (the schema's ``line``)."""

PROJECT_PAGES = "projects"
GLOBAL_PAGES = "global"

_WORD = re.compile(r"\w+")


class DecideError(Exception):
    """The prompt or schema that ships with aft cannot be read."""


@dataclass(frozen=True)
class Line:
    """One existing line shown to the model."""

    label: str
    """``L1`` to ``L10``, in rank order: ``L1`` is the most related."""

    path: str
    """The page's vault-relative path."""

    project: str | None
    """The page's ``aft.project``, or None for a page under ``global/`` (R6)."""

    fact: page.Fact

    sources: dict[str, dict]
    """The page's source entries for the footnotes this line cites, by id. The caller
    builds the line's ``rules.Evidence`` from them (``rules.Context.line_evidence``)."""


@dataclass(frozen=True)
class Proposal:
    """One fact, the lines it was shown with, and what the model decided about it."""

    new_fact: rules.NewFact
    lines: tuple[Line, ...]
    decision: rules.Decision
    target: Line | None
    """The line the decision names, when it named one that was shown and its action
    edits a line. ``add`` and ``skip`` touch no existing line, so they have none."""

    def page_project(self, run_project: str) -> str | None:
        """The ``aft.project`` of the page the decision would edit, or None for a global
        page (``rules.Context.page_project``). A named line's page is its own; a new line
        goes on a global page for a global fact and on the run's project page otherwise
        (design 7)."""
        if self.target is not None:
            return self.target.project
        return None if self.new_fact.scope == "global" else run_project

    def context(
        self,
        today: str,
        evidence: tuple[rules.Evidence, ...],
        run_project: str,
        line_evidence: tuple[rules.Evidence, ...] = (),
    ) -> rules.Context:
        """The rules' context for this proposal. ``evidence`` is the new fact's own quotes
        and ``line_evidence`` the target line's, built from ``target.sources``."""
        return rules.Context(
            today=today,
            evidence=evidence,
            run_project=run_project,
            page_project=self.page_project(run_project),
            line_evidence=line_evidence,
        )

    def check(self, context: rules.Context) -> rules.Outcome:
        """Carry the decision and its target to the rules (design 9 step 4)."""
        target = self.target.fact if self.target is not None else None
        return rules.check(self.decision, self.new_fact, target, context)


def decide(config: Config, new_fact: rules.NewFact, quote: str, slug: str | None) -> Proposal:
    """Retrieve the lines related to one fact, ask the model, and resolve its answer.

    ``slug`` names the run's project directory, or None for a project with no pages yet.
    ``quote`` is the fact's verified quote from its digest. Raises ``model.ModelError``
    when the endpoint fails, which leaves the digest waiting (design 14)."""
    lines = related(config.vault, new_fact, slug)
    decision = ask(config, new_fact, quote, lines)
    return Proposal(new_fact, lines, decision, _target(decision, lines))


def related(vault: Path, new_fact: rules.NewFact, slug: str | None) -> tuple[Line, ...]:
    """The existing lines most related to the fact, at most ``LIMIT``, best first.

    A line shares at least one word with the fact or it is not related. Ties keep page
    and line order, so the same vault always gives the same list."""
    candidates: list[tuple[str, str | None, page.Fact, dict[str, dict]]] = []
    for path in _pages(vault, new_fact.scope, slug):
        candidates.extend(_lines(vault, path))
    words = sorted({word.lower() for word in _WORD.findall(new_fact.statement)})
    if not candidates or not words:
        return ()
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE lines USING fts5(statement)")
        conn.executemany(
            "INSERT INTO lines (rowid, statement) VALUES (?, ?)",
            [(n, fact.statement) for n, (_, _, fact, _) in enumerate(candidates)],
        )
        query = " OR ".join(f'"{word}"' for word in words)
        ranked = conn.execute(
            "SELECT rowid FROM lines WHERE lines MATCH ? ORDER BY bm25(lines), rowid LIMIT ?",
            (query, LIMIT),
        ).fetchall()
    finally:
        conn.close()
    return tuple(Line(f"L{n}", *candidates[rowid]) for n, (rowid,) in enumerate(ranked, start=1))


def ask(
    config: Config, new_fact: rules.NewFact, quote: str, lines: Sequence[Line]
) -> rules.Decision:
    """Put the fact and the lines to the model and return its decision as it gave it."""
    answer = model.complete_json(
        config, prompt(), render(new_fact, quote, lines), schema(), MAX_TOKENS
    )
    return rules.Decision(action=answer["action"], line=answer["line"], reason=answer["reason"])


def render(new_fact: rules.NewFact, quote: str, lines: Sequence[Line]) -> str:
    """The user message: the fact, its quote, and the numbered lines."""
    parts = [
        f"New fact ({new_fact.kind}): {new_fact.statement}",
        f"Quote: {json.dumps(quote, ensure_ascii=False)}",
        "",
    ]
    if not lines:
        parts.append("Existing lines: none.")
    else:
        parts.append("Existing lines:")
        parts.extend(
            f"{line.label} ({line.fact.kind or 'candidate'}): {line.fact.statement}"
            for line in lines
        )
    return "\n".join(parts) + "\n"


def prompt() -> str:
    return _shipped(PROMPT_PATH)


def schema() -> dict:
    return json.loads(_shipped(SCHEMA_PATH))


def _shipped(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DecideError(f"cannot read {path.name}, which ships with aft: {exc}") from exc


def _target(decision: rules.Decision, lines: Sequence[Line]) -> Line | None:
    if decision.action not in TARGETED:
        return None
    return next((line for line in lines if line.label == decision.line), None)


def _pages(vault: Path, scope: str, slug: str | None) -> list[Path]:
    """The pages a fact's decision may edit, in path order."""
    roots = [vault / PROJECT_PAGES / slug] if slug else []
    if scope == "global":
        roots.append(vault / GLOBAL_PAGES)
    return [path for root in roots if root.is_dir() for path in sorted(root.rglob("*.md"))]


def _lines(vault: Path, path: Path) -> list[tuple[str, str | None, page.Fact, dict[str, dict]]]:
    """The retrievable lines of one page, with the project the page belongs to.

    Whether a page is global is read from its path, never from its frontmatter, because
    the rules read a None project as a global page (R6). A project page takes its project
    from ``aft.project``; one without it, a page that cannot be read, and one whose
    frontmatter does not parse give no lines, since which project they belong to is
    unknown. ``aft lint``, which the run passes before it commits, names each of them."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    frontmatter, _, _, error = page.split_frontmatter(text)
    if error:
        return []
    rel = path.relative_to(vault).as_posix()
    project: str | None = None
    if not rel.startswith(f"{GLOBAL_PAGES}/"):
        aft = (frontmatter or {}).get("aft")
        project = aft.get("project") if isinstance(aft, dict) else None
        if not isinstance(project, str) or not project:
            return []
    sources = {
        str(entry["id"]): entry
        for entry in (frontmatter or {}).get("sources") or []
        if isinstance(entry, dict) and entry.get("id") is not None
    }
    return [
        (
            rel,
            project,
            fact,
            {ref: sources[ref] for ref in fact.refs if ref in sources},
        )
        for fact in page.parse(text, path.relative_to(vault)).facts
        if fact.state in RETRIEVED
    ]
