"""The ``aft`` command line."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from aft import (
    __version__,
    adapters,
    claude_memory,
    digest,
    evidence,
    forget,
    gitutil,
    index,
    inject,
    keys,
    lint,
    queue,
    redact,
    search,
    vault,
    work,
)
from aft import (
    eval as evalmod,
)
from aft.config import (
    DEFAULT_CONFIG_TEXT,
    HOME_ENV,
    Config,
    ConfigError,
    is_excluded,
    load,
    resolve_home,
)
from aft.dream import schedule
from aft.events import COMPACTION, SESSION_END, SESSION_START, AdapterError, SessionEvent
from aft.gitutil import GitError
from aft.install import claude as claude_install
from aft.install import pi as pi_install
from aft.install import skill as skill_install
from aft.install import systemd as systemd_install
from aft.library import describe, importing, manifest, map_evidence, propose, survey
from aft.registry import Registry, RegistryError, path_key, slugify


class UsageError(Exception):
    pass


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 2
    try:
        return args.handler(args)
    except (
        ConfigError,
        claude_install.InstallError,
        pi_install.InstallError,
        skill_install.InstallError,
        systemd_install.InstallError,
        RegistryError,
        vault.VaultError,
        GitError,
        index.SearchIndexError,
        search.SearchError,
        UsageError,
        evalmod.EvalError,
        claude_memory.ImportMemoryError,
        survey.SurveyError,
        manifest.ManifestError,
        queue.QueueError,
        sqlite3.Error,
        digest.DigestError,
        evidence.EvidenceError,
        importing.ImportError,
        propose.ProposeError,
        redact.BadPattern,
    ) as exc:
        print(f"aft: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aft", description="Shared memory for coding agents.")
    parser.add_argument("--version", action="version", version=f"aft {__version__}")
    sub = parser.add_subparsers(metavar="command")

    p = sub.add_parser("init", help="create the config file and the vault")
    p.set_defaults(handler=cmd_init)

    p = sub.add_parser("key", help="show which project a directory belongs to")
    p.add_argument("path", nargs="?", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(handler=cmd_key)

    p = sub.add_parser("inject", help="print the memory a session starts with")
    p.add_argument("path", nargs="?", default=".")
    p.set_defaults(handler=cmd_inject)

    p = sub.add_parser("project", help="manage registered projects")
    psub = p.add_subparsers(metavar="action")
    q = psub.add_parser("list", help="list projects")
    q.add_argument("--json", action="store_true")
    q.set_defaults(handler=cmd_project_list)
    q = psub.add_parser("add", help="register the project at a path")
    q.add_argument("path", nargs="?", default=".")
    q.add_argument("--slug", help="defaults to the directory name")
    q.add_argument("--title")
    q.add_argument(
        "--allow-temp",
        action="store_true",
        help="register a path under the system temporary directory in the default home",
    )
    q.set_defaults(handler=cmd_project_add)
    q = psub.add_parser("rename", help="change a project's slug")
    q.add_argument("old")
    q.add_argument("new")
    q.set_defaults(handler=cmd_project_rename)
    q = psub.add_parser("forget", help="remove a project from the registry")
    q.add_argument("slug")
    q.set_defaults(handler=cmd_project_forget)

    p = sub.add_parser("lint", help="check the vault's pages")
    p.set_defaults(handler=cmd_lint)

    p = sub.add_parser(
        "work",
        help="sweep for missed sessions and digest queued work within the day's budget",
    )
    p.set_defaults(handler=cmd_work)

    p = sub.add_parser("reindex", help="rebuild the search index from the vault and the archive")
    p.set_defaults(handler=cmd_reindex)

    p = sub.add_parser("dream", help="turn waiting digests into curated facts")
    p.set_defaults(handler=cmd_dream)

    p = sub.add_parser("forget", help="remove session evidence or redact patterns")
    fsub = p.add_subparsers(metavar="target")
    q = fsub.add_parser("session", help="remove every trace of one session")
    q.add_argument("session_id", help="the session id to forget")
    q.set_defaults(handler=cmd_forget_session)
    q = fsub.add_parser("pattern", help="redact a regular expression across the archive")
    q.add_argument("pattern", help="the regular expression to redact")
    q.set_defaults(handler=cmd_forget_pattern)

    p = sub.add_parser("redigest", help="rebuild a session digest from archived evidence")
    p.add_argument("session_id", help="the session id to redigest")
    p.set_defaults(handler=cmd_redigest)

    p = sub.add_parser("search", help="search memory with BM25 ranking")
    p.add_argument("query", help="words to search for")
    p.add_argument("--project", help="search within a project and global pages")
    p.add_argument("--all", action="store_true", help="search across all projects")
    p.add_argument("--archive", action="store_true", help="include archived superseded facts")
    p.add_argument("--reports", action="store_true", help="include dream reports")
    p.add_argument("--json", action="store_true", help="output hits as JSON")
    p.add_argument("--limit", type=int, default=8, help="maximum number of hits (default 8)")
    p.set_defaults(handler=cmd_search)

    p = sub.add_parser("read", help="read a memory page at the specified depth")
    p.add_argument("path", help="path to the page file")
    p.add_argument(
        "--level",
        choices=["outline", "abstract", "full"],
        default="full",
        help="depth: outline, abstract, or full (default full)",
    )
    p.set_defaults(handler=cmd_read)

    p = sub.add_parser("trace", help="trace a fact quote back to session evidence")
    p.add_argument(
        "page",
        help="path to the page, or an evidence hit's path (aft://session/<agent>/<id>#<entry>)",
    )
    p.add_argument(
        "source_id",
        nargs="?",
        default=None,
        help="source id, e.g. s1 or [^s1]; with an evidence path, the entry id",
    )
    p.set_defaults(handler=cmd_trace)

    p = sub.add_parser("eval", help="check memory against a golden set of known answers")
    p.add_argument("golden", nargs="?", help="the golden set; defaults to <vault>/golden.yaml")
    p.add_argument(
        "--limit",
        type=int,
        default=evalmod.DEFAULT_LIMIT,
        help="how many search results a line must be found in",
    )
    p.set_defaults(handler=cmd_eval)

    p = sub.add_parser("hook", help="handle one agent lifecycle event read from stdin")
    p.add_argument("agent", help="the agent that fired the event: " + " or ".join(adapters.PARSERS))
    p.add_argument("event", help="that agent's own event name, e.g. SessionStart")
    p.set_defaults(handler=cmd_hook)

    p = sub.add_parser("install", help="install afterthought into an agent's own configuration")
    isub = p.add_subparsers(metavar="agent")
    q = isub.add_parser("claude", help="install the Claude Code hooks in ~/.claude/settings.json")
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="print the entries aft would add or remove, write nothing",
    )
    q.add_argument(
        "--uninstall",
        action="store_true",
        help="remove afterthought's own hook entries and nothing else",
    )
    q.set_defaults(handler=cmd_install_claude)
    q = isub.add_parser("pi", help="install the afterthought extension in pi's extensions dir")
    q.add_argument("--dry-run", action="store_true", help="print the extension diff, write nothing")
    q.add_argument(
        "--uninstall",
        action="store_true",
        help="remove afterthought's own extension and nothing else",
    )
    q.set_defaults(handler=cmd_install_pi)
    q = isub.add_parser(
        "timer",
        help="install systemd user timer and service units for background work",
    )
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="print the unit files aft would write or remove, write nothing",
    )
    q.add_argument(
        "--uninstall",
        action="store_true",
        help="remove afterthought's systemd user units and nothing else",
    )
    q.set_defaults(handler=cmd_install_timer)

    p = sub.add_parser("library", help="import and manage library collections")
    lsub = p.add_subparsers(metavar="action")
    q = lsub.add_parser("survey", help="survey a collection directory before import")
    q.add_argument("path", help="path to the collection directory")
    q.add_argument("--json", action="store_true", help="output report as JSON")
    q.set_defaults(handler=cmd_library_survey)

    q = lsub.add_parser("validate", help="validate a collection manifest against DESIGN 11")
    q.add_argument("manifest", help="path to manifest YAML file")
    q.add_argument("--path", help="override or specify collection directory path")
    q.set_defaults(handler=cmd_library_validate)

    q = lsub.add_parser("import", help="import a library collection into the vault")
    q.add_argument("manifest", help="path to manifest YAML file")
    q.add_argument("--path", help="override or specify collection directory path")
    q.set_defaults(handler=cmd_library_import)

    q = lsub.add_parser("check-map", help="verify claims in an imported index or map file")
    q.add_argument("manifest", help="path to manifest YAML file")
    q.add_argument("map_files", nargs="*", help="path to index or map file(s) to check")
    q.add_argument("--path", help="override or specify collection directory path")
    q.set_defaults(handler=cmd_library_check_map)

    q = lsub.add_parser(
        "describe",
        help="verify and add document descriptions to reference pages",
    )
    q.add_argument("manifest", help="path to manifest YAML file")
    q.add_argument(
        "descriptions_file",
        help="path to YAML file containing document descriptions (or '-' for stdin)",
    )
    q.add_argument("--path", help="override or specify collection directory path")
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="verify descriptions without writing to vault",
    )
    q.set_defaults(handler=cmd_library_describe)

    q = lsub.add_parser(
        "propose",
        aliases=["propose-facts"],
        help="propose candidate facts from notes and instruction files",
    )
    q.add_argument("manifest", help="path to manifest YAML file")
    q.add_argument(
        "facts_file",
        help="path to YAML file containing proposed facts (or '-' for stdin)",
    )
    q.add_argument("--path", help="override or specify collection directory path")
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="verify quotes and roles without writing to vault",
    )
    q.set_defaults(handler=cmd_library_propose)

    p = sub.add_parser("import", help="import memory another tool wrote, as candidate digests")
    msub = p.add_subparsers(metavar="source")
    q = msub.add_parser(
        "claude-memory", help="import Claude Code's memory files so the dream can judge them"
    )
    q.add_argument(
        "--from",
        dest="source",
        help="where the memory lives; defaults to ~/.claude/projects",
    )
    q.add_argument("--project", help="import one registered project's memory and no other's")
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="say what would be imported and write nothing",
    )
    q.set_defaults(handler=cmd_import_claude_memory)
    return parser


def cmd_init(args) -> int:
    config = load()
    config.home.mkdir(parents=True, exist_ok=True)
    if not config.config_path.exists():
        config.config_path.write_text(DEFAULT_CONFIG_TEXT)
        print(f"wrote {config.config_path}")
        config = load()
    created = vault.init(config)
    if created:
        print(f"initialized the vault at {config.vault}")
    else:
        print(f"the vault at {config.vault} is already initialized")
    return 0


def cmd_key(args) -> int:
    config = load()
    registry = Registry.load(config.registry_path)
    result = keys.resolve(Path(args.path), registry, config.host, on_alias=_alias_writer(config))
    if args.json:
        print(
            json.dumps(
                {
                    "kind": result.kind,
                    "slug": result.slug,
                    "project_id": result.project.id if result.project else None,
                    "root": str(result.root) if result.root else None,
                    "cwd": str(result.cwd),
                    "alias_added": result.alias_added,
                }
            )
        )
    else:
        print(f"{result.slug or '-'}\t{result.kind}\t{result.root or '-'}")
    return 0 if result.registered else 1


def cmd_inject(args) -> int:
    """Print the block. The lookup records no alias: injection only reads memory."""
    block = _injection_block(Path(args.path))
    if block:
        print(block)
    return 0


def _injection_block(path: Path) -> str:
    """The block a session starting in ``path`` is given, "" when there is none.

    `aft inject` and the session-start hook both print what this returns, so an agent's
    session starts with exactly the text `aft inject` shows for its directory.
    """
    config = load()
    registry = Registry.load(config.registry_path)
    result = keys.resolve(path, registry, config.host)
    return inject.build(config, result)


def _alias_writer(config: Config):
    """Record a path found by remote, if the vault exists. Never fails the lookup."""

    def write(project, key: str) -> None:
        if not vault.is_initialized(config.vault):
            return
        with vault.locked(config):
            registry = Registry.load(config.registry_path)
            current = registry.by_id(project.id)
            if current is None or not registry.add_path(current, key):
                return
            registry.save()
            vault.commit(config.vault, ["registry.yaml"], f"aft: record {key} for {current.slug}")

    return write


def cmd_project_list(args) -> int:
    config = load()
    registry = Registry.load(config.registry_path)
    if args.json:
        print(json.dumps([p.to_dict() for p in registry.projects], indent=2))
        return 0
    for p in registry.projects:
        print(f"{p.slug}\t{p.id}\t{', '.join(p.paths) or '-'}\t{', '.join(p.remotes) or '-'}")
    return 0


def cmd_project_add(args) -> int:
    config = load()
    vault.require(config)
    info = gitutil.repo_info(Path(args.path).resolve())
    root = info.main if info else Path(args.path).resolve()
    if not args.allow_temp:
        _refuse_temporary_project(root)
    slug = args.slug or slugify(root.name)
    with vault.locked(config):
        registry = Registry.load(config.registry_path)
        project = registry.add(slug, args.title)
        registry.add_path(project, path_key(config.host, root))
        if info and info.origin:
            registry.add_remote(project, gitutil.normalize_remote(info.origin))
        registry.save()
        vault.commit(config.vault, ["registry.yaml"], f"aft: register {slug}")
    print(f"registered {slug} ({project.id}) at {config.host}:{root}")
    return 0


def _refuse_temporary_project(root: Path) -> None:
    """Refuse to register a temporary directory in the default home (BUILD-PLAN F25).

    With ``AFT_HOME`` unset the command is using the default home and so the user's real
    vault, where a project under the system temporary directory is a scratch run that lost
    its way: the directory will be gone and the registry will keep naming it. A run that set
    ``AFT_HOME`` chose its home, so it registers whatever it likes.
    """
    if os.environ.get(HOME_ENV):
        return
    temporary = Path(tempfile.gettempdir()).resolve()
    if root == temporary or temporary in root.parents:
        raise UsageError(
            f"{root} is under the temporary directory {temporary}, and {HOME_ENV} is not set, "
            f"so this would register it in the vault of the default home. Set {HOME_ENV} to a "
            "scratch home for a test or benchmark run, or pass --allow-temp to register it here."
        )


def cmd_project_rename(args) -> int:
    config = load()
    vault.require(config)
    with vault.locked(config):
        registry = Registry.load(config.registry_path)
        registry.rename(args.old, args.new)
        registry.save()
        changed = ["registry.yaml"]
        renamed: list[tuple[str, str]] = []
        try:
            for old, new in (
                (f"projects/{args.old}", f"projects/{args.new}"),
                (f"digests/{args.old}", f"digests/{args.new}"),
                (f"archive/projects/{args.old}", f"archive/projects/{args.new}"),
                (f"archive/{args.old}.md", f"archive/{args.new}.md"),
            ):
                if (config.vault / old).exists():
                    (config.vault / new).parent.mkdir(parents=True, exist_ok=True)
                    (config.vault / old).rename(config.vault / new)
                    renamed.append((old, new))
                    changed += [old, new]
        except OSError as exc:
            for old, new in reversed(renamed):
                try:
                    (config.vault / new).rename(config.vault / old)
                except OSError:
                    pass
            registry.rename(args.new, args.old)
            registry.save()
            raise vault.VaultError(f"cannot rename project directories: {exc}") from exc
        vault.commit(config.vault, changed, f"aft: rename {args.old} to {args.new}")
    print(f"renamed {args.old} to {args.new}")
    return 0


def cmd_project_forget(args) -> int:
    config = load()
    vault.require(config)
    with vault.locked(config):
        registry = Registry.load(config.registry_path)
        project = registry.remove(args.slug)
        registry.save()
        vault.commit(config.vault, ["registry.yaml"], f"aft: forget {args.slug}")
    print(f"removed {args.slug} ({project.id}) from the registry")
    if (config.vault / "projects" / args.slug).exists():
        print(
            f"its pages remain in projects/{args.slug}; delete them yourself if you want them gone"
        )
    return 0


def cmd_lint(args) -> int:
    config = load()
    vault.require(config)
    findings = lint.lint_vault(config.vault)
    for finding in findings:
        print(finding)
    if findings:
        print(f"{len(findings)} problem(s)", file=sys.stderr)
        return 1
    pages = len(lint.vault_pages(config.vault))
    print(f"checked {pages} page{'' if pages == 1 else 's'}, no findings")
    return 0


def cmd_eval(args) -> int:
    """Run the golden set. No vault lock and no model: eval only ever reads."""
    config = load()
    vault.require(config)
    return evalmod.run(config, args.golden, limit=args.limit)


def cmd_import_claude_memory(args) -> int:
    """Import memory files as digests. What survives them is the dream's, not this call's."""
    config = load()
    vault.require(config)
    return claude_memory.run(config, args.source, project=args.project, dry_run=args.dry_run)


def cmd_work(args) -> int:
    """Digest the queue within the day's budget, say how far the run got, then dream.

    A job that could not be digested is an exit status a timer can see, not a line only
    the run's own log holds; the work it could not do stays queued either way (DESIGN 14).
    """
    config = load()
    report = work.run(config)
    print(f"aft work: {report.line()}")
    for failure in report.failures:
        print(f"aft work: {failure}", file=sys.stderr)
    _dream(config)
    return 1 if report.failures else 0


def cmd_reindex(args) -> int:
    """Rebuild the search index. Everything it does is `aft.index`'s; this names the file it
    wrote and repeats the pages it could not read."""
    config = load()
    vault.require(config)
    report = index.reindex(config)
    for line in report:
        print(line)
    print(f"rebuilt the search index at {config.home / index.INDEX_NAME}")
    return 0


def cmd_dream(args) -> int:
    """Run the dream once. Everything it does is `aft.dream.schedule`'s."""
    _dream(load())
    return 0


def _dream(config: Config) -> None:
    """Run the dream and say what it did with each project that has digests waiting."""
    result = schedule.run(config)
    if not result.waiting:
        print("aft dream: no digests waiting")
    if result.checkpoint:
        print(f"aft dream: committed hand edits as {result.checkpoint[:12]}")
    if result.tagged:
        print(f"aft dream: tagged hand-typed lines [stated] in {', '.join(result.tagged)}")
    for slug in result.ran:
        print(f"aft dream: {slug}: {len(result.waiting[slug])} digest(s) taken up")
    for slug, err in result.failures.items():
        print(f"aft dream: {slug}: failed: {err}")
    for slug in result.active:
        print(f"aft dream: {slug}: skipped, a session was active within the quiet period")
    for slug in result.edited:
        print(f"aft dream: {slug}: skipped, the vault has hand edits within the quiet period")


def cmd_forget_session(args) -> int:
    config = load()
    vault.require(config)
    report = forget.forget_session(config, args.session_id)
    print(
        f"forgot session {args.session_id}: removed evidence, "
        f"{len(report.digests_removed)} digest file(s), "
        f"{report.jobs_removed} queue job row(s), "
        f"withdrew {len(report.lines_withdrawn)} curated line(s), "
        f"removed {len(report.candidates_removed)} candidate line(s), "
        f"scrubbed {len(report.reports_scrubbed)} report page(s)"
    )
    if report.lines_unwithdrawn:
        print(
            f"{len(report.lines_unwithdrawn)} line(s) with open notes could not be withdrawn; "
            "close the note and rerun:"
        )
        for line in report.lines_unwithdrawn:
            print(f"  {line}")
    print("the vault's git history still holds older commits; rewrite history to remove them")
    return 0


def cmd_forget_pattern(args) -> int:
    config = load()
    vault.require(config)
    report = forget.forget_pattern(config, args.pattern)
    count = report.entries_redacted
    print(
        f"redacted {count} {'entry' if count == 1 else 'entries'} in the evidence archive"
        f" and {len(report.pages_redacted)} vault page(s)"
    )
    if report.pages_refused:
        print(
            f"{len(report.pages_refused)} page(s) would not read as itself redacted,"
            " and were left as they stand:"
        )
        for rel in report.pages_refused:
            print(f"  {rel}")
    if report.verified_quotes_redacted:
        print(
            f"redacted {report.verified_quotes_redacted} imported quote(s)"
            " in library/verified_quotes.json"
        )

    existing = list(config.settings.capture.redact_patterns)
    if args.pattern not in existing:
        existing.append(args.pattern)
    print("Add this line to config.toml under [capture]:")
    print(f"redact_patterns = {json.dumps(existing)}")
    print("the vault's git history still holds older commits; rewrite history to remove them")
    return 0


def cmd_redigest(args) -> int:
    config = load()
    vault.require(config)
    result = digest.redigest(config, args.session_id)
    print(f"rebuilt digest at {result.path}")
    return 0


def cmd_search(args) -> int:
    config = load()
    return search.search_command(
        config,
        args.query,
        project=args.project,
        all_projects=args.all,
        archive=args.archive,
        reports=args.reports,
        as_json=args.json,
        limit=args.limit,
    )


def cmd_read(args) -> int:
    config = load()
    return search.read_command(config, args.path, level=args.level)


def cmd_trace(args) -> int:
    config = load()
    return search.trace_command(config, args.page, args.source_id)


def cmd_install_claude(args) -> int:
    """Add or remove afterthought's hook entries and skill in Claude Code's configuration."""
    settings = claude_install.settings_path()
    skills = claude_install.skills_dir(settings)
    try:
        if settings.exists():
            _ = settings.read_text(encoding="utf-8")
    except OSError as exc:
        raise claude_install.InstallError(f"cannot read {settings}: {exc}") from exc
    skill_install.check_target(skills)
    change = (
        claude_install.uninstall(settings, dry_run=args.dry_run)
        if args.uninstall
        else claude_install.install(settings, dry_run=args.dry_run)
    )
    skill_change = (
        claude_install.uninstall_skill(skills, dry_run=args.dry_run)
        if args.uninstall
        else claude_install.install_skill(skills, dry_run=args.dry_run)
    )
    if args.dry_run:
        preview_lines = []
        if change.preview:
            preview_lines.append(change.preview)
        if change.diff:
            preview_lines.append(change.diff)
        if skill_change.changed:
            verb = "would remove" if args.uninstall else "would write"
            preview_lines.append(f"{verb} skill {skill_change.target}")
            if skill_change.diff:
                preview_lines.append(skill_change.diff)
        if skill_change.backup is not None:
            preview_lines.append(
                f"would back up {skill_change.target.name} to {skill_change.backup}"
            )
        if not preview_lines:
            print(f"no changes to {settings}")
        else:
            print("\n".join(preview_lines))
        return 0
    if change.backup is not None:
        print(f"backed up {settings} to {change.backup}")
    if skill_change.backup is not None:
        print(f"backed up {skill_change.target.name} to {skill_change.backup}")
    if args.uninstall:
        print(
            f"removed afterthought's hooks from {settings}"
            if change.changed
            else f"no afterthought hooks to remove in {settings}"
        )
        if skill_change.changed:
            print(f"removed skill {skill_change.target}")
        else:
            print(f"no afterthought skill to remove in {skills}")
    else:
        print(
            f"installed afterthought's hooks in {settings}"
            if change.changed
            else f"afterthought's hooks are already in {settings}"
        )
        if skill_change.changed:
            print(f"installed skill {skill_change.target}")
        else:
            print(f"afterthought skill is already in {skills}")
    return 0


def cmd_install_pi(args) -> int:
    """Copy or remove afterthought's extension and skill in pi's configuration."""
    extensions = pi_install.extensions_dir()
    skills = pi_install.skills_dir(extensions)
    skill_install.check_target(skills)
    change = (
        pi_install.uninstall(extensions, dry_run=args.dry_run)
        if args.uninstall
        else pi_install.install(extensions, dry_run=args.dry_run)
    )
    skill_change = (
        pi_install.uninstall_skill(skills, dry_run=args.dry_run)
        if args.uninstall
        else pi_install.install_skill(skills, dry_run=args.dry_run)
    )
    target = pi_install.target_path(extensions)
    if not change.changed and not skill_change.changed:
        if args.uninstall:
            print(f"no afterthought extension in {extensions}")
            print(f"no afterthought skill in {skills}")
        else:
            print(f"{target} is already up to date")
            print(f"{skill_change.target} is already up to date")
        return 0
    if args.dry_run:
        if change.diff:
            print(change.diff)
        if skill_change.diff:
            print(skill_change.diff)
    if change.backup is not None:
        verb = "would back up" if args.dry_run else "backed up"
        print(f"{verb} the previous {target.name} to {change.backup}")
    if skill_change.backup is not None:
        verb = "would back up" if args.dry_run else "backed up"
        print(f"{verb} the previous {skill_change.target.name} to {skill_change.backup}")
    if args.uninstall:
        verb = "would remove" if args.dry_run else "removed"
        if change.changed:
            print(f"{verb} {', '.join(str(path) for path in change.removed)}")
        if skill_change.changed:
            print(f"{verb} {', '.join(str(path) for path in skill_change.removed)}")
    else:
        verb = "would write" if args.dry_run else "installed"
        if change.changed:
            print(f"{verb} {target}")
        else:
            print(f"{target} is already up to date")
        if skill_change.changed:
            print(f"{verb} {skill_change.target}")
        else:
            print(f"{skill_change.target} is already up to date")
    return 0


def cmd_install_timer(args) -> int:
    """Install or remove afterthought's systemd user service and timer units."""
    unit_dir = systemd_install.unit_dir()
    cfg = load()
    change = (
        systemd_install.uninstall(unit_dir, dry_run=args.dry_run)
        if args.uninstall
        else systemd_install.install(unit_dir, config=cfg, dry_run=args.dry_run)
    )
    if not change.changed:
        if args.uninstall:
            print(f"no afterthought timer units to remove in {unit_dir}")
        else:
            print(f"afterthought timer units in {unit_dir} are already up to date")
        return 0
    if args.dry_run:
        print(change.preview or f"no changes to {unit_dir}")
        return 0
    if args.uninstall:
        print(f"removed {', '.join(str(p) for p in change.units)}")
    else:
        print(f"installed {', '.join(str(p) for p in change.units)}")
    return 0


def cmd_library_survey(args) -> int:
    return survey.run(args.path, as_json=args.json)


def cmd_library_validate(args) -> int:
    return manifest.run(args.manifest, path_override=args.path)


def cmd_library_import(args) -> int:
    return importing.run(args.manifest, path_override=args.path)


def cmd_library_check_map(args) -> int:
    return map_evidence.run(args.manifest, map_files=args.map_files, path_override=args.path)


def cmd_library_describe(args) -> int:
    return describe.run(
        args.manifest,
        args.descriptions_file,
        path_override=args.path,
        dry_run=args.dry_run,
    )


def cmd_library_propose(args) -> int:
    return propose.run(
        args.manifest,
        args.facts_file,
        path_override=args.path,
        dry_run=args.dry_run,
    )


# An agent's session waits on this command, so the whole hook gets one hard budget
# before the agent's own hook timeout would have to rescue it (BUILD-PLAN M1.1). The
# budget is split up front between the two steps that can block: the stdin read takes
# the larger share, the failure-line write keeps the remainder named below, so no step
# can hang and the hook as a whole never holds a session past the bound.
HOOK_TIMEOUT_SECONDS = 2
HOOK_LOG_TIMEOUT_SECONDS = 0.5
# Recording a starting session's project is skipped when less than this is left of the
# hook's working share: the block is already out, and the record is not worth the budget.
HOOK_RECORD_MIN_SECONDS = 0.05


def cmd_hook(args) -> int:
    """Handle one agent lifecycle event: read a payload, build the event, dispatch it.

    An agent can do nothing useful with a hook failure, so this command never raises,
    never prints on a failure, and always exits 0. The one line it leaves in the hook
    log is the whole record of what went wrong (BUILD-PLAN M1.1).
    """
    remember = None
    deadline = time.monotonic() + HOOK_TIMEOUT_SECONDS - HOOK_LOG_TIMEOUT_SECONDS
    try:
        with _hook_time_limit(HOOK_TIMEOUT_SECONDS - HOOK_LOG_TIMEOUT_SECONDS):
            payload = json.load(sys.stdin)
            event = _hook_event(args, payload)
            if event.kind == SESSION_START:
                block, remember = _session_start(event)
            else:
                block = _dispatch(event)
        # Printed once the block is whole and the time limit is behind it, so an expiry
        # never leaves an agent holding the first half of a block.
        if block:
            print(block, flush=True)
    except (Exception, HookTimeoutError) as exc:
        _log_hook_failure(args.agent, args.event, exc)
    remaining = deadline - time.monotonic()
    if remember is not None and remaining > HOOK_RECORD_MIN_SECONDS:
        # After the block is out, so a busy queue never costs a session its memory, and
        # inside what is left of the same share of the budget the block was built in.
        try:
            with _hook_time_limit(remaining):
                remember()
        except (Exception, HookTimeoutError) as exc:
            _log_hook_failure(args.agent, args.event, exc)
    return 0


def _hook_event(args, payload: object) -> SessionEvent:
    """The event named on the command line, built from the payload read off stdin."""
    parse = adapters.PARSERS.get(args.agent)
    if parse is None:
        raise AdapterError(
            f"unknown agent {args.agent!r}, expected one of {' or '.join(adapters.PARSERS)}"
        )
    if not isinstance(payload, dict):
        raise AdapterError(f"{args.agent} payload is not a JSON object")
    return parse(args.event, payload, datetime.now(UTC))


def _dispatch(event: SessionEvent) -> str:
    """Hand one normalized event to the part of afterthought that consumes it, and return
    what the hook prints for it.

    ``session_start`` is injection's: it returns the block `aft inject` prints for the
    event's working directory (BUILD-PLAN M1.1, M1.2). The block goes out as plain text.
    Claude Code adds a ``SessionStart`` hook's plain stdout to the session's context, for
    every source including ``compact`` (hooks reference, "SessionStart"), and pi's extension
    reads the same text and adds it as a system prompt section. Injection only reads, so
    ``capture.exclude_paths`` and ``capture.exclude_projects`` do not apply to it. A vault
    that is not initialized has no memory to give and returns nothing. `cmd_hook` also
    records the project a captured session started in, once the block is printed
    (`_session_start`).

    ``compaction`` and ``session_end`` are capture work, so each writes one queue row and
    returns nothing to print: no model call, no transcript read, no project resolution
    (DESIGN 8, BUILD-PLAN M2.2).
    """
    if event.kind == SESSION_START:
        return _session_start(event)[0]
    if event.kind in (COMPACTION, SESSION_END):
        cfg = load()
        if is_excluded(event.cwd, cfg):
            return ""
        queue.enqueue(cfg, event)
        systemd_install.trigger_early(cfg)
    return ""


def _session_start(event: SessionEvent) -> tuple[str, Callable[[], None] | None]:
    """The block a starting session is given, and the step that records its project.

    The directory is resolved once, for both. Recording is returned rather than done, so the
    hook prints the block first: the record is what files this session's words if its
    directory is gone by the time they are digested (DESIGN 7), and it is capture's, so a
    session in an excluded path or project records nothing, and neither does a directory
    that resolves to no project.
    """
    config = load()
    if not vault.is_initialized(config.vault):
        return "", None
    registry = Registry.load(config.registry_path)
    result = keys.resolve(event.cwd, registry, config.host)
    block = inject.build(config, result)
    if result.slug is None or is_excluded(event.cwd, config):
        return block, None
    project_id = result.project.id if result.project is not None else None
    slug = result.slug

    def remember() -> None:
        queue.record_project(config, event.agent, event.session_id, project_id, slug)

    return block, remember


class HookTimeoutError(BaseException):
    """One blocking step of the hook spent its share of the hook budget.

    The timer behind it fires once, so the expiry has to reach `cmd_hook` the first time it
    is raised. It is a ``BaseException`` for that reason: the code a hook runs treats an
    ``OSError`` as a page that is absent or a command that would not start, and carries on,
    and the built-in ``TimeoutError`` is an ``OSError``. Only the two handlers in this module
    that name this class catch it.
    """


@contextmanager
def _hook_time_limit(seconds: float) -> Iterator[None]:
    """Bound one blocking step of the hook to its share of the hook budget.

    The two callers pass the two shares of HOOK_TIMEOUT_SECONDS, so every blocking step
    is bounded on its own and the hook as a whole still fits the bound in that constant.
    Where SIGALRM is unavailable the limit is not enforced here and the agent's own hook
    timeout is the bound.
    """
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def expire(signum, frame) -> None:
        raise HookTimeoutError(f"the hook gave up after {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


# The one line a hook failure leaves behind, in characters.
HOOK_LOG_LINE_LIMIT = 500

# What one caller-supplied argument may take of that line.
HOOK_LOG_FIELD_LIMIT = 64


def _log_hook_failure(agent: str, event: str, exc: BaseException) -> None:
    """Append the one line a failed hook is allowed to leave behind.

    Agent, event and the failure's own words all come from outside this process, so
    the whole line is flattened and capped: whatever they contain, a failed hook
    leaves one bounded line and nothing else. Each caller-supplied argument is clamped
    to its own budget and the event name goes last, so a runaway argument loses its
    own tail rather than the reason for the failure. Best effort all the way down, and
    inside the share of the hook budget HOOK_LOG_TIMEOUT_SECONDS reserves for this write:
    a home that cannot be resolved or written to, a line that cannot be built or
    written, and an expiry that lands mid-write are a lost record, not a reason to
    fail the agent.
    """
    try:
        line = (
            f"{datetime.now(UTC).isoformat()} {agent[:HOOK_LOG_FIELD_LIMIT]}"
            f" {type(exc).__name__}: {exc} | {event[:HOOK_LOG_FIELD_LIMIT]}"
        )
        line = " ".join(line.split())[:HOOK_LOG_LINE_LIMIT]
        with _hook_time_limit(HOOK_LOG_TIMEOUT_SECONDS):
            home = resolve_home()
            (home / "logs").mkdir(parents=True, exist_ok=True)
            with (home / "logs" / "hooks.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except (Exception, HookTimeoutError):
        pass
