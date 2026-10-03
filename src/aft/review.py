"""``aft review``: the lines of memory that F46's test would not keep (BUILD-PLAN F48).

Memory written before the test existed holds lines it would not keep: progress on the
work, the status of a pull request, what a change did to the code. ``aft review``
<project|global> puts every active line of that scope's fact pages (``recent.md``
included; a line with an open note is active memory too; never digests, reports,
the archive or references) to the model, about
twenty lines per call, with F46's one test and never-a-fact list
(``prompts/review.md``), and saves the numbered lines it would withdraw, each with a
one-line reason, to ``reports/review-<scope>.md``. It changes no page.

Withdrawing is the user's order, given after the list is shown: ``aft review <scope>
--withdraw <n,n,...|all>`` strikes the chosen lines the way ``aft settle --withdraw``
does, records beside each in the report what became of it, and commits the pages and
the report as one vault commit. The saved list is read and the numbers resolved under
the vault lock. A line that changed since the list was saved, or one with an open note,
is skipped and named; a number the list does not carry refuses the whole withdrawal, and
a number that already has its result is left as it is.

The model only judges; the script does every write (R9). The calls count against the
day's token budget with the digests' and the dream's (DESIGN 13): a budget already
spent refuses the review, and one spent part-way leaves the rest of the lines waiting.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from aft import page
from aft.config import Config

PROMPTS = Path(__file__).resolve().parent / "prompts"
PROMPT_PATH = PROMPTS / "review.md"
SCHEMA_PATH = PROMPTS / "review.schema.json"

PART = 20
"""How many lines one call sees (BUILD-PLAN F48: about twenty lines per call)."""

MAX_TOKENS = 2048
"""The answer carries up to twenty line numbers, each with a 200-character reason."""

GLOBAL = "global"
"""The scope that reviews the global pages (BUILD-PLAN F48)."""


_MISMATCH = "the line changed since the list was saved"

REPORT_DIR = "reports"
"""Where a review's list is saved, beside the other reports (DESIGN 6)."""

REVIEWED_STATES = (page.ACTIVE, page.CONFLICT, page.NEEDS_YOU)
"""The states whose lines are active memory (DESIGN 6 injects all three), so the
review asks about a line with an open note too; a withdrawal still skips it."""


class ReviewError(Exception):
    """The review or a withdrawal cannot be done as asked."""


@dataclass(frozen=True)
class Line:
    """One active line the review reads, before the model has judged it."""

    page: str
    """The page's vault-relative path."""

    line: int
    """The line's number in the file."""

    section: str
    statement: str
    tag: str
    date: str
    refs: tuple[str, ...]

    @property
    def kind(self) -> str:
        return page.KIND_SECTIONS[self.section]

    @property
    def name(self) -> str:
        """The page's file name, which the render shows the model."""
        return Path(self.page).name


@dataclass(frozen=True)
class Finding:
    """One line the model would withdraw, numbered across the whole review."""

    number: int
    """The list's number, the one the withdrawal takes."""

    page: str
    """The page's vault-relative path, as it was when the list was saved."""

    line: int
    """The line's number in the file, as it was when the list was saved."""

    section: str
    statement: str
    tag: str
    date: str
    refs: tuple[str, ...]
    reason: str
    result: str | None = None
    """What became of the line once a withdrawal ran: ``withdrawn <date>``, or
    ``skipped: <why>``. Empty until then."""


@dataclass(frozen=True)
class Saved:
    """The review list as it was saved: what it recorded, and its findings."""

    scope: str
    when: str
    reviewed: int
    calls: int
    shown: int
    pages_read: int
    waiting: int
    failure: str | None
    unreadable: tuple[str, ...]
    findings: tuple[Finding, ...]


@dataclass
class ReviewOutcome:
    """What one review did, and where its list is."""

    scope: str
    reviewed: int
    pages_read: int
    unreadable: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    unshown: tuple[str, ...] = ()
    """Labels the model named that no part showed, ignored."""
    calls: int = 0
    shown: int = 0
    """Lines a part the model answered carried; a part that waited or failed showed
    its lines to no one, so the report does not count them as kept."""
    waiting: int = 0
    """Parts not reviewed because the day's token budget was spent (DESIGN 13)."""
    failure: str | None = None
    report: Path | None = None
    committed: bool = False


@dataclass
class WithdrawOutcome:
    """What one withdrawal did, and what it named."""

    scope: str
    withdrawn: tuple[Finding, ...] = ()
    skipped: tuple[tuple[Finding, str], ...] = ()
    message: str | None = None
    committed: bool = False


def review(config: Config, scope: str, now: datetime | None = None) -> ReviewOutcome:
    """Put ``scope``'s active memory lines to the model and save what it would withdraw.

    ``scope`` is a registered project's slug or ``global``. Nothing a page holds
    changes; the list lands in ``reports/review-<scope>.md``, committed so the dream's
    quiet-period check never takes it for a hand edit. Raises ``ReviewError`` for a
    scope that is not registered and, when the day's token budget is already spent,
    before the first call (DESIGN 13).
    """
    from aft import model
    from aft import vault as vaultmod

    vaultmod.require(config)
    _check_scope(config, scope)
    moment = now or datetime.now(UTC)
    pgs = pages(config.vault, scope)
    lines, unreadable = collect(config.vault, scope)
    if not lines:
        return ReviewOutcome(
            scope=scope, reviewed=0, pages_read=len(pgs), unreadable=tuple(unreadable)
        )
    budget = config.settings.work.daily_token_budget
    if model.tokens_used_today(config, moment) >= budget:
        raise ReviewError(
            f"the day's token budget of {budget} tokens is already spent, so reviewing"
            f" {scope} waits for a day with room in it (DESIGN 13)"
        )
    parts = [lines[start : start + PART] for start in range(0, len(lines), PART)]
    findings: list[Finding] = []
    unshown: list[str] = []
    waiting = 0
    failure: str | None = None
    calls = 0
    lines_shown = 0
    for index, part in enumerate(parts):
        if index and model.tokens_used_today(config, moment) >= budget:
            waiting = len(parts) - index
            break
        try:
            answer = model.complete_json(
                config, prompt(), render(scope, part), schema(), MAX_TOKENS
            )
        except model.ModelError as exc:
            failure = f"part {index + 1} of {len(parts)} failed: {exc}"
            break
        calls += 1
        lines_shown += len(part)
        labelled = {f"L{position}": line for position, line in enumerate(part, start=1)}
        seen: set[str] = set()
        for item in answer["withdraw"]:
            label = item["line"]
            if label not in labelled:
                unshown.append(label)
                continue
            if label in seen:
                continue
            seen.add(label)
            line = labelled[label]
            findings.append(
                Finding(
                    number=len(findings) + 1,
                    page=line.page,
                    line=line.line,
                    section=line.section,
                    statement=line.statement,
                    tag=line.tag,
                    date=line.date,
                    refs=line.refs,
                    reason=item["reason"],
                )
            )
    outcome = ReviewOutcome(
        scope=scope,
        reviewed=len(lines),
        pages_read=len(pgs),
        unreadable=tuple(unreadable),
        findings=tuple(findings),
        unshown=tuple(unshown),
        calls=calls,
        shown=lines_shown,
        waiting=waiting,
        failure=failure,
    )
    report = report_path(config.vault, scope)
    with vaultmod.locked(config):
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(report_text(outcome, moment), encoding="utf-8")
        outcome.committed = vaultmod.commit(
            config.vault,
            [report.relative_to(config.vault).as_posix()],
            f"aft: review {scope}",
        )
    outcome.report = report
    return outcome


def withdraw(
    config: Config, scope: str, requested: str, now: datetime | None = None
) -> WithdrawOutcome:
    """Withdraw the chosen numbered lines of the saved review list for ``scope``.

    ``requested`` is ``all`` or a comma-separated list of the numbers the saved list
    carries; a number it does not carry refuses the whole withdrawal and nothing is
    written, and a number that already has its result is left as it is. Every chosen
    line is struck the way ``aft settle --withdraw`` does, dated today, and the report
    records what became of each; pages and report are one commit. The saved list is
    read and the numbers resolved under the vault lock. A line that changed since the
    list was saved, or one with an open note, is skipped and named.
    """
    from aft import index, pagewrite
    from aft import vault as vaultmod

    vaultmod.require(config)
    _check_scope(config, scope)
    moment = now or datetime.now(UTC)
    today = moment.astimezone(UTC).date().isoformat()
    withdrawn: list[Finding] = []
    skipped: list[tuple[Finding, str]] = []
    changed: set[str] = set()
    results: dict[int, str] = {}
    with vaultmod.locked(config):
        saved = _load_findings(config.vault, scope)
        wanted = _requested(saved.findings, scope, requested)
        _check_pages(scope, wanted)
        for rel in sorted({finding.page for finding in wanted}):
            try:
                doc = pagewrite.PageDoc.read(config.vault / rel, vault=config.vault)
            except (OSError, UnicodeDecodeError, pagewrite.WriteError) as exc:
                why = f"the page cannot be read: {exc}"
                for finding in (f for f in wanted if f.page == rel):
                    skipped.append((finding, why))
                    results[finding.number] = f"skipped: {why}"
                continue
            for finding in (f for f in wanted if f.page == rel):
                fact = _find(doc, finding)
                if fact is None:
                    skipped.append((finding, _MISMATCH))
                    results[finding.number] = f"skipped: {_MISMATCH}"
                    continue
                why = _why_not(fact)
                if why is not None:
                    skipped.append((finding, why))
                    results[finding.number] = f"skipped: {why}"
                    continue
                try:
                    doc.withdraw(fact, today)
                except pagewrite.WriteError as exc:
                    skipped.append((finding, str(exc)))
                    results[finding.number] = f"skipped: {exc}"
                    continue
                withdrawn.append(replace_result(finding, f"withdrawn {today}"))
                results[finding.number] = f"withdrawn {today}"
                changed.add(rel)
            if rel in changed:
                (config.vault / rel).write_text(doc.render(), encoding="utf-8")
        updated = tuple(
            replace_result(f, results[f.number]) if f.number in results else f
            for f in saved.findings
        )
        message = None
        committed = False
        if updated != saved.findings:
            report = report_path(config.vault, scope)
            report.write_text(
                report_text(_as_outcome(scope, saved, updated), moment, reviewed_at=saved.when),
                encoding="utf-8",
            )
            paths = sorted(changed) + [report.relative_to(config.vault).as_posix()]
            message = f"aft: withdraw {len(withdrawn)} review line(s) in {scope}"
            committed = vaultmod.commit(config.vault, paths, message)
    index.update(config)
    return WithdrawOutcome(
        scope=scope,
        withdrawn=tuple(withdrawn),
        skipped=tuple(skipped),
        message=message,
        committed=committed,
    )


def pages(vault: Path, scope: str) -> list[Path]:
    """The fact pages the review of ``scope`` reads, in path order: the project's own
    under ``projects/<slug>/``, or the ones under ``global/``, each without its
    ``index.md``. Never digests, reports, the archive or references (BUILD-PLAN F48)."""
    root = vault / "global" if scope == GLOBAL else vault / "projects" / scope
    if not root.is_dir():
        return []
    return [path for path in sorted(root.rglob("*.md")) if path.name != "index.md"]


def collect(vault: Path, scope: str) -> tuple[list[Line], list[str]]:
    """The lines those pages hold that the review asks about, in page and line order,
    and the pages that could not be read, each with why. A line with an open note is
    active memory too (DESIGN 6 injects it), so it is asked about, though a withdrawal
    still skips it; struck, stale and candidate lines are not asked about."""
    lines: list[Line] = []
    unreadable: list[str] = []
    for path in pages(vault, scope):
        rel = path.relative_to(vault).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            unreadable.append(f"{rel}: cannot be read: {exc}")
            continue
        frontmatter, _, _, error = page.split_frontmatter(text)
        if error or frontmatter is None:
            unreadable.append(f"{rel}: {error or 'no frontmatter, so not a fact page'}")
            continue
        for fact in page.parse(text, Path(rel)).facts:
            if fact.section in page.KIND_SECTIONS and fact.state in REVIEWED_STATES:
                lines.append(
                    Line(
                        page=rel,
                        line=fact.line,
                        section=fact.section,
                        statement=fact.statement,
                        tag=fact.source,
                        date=fact.date,
                        refs=fact.refs,
                    )
                )
    return lines, unreadable


def report_path(vault: Path, scope: str) -> Path:
    return vault / REPORT_DIR / f"review-{scope}.md"


def prompt() -> str:
    return _shipped(PROMPT_PATH)


def schema() -> dict:
    try:
        parsed = json.loads(_shipped(SCHEMA_PATH))
    except json.JSONDecodeError as exc:
        raise ReviewError(f"the review schema {SCHEMA_PATH.name} is malformed: {exc}") from None
    if not isinstance(parsed, dict):
        raise ReviewError(f"the review schema {SCHEMA_PATH.name} is not a JSON object")
    return parsed


def replace_result(finding: Finding, result: str | None) -> Finding:
    return Finding(
        number=finding.number,
        page=finding.page,
        line=finding.line,
        section=finding.section,
        statement=finding.statement,
        tag=finding.tag,
        date=finding.date,
        refs=finding.refs,
        reason=finding.reason,
        result=result,
    )


def render(scope: str, part: Sequence[Line]) -> str:
    """The user message: which memory the lines come from, and the numbered lines."""
    name = "the global pages" if scope == GLOBAL else f"the project {scope}"
    parts = [f"Lines of memory in {name}, numbered L1 to L{len(part)}:", ""]
    parts.extend(
        f"L{position} ({line.kind}, {line.tag}, {line.name}, {line.date}): {line.statement}"
        for position, line in enumerate(part, start=1)
    )
    return "\n".join(parts) + "\n"


def report_text(outcome: ReviewOutcome, when: datetime, reviewed_at: str | None = None) -> str:
    """The saved list: frontmatter the withdrawal reads back, and the list itself."""
    frontmatter = {
        "type": "Review",
        "title": f"Review {outcome.scope}",
        "description": "The lines the review would withdraw, and what became of each.",
        "generated": {"by": "aft", "at": when.isoformat()},
        "aft": {
            "review": {
                "scope": outcome.scope,
                "when": reviewed_at or when.isoformat(),
                "reviewed": outcome.reviewed,
                "calls": outcome.calls,
                "shown": outcome.shown,
                "pages_read": outcome.pages_read,
                "waiting": outcome.waiting,
                "failure": outcome.failure,
                "unreadable": list(outcome.unreadable),
                "findings": [_as_record(f) for f in outcome.findings],
            }
        },
    }
    reviewed_on = reviewed_at[:10] if reviewed_at else when.astimezone(UTC).date().isoformat()
    body = [
        f"# Review {outcome.scope}",
        "",
        f"The review read {outcome.reviewed} active line(s) on {outcome.pages_read}"
        f" page(s) of {outcome.scope} in {outcome.calls} model call(s) on"
        f" {reviewed_on}, and the model would withdraw"
        f" {len(outcome.findings)} of them. No page has changed; the withdrawal is"
        " yours to order, by the numbers below:",
        "",
        f"    aft review {outcome.scope} --withdraw <n,n,...>",
        f"    aft review {outcome.scope} --withdraw all",
        "",
        "A line that changed since this list was saved, or one with an open note, is"
        " skipped and named when the withdrawal runs.",
        "",
    ]
    body.append("## Would withdraw")
    body.append("")
    if outcome.findings:
        for finding in outcome.findings:
            body.append(
                f"{finding.number}. {finding.page}:{finding.line} - {finding.statement}"
                f" [{finding.tag}] {finding.date}"
            )
            body.append(f"   Reason: {finding.reason}")
            if finding.result is not None:
                body.append(f"   {finding.result[0].upper()}{finding.result[1:]}.")
        body.append("")
    elif outcome.shown:
        body.append("Nothing. Memory keeps every line the model saw.")
    else:
        body.append("Nothing. No line was judged; see Incomplete for why.")
    body.append("")
    body.append("## Kept")
    body.append("")
    body.append(
        f"The other {outcome.shown - len(outcome.findings)} line(s) the model saw stay as they are."
    )
    body.append("")
    if outcome.unreadable:
        body.append("## Not read")
        body.append("")
        body.extend(f"- {entry}" for entry in outcome.unreadable)
        body.append("")
    for section in _incomplete(outcome):
        body.append("## Incomplete")
        body.append("")
        body.append(section)
        body.append("")
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=100000).strip()
        + "\n---\n\n"
        + "\n".join(body).rstrip()
        + "\n"
    )


def _incomplete(outcome: ReviewOutcome) -> list[str]:
    """Why the list may not cover every line, when it may not."""
    notes = []
    if outcome.waiting:
        notes.append(
            f"{outcome.waiting} part(s) wait, the day's token budget is spent; rerun the"
            " review on a day with room in it."
        )
    if outcome.failure:
        notes.append(outcome.failure)
    return notes


def _as_record(finding: Finding) -> dict:
    return {
        "number": finding.number,
        "page": finding.page,
        "line": finding.line,
        "section": finding.section,
        "statement": finding.statement,
        "tag": finding.tag,
        "date": finding.date,
        "refs": list(finding.refs),
        "reason": finding.reason,
        "result": finding.result,
    }


def _load_findings(vault: Path, scope: str) -> Saved:
    """The saved review list for ``scope``, read back from its frontmatter."""
    path = report_path(vault, scope)
    rel = path.relative_to(vault).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewError(
            f"no saved review list for {scope}: {rel} cannot be read: {exc}"
        ) from None
    frontmatter, _, _, error = page.split_frontmatter(text)
    if error or not isinstance(frontmatter, dict):
        raise ReviewError(
            f"the saved review list {rel} cannot be read: {error or 'no frontmatter'}"
        ) from None
    aft = frontmatter.get("aft")
    record = aft.get("review") if isinstance(aft, dict) else None
    if not isinstance(record, dict):
        raise ReviewError(
            f"the saved review list {rel} holds no review record; run aft review {scope} again"
        ) from None
    try:
        saved = Saved(
            scope=str(record["scope"]),
            when=str(record["when"]),
            reviewed=int(record["reviewed"]),
            calls=int(record["calls"]),
            shown=int(record.get("shown") or 0),
            pages_read=int(record["pages_read"]),
            waiting=int(record.get("waiting") or 0),
            failure=str(record["failure"]) if record.get("failure") else None,
            unreadable=tuple(str(entry) for entry in record.get("unreadable") or ()),
            findings=_read_findings(record.get("findings"), rel),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ReviewError(f"the saved review list {rel} is malformed: {exc}") from None
    if saved.scope != scope:
        raise ReviewError(
            f"the saved review list {rel} is for {saved.scope}, not {scope};"
            f" run aft review {scope} again"
        )
    return saved


def _read_findings(records: object, rel: str) -> tuple[Finding, ...]:
    if records is None:
        return ()
    if not isinstance(records, list):
        raise ReviewError(f"the saved review list {rel} holds a malformed findings list") from None
    findings: list[Finding] = []
    for record in records:
        if not isinstance(record, dict):
            raise ReviewError(f"the saved review list {rel} holds a malformed finding") from None
        try:
            findings.append(
                Finding(
                    number=int(record["number"]),
                    page=str(record["page"]),
                    line=int(record["line"]),
                    section=str(record["section"]),
                    statement=str(record["statement"]),
                    tag=str(record["tag"]),
                    date=str(record["date"]),
                    refs=tuple(str(ref) for ref in record.get("refs") or ()),
                    reason=str(record["reason"]),
                    result=record.get("result") or None,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError(
                f"the saved review list {rel} holds a malformed finding: {exc}"
            ) from None
    return tuple(findings)


def _requested(saved: tuple[Finding, ...], scope: str, requested: str) -> tuple[Finding, ...]:
    """The findings the user chose: every one without a recorded result, for ``all``
    as for a number; a line with a result is left as it is. A number the list does not
    carry refuses the whole withdrawal (BUILD-PLAN F48)."""
    text = requested.strip()
    if text.lower() == "all":
        return tuple(f for f in saved if f.result is None)
    numbers: list[int] = []
    for token in text.split(","):
        token = token.strip()
        if not token.isdigit():
            raise ReviewError(
                f"--withdraw takes the numbers of the saved list or 'all', not {token!r}"
            )
        numbers.append(int(token))
    if not numbers:
        raise ReviewError("--withdraw takes the numbers of the saved list or 'all'")
    carried = {finding.number for finding in saved}
    unknown = sorted({number for number in numbers if number not in carried})
    if unknown:
        span = f"it numbers {min(carried)} to {max(carried)}" if carried else "it is empty"
        raise ReviewError(
            f"the saved list for {scope} carries no line"
            f" {', '.join(str(number) for number in unknown)} ({span});"
            f" run aft review {scope} again to save a new list"
        )
    wanted = set(numbers)
    return tuple(f for f in saved if f.number in wanted and f.result is None)


def _find(doc, finding: Finding) -> page.Fact | None:
    """The line the finding names, found by what it says: its section, statement, tag,
    date and sources (BUILD-PLAN F48 review fix). A line that only moved since the list
    was saved, a dream add above it, say, is still the same line; the saved line number
    only chooses between identical lines."""
    matches = [
        fact
        for fact in doc.page.facts
        if fact.section == finding.section
        and fact.statement == finding.statement
        and fact.source == finding.tag
        and fact.date == finding.date
        and fact.refs == finding.refs
    ]
    if not matches:
        return None
    return min(matches, key=lambda fact: (abs(fact.line - finding.line), fact.line))


def _why_not(fact: page.Fact) -> str | None:
    """Why the line the finding names cannot be struck as it was saved, or None when it
    can be withdrawn. The line was found by its section, statement, tag, date and
    sources, so only its state can stand in the way."""
    if fact.state == page.WITHDRAWN:
        return "already withdrawn"
    if fact.state in (page.CONFLICT, page.NEEDS_YOU):
        return "it has an open note; aft settle settles it"
    if fact.state != page.ACTIVE:
        return _MISMATCH
    return None


def _check_pages(scope: str, wanted: tuple[Finding, ...]) -> None:
    """A finding's page is validated the way ``aft settle`` validates the page it is
    given (DESIGN 12): inside the vault, under a directory fact pages live in. A page
    outside them means the saved list was edited or corrupted by hand, so the withdrawal
    is refused before anything is written."""
    for finding in wanted:
        parts = Path(finding.page).parts
        if not parts or parts[0] not in page.FACT_DIRS or ".." in parts:
            raise ReviewError(
                f"the saved review list for {scope} names {finding.page!r}, which is"
                f" not a fact page; run aft review {scope} again to save a new list"
            )


def _as_outcome(scope: str, saved: Saved, findings: tuple[Finding, ...]) -> ReviewOutcome:
    """The outcome the report records after a withdrawal: the same review, each finding
    carrying its result."""
    return ReviewOutcome(
        scope=scope,
        reviewed=saved.reviewed,
        pages_read=saved.pages_read,
        unreadable=saved.unreadable,
        findings=findings,
        calls=saved.calls,
        shown=saved.shown,
        waiting=saved.waiting,
        failure=saved.failure,
    )


def _check_scope(config: Config, scope: str) -> None:
    """The scope is ``global`` or a registered project's slug (BUILD-PLAN F48)."""
    if scope == GLOBAL:
        return
    from aft.registry import Registry

    if Registry.load(config.registry_path).by_slug(scope) is None:
        raise ReviewError(f"no project {scope!r} is registered; aft project list names them")


def _shipped(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewError(f"cannot read {path.name}, which ships with aft: {exc}") from exc
