"""What the newest session shows about a project's short-term memory (BUILD-PLAN F47).

A short-term line that a later session showed was finished, wrong, or replaced used to
wait for session roll-off, and a project with no new sessions kept it for good. When a
run applies a project's digests and ``recent.md`` holds active lines, one call per part
of the newest primary session applied - its archived entries, split under the digest's
chunk cap - puts the numbered short-term lines beside the session's own words. For each
line that a part shows is done, false or replaced, the answer names the line's number, a
quote, and the entry the quote comes from.

The script judges nothing. It verifies each quote against its entry as R1 does, and a
withdrawal passes only when the evidence may replace the line, under the same authority
table and R4 a supersede answers to (DESIGN 6): a [stated] preference is withdrawn only
on your own words, and the words have to be newer than the line: a later day, or a later
entry the same day than every source the line cites (F58). Anything
less changes nothing and is reported. The step never touches a long-term line (it can
name only lines it numbered from ``recent.md``), and a worker session's words never reach
it: it reads only the newest session this run applied that was a primary session, and the
entries it reads are that agent's, matched by agent and session id and never by the id
alone. A line with an open note is numbered by no one - a note's state is the note's, not
``active`` - so the model is never told of it and cannot withdraw it; the note waits on
you (DESIGN 6). Like every model call it counts against the day's token budget, and it is
skipped when that budget is spent.
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
from aft.dream import rules, shortterm
from aft.readers import TOOL, USER

if TYPE_CHECKING:
    from aft.dream.apply import ProjectRunReport

PROMPT_FILE = "prompts/reconcile.md"
SCHEMA_FILE = "prompts/reconcile.schema.json"

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
    page_slug: str,
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
    no primary session applied, when that session's entries are not in the evidence
    archive, or when the day's token budget is spent. ``page_slug`` is the project's slug,
    which is where ``recent.md`` lives; it and the directory the digests were filed under
    are the same unless the project was registered under another name (DESIGN 7, F49).
    """
    recent = vault_path / "projects" / page_slug / page.RECENT_PAGE
    if not recent.exists():
        return
    doc = pagewrite.PageDoc.read(recent, vault=vault_path)
    numbered = [fact for fact in doc.page.facts if fact.state == page.ACTIVE]
    if not numbered:
        return
    target = newest_primary(vault_path, digests)
    if target is None:
        return
    agent, session_id = target
    entries = evidence.entries(config, session_id, agent=agent)
    if not entries:
        return
    system = prompt()
    preamble = _numbered(numbered)
    answer_schema = schema()
    # The cap bounds one call's input, the prompt and the lines block included, as the
    # digest's cap does (DESIGN 13), so the entries are divided by what is left of it.
    budget = max(1, digest.MAX_CHUNK_TOKENS - digest.tokens(system) - digest.tokens(preamble))
    cap = max_tokens(answer_schema)
    for part in digest.split_parts(entries, budget):
        # The daily cap counts the reconcile's calls with the digests' (DESIGN 13), so it
        # is checked before each part: a session of many parts stops at the day's last
        # affordable call instead of spending into a budget it has already used up.
        if model.tokens_used_today(config, now) >= config.settings.work.daily_token_budget:
            break
        answer = model.complete_json(
            config, system, _user_message(preamble, part), answer_schema, cap
        )
        _apply_answers(
            config,
            agent,
            session_id,
            page_slug,
            doc,
            recent,
            numbered,
            part,
            answer,
            today,
            report,
            record_write,
        )


def newest_primary(vault_path: Path, digests: Sequence[Path]) -> tuple[str, str] | None:
    """The newest primary session among the digests a run applied, as (agent, session id).

    The digests arrive in session order and are applied in it, so the last primary one is
    the newest applied. A worker's digest is no session of the project's own work (DESIGN
    8), and neither is a library or memory-import digest (DESIGN 11): neither reconciles
    short-term memory, so an applied digest of that kind leaves the newest applied session
    where it was. A digest filed as a later part of one day names its session's first
    part, where the evidence archive holds the session.
    """
    stems = {path.stem for path in (vault_path / shortterm.DIGESTS).rglob("*.md")}
    newest: tuple[str, str] | None = None
    for path in digests:
        named = shortterm.names_session(path, stems)
        if named is not None:
            agent, session, _ = named
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
    agent: str,
    session_id: str,
    page_slug: str,
    doc: pagewrite.PageDoc,
    recent: Path,
    numbered: Sequence[page.Fact],
    part: Sequence[digest.Entry],
    answer: dict,
    today: str,
    report: ProjectRunReport,
    record_write,
) -> None:
    """Withdraw what one part's answer shows, verifies and may replace; refuse the rest.

    A withdrawal strikes the line through and dates it, the grammar's own ``withdrawn``
    form; the line keeps the sources it was built on, and the quote that showed it done is
    reported beside it. The answer's numbers name the lines as they were numbered, and a
    withdrawal rewrites its line in place, so they hold across the whole answer.

    The quote alone does not withdraw: the answer's evidence has to carry the authority to
    replace the line and be newer than it, by the same authority table and R4 a supersede
    answers to (DESIGN 6). A withdrawal that fails that check changes nothing and is
    reported, like one whose quote does not verify.
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
        if not evidence.find_quote(config, session_id, entry.id, item["quote"], agent=agent):
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
        refused = _refused_withdrawal(
            current, entry, agent, session_id, page_slug, today, _source_times(config, doc, current)
        )
        if refused is not None:
            report.refused.append(refused)
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


def _refused_withdrawal(
    current: page.Fact,
    entry: digest.Entry,
    agent: str,
    session_id: str,
    page_slug: str,
    today: str,
    source_times: Sequence[datetime] = (),
) -> RefusedWithdrawal | None:
    """Why the rules refuse to withdraw ``current`` on ``entry``'s words, or None when
    they allow it.

    The answer's evidence is one verified quote out of the newest primary session, so it
    travels to the rules as one quote whose tag follows the entry: ``stated`` when
    the entry is one of your turns, ``observed`` for tool or error output, and
    ``inferred`` for assistant claims. The verdict is the one a
    supersede of this line on this evidence gets, judged by the rules module's own
    ``_supersede``, so a withdrawal answers to the same authority table and R4 a supersede
    does (DESIGN 6). A refusal writes nothing: the note a downgraded supersede would write
    is the decide step's to settle, so the line stands and the report carries why.
    """
    if entry.role == USER:
        tagged = "stated"
    elif entry.role == TOOL or entry.is_error:
        tagged = "observed"
    else:
        tagged = "inferred"
    quote_date = f"{entry.time:%Y-%m-%d}"
    quote = rules.Evidence(
        source=entry.id,
        resource=f"aft://session/{agent}/{session_id}#{entry.id}",
        origin=rules.PRIMARY,
        date=quote_date,
        verified=True,
        yours=entry.role == USER,
    )
    outcome = rules._supersede(
        rules.NewFact(current.statement, current.kind or "", tagged, quote_date, term=rules.SHORT),
        current,
        rules.Context(today=today, evidence=(quote,), run_project=page_slug, target_recent=True),
    )
    if isinstance(outcome, rules.Apply):
        return None
    authorized = tagged in rules.AUTHORITY.get(current.kind or "", ())
    if authorized and quote_date == current.date and _later(entry.time, source_times):
        # A recent line is withdrawn by a later entry of the same day (DESIGN 6, F58): the
        # day R4 compares cannot tell this morning's line from this afternoon's word.
        return None
    if not authorized:
        reason = (
            f"a {current.kind} line is withdrawn only on your own words"
            if current.kind in ("preference", "decision")
            else f"a {current.kind} line is withdrawn only on your own words or [observed] evidence"
            if current.kind
            else "the line has no kind the authority table gives a row"
        )
    else:
        reason = f"the quote is from {quote_date}, not newer than the line's {current.date}"
    return RefusedWithdrawal(statement=current.statement, rule=outcome.rule, reason=reason)


def _source_times(config: Config, doc: pagewrite.PageDoc, line: page.Fact) -> list[datetime]:
    """When each source the line cites was written, as far as the archive holds them."""
    sources = doc.sources
    times = []
    for ref in line.refs:
        resource = str(sources.get(ref, {}).get("resource", ""))
        when = evidence.entry_time(config, resource) if resource else None
        if when is not None:
            times.append(when)
    return times


def _later(when: datetime, source_times: Sequence[datetime]) -> bool:
    """Whether ``when`` follows every source time; with none known, the day decides alone."""
    return bool(source_times) and all(when > time for time in source_times)


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


def max_tokens(answer_schema: dict) -> int:
    """The output cap one call needs to hold the schema's worst answer (DESIGN 13).

    The answer is a list of line numbers with a quote and an entry each, so the worst
    answer is the lines array at its ``maxItems`` with every quote at its ``maxLength``,
    priced at the digest's characters-to-token estimate. A cap under it truncates the
    answer, the call fails its schema, and the project rolls back to hit the same wall on
    every later run. The punctuation around the quotes is priced at the prose rate, which
    undercounts it, so the cap is rounded up by half again.
    """
    lines = answer_schema["properties"]["lines"]
    items = lines["maxItems"]
    quote_cap = lines["items"]["properties"]["quote"]["maxLength"]
    worst = {
        "lines": [{"line": items, "quote": "x" * quote_cap, "entry": "#99"} for _ in range(items)]
    }
    return digest.tokens(json.dumps(worst)) * 3 // 2
