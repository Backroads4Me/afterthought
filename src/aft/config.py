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


@dataclass(frozen=True)
class CaptureConfig:
    worker_paths: tuple[str, ...] = ()
    exclude_paths: tuple[str, ...] = ()
    redact_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceConfig:
    retention_days: int = 90


@dataclass(frozen=True)
class DreamConfig:
    interval_minutes: int = 30
    quiet_minutes: int = 15


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
    if typing.get_origin(hint) is tuple:
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{name} must be a list of strings")
        return tuple(value)
    raise ConfigError(f"{name} has an unsupported type")


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

[capture]
# Sessions under these paths are worker sessions: archived and searchable, digested
# only with spare budget, and never promoted without a primary session's support.
worker_paths = []
# Sessions under these paths are never captured.
exclude_paths = []
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

[inject]
max_chars = 8000

[library]
# Commit library files to the vault's git repository.
commit = true
"""
