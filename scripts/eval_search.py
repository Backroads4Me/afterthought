#!/usr/bin/env python3
"""F80: measure where an answer ranks in search, over a frozen copy of a home.

`aft eval --ranks` measures recall and MRR over search alone (BUILD-PLAN F80). The
numbers only mean something when every run searches the same memory, so this script
freezes a home first and runs the measure against the copy:

    eval_search.py snapshot DIR   clone the vault, copy the registry, config and evidence
                                  archive into DIR as a home of its own, rebuild the index
    eval_search.py run DIR KEY    run `aft eval KEY --ranks` with AFT_HOME=DIR

The snapshot reads the home AFT_HOME names (a clone and a sqlite backup write nothing
there) and writes only into DIR. `run` and the snapshot's own destination refuse any path
that is, or sits below, the user's real home or real vault, like eval_digest.py does.
DIR/snapshot.json records the vault commit the clone was taken at, the archive row count
and the date, so two snapshots can be told apart later.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from aft import config as configmod
from aft import evidence
from aft.config import Config

REPO_ROOT = Path(__file__).resolve().parents[1]

#: How deep `run` looks by default, which is the rank measure's own default.
DEFAULT_LIMIT = 10


class EvalError(Exception):
    """A snapshot or run that cannot proceed without touching something it must not."""


def _refuse_real(path: Path, what: str) -> None:
    """Refuse a path that is, or sits below, the user's real home or real vault."""
    home = path.expanduser().resolve()
    for banned in (Path("~/.afterthought"), Path("~/afterthought")):
        real = banned.expanduser().resolve()
        if home == real or real in home.parents:
            raise EvalError(f"refusing {what} {path}: the real home and vault are never written")


def snapshot(dest: str) -> int:
    """Freeze the home AFT_HOME names into ``dest`` as a home of its own.

    The vault is cloned at its current commit, the live registry is copied over the
    clone's (the clone holds the last committed one, and uncommitted edits are exactly
    what a snapshot should catch), the evidence archive is copied through sqlite's backup
    API, and the config is rewritten with the vault pointed at the clone. The index is
    then rebuilt inside the snapshot, so a `run` against it reads what the real home
    reads.
    """
    target = Path(dest).expanduser()
    _refuse_real(target, "snapshot home")
    if target.exists() and any(target.iterdir()):
        raise EvalError(f"refusing {target}: it exists and is not empty")
    cfg = configmod.load()
    if not cfg.registry_path.is_file():
        raise EvalError(f"no registry at {cfg.registry_path}; the home is not initialized")
    target.mkdir(parents=True)
    vault_dir = target / "vault"
    _git(["clone", "--quiet", str(cfg.vault), str(vault_dir)])
    shutil.copy2(cfg.registry_path, vault_dir / cfg.registry_path.name)
    rows = _copy_archive(cfg, target)
    _write_config(cfg, target)
    commit = _git(["-C", str(vault_dir), "rev-parse", "HEAD"])
    record = {
        "vault_commit": commit,
        "archive_rows": rows,
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (target / "snapshot.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    _aft(["reindex"], target)
    print(f"snapshot of {cfg.vault} at {commit} in {target} ({rows} archived entries)")
    return 0


def run(
    home: str,
    key: str,
    *,
    json_out: str | None,
    limit: int,
    variant: str | None = None,
) -> int:
    """Run the rank measure against the snapshot ``home`` and pass the exit through.

    The child is `aft eval KEY --ranks` with AFT_HOME set to the snapshot, so the measure
    reads the frozen vault and archive and nothing else. Rank mode exits 0 however the
    entries ranked; a nonzero exit here means the run itself failed.
    """
    target = Path(home).expanduser()
    _refuse_real(target, "snapshot home")
    if not (target / configmod.CONFIG_NAME).is_file():
        raise EvalError(f"{target} holds no config.toml; run snapshot first")
    args = ["eval", key, "--ranks", "--limit", str(limit)]
    if json_out is not None:
        args += ["--json", json_out]
    if variant is not None:
        args += ["--variant", variant]
    env = dict(os.environ)
    env[configmod.HOME_ENV] = str(target)
    completed = subprocess.run([sys.executable, "-m", "aft", *args], env=env)
    return completed.returncode


def _git(args: list[str]) -> str:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=REPO_ROOT, check=False
    )
    if result.returncode != 0:
        raise EvalError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _aft(args: list[str], home: Path) -> None:
    """Run one aft command against ``home``, which AFT_HOME names for the child."""
    env = dict(os.environ)
    env[configmod.HOME_ENV] = str(home)
    result = subprocess.run(
        [sys.executable, "-m", "aft", *args],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise EvalError(f"aft {' '.join(args)} failed:\n{result.stdout}{result.stderr}")


def _copy_archive(cfg: Config, target: Path) -> int:
    """Copy the evidence archive through sqlite's backup API, and return its row count.

    The archive may be open by another process, so a file copy could take a torn file;
    the backup API reads a consistent copy through sqlite itself, and a read-only source
    connection leaves the original untouched. A home with no archive yet copies nothing
    and reports no rows.
    """
    source = cfg.home / evidence.EVIDENCE_NAME
    if not source.exists():
        return 0
    dest = target / evidence.EVIDENCE_NAME
    uri = "file:" + urllib.parse.quote(str(source.resolve())) + "?mode=ro"
    src = sqlite3.connect(uri, uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    conn = sqlite3.connect(dest)
    try:
        (rows,) = conn.execute("SELECT COUNT(*) FROM evidence").fetchone()
    finally:
        conn.close()
    return int(rows)


def _write_config(cfg: Config, target: Path) -> None:
    """The snapshot's config: today's settings with the vault pointed at the clone."""
    lines = [
        f"vault = {_toml(str(target / 'vault'))}",
        f"host = {_toml(cfg.host)}",
    ]
    for name, value in asdict(cfg.settings).items():
        if not isinstance(value, dict):
            continue  # the vault setting itself is written above, pointed at the clone
        lines += ["", f"[{name}]", *(_setting(key, item) for key, item in value.items())]
    (target / configmod.CONFIG_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _setting(key: str, value: Any) -> str:
    return f"{_key(key)} = {_toml(value)}"


def _toml(value: Any) -> str:
    """One config value as the TOML text that holds it.

    The settings are plain scalars, lists and tables once the dataclasses are flattened,
    which is all this has to cover; anything else is a bug in this script and is refused
    rather than written half-formed.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        inner = ", ".join(f"{_key(key)} = {_toml(item)}" for key, item in value.items())
        return "{ " + inner + " }" if inner else "{}"
    raise EvalError(f"cannot write a config value of {type(value).__name__}")


def _key(name: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return json.dumps(name)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Freeze an afterthought home and run the search rank measure on the copy."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("snapshot", help="freeze the current home into DIR as a home of its own")
    p.add_argument("dir", help="the snapshot home to create; it must not exist or be empty")
    q = sub.add_parser("run", help="run the rank measure against a snapshot home")
    q.add_argument("dir", help="the snapshot home, as created by snapshot")
    q.add_argument(
        "key", help="the golden set to rank, e.g. ~/src/aft-eval-data/search/coding.yaml"
    )
    q.add_argument(
        "--json",
        dest="json_out",
        metavar="FILE",
        help="write the per-entry records and the summary as JSON",
    )
    q.add_argument(
        "--limit", type=int, default=DEFAULT_LIMIT, help="how deep the rank measure looks"
    )
    q.add_argument(
        "--variant",
        default=None,
        help="search variant to evaluate (default: today's search)",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "snapshot":
            return snapshot(args.dir)
        return run(
            args.dir,
            args.key,
            json_out=args.json_out,
            limit=args.limit,
            variant=args.variant,
        )
    except EvalError as exc:
        print(f"eval_search: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
