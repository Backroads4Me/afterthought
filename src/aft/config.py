"""Configuration, read from ``config.toml`` in the afterthought home directory.

Every setting has a default, so a missing file is a valid configuration. Unknown keys
and wrongly typed values are errors, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import os
import socket
import tomllib
import typing
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path

HOME_ENV = "AFT_HOME"
DEFAULT_HOME = "~/.afterthought"
CONFIG_NAME = "config.toml"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ModelConfig:
    endpoint: str = "http://localhost:8000/v1"
    name: str = ""
    api_key_env: str = ""
    # Fields merged into every request body, for what one endpoint needs and another does
    # not: turning a reasoning model's thinking off, for example. The fields aft sets itself
    # are refused here, so this can add to a request and never change what aft asks for.
    extra_body: dict[str, typing.Any] = field(default_factory=dict)


# The request fields aft sets itself, which model.extra_body may not name.
MODEL_REQUEST_FIELDS = frozenset({"model", "messages", "max_tokens", "response_format"})


@dataclass(frozen=True)
class CaptureConfig:
    worker_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    # Projects whose sessions are never captured, named by id or by slug. A path cannot
    # answer this: the same project reaches the host through a second clone or a worktree
    # at a directory no one listed.
    exclude_projects: tuple[str, ...] = ()
    redact_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceConfig:
    retention_days: int = 90


@dataclass(frozen=True)
class DreamConfig:
    interval_minutes: int = 30
    quiet_minutes: int = 15


@dataclass(frozen=True)
class WorkConfig:
    """The cap on one day of ``aft work``. DESIGN 13 expects about 55,000 input tokens a
    day of digests plus a smaller amount of dreaming; four times that bounds a loop that
    has gone wrong without ever parking an ordinary day's work."""

    daily_token_budget: int = 200_000


@dataclass(frozen=True)
class InjectConfig:
    max_chars: int = 8000


@dataclass(frozen=True)
class LibraryConfig:
    commit: bool = True


@dataclass(frozen=True)
class Settings:
    """The contents of config.toml, with defaults for everything left out."""

    vault: str = "~/afterthought"
    host: str = ""
    model: ModelConfig = field(default_factory=ModelConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    dream: DreamConfig = field(default_factory=DreamConfig)
    work: WorkConfig = field(default_factory=WorkConfig)
    inject: InjectConfig = field(default_factory=InjectConfig)
    library: LibraryConfig = field(default_factory=LibraryConfig)


@dataclass(frozen=True)
class Config:
    home: Path
    vault: Path
    host: str
    settings: Settings

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_NAME

    @property
    def registry_path(self) -> Path:
        return self.vault / "registry.yaml"

    @property
    def lock_path(self) -> Path:
        return self.home / "vault.lock"


def resolve_home(environ: typing.Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    return Path(env.get(HOME_ENV) or DEFAULT_HOME).expanduser()


def default_host() -> str:
    return socket.gethostname().split(".")[0].lower()


def load(environ: typing.Mapping[str, str] | None = None) -> Config:
    home = resolve_home(environ)
    path = home / CONFIG_NAME
    table: dict = {}
    if path.exists():
        try:
            table = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
    settings = _build(Settings, table, where="")
    vault = Path(settings.vault).expanduser()
    if not vault.is_absolute():
        raise ConfigError(f"{path}: vault must be an absolute path, got {settings.vault!r}")
    taken = sorted(MODEL_REQUEST_FIELDS & set(settings.model.extra_body))
    if taken:
        raise ConfigError(
            f"{path}: model.extra_body may not set {', '.join(taken)}; aft sets those itself"
        )
    if settings.dream.interval_minutes <= 0:
        raise ConfigError(f"{path}: dream.interval_minutes must be positive")
    host = settings.host or default_host()
    return Config(home=home, vault=vault, host=host, settings=settings)


def _build(cls: type, table: dict, where: str):
    hints = typing.get_type_hints(cls)
    known = {f.name for f in fields(cls)}
    for key in table:
        if key not in known:
            raise ConfigError(f"unknown setting {where}{key}")
    values = {}
    for f in fields(cls):
        if f.name not in table:
            continue
        name = f"{where}{f.name}"
        value = table[f.name]
        hint = hints[f.name]
        if is_dataclass(hint):
            if not isinstance(value, dict):
                raise ConfigError(f"[{name}] must be a table")
            values[f.name] = _build(hint, value, where=f"{name}.")
        else:
            values[f.name] = _coerce(hint, value, name)
    return cls(**values)


def _coerce(hint, value, name: str):
    if hint is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{name} must be true or false")
        return value
    if hint is int:
        # bool is a subclass of int, so it has to be refused explicitly.
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{name} must be an integer")
        return value
    if hint is str:
        if not isinstance(value, str):
            raise ConfigError(f"{name} must be a string")
        return value
    if hint is dict or typing.get_origin(hint) is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{name} must be a table")
        return value
    if typing.get_origin(hint) is tuple:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{name} must be a list of strings")
        return tuple(value)
    raise ConfigError(f"{name} has an unsupported type")


def is_under(path: Path | str, parents: tuple[str, ...]) -> bool:
    """Whether path is equal to or a descendant of any path in parents."""
    if not parents:
        return False
    resolved = Path(path).expanduser().resolve()
    for parent in parents:
        if not parent.strip():
            continue
        try:
            if resolved.is_relative_to(Path(parent).expanduser().resolve()):
                return True
        except ValueError:
            continue
    return False


def is_excluded(path: Path | str, config: Config) -> bool:
    """Whether this directory is excluded from capture.

    Two settings answer it: a directory under ``capture.exclude_paths``, and a directory
    belonging to a project named in ``capture.exclude_projects`` - by id or by slug, and by
    the project rather than by one of its directories, which is how a second clone or a
    worktree of it is kept out too.

    The project question is asked only when someone named a project, because answering it
    resolves the directory the way a digest does, and the capture hook has a budget (DESIGN
    8). With nothing named, this costs the path check and nothing else.
    """
    if is_under(path, config.settings.capture.exclude_paths):
        return True
    names = config.settings.capture.exclude_projects
    if not names:
        return False
    # Read here rather than imported at the top: the resolver reads the registry, and the
    # registry is a vault file this module is asked about before any vault is settled.
    from aft import keys, registry  # a local import breaks the cycle with the resolver
    from aft.gitutil import GitError

    try:
        resolved = keys.resolve(
            Path(path), registry.Registry.load(config.registry_path), config.host
        )
    except GitError:
        # A directory git will not read is not a project this setting can name. The digest
        # fails such a session on its own resolve, which is what happens when no project is
        # named here either, so this path neither captures the words nor loses a session the
        # resolver would have placed.
        return False
    project = resolved.project
    return project is not None and (project.id in names or project.slug in names)


def is_worker(path: Path | str, config: Config) -> bool:
    """Whether path is a worker session path under capture.worker_paths."""
    return not is_excluded(path, config) and is_under(path, config.settings.capture.worker_paths)


DEFAULT_CONFIG_TEXT = """\
# Afterthought configuration. Every setting is optional; the values shown are the defaults.

# The memory vault: a git repository and Obsidian vault.
vault = "~/afterthought"

# The name this machine uses in project keys. Defaults to the short hostname.
# host = ""

[model]
# An OpenAI-compatible endpoint used for digests and dreaming.
endpoint = "http://localhost:8000/v1"
name = ""
# The environment variable holding this endpoint's API key, sent as a bearer token.
# aft stores the name of a key and never the key itself. Empty means the endpoint
# takes no key.
# api_key_env = ""
# Fields added to every request, for what this endpoint needs. A reasoning model served
# by vLLM that spends its whole answer thinking is told not to with:
# extra_body = { chat_template_kwargs = { enable_thinking = false } }

[capture]
# Sessions under these paths are worker sessions: archived and searchable, digested
# only with spare budget, and never promoted without a primary session's support.
worker_paths = []
# Sessions under these paths are never captured.
exclude_paths = []
# Sessions of these projects are never captured, by project id or slug. A project named
# here is kept out whatever directory its session ran in, including a second clone or a
# worktree of it that no path above names.
exclude_projects = []
# Extra regular expressions to redact, on top of the built-in secret patterns
# (see aft.redact). They match case-insensitively and are applied after the
# built-ins. One that does not compile, or that holds nothing to match, is an error
# raised before any text is filtered, never a silent no-op.
redact_patterns = []

[evidence]
# Days to keep filtered conversation in the evidence archive.
retention_days = 90

[dream]
interval_minutes = 30
quiet_minutes = 15

[work]
# Tokens one day of "aft work" may spend before the rest of the queue waits for
# tomorrow. An ordinary day of digests is about 55,000, so this is about four times
# that: enough for a heavy day, short enough to bound a run that has gone wrong.
daily_token_budget = 200000

[inject]
max_chars = 8000

[library]
# Commit library files to the vault's git repository. Every import brings the vault's
# .gitignore in line with this setting, so switching it off at any time keeps the files on
# disk and out of git rather than leaving them untracked, unignored and collectable as
# human edits.
commit = true
"""
