"""Maintenance steps for the dream (DESIGN 9, steps 6 and 7).

Anchor checks, moving superseded lines older than 90 days to the archive,
flagging oversize pages, regenerating index.md files, and appending to log.md.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from aft import page, pagewrite
from aft import vault as vaultmod
from aft.gitutil import git
from aft.registry import Project, RegistryError, split_key

ANCHOR_AGE_DAYS = 90
MAX_PAGE_CHARS = 12_000


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
) -> tuple[list[BrokenAnchor], list[ClearedAnchor]]:
    """Check fact anchors with ripgrep against the project default branch.

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
        for sid, anchor_info in anchors.items():
            if not isinstance(anchor_info, dict):
                continue
            anchor_path = anchor_info.get("path")
            all_of = anchor_info.get("all_of") or []
            commit = anchor_info.get("commit")
            if not anchor_path or not commit:
                continue

            ancestor_check = git(
                ["merge-base", "--is-ancestor", str(commit), default_branch],
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
                is_broken = False
                rg_bin = shutil.which("rg")
                for word in all_of:
                    target_str = str(word)
                    if rg_bin is not None:
                        rg_res = subprocess.run(
                            [rg_bin, "-F", "-q", target_str],
                            input=content,
                            capture_output=True,
                            text=True,
                        )
                        if rg_res.returncode != 0:
                            is_broken = True
                            break
                    else:
                        if target_str not in content:
                            is_broken = True
                            break

            for fact in list(doc.page.facts):
                if sid not in fact.refs:
                    continue
                if is_broken:
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
                        reason = f"anchor words gone from {anchor_path}"
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
                                reason=fact.reason or "anchor words gone",
                                reported_only=False,
                            )
                        )
                else:
                    if fact.state == page.STALE:
                        if doc.clear_stale(fact, today):
                            modified = True
                        cleared_list.append(
                            ClearedAnchor(
                                page=rel,
                                statement=fact.statement,
                                sid=sid,
                            )
                        )

        if modified:
            page_path.write_text(doc.render(), encoding="utf-8")

    return broken_list, cleared_list


def archive_superseded(vault: Path, slug: str, today: str) -> list[ArchivedLine]:
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
        if page_path.name == "index.md":
            continue
        rel = page_path.relative_to(vault).as_posix()
        try:
            text = page_path.read_text(encoding="utf-8")
        except OSError:
            continue

        p = page.parse(text, page_path.relative_to(vault))
        fm, _, _, _ = page.split_frontmatter(text)
        sources_map = {
            str(s["id"]): s
            for s in (fm or {}).get("sources") or []
            if isinstance(s, dict) and s.get("id") is not None
        }

        expired = [
            f
            for f in p.facts
            if f.state == page.SUPERSEDED
            and f.state_date is not None
            and _is_older_than(f.state_date, today_date, ANCHOR_AGE_DAYS)
        ]
        if not expired:
            continue

        archive_path = vault / "archive" / rel
        archive_path.parent.mkdir(parents=True, exist_ok=True)

        if archive_path.exists():
            archive_text = archive_path.read_text(encoding="utf-8")
        else:
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
        raw_lines = text.split("\n")
        lines_to_remove: set[int] = set()

        for fact in expired:
            needed_refs = list(fact.refs)
            if fact.superseded_by and fact.superseded_by not in needed_refs:
                needed_refs.append(fact.superseded_by)
            for ref in needed_refs:
                if ref not in doc_archive.sources:
                    src_entry = sources_map.get(ref)
                    if src_entry:
                        doc_archive.add_source(
                            resource=src_entry.get("resource", ""),
                            title=src_entry.get("title", ""),
                            quote=src_entry.get("quote", ""),
                        )
            line_idx = fact.line - 1
            if 0 <= line_idx < len(raw_lines):
                line_content = raw_lines[line_idx]
                section = fact.section or "Decisions"
                doc_archive._append_line(section, line_content)
                lines_to_remove.add(line_idx)
                archived.append(
                    ArchivedLine(
                        page=rel,
                        archive_page=f"archive/{rel}",
                        statement=fact.statement,
                        superseded_date=fact.state_date or "",
                    )
                )

        archive_path.write_text(doc_archive.render(), encoding="utf-8")
        remaining_lines = [line for idx, line in enumerate(raw_lines) if idx not in lines_to_remove]
        page_path.write_text("\n".join(remaining_lines), encoding="utf-8")

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


def regenerate_index(vault: Path, slug: str, title: str) -> None:
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
    (project_dir / "index.md").write_text("\n".join(lines), encoding="utf-8")


def ensure_root_index(vault: Path) -> None:
    """Ensure the root index.md exists and carries okf_version: 0.2."""
    root_index = vault / "index.md"
    if not root_index.exists():
        root_index.write_text(vaultmod.ROOT_INDEX, encoding="utf-8")


def append_log(vault: Path, slug: str, entry: str) -> None:
    """Append one entry to log.md."""
    log_path = vault / "log.md"
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
