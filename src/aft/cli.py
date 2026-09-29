"""The ``aft`` command line."""

from __future__ import annotations

import argparse
import json
import signal
import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from aft import __version__, adapters, gitutil, index, inject, keys, lint, queue, vault, work
from aft.config import DEFAULT_CONFIG_TEXT, Config, ConfigError, load, resolve_home
from aft.dream import schedule
from aft.events import COMPACTION, SESSION_END, AdapterError, SessionEvent
from aft.gitutil import GitError
from aft.install import claude as claude_install
from aft.install import pi as pi_install
from aft.install import systemd as systemd_install
from aft.library import manifest, survey
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
        systemd_install.InstallError,
        RegistryError,
        vault.VaultError,
        GitError,
        index.SearchIndexError,
        UsageError,
        survey.SurveyError,
        manifest.ManifestError,
        queue.QueueError,
        sqlite3.Error,
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
    config = load()
    registry = Registry.load(config.registry_path)
    result = keys.resolve(Path(args.path), registry, config.host)
    block = inject.build(config, result)
    if block:
        print(block)
    return 0


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


def cmd_project_rename(args) -> int:
    config = load()
    vault.require(config)
    with vault.locked(config):
        registry = Registry.load(config.registry_path)
        registry.rename(args.old, args.new)
        registry.save()
        changed = ["registry.yaml"]
        for old, new in (
            (f"projects/{args.old}", f"projects/{args.new}"),
            (f"digests/{args.old}", f"digests/{args.new}"),
            (f"archive/{args.old}.md", f"archive/{args.new}.md"),
        ):
            if (config.vault / old).exists():
                (config.vault / old).rename(config.vault / new)
                changed += [old, new]
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
    return 0


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
    for slug in result.active:
        print(f"aft dream: {slug}: skipped, a session was active within the quiet period")
    for slug in result.edited:
        print(f"aft dream: {slug}: skipped, the vault has hand edits within the quiet period")


def cmd_install_claude(args) -> int:
    """Add or remove afterthought's hook entries in Claude Code's own settings file."""
    settings = claude_install.settings_path()
    change = (
        claude_install.uninstall(settings, dry_run=args.dry_run)
        if args.uninstall
        else claude_install.install(settings, dry_run=args.dry_run)
    )
    if args.dry_run:
        print(change.preview or f"no changes to {settings}")
        return 0
    if change.backup is not None:
        print(f"backed up {settings} to {change.backup}")
    if args.uninstall:
        print(
            f"removed afterthought's hooks from {settings}"
            if change.changed
            else f"no afterthought hooks to remove in {settings}"
        )
    else:
        print(
            f"installed afterthought's hooks in {settings}"
            if change.changed
            else f"afterthought's hooks are already in {settings}"
        )
    return 0


def cmd_install_pi(args) -> int:
    """Copy or remove afterthought's extension in the directory pi loads extensions from."""
    extensions = pi_install.extensions_dir()
    change = (
        pi_install.uninstall(extensions, dry_run=args.dry_run)
        if args.uninstall
        else pi_install.install(extensions, dry_run=args.dry_run)
    )
    target = pi_install.target_path(extensions)
    if not change.changed:
        if args.uninstall:
            print(f"no afterthought extension in {extensions}")
        else:
            print(f"{target} is already up to date")
        return 0
    # Every line of a preview says what the run itself would say, so the preview cannot
    # promise less than the command does: the text as a diff when there is one, the copy a
    # change makes of the text it replaces, and the paths it writes or takes away.
    if args.dry_run and change.diff:
        print(change.diff)
    if change.backup is not None:
        verb = "would back up" if args.dry_run else "backed up"
        print(f"{verb} the previous {target.name} to {change.backup}")
    if args.uninstall:
        verb = "would remove" if args.dry_run else "removed"
        print(f"{verb} {', '.join(str(path) for path in change.removed)}")
    else:
        verb = "would write" if args.dry_run else "installed"
        print(f"{verb} {target}")
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


# An agent's session waits on this command, so the whole hook gets one hard budget
# before the agent's own hook timeout would have to rescue it (BUILD-PLAN M1.1). The
# budget is split up front between the two steps that can block: the stdin read takes
# the larger share, the failure-line write keeps the remainder named below, so no step
# can hang and the hook as a whole never holds a session past the bound.
HOOK_TIMEOUT_SECONDS = 2
HOOK_LOG_TIMEOUT_SECONDS = 0.5


def cmd_hook(args) -> int:
    """Handle one agent lifecycle event: read a payload, build the event, dispatch it.

    An agent can do nothing useful with a hook failure, so this command never raises,
    never prints on a failure, and always exits 0. The one line it leaves in the hook
    log is the whole record of what went wrong (BUILD-PLAN M1.1).
    """
    try:
        with _hook_time_limit(HOOK_TIMEOUT_SECONDS - HOOK_LOG_TIMEOUT_SECONDS):
            payload = json.load(sys.stdin)
            _dispatch(_hook_event(args, payload))
    except Exception as exc:
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


def _dispatch(event: SessionEvent) -> None:
    """Hand one normalized event to the part of afterthought that consumes it.

    ``compaction`` and ``session_end`` are capture work, so each writes one queue row and
    returns: no model call, no transcript read, no project resolution (DESIGN 8, BUILD-PLAN
    M2.2). ``session_start`` is injection's, which the agents run as `aft inject` (M1.2),
    so it prints nothing here, and the hook contract asks for silence and exit 0.
    """
    if event.kind in (COMPACTION, SESSION_END):
        cfg = load()
        queue.enqueue(cfg, event)
        systemd_install.trigger_early(cfg)


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
        raise TimeoutError(f"the hook gave up after {seconds} seconds")

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
    except Exception:
        pass
