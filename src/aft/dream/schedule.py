"""When the dream runs, and on what (DESIGN 9, "A run", step 1).

``run`` is ``aft dream``. It takes the vault lock and holds it for the whole run, so no
digest, registry change or other dream lands in the vault while this one reads and writes
it. With nothing waiting for any project it does one piece of maintenance and stops: the
short-term lines whose ``dream.short_term_days`` days are spent are expired for every
registered project with a recent.md and nothing waiting (DESIGN 6, R10), because a
project whose sessions stopped gets no run to expire them.

A digest is waiting until the dream marks it consumed with ``aft.consumed`` in its
frontmatter (M4.5). A project with waiting digests runs only once its sessions have been
quiet for ``dream.quiet_minutes``, measured from the newest event the capture queue holds
for any directory that resolves to it.

Before any project runs, the vault's uncommitted changes are looked at, because the only
writer that leaves changes uncommitted is a person editing the vault by hand. If any of
them was modified within the quiet period, the person may still be typing, so no project
runs and nothing is written this time. Otherwise they are committed on their own as
``checkpoint: human edits``, so a dream commit never carries a person's bytes and each
can be reverted alone. The bare bullets the person added to fact pages are then tagged
``[stated]`` in a commit of their own, citing that checkpoint as their source (R2: your
own edit).

A run marks itself in progress before it writes and unmarks itself when it ends cleanly
(M8). A marker standing when no run is under it is a run that died mid-write: the next
run rolls the dirty working tree it left back instead of committing its partial edits as
a person's.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aft import digest, keys, model, page, pagewrite, queue, vault
from aft.config import Config
from aft.dream import apply, shortterm
from aft.dream.apply import ProjectRunReport
from aft.gitutil import git
from aft.registry import Registry

DIGESTS = "digests"
CHECKPOINT_MESSAGE = "checkpoint: human edits"
RUN_MARKER = "dream-run-in-progress"
"""The name of the file in the afterthought home that says a dream run is writing the vault."""

Process = Callable[[Config, str, Sequence[Path]], Any]
"""What a run does with one project that is ready: its slug and its waiting digests, in
session order. The decisions, rules and apply steps (M4.3 to M4.5) fill this in."""


@dataclass
class Run:
    """What one ``aft dream`` did, for the command line and the report."""

    waiting: dict[str, list[Path]] = field(default_factory=dict)
    """Every project with waiting digests, by slug, each list in session order."""

    taken: dict[str, int] = field(default_factory=dict)
    """The digests each project's runs consumed, summed over the runs one slug splits into
    (DESIGN 7, F49): a run that failed consumed nothing, and its digests wait again."""

    ran: list[str] = field(default_factory=list)
    active: list[str] = field(default_factory=list)
    """Projects skipped because a session was active within the quiet period."""

    edited: list[str] = field(default_factory=list)
    """Projects skipped because the vault holds a hand edit made within the quiet period."""

    checkpoint: str | None = None
    """The ``checkpoint: human edits`` commit this run made, if any."""

    tag_commit: str | None = None
    """The ``aft: tag hand edits from <checkpoint>`` commit this run made, if any."""

    tagged: list[str] = field(default_factory=list)
    """The pages whose hand-typed bullets this run tagged ``[stated]``."""

    reports: dict[str, Any] = field(default_factory=dict)
    """Structured report data from each project's run (M4.5, M4.6): one report per slug,
    which names every group's work when one slug ran as several (DESIGN 7, F49)."""

    failures: dict[str, str] = field(default_factory=dict)
    """Projects that failed this run, each mapped to the error that stopped it."""

    over_budget: list[str] = field(default_factory=list)
    """Projects left waiting because the day's token budget was spent (DESIGN 13)."""

    expired: list[str] = field(default_factory=list)
    """Projects whose short-term memory this run expired as maintenance, with nothing
    waiting for them (DESIGN 6, R10; BUILD-PLAN F47)."""


def run(config: Config, process: Process | None = None, now: datetime | None = None) -> Run:
    """Run the dream once: the short-term memory of every project with nothing waiting
    expired as maintenance, then every quiet project with waiting digests, one at a time.
    A run the last one left mid-write has its partial edits rolled back first (M8)."""
    vault.require(config)
    with vault.locked(config):
        now = now or datetime.now(UTC)
        # A run the last one left mid-write has its partial edits rolled back here, before
        # this run can commit them as a person's under the checkpoint message.
        _recover_interrupted_run(config)
        since = now - timedelta(minutes=config.settings.dream.quiet_minutes)
        result = Run(waiting=waiting_digests(config.vault))
        activity = last_activity(config)
        for slug in result.waiting:
            last = activity.get(slug)
            if last is not None and last > since:
                result.active.append(slug)
        ready = [slug for slug in result.waiting if slug not in result.active]
        changes = uncommitted(config.vault)
        if any(modified > since for modified in changes.values()):
            result.edited = ready
            return result
        from aft.dream import report as reportmod

        if not ready:
            # No project's digests are up, so the dream has nothing to decide and makes no
            # checkpoint here: that is a project run's work, and a run with nothing
            # waiting commits nothing of a person's. The expiry below waits for a clean
            # vault, so it never carries a hand edit in its own commit.
            if changes:
                return result
            with _run_in_progress(config, now):
                _expire_short_term(config, result, now, None, None)
                # A run with nothing waiting still writes the idle and gone section: a home
                # whose every project is idle is exactly the state the section exists to name
                # (DESIGN 7, F49). It appends to the day's report, once a day.
                try:
                    reportmod.write_idle_gone(config, now=now)
                except Exception as exc:
                    result.failures["idle-gone report"] = str(exc)
            return result
        if changes and vault.commit(config.vault, sorted(changes), CHECKPOINT_MESSAGE):
            result.checkpoint = git(["rev-parse", "HEAD"], cwd=config.vault).stdout.strip()
        # The checkpoint above went in first, so a person's hand edits are never under the
        # marker: every write from here on is this run's, and the next run meets a dirty
        # tree only if this one died mid-write, to roll it back rather than commit it as
        # a person's.
        with _run_in_progress(config, now):
            if result.checkpoint is not None:
                result.tagged = tag_hand_edits(config, result.checkpoint)
                if result.tagged:
                    result.tag_commit = git(["rev-parse", "HEAD"], cwd=config.vault).stdout.strip()
            checkpoint_reported = _expire_short_term(
                config, result, now, result.checkpoint, result.tag_commit
            )
            budget = config.settings.work.daily_token_budget
            registry = Registry.load(config.registry_path)
            for slug in ready:
                # The daily cap counts the dream's model calls with the digests' (DESIGN 13): a
                # project waits for the next day once it is spent, its digests still waiting.
                if model.tokens_used_today(config, now) >= budget:
                    result.over_budget.append(slug)
                    continue
                # One slug's digests may hold two directories' sessions (a project registered
                # over the claim, or a record from before paths were kept), so they are run by
                # the project each digest's own record names (DESIGN 7, F49).
                groups = apply.digest_groups(config, registry, slug, result.waiting[slug])
                taken = 0
                reports: list[Any] = []
                for group in groups:
                    # Any failure in one project is that project's own: its writes are rolled back
                    # by run_project, its error reaches the day's report, and the other projects
                    # still run (DESIGN 9, and BUILD-PLAN M4 follow-up).
                    try:
                        if process is not None:
                            rep = process(config, slug, group)
                        else:
                            rep = apply.run_project(config, slug, group, now=now)
                        result.ran.append(slug)
                    except Exception as exc:
                        result.failures[slug] = str(exc)
                        rep = ProjectRunReport(project=slug, error=str(exc))
                    else:
                        taken += len(group)
                    if rep is None:
                        continue
                    reports.append(rep)
                    # A failed run has nothing to revert of its own, but it still names the
                    # checkpoint and tag commits, so the run's Revert item is never lost
                    # (DESIGN 9 item 9).
                    if getattr(rep, "commit", None) is not None or slug in result.failures:
                        checkpoint, tag_commit = (
                            (None, None)
                            if checkpoint_reported
                            else (result.checkpoint, result.tag_commit)
                        )
                        failure = _report_run(config, slug, rep, now, checkpoint, tag_commit)
                        if failure is None:
                            checkpoint_reported = True
                        elif slug in result.failures:
                            result.failures[slug] = (
                                f"{result.failures[slug]}; its report failed: {failure}"
                            )
                        else:
                            result.failures[slug] = f"report not written: {failure}"
                if taken:
                    result.taken[slug] = taken
                if reports:
                    result.reports[slug] = _merge_reports(slug, reports)
            # gone are named in the report; nothing is archived or forgotten without the
            # person's go-ahead (DESIGN 7, F49).
            try:
                reportmod.write_idle_gone(config, now=now)
            except Exception as exc:
                result.failures["idle-gone report"] = str(exc)

        return result


def run_marker_path(config: Config) -> Path:
    """Where a dream run's in-progress marker lives: the afterthought home, beside the
    vault lock and the other run state, never the vault itself, so git never reads it as
    a person's uncommitted edit (M8)."""
    return config.home / RUN_MARKER


@contextmanager
def _run_in_progress(config: Config, now: datetime) -> Iterator[None]:
    """Mark a dream run in progress for the writes of the body it wraps.

    The marker comes off when the body ends cleanly, by return or fall-through; a body
    that raises, as a crashed run's does, leaves it standing, so the next run rolls the
    partial edits back instead of committing them as a person's. The vault lock is held
    for the whole body, so a marker standing while this run holds the lock belongs to a
    run that is no longer alive.
    """
    marker = run_marker_path(config)
    marker.parent.mkdir(parents=True, exist_ok=True)
    pagewrite.atomic_write_text(marker, f"{now.isoformat()} pid={os.getpid()}\n")
    finished = False
    try:
        yield
        finished = True
    finally:
        if finished:
            marker.unlink(missing_ok=True)


def _recover_interrupted_run(config: Config) -> bool:
    """Roll back the dirty working tree a crashed dream run left, and drop its marker.

    A run marks itself before it writes and unmarks itself when it ends cleanly, so a
    marker standing with no run under it is a run that died mid-write: every uncommitted
    change in the vault is that run's partial work, never a person's hand edit, and it
    is reverted rather than committed under ``CHECKPOINT_MESSAGE``. Returns whether a
    crashed run was recovered.
    """
    marker = run_marker_path(config)
    if not marker.is_file():
        return False
    changes = uncommitted(config.vault)
    if changes:
        paths = sorted(changes)
        # The checkout runs only on the paths still differing from the index, because a
        # checkout is all or nothing: one pathspec it does not know would leave every
        # restoration undone. The reset first takes the staged half of the dirt away, so
        # the diff names exactly the worktree the crashed run left behind.
        git(["reset", "-q", "--", *paths], cwd=config.vault, check=False)
        diff = git(["diff", "--name-only", "-z"], cwd=config.vault).stdout
        restore = [rel for rel in diff.split("\0") if rel]
        if restore:
            git(["checkout", "-q", "--", *restore], cwd=config.vault, check=False)
        git(["clean", "-qfd", "--", *paths], cwd=config.vault, check=False)
    marker.unlink(missing_ok=True)
    return True


def _merge_reports(slug: str, reports: Sequence[Any]) -> Any:
    """One slug's group reports as one, in group order (DESIGN 7, F49).

    One slug may run as several groups - two directories that once claimed one name - and
    each group's run reports what it did. The day's report takes every group's section as
    the run reaches it; what the run hands back is one report per slug that names every
    group's work: the lists carry all the groups' items, and the commit and the error are
    the first group's that has one. A report this run did not build is kept as the first
    group left it.
    """
    first = reports[0]
    if len(reports) == 1 or not all(isinstance(rep, ProjectRunReport) for rep in reports):
        return first
    merged = ProjectRunReport(project=slug)
    for name in _REPORT_LIST_FIELDS:
        value = getattr(merged, name)
        for rep in reports:
            value.extend(getattr(rep, name))
    for rep in reports:
        if merged.commit is None and rep.commit is not None:
            merged.commit = rep.commit
        if merged.error is None and rep.error is not None:
            merged.error = rep.error
    return merged


_REPORT_LIST_FIELDS = tuple(
    f.name for f in dataclass_fields(ProjectRunReport) if str(f.type).startswith("list")
)


def _expire_short_term(
    config: Config,
    result: Run,
    now: datetime,
    checkpoint: str | None,
    tag_commit: str | None,
) -> bool:
    """Expire the short-term lines whose days are spent, as maintenance, for every
    registered project with a recent.md and nothing waiting (DESIGN 6, R10).

    A project whose sessions stopped gets no dream run, so its stale lines would wait for a
    digest that never comes; this expires them instead, through the same expiry a project
    run applies, one commit per project that changed and one report section naming it. A
    project whose expiry fails has the failure in the run result and its error in the day's
    report once, so a run that repeats the failure records it and writes nothing (T30). A
    project with waiting digests is left to its own run, which expires the same lines when
    it is applied. Returns whether any report section was written.
    """
    from aft.dream import report as reportmod

    registry = Registry.load(config.registry_path)
    reported = False
    for slug in sorted(project.slug for project in registry.projects):
        if slug in result.waiting:
            continue
        if not (config.vault / "projects" / slug / page.RECENT_PAGE).exists():
            continue
        try:
            expired = apply.expire_short_term(config, slug, now=now)
        except Exception as exc:
            error = str(exc)
            result.failures[slug] = error
            if reportmod.error_reported(config, slug, error, now=now):
                # The day's page carries this error already (T30): the failure stays in the
                # run result, which every run's output prints, and the page is not written
                # again for it.
                continue
            rep = ProjectRunReport(project=slug, error=error)
        else:
            if expired is None:
                continue
            rep, paths = expired
            if vault.commit(config.vault, paths, f"aft: expire short-term memory {slug}"):
                rep.commit = git(["rev-parse", "HEAD"], cwd=config.vault).stdout.strip()
            result.expired.append(slug)
        result.reports[slug] = rep
        failure = _report_run(config, slug, rep, now, checkpoint, tag_commit)
        if failure is None:
            reported = True
        elif slug in result.failures:
            result.failures[slug] = f"{result.failures[slug]}; its report failed: {failure}"
        else:
            result.failures[slug] = f"report not written: {failure}"
    return reported


def _report_run(
    config: Config,
    slug: str,
    rep: ProjectRunReport,
    now: datetime,
    checkpoint: str | None,
    tag_commit: str | None,
) -> str | None:
    """Append one run's section to the day's report, refresh needs-you, and commit both.

    Returns None when they are committed, or the reason they are not. A report page that
    will not write must not stop the other projects from dreaming, so the caller carries
    the reason into its own output and goes on.
    """
    from aft.dream import report as reportmod

    backlog = len(waiting_digests(config.vault).get(slug, []))
    try:
        report_path = reportmod.write_report(
            config,
            rep,
            now=now,
            checkpoint=checkpoint,
            tag_commit=tag_commit,
            backlog=backlog,
        )
        needs_you = reportmod.regenerate_needs_you(config.vault)
        vault.commit(
            config.vault,
            [
                report_path.relative_to(config.vault).as_posix(),
                needs_you.relative_to(config.vault).as_posix(),
            ],
            f"aft: report {slug}",
        )
    except Exception as exc:
        return str(exc)
    return None


def tag_hand_edits(config: Config, checkpoint: str) -> list[str]:
    """Tag the bare bullets a person added in ``checkpoint`` as ``[stated]``, in a second
    commit ``aft: tag hand edits from <checkpoint>`` (DESIGN 9 step 1, R2).

    A line counts when the checkpoint added it as ``- <text>`` with no tag, date or
    footnote, in a kind section of a fact page (``projects/<slug>/*.md`` except
    ``index.md``, and ``global/*.md``). It becomes ``- <text> [stated] <date> [^id]``,
    dated with the checkpoint's UTC date, citing a new source ``aft://edit/<checkpoint>``
    whose quote is the text. A line typed in full fact-line form keeps exactly what the
    person wrote, and a page whose tagged lines would not read back as the statements they
    carry is left as written. Returns the pages tagged.
    """
    root = config.vault
    stamp = int(git(["show", "-s", "--format=%ct", checkpoint], cwd=root).stdout.strip())
    when = datetime.fromtimestamp(stamp, UTC).date().isoformat()
    changed = git(
        [
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--diff-filter=AM",
            f"{checkpoint}~1",
            checkpoint,
        ],
        cwd=root,
    ).stdout.split("\0")
    tagged = []
    for rel in sorted(path for path in changed if _fact_page(path)):
        added = _added_lines(root, checkpoint, rel)
        text = _tagged(root / rel, added, checkpoint, when)
        if text is not None:
            pagewrite.atomic_write_text(root / rel, text)
            tagged.append(rel)
    if tagged:
        vault.commit(root, tagged, f"aft: tag hand edits from {checkpoint}")
    return tagged


def _fact_page(rel: str) -> bool:
    parts = rel.split("/")
    if not rel.endswith(".md"):
        return False
    if len(parts) == 3 and parts[0] == "projects":
        return parts[2] != "index.md"
    return len(parts) == 2 and parts[0] == "global"


_HUNK = re.compile(r"^@@ -\S+ \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


def _added_lines(root: Path, checkpoint: str, rel: str) -> set[int]:
    """The line numbers, in the checkpoint's version of ``rel``, that it added."""
    diff = git(
        ["diff", "-U0", "--no-color", "--no-renames", f"{checkpoint}~1", checkpoint, "--", rel],
        cwd=root,
    ).stdout
    added: set[int] = set()
    for line in diff.splitlines():
        match = _HUNK.match(line)
        if match:
            start = int(match.group("start"))
            count = 1 if match.group("count") is None else int(match.group("count"))
            added.update(range(start, start + count))
    return added


# A bare bullet carries none of a fact line's tail: no source tag, no footnote and no
# strikethrough, so the grammar reads no fact out of it and the person's words reach no memory.
# A bullet that ends in a date while carrying no tag and no footnote is such a line: a date with
# nothing to attach it to is a fact line missing its source, not a complete one. Which lines hold
# a fact is the grammar's to say, and `page.Page.rejected` is what the grammar says (F2).
_TAG = re.compile(r"\[(?:" + "|".join(page.SOURCES) + r")\]")
_FOOTNOTE = re.compile(r"\[\^[A-Za-z0-9_-]+\]")


def _tagged(path: Path, added: set[int], checkpoint: str, when: str) -> str | None:
    """The page with its added bare bullets tagged, or None when there is nothing to tag
    or the tagged page would not read back as it should."""
    text = path.read_text(encoding="utf-8")
    before = page.parse(text, path)
    refused = {number for number, _, _ in before.rejected}
    bare = [
        (number, line[2:].strip())
        for number, section, line in _body_lines(text, before.body_start)
        if number in added and number in refused and section in page.KIND_SECTIONS and _bare(line)
    ]
    if not bare:
        return None
    try:
        doc = pagewrite.PageDoc(text, path)
        ids = [
            doc.add_source(f"aft://edit/{checkpoint}", title=f"File edit {when}", quote=statement)
            for _, statement in bare
        ]
    except pagewrite.WriteError:
        return None
    lines = doc.render().split("\n")
    shift = len(lines) - len(text.split("\n"))  # add_source writes only frontmatter
    for (number, statement), source in zip(bare, ids, strict=True):
        lines[number - 1 + shift] = f"- {statement} [stated] {when} [^{source}]"
    result = "\n".join(lines)
    try:
        doc = pagewrite.PageDoc(result, path)
        for source in reversed(ids):
            fact = next((f for f in doc.page.facts if source in f.refs), None)
            if fact is not None and not fact.callouts:
                doc.add_callout(
                    fact,
                    "question",
                    "File edit in checkpoint. Confirm to keep this fact.",
                    when,
                )
    except pagewrite.WriteError:
        return None
    result = doc.render()
    after = page.parse(result, path)
    facts_by_ref = {ref: fact for fact in after.facts for ref in fact.refs}
    for (_, statement), source in zip(bare, ids, strict=True):
        fact = facts_by_ref.get(source)
        if (
            fact is None
            or fact.statement != statement
            or fact.source != "stated"
            or fact.state != page.NEEDS_YOU
        ):
            return None
    if _new_problem(before, after):
        return None
    return result


def _new_problem(before: page.Page, after: page.Page) -> bool:
    """Whether tagging wrote a complaint the page did not already carry.

    Tagging a bare bullet can quiet more than its own refusal: a note written under that bullet
    marked nothing while the line held no fact, and marks the stated fact once it does. So the
    check is that no kind of complaint grew, not that one error per tagged line went away - the
    latter left a page whose bullets carry notes untagged, whole, person's words and all.
    """
    was = Counter(message for _, message in before.errors)
    now = Counter(message for _, message in after.errors)
    return any(count > was[message] for message, count in now.items())


def _body_lines(text: str, body_start: int):
    """Each body line with its number and the ``##`` section it sits in."""
    section = None
    for number, line in enumerate(text.split("\n")[body_start - 1 :], start=body_start):
        if line.startswith("## "):
            section = line[3:].strip()
        yield number, section, line


def _bare(line: str) -> bool:
    if not line.startswith("- "):
        return False
    body = line[2:].strip()
    return bool(body and "~~" not in body and not _TAG.search(body) and not _FOOTNOTE.search(body))


def waiting_digests(vault_path: Path) -> dict[str, list[Path]]:
    """The digests no dream has consumed, by project slug, each in session order.

    A digest lives at ``digests/<slug>/<yyyy>/<mm>/<name>.md`` (DESIGN 6, M2.6), so the
    directory names its project. A digest whose frontmatter cannot be read carries no
    ``aft.consumed`` mark, so it is still waiting; the step that reads it reports it.
    Session order is the order the digests were written in (``generated.at``), then the
    path, which puts a session's ``-2`` part after its first.
    """
    root = vault_path / DIGESTS
    found: dict[str, list[tuple[str, Path]]] = {}
    for path in root.glob("*/*/*/*.md"):
        frontmatter = page.split_frontmatter(path.read_text(encoding="utf-8"))[0] or {}
        aft = frontmatter.get("aft")
        if isinstance(aft, dict) and aft.get("consumed"):
            continue
        slug = path.relative_to(root).parts[0]
        found.setdefault(slug, []).append((_written_at(frontmatter), path))
    return {
        slug: [path for _, path in sorted(digests, key=lambda d: (d[0], str(d[1])))]
        for slug, digests in sorted(found.items())
    }


_written_at = shortterm.written_at


def last_activity(config: Config) -> dict[str, datetime]:
    """The newest session event per project slug, from the capture queue.

    A directory is dated under the name its sessions' digests file under: the project it
    answers for when one exists, the name its session-start records settled when it answers
    for none and a record stands there, and a fresh answer the way the digest resolves it
    when no record does (DESIGN 7, F49). The record's name is looked up by the resolved
    root, not the raw cwd: a session below the root, in a linked worktree, or reached
    through a symlink holds back the group its digests file under. The lookup records no
    alias: scheduling only reads the registry. A directory that no longer exists, such as
    a removed worktree, has no session left running in it and nothing to resolve, so it
    is passed over.
    """
    registry = Registry.load(config.registry_path)
    settled = queue.settled_slugs(config, registry)
    newest: dict[str, datetime] = {}
    for cwd, moment in queue.last_events(config).items():
        if not cwd.is_dir():
            continue
        resolution = keys.resolve(
            cwd, registry, config.host, never=config.settings.capture.exclude_paths
        )
        if resolution.project is not None:
            # The project the directory answers for is where its sessions' digests file,
            # even when a record wrote an earlier, unregistered name (DESIGN 7, F49).
            slug = resolution.project.slug
            slug = resolution.project.slug
        elif resolution.root is not None:
            # A record is the name its session's digest files under, and a fresh answer for
            # an unregistered directory is not: the record's settled name decides (DESIGN 7,
            # F49). A directory of no record and of no project files its sessions as unfiled
            # (DESIGN 7); one capture excludes resolves to none, and files them as unfiled
            # too.
            slug = settled.get(resolution.root) or resolution.slug or digest.UNFILED
        else:
            # A directory that resolves to no root has no record standing in it to look up.
            slug = resolution.slug or digest.UNFILED
        if slug not in newest or moment > newest[slug]:
            newest[slug] = moment
    return newest


def uncommitted(vault_path: Path) -> dict[str, datetime]:
    """Every path git reports as changed and not committed, with when it last changed.

    Untracked files count, ignored ones do not. A deleted path has no time of its own, so
    it takes the time of the nearest directory still standing above it, which a deletion
    updates. A rename is reported as both of its paths, so committing it stages both.
    """
    status = git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=vault_path).stdout
    fields = status.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        paths.append(path)
        if "R" in code or "C" in code:
            paths.append(fields[index])  # the source path follows as its own field
            index += 1
    return {path: _modified(vault_path, path) for path in paths}


def _modified(vault_path: Path, rel: str) -> datetime:
    target = vault_path / rel
    while not os.path.lexists(target) and target != vault_path:
        target = target.parent
    return datetime.fromtimestamp(os.lstat(target).st_mtime, UTC)
