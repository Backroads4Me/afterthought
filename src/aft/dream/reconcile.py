"""What the newest session shows about a project's short-term memory (BUILD-PLAN F47).

A short-term line that a later session showed was finished, wrong, or replaced used to
wait for session roll-off, and a project with no new sessions kept it for good. When a
run applies a project's digests and ``recent.md`` holds active lines, one call per part
of the newest primary session applied - its archived entries, split under the digest's
chunk cap - puts the numbered short-term lines beside the session's own words. For each
line that a part shows is done, false or replaced, the answer names the line's number, a
quote, and the entry the quote comes from.

The script judges nothing. It verifies each quote against its entry as R1 does, and
withdraws on ``recent.md`` only what verifies; an answer whose quote is not in the entry
it names changes nothing and is reported. The step never touches a long-term line (it can
name only lines it numbered from ``recent.md``), and a worker session's words never reach
it: it reads only the newest session this run applied that was a primary session. A line
with an open note is numbered by no one - a note's state is the note's, not ``active`` -
so the model is never told of it and cannot withdraw it; the note waits on you (DESIGN 6).
Like every model call it counts against the day's token budget, and it is skipped when
that budget is spent.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from aft import digest, evidence, model, page, pagewrite
from aft.config import Config
from aft.dream import shortterm

if TYPE_CHECKING:
    from aft.dream.apply import ProjectRunReport

PROMPT_FILE = "prompts/reconcile.md"
SCHEMA_FILE = "prompts/reconcile.schema.json"
MAX_TOKENS = 512
"""The answer is a list of line numbers with a quote and an entry each."""

R1 = "R1"
RULE = "reconcile"
"""The rule a refused answer names: a quote that verifies nothing breaks R1, and an answer
that names a line or an entry it was not given fails this step's own mismatch guard."""


class ReconcileError(Exception):
    """The prompt or schema that ships with aft cannot be read."""


@dataclass(frozen=True)
class WithdrawnLine:
    """A short-term line the newest session showed done, false, or replaced."""

    statement: str
    quote: str
    """The quote, verified against the entry it came from, that showed it."""


@dataclass(frozen=True)
class RefusedWithdrawal:
    """A recent line the model asked to withdraw and the script refused."""

    statement: str
    """The line's statement, or the number the answer gave when it named no line."""
    rule: str
    reason: str


def run(
    config: Config,
    vault_path: Path,
    slug: str,
    digests: Sequence[Path],
    today: str,
    report: ProjectRunReport,
    record_write,
    now: datetime | None = None,
) -> None:
    """Reconcile the project's short-term lines with the newest primary session applied.

    ``digests`` is the list this run applied, in session order; the newest primary
    session among them is the one the short-term lines are read against. Makes no call
    and changes nothing when the project has no ``recent.md``, no active line on it, or
    no primary session applied, when the session's entries are not in the evidence
    archive, or when the day's token budget is spent.
    """
    recent = vault_path / "projects" / slug / page.RECENT_PAGE
    if not recent.exists():
        return
    doc = pagewrite.PageDoc.read(recent, vault=vault_path)
    numbered = [fact for fact in doc.page.facts if fact.state == page.ACTIVE]
    if not numbered:
        return
    target = newest_primary(vault_path, digests)
    if target is None:
        return
    _agent, session_id = target
    entries = evidence.entries(config, session_id)
    if not entries:
        return
    if model.tokens_used_today(config, now) >= config.settings.work.daily_token_budget:
        return
    system = prompt()
    preamble = _numbered(numbered)
    # The cap bounds one call's input, the prompt and the lines block included, as the
    # digest's cap does (DESIGN 13), so the entries are divided by what is left of it.
    budget = max(1, digest.MAX_CHUNK_TOKENS - digest.tokens(system) - digest.tokens(preamble))
    for part in digest.split_parts(entries, budget):
        answer = model.complete_json(
            config, system, _user_message(preamble, part), schema(), MAX_TOKENS
        )
        _apply_answers(
            config, doc, recent, numbered, part, session_id, answer, today, report, record_write
        )


def newest_primary(vault_path: Path, digests: Sequence[Path]) -> tuple[str, str] | None:
    """The newest primary session among the digests a run applied, as (agent, session id).

    The digests arrive in session order and are applied in it, so the last primary one is
    the newest applied. A worker's digest is no session of the project's own work (DESIGN
    8): its entries never reconcile short-term memory. A digest filed as a later part of
    one day names its session's first part, where the evidence archive holds the session.
    """
    stems = {path.stem for path in (vault_path / shortterm.DIGESTS).rglob("*.md")}
    newest: tuple[str, str] | None = None
    for path in digests:
        named = digest._DIGEST_NAME.match(path.stem)
        if named is None:
            continue
        try:
            frontmatter = page.split_frontmatter(path.read_text(encoding="utf-8"))[0] or {}
        except (OSError, UnicodeDecodeError):
            continue
        aft = frontmatter.get("aft")
        if isinstance(aft, dict) and aft.get("role") == "worker":
            continue
        agent, session = named.group("agent"), named.group("session")
        part = shortterm._PART.match(session)
        if part and f"{named.group('date')}-{agent}-{part.group('base')}" in stems:
            session = part.group("base")
        newest = (agent, session)
    return newest


def _numbered(numbered: Sequence[page.Fact]) -> str:
    """The short-term lines as the answer's numbers name them."""
    lines = ["Short-term lines:"]
    lines.extend(f"{n}: {fact.statement}" for n, fact in enumerate(numbered, start=1))
    return "\n".join(lines)


def _user_message(preamble: str, part: Sequence[digest.Entry]) -> str:
    """The lines block and one part of the session's entries, under the labels ``render``
    shows them by."""
    return f"{preamble}\n\nSession entries:\n\n{digest.render(part)}"


def _apply_answers(
    config: Config,
    doc: pagewrite.PageDoc,
    recent: Path,
    numbered: Sequence[page.Fact],
    part: Sequence[digest.Entry],
    session_id: str,
    answer: dict,
    today: str,
    report: ProjectRunReport,
    record_write,
) -> None:
    """Withdraw what one part's answer shows and verifies; refuse and report the rest.

    A withdrawal strikes the line through and dates it, the grammar's own ``withdrawn``
    form; the line keeps the sources it was built on, and the quote that showed it done is
    reported beside it. The answer's numbers name the lines as they were numbered, and a
    withdrawal rewrites its line in place, so they hold across the whole answer.
    """
    by_entry = digest._cited(part)
    changed = False
    for item in answer["lines"]:
        number = item["line"]
        fact = numbered[number - 1] if 1 <= number <= len(numbered) else None
        if fact is None:
            report.refused.append(
                RefusedWithdrawal(
                    statement=f"line {number}",
                    rule=RULE,
                    reason=f"line {number} is not a line this run numbered",
                )
            )
            continue
        entry = by_entry.get(item["entry"])
        if entry is None:
            report.refused.append(
                RefusedWithdrawal(
                    statement=fact.statement,
                    rule=RULE,
                    reason=f"entry {item['entry']!r} is not an entry of this part",
                )
            )
            continue
        if not evidence.find_quote(config, session_id, entry.id, item["quote"]):
            report.refused.append(
                RefusedWithdrawal(
                    statement=fact.statement,
                    rule=R1,
                    reason=f"the quote is not in entry {entry.id}",
                )
            )
            continue
        current = _active(doc, fact.statement)
        if current is None:
            # An answer may name a line an earlier item already withdrew; the withdrawal
            # is counted once, where it happened.
            continue
        try:
            doc.withdraw(current, today)
        except pagewrite.WriteError as exc:
            report.refused.append(
                RefusedWithdrawal(statement=current.statement, rule=RULE, reason=str(exc))
            )
            continue
        report.withdrawn.append(WithdrawnLine(statement=current.statement, quote=item["quote"]))
        changed = True
    if changed:
        record_write(recent)
        recent.write_text(doc.render(), encoding="utf-8")


def _active(doc: pagewrite.PageDoc, statement: str) -> page.Fact | None:
    """The one active line with this statement on the page as it stands now, or None."""
    found = [
        fact for fact in doc.page.facts if fact.statement == statement and fact.state == page.ACTIVE
    ]
    return found[0] if len(found) == 1 else None


def prompt() -> str:
    """The system prompt every reconcile call sends, shipped as the plan's fixed text."""
    return (files("aft") / PROMPT_FILE).read_text(encoding="utf-8")


def schema() -> dict:
    """The schema the model's answer is constrained and checked by."""
    try:
        text = (files("aft") / SCHEMA_FILE).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReconcileError(
            f"the shipped reconcile schema at {SCHEMA_FILE} cannot be read: {exc}"
        ) from exc
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ReconcileError(
            f"the shipped reconcile schema at {SCHEMA_FILE} is not JSON: {exc}"
        ) from exc
