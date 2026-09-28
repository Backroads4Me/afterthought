"""The ``aft`` command line."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from aft import __version__, adapters, gitutil, keys, lint, vault
from aft.config import DEFAULT_CONFIG_TEXT, Config, ConfigError, load, resolve_home
from aft.events import AdapterError, SessionEvent
from aft.gitutil import GitError
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
    except (ConfigError, RegistryError, vault.VaultError, GitError, UsageError) as exc:
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

    p = sub.add_parser("hook", help="handle one agent lifecycle event read from stdin")
    p.add_argument("agent", help="the agent that fired the event: " + " or ".join(adapters.PARSERS))
    p.add_argument("event", help="that agent's own event name, e.g. SessionStart")
    p.set_defaults(handler=cmd_hook)
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

    ``session_start`` prints the injection block (M1.2); ``compaction`` and
    ``session_end`` enqueue capture (M2.2). Neither exists yet, so a parsed event is
    recognized and dropped here: there is nothing to print and nothing to enqueue,
    which is the no output and exit 0 the hook contract asks for in the meantime.
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
    a home that cannot be resolved or written to, a line that cannot be written, and an
    expiry that lands mid-write are a lost record, not a reason to fail the agent.
    """
    line = (
        f"{datetime.now(UTC).isoformat()} {agent[:HOOK_LOG_FIELD_LIMIT]}"
        f" {type(exc).__name__}: {exc} | {event[:HOOK_LOG_FIELD_LIMIT]}"
    )
    line = " ".join(line.split())[:HOOK_LOG_LINE_LIMIT]
    try:
        with _hook_time_limit(HOOK_LOG_TIMEOUT_SECONDS):
            home = resolve_home()
            (home / "logs").mkdir(parents=True, exist_ok=True)
            with (home / "logs" / "hooks.log").open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        pass
