"""Maintenance steps for the dream (DESIGN 9, steps 6 and 7).

Anchor checks, flagging lines whose words left the repository, moving superseded lines
older than 90 days to the archive, flagging oversize pages, regenerating index.md files,
and appending to log.md.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from aft import page, pagewrite
from aft import vault as vaultmod
from aft.gitutil import git
from aft.registry import Project, RegistryError, split_key

ANCHOR_AGE_DAYS = 90
MAX_PAGE_CHARS = 12_000

#: The most lines one dream flags in a project, so a large rename does not bury the user in
#: notes; the rest are flagged by the dreams after it.
WORD_FLAGS_PER_RUN = 5

#: A word shorter than this matches too much of a repository to say anything.
MIN_WORD_CHARS = 4


@dataclass(frozen=True)
class BrokenAnchor:
    """An anchor whose verified words no longer appear on the default branch."""

    page: str
    statement: str
    sid: str
    reason: str
    reported_only: bool = False


@dataclass(frozen=True)
class ClearedAnchor:
    """A stale anchor whose verified words appear again on the default branch."""

    page: str
    statement: str
    sid: str


@dataclass(frozen=True)
class WordLeft:
    """A line with a question under it: a word it names was in the repository at the line's
    date and is gone from the default branch."""

    page: str
    statement: str
    word: str


@dataclass(frozen=True)
class ArchivedLine:
    """A superseded line older than 90 days moved to the archive."""

    page: str
    archive_page: str
    statement: str
    superseded_date: str


def check_anchors(
    vault: Path,
    project: Project | None,
    today: str,
    config_host: str,
    record_write: Callable[[Path], None] | None = None,
) -> tuple[list[BrokenAnchor], list[ClearedAnchor]]:
    """Check fact anchors against the project default branch.

    Anchors whose commit is not yet on the default branch are skipped.
    A broken anchor marks the fact line stale, unless the line has an open note,
    in which case the break is only reported.
    When the words appear again on the default branch, the stale mark is cleared.
    """
    if project is None or not project.paths:
        return [], []

    repo_path = _find_repo(project, config_host)
    if repo_path is None or not (repo_path / ".git").exists():
        return [], []

    default_branch = _get_default_branch(repo_path)
    if not default_branch:
        return [], []

    project_dir = vault / "projects" / project.slug
    if not project_dir.is_dir():
        return [], []

    broken_list: list[BrokenAnchor] = []
    cleared_list: list[ClearedAnchor] = []

    for page_path in sorted(project_dir.rglob("*.md")):
        if page_path.name == "index.md":
            continue
        rel = page_path.relative_to(vault).as_posix()
        try:
            doc = pagewrite.PageDoc.read(page_path)
        except (OSError, pagewrite.WriteError):
            continue

        frontmatter = page.split_frontmatter(doc.render())[0] or {}
        aft_section = frontmatter.get("aft")
        if not isinstance(aft_section, dict):
            continue
        anchors = aft_section.get("anchors")
        if not isinstance(anchors, dict) or not anchors:
            continue

        modified = False
        anchor_status: dict[str, tuple[bool, str]] = {}
        for sid, anchor_info in anchors.items():
            if not isinstance(anchor_info, dict):
                continue
            anchor_path = anchor_info.get("path")
            all_of = anchor_info.get("all_of") or []
            commit = anchor_info.get("commit")
            if not anchor_path or not commit:
                continue

            ancestor_check = git(
                ["merge-base", "--is-ancestor", "--end-of-options", str(commit), default_branch],
                cwd=repo_path,
                check=False,
            )
            if ancestor_check.returncode != 0:
                continue

            show_res = git(
                ["show", f"{default_branch}:{anchor_path}"],
                cwd=repo_path,
                check=False,
            )
            if show_res.returncode != 0:
                is_broken = True
            else:
                content = show_res.stdout
                is_broken = any(str(word) not in content for word in all_of)

            reason = f"anchor words gone from {anchor_path}" if is_broken else ""
            anchor_status[sid] = (is_broken, reason)

        for fact in list(doc.page.facts):
            checked_sids = [sid for sid in fact.refs if sid in anchor_status]
            if not checked_sids:
                continue

            broken_sids = [sid for sid in checked_sids if anchor_status[sid][0]]
            if broken_sids:
                for sid in broken_sids:
                    _, reason = anchor_status[sid]
                    if fact.callouts:
                        broken_list.append(
                            BrokenAnchor(
                                page=rel,
                                statement=fact.statement,
                                sid=sid,
                                reason="anchor words gone",
                                reported_only=True,
                            )
                        )
                    elif fact.state == page.ACTIVE:
                        if doc.mark_stale(fact, reason, today):
                            modified = True
                        broken_list.append(
                            BrokenAnchor(
                                page=rel,
                                statement=fact.statement,
                                sid=sid,
                                reason=reason,
                                reported_only=False,
                            )
                        )
                    elif fact.state == page.STALE:
                        broken_list.append(
                            BrokenAnchor(
                                page=rel,
                                statement=fact.statement,
                                sid=sid,
                                reason=fact.reason or reason,
                                reported_only=False,
                            )
                        )
            else:
                if fact.state == page.STALE:
                    if doc.clear_stale(fact, today):
                        modified = True
                    for sid in checked_sids:
                        cleared_list.append(
                            ClearedAnchor(
                                page=rel,
                                statement=fact.statement,
                                sid=sid,
                            )
                        )

        if modified:
            if record_write is not None:
                record_write(page_path)
            page_path.write_text(doc.render(), encoding="utf-8")

    return broken_list, cleared_list


def check_words(
    vault: Path,
    project: Project | None,
    today: str,
    config_host: str,
    record_write: Callable[[Path], None] | None = None,
) -> tuple[list[WordLeft], bool]:
    """Ask about long-term lines that name a word the repository no longer holds (DESIGN 9).

    A word counts when it held in the repository at the line's date and is gone from the
    default branch. The check asks and changes nothing else: a line under a question stays
    in memory, since a word can leave the code while the fact stays true. The user's answer
    to a question stands, so a word settled on a page is not asked about there again.

    Returns the lines asked about, and whether this host's registered path for the project
    is gone, in which case nothing was checked.
    """
    if project is None or not project.paths:
        return [], False

    repo_path = _find_repo(project, config_host)
    if repo_path is None:
        return [], _repo_gone(project, config_host)

    default_branch = _get_default_branch(repo_path)
    project_dir = vault / "projects" / project.slug
    if not default_branch or not project_dir.is_dir():
        return [], False

    flags: list[WordLeft] = []
    grepped: dict[tuple[str, str], int] = {}
    commits: dict[str, str] = {}

    def grep(word: str, rev: str) -> int:
        if (word, rev) not in grepped:
            found = git(
                ["grep", "-q", "-I", "-F", "-e", word, rev, "--"], cwd=repo_path, check=False
            )
            grepped[(word, rev)] = found.returncode
        return grepped[(word, rev)]

    def commit_at(day: str) -> str:
        if day not in commits:
            found = git(
                ["rev-list", "-1", f"--before={day}T23:59:59Z", default_branch],
                cwd=repo_path,
                check=False,
            )
            commits[day] = found.stdout.strip() if found.returncode == 0 else ""
        return commits[day]

    for page_path in sorted(project_dir.rglob("*.md")):
        if page_path.name == "index.md" or page.is_recent(page_path.relative_to(vault)):
            continue
        if len(flags) >= WORD_FLAGS_PER_RUN:
            break
        rel = page_path.relative_to(vault).as_posix()
        try:
            doc = pagewrite.PageDoc.read(page_path, vault=vault)
            archive = doc._archive_file()
        except (OSError, pagewrite.WriteError):
            continue
        settled = archive.read_text(encoding="utf-8") if archive.exists() else ""

        found: list[tuple[page.Fact, str, str]] = []
        for fact in doc.page.facts:
            if len(flags) + len(found) >= WORD_FLAGS_PER_RUN:
                break
            if fact.state != page.ACTIVE or fact.section == page.CANDIDATES:
                continue
            at = commit_at(fact.date)
            if not at:
                continue
            for word in _line_words(fact.statement):
                if _word_mark(word) in settled:
                    continue
                if grep(word, default_branch) == 1 and grep(word, at) == 0:
                    found.append((fact, word, at))
                    break

        # Last line first: a note moves every line below it, and a handle refuses a move.
        written = False
        for fact, word, at in reversed(found):
            try:
                doc.add_callout(fact, "question", _word_note(word, at), today)
            except pagewrite.WriteError:
                continue
            written = True
            flags.append(WordLeft(page=rel, statement=fact.statement, word=word))
        if written:
            if record_write is not None:
                record_write(page_path)
            page_path.write_text(doc.render(), encoding="utf-8")

    return flags, False


def _line_words(statement: str) -> list[str]:
    """The words of a line a repository can hold: names, paths, flags and config keys. A
    number or version moves on without the fact behind it changing, so it needs a letter."""
    from aft import digest

    words = [word.strip("`") for word in digest.key_tokens(statement)]
    return list(
        dict.fromkeys(
            word for word in words if len(word) >= MIN_WORD_CHARS and any(c.isalpha() for c in word)
        )
    )


def _word_mark(word: str) -> str:
    """The words of a note that name ``word``, as a settled note keeps them in the archive."""
    return f"`{word}` left the repository"


def _word_note(word: str, commit: str) -> str:
    """The question under a line."""
    return (
        f"{_word_mark(word)}: {commit[:8]} holds it and the default branch does not."
        " Does this line still hold?"
    )


def _repo_gone(project: Project, config_host: str) -> bool:
    """Whether a path registered for this host is gone. A directory that stands but holds
    no repository was never checked, and that is no change to report."""
    for key in project.paths:
        try:
            host, path = split_key(key)
        except RegistryError:
            continue
        if host == config_host and not path.exists():
            return True
    return False


def archive_superseded(
    vault: Path,
    slug: str,
    today: str,
    record_write: Callable[[Path], None] | None = None,
) -> list[ArchivedLine]:
    """Move superseded lines older than 90 days to archive/<page path>."""
    project_dir = vault / "projects" / slug
    if not project_dir.is_dir():
        return []

    try:
        today_date = date.fromisoformat(today)
    except ValueError:
        return []

    archived: list[ArchivedLine] = []

    for page_path in sorted(project_dir.rglob("*.md")):
        # Short-term lines leave recent.md when their sessions roll by, never by age.
        if page_path.name == "index.md" or page.is_recent(page_path.relative_to(vault)):
            continue
        rel = page_path.relative_to(vault).as_posix()
        try:
            doc = pagewrite.PageDoc.read(page_path)
        except (OSError, pagewrite.WriteError):
            continue

        initial_expired = [
            f
            for f in doc.page.facts
            if f.state == page.SUPERSEDED
            and f.state_date is not None
            and _is_older_than(f.state_date, today_date, ANCHOR_AGE_DAYS)
        ]
        if not initial_expired:
            continue

        archive_path = vault / "archive" / rel
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        if archive_path.exists():
            doc_archive = pagewrite.PageDoc.read(archive_path)
        else:
            fm, _, _, _ = page.split_frontmatter(doc.render())
            title = (fm or {}).get("title") or page_path.stem
            archive_text = (
                f"---\n"
                f"type: Archive\n"
                f"title: {title} archive\n"
                f"description: Archived lines from {rel}\n"
                f"sources: []\n"
                f"---\n"
            )
            doc_archive = pagewrite.PageDoc(archive_text, archive_path)

        while True:
            expired = [
                f
                for f in doc.page.facts
                if f.state == page.SUPERSEDED
                and f.state_date is not None
                and _is_older_than(f.state_date, today_date, ANCHOR_AGE_DAYS)
            ]
            if not expired:
                break
            fact = expired[0]
            needed_refs = list(fact.refs)
            if fact.superseded_by and fact.superseded_by not in needed_refs:
                needed_refs.append(fact.superseded_by)

            ref_mapping: dict[str, str] = {}
            for ref in needed_refs:
                src_entry = doc.sources.get(ref)
                if src_entry:
                    res = src_entry.get("resource", "")
                    q = src_entry.get("quote", "")
                    title = src_entry.get("title", "")
                    target_id: str | None = None
                    for existing_id, existing_s in doc_archive.sources.items():
                        if existing_s.get("resource") == res and existing_s.get("quote") == q:
                            target_id = existing_id
                            break
                    if target_id is None:
                        target_id = doc_archive.add_source(
                            resource=res,
                            title=title,
                            quote=q,
                            verified=str(src_entry.get("verified", "")),
                            said=str(src_entry.get("said", "")),
                            role=str(src_entry.get("role", "")),
                            check=str(src_entry.get("check", "")),
                        )
                    ref_mapping[ref] = target_id

            section = fact.section or "Decisions"
            line_content = doc.archive_superseded(fact)
            if ref_mapping:
                line_content = re.sub(
                    r"\[\^([A-Za-z0-9_-]+)\]",
                    lambda m, mapping=ref_mapping: f"[^{mapping.get(m.group(1), m.group(1))}]",
                    line_content,
                )
            doc_archive._append_line(section, line_content)
            archived.append(
                ArchivedLine(
                    page=rel,
                    archive_page=f"archive/{rel}",
                    statement=fact.statement,
                    superseded_date=fact.state_date or "",
                )
            )

        if record_write is not None:
            record_write(archive_path)
            record_write(page_path)
        archive_path.write_text(doc_archive.render(), encoding="utf-8")
        page_path.write_text(doc.render(), encoding="utf-8")

    return archived


def check_oversize(vault: Path, pages: Sequence[Path]) -> list[str]:
    """Flag pages whose text length exceeds 12,000 characters."""
    oversize = []
    for p in pages:
        if p.exists() and p.is_file():
            try:
                text = p.read_text(encoding="utf-8")
                if len(text) > MAX_PAGE_CHARS:
                    oversize.append(p.relative_to(vault).as_posix())
            except OSError:
                pass
    return oversize


def regenerate_index(
    vault: Path,
    slug: str,
    title: str,
    record_write: Callable[[Path], None] | None = None,
) -> None:
    """Regenerate projects/<slug>/index.md without frontmatter."""
    project_dir = vault / "projects" / slug
    if not project_dir.is_dir():
        return

    pages = [p.name for p in sorted(project_dir.glob("*.md")) if p.name != "index.md"]
    ordered = []
    if "overview.md" in pages:
        ordered.append("overview.md")
    for name in pages:
        if name != "overview.md":
            ordered.append(name)

    lines = [f"# {title}", ""]
    for name in ordered:
        lines.append(f"- {name}")
    lines.append("")
    index_path = project_dir / "index.md"
    if record_write is not None:
        record_write(index_path)
    index_path.write_text("\n".join(lines), encoding="utf-8")


def ensure_root_index(
    vault: Path,
    record_write: Callable[[Path], None] | None = None,
) -> None:
    """Ensure the root index.md exists and carries okf_version: 0.2."""
    root_index = vault / "index.md"
    if not root_index.exists():
        if record_write is not None:
            record_write(root_index)
        root_index.write_text(vaultmod.ROOT_INDEX, encoding="utf-8")


def append_log(
    vault: Path,
    slug: str,
    entry: str,
    record_write: Callable[[Path], None] | None = None,
) -> None:
    """Append one entry to log.md."""
    log_path = vault / "log.md"
    if record_write is not None:
        record_write(log_path)
    if not log_path.exists():
        log_path.write_text(vaultmod.LOG, encoding="utf-8")
    current = log_path.read_text(encoding="utf-8")
    if not current.endswith("\n"):
        current += "\n"
    log_path.write_text(f"{current}- {entry}\n", encoding="utf-8")


def _is_older_than(date_str: str, today: date, days: int) -> bool:
    try:
        fact_date = date.fromisoformat(date_str)
        return (today - fact_date).days > days
    except ValueError:
        return False


def _find_repo(project: Project, config_host: str) -> Path | None:
    for key in project.paths:
        try:
            host, path = split_key(key)
        except RegistryError:
            continue
        if host == config_host and path.exists() and (path / ".git").exists():
            return path
    for key in project.paths:
        try:
            _, path = split_key(key)
        except RegistryError:
            continue
        if path.exists() and (path / ".git").exists():
            return path
    return None


def _get_default_branch(repo_path: Path) -> str:
    res = git(["symbolic-ref", "refs/remotes/origin/HEAD"], cwd=repo_path, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().removeprefix("refs/remotes/origin/")
    res = git(["rev-parse", "--verify", "refs/heads/main"], cwd=repo_path, check=False)
    if res.returncode == 0:
        return "main"
    res = git(["rev-parse", "--verify", "refs/heads/master"], cwd=repo_path, check=False)
    if res.returncode == 0:
        return "master"
    res = git(["symbolic-ref", "--short", "HEAD"], cwd=repo_path, check=False)
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip()
    return "main"
