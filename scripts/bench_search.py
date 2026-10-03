"""Time typical `aft search` queries against a 20,000-row index (BUILD-PLAN F50).

The script builds its own AFT_HOME and vault under the system temporary directory,
exactly as tests/conftest.py does, fills them with several projects' pages, digests and
evidence until the index holds about 20,000 rows, and times the searches a person runs
from one project: the default query, a narrowed one, ``--all``, ``--archive``, and the
default from a directory that is no project's. It times `aft.search.search` in process,
best of several runs per scenario, so two builds can be compared on one machine.

Run it from a repository checkout, so `uv run` installs and imports that checkout's code:
`uv run python scripts/bench_search.py`. Nothing outside the temporary home is touched.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft import evidence, index, registry, search, vault
from aft.config import Config, load
from aft.readers import Entry

HOME_ENV = "AFT_HOME"

# The corpus shape. The knobs are tuned so the index lands near 20,000 rows with what the
# index actually writes: one row per page section, per digest section, and per archived
# entry (BUILD-PLAN F50 sizes the bench at 20,000 rows).
PROJECTS = 8
PAGES_PER_PROJECT = 24
SECTIONS_PER_PAGE = 6
SESSIONS_PER_PROJECT = 30
ENTRIES_PER_SESSION = 76

REPEATS = 5

PARTS = (
    "bracket",
    "flange",
    "gasket",
    "spindle",
    "bearing",
    "coupling",
    "manifold",
    "harness",
    "actuator",
    "retainer",
)
MATERIALS = (
    "titanium",
    "molybdenum",
    "zirconium",
    "vanadium",
    "hafnium",
    "rhenium",
    "osmium",
    "iridium",
    "palladium",
    "rhodium",
    "ruthenium",
    "cadmium",
    "indium",
    "gallium",
    "germanium",
)


KIND_SECTIONS = ("State", "Facts", "Decisions", "Preferences", "Lessons", "Procedures")


ACTIONS = (
    "torqued",
    "measured",
    "cleaned",
    "rebuilt",
    "moved",
    "scanned",
    "logged",
    "checked",
)
#: A word planted in about one line in ninety, so one query in the set matches a few
#: dozen rows the way a distinctive codename does.
RARE_WORD = "tantalum"

GLOBAL_TEXT = """---
type: Preferences
title: Preferences
description: How the benches are run, everywhere.
sources: []
---

# Preferences

## Preferences

- Every bench keeps its tantalum audit log for ninety days. [stated] 2026-09-20 [^s1]
- The bench0 bench is the reference for bracket torque. [stated] 2026-09-20 [^s1]

## State

- The tantalum audit runs on the first Monday of the month. [observed] 2026-09-20 [^s1]

## Decisions

- A bench whose baseline is unconfirmed reports the number as a range. [stated] 2026-09-20 [^s1]
"""


def page_text(project: int, page: int) -> str:
    """One project page: a title, a description, and one fact line per kind section."""
    lines = []
    for section, kind in enumerate(KIND_SECTIONS):
        part = PARTS[(page + section) % len(PARTS)]
        action = ACTIONS[(page * 2 + section) % len(ACTIONS)]
        material = MATERIALS[(project * 7 + page * 3 + section * 5) % len(MATERIALS)]
        audit = f" during the {RARE_WORD} audit" if (page * 6 + section) % 89 == 0 else ""
        lines.append(
            f"## {kind}\n\n"
            f"- The {part} was {action} to {page * section + 10} units on the {material}"
            f" bracket at site bench{project}{audit}. [observed] 2026-09-20 [^s1]\n"
        )
    body = "\n".join(lines)
    return (
        "---\n"
        "type: Project\n"
        f"title: {project} bench page {page}\n"
        f"description: What project {project} knows about page {page}.\n"
        "sources: []\n"
        "---\n\n"
        f"# Bench page {page}\n\n"
        f"{body}"
    )


def digest_text(project: int, session: int) -> str:
    """One session digest: a summary and a handoff, as BUILD-PLAN M2.6 files them."""
    part = PARTS[session % len(PARTS)]
    material = MATERIALS[(project * 11 + session * 3) % len(MATERIALS)]
    return (
        "---\n"
        "type: Session Digest\n"
        f"title: Session {session} of project {project}\n"
        f"description: What the session said about the {part}.\n"
        "---\n\n"
        f"# Session {session}\n\n"
        "## Summary\n\n"
        f"The session {ACTIONS[session % len(ACTIONS)]} the {part} against the {material}"
        f" baseline at site bench{project} and wrote down {session} numbers.\n\n"
        "## Handoff\n\n"
        f"Open: the {part} follow-up, and the {material} baseline is still unconfirmed.\n"
    )


def entry_text(project: int, session: int, entry: int) -> str:
    """One archived conversation entry, in the words the filter would have kept."""
    part = PARTS[(session + entry) % len(PARTS)]
    action = ACTIONS[(session * 3 + entry) % len(ACTIONS)]
    material = MATERIALS[(project * 5 + session + entry * 2) % len(MATERIALS)]
    audit = f" during the {RARE_WORD} audit" if (session * 76 + entry) % 89 == 0 else ""
    return (
        f"we {action} the {part} on the {material} bracket at site bench{project}"
        f" and set it to {entry % 90 + 10} units{audit}"
    )


def build(config: Config) -> None:
    """Create the vault, the projects, their pages, digests and evidence, and the index."""
    started = time.perf_counter()
    vault.init(config)
    base = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    projects = []
    for number in range(PROJECTS):
        slug = f"bench{number}"
        directory = config.vault / "projects" / slug
        directory.mkdir(parents=True, exist_ok=True)
        projects.append((slug, directory))
    registry.Registry(
        config.registry_path,
        [
            registry.Project(
                id=f"p_bench{number}",
                slug=slug,
                title=slug,
                paths=[f"aft-bench:{directory}"],
            )
            for number, (slug, directory) in enumerate(projects)
        ],
    ).save()
    for number, (slug, directory) in enumerate(projects):
        for page in range(PAGES_PER_PROJECT):
            (directory / f"page-{page}.md").write_text(page_text(number, page), encoding="utf-8")
        for session in range(SESSIONS_PER_PROJECT):
            rel = f"digests/{slug}/2026/09/2026-09-28-pi-s-bench{number}-{session}.md"
            path = config.vault / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(digest_text(number, session), encoding="utf-8")
            entries = [
                Entry(
                    id=f"e-{entry}",
                    time=base + timedelta(seconds=entry),
                    role="user" if entry % 2 == 0 else "assistant",
                    text=entry_text(number, session, entry),
                    is_error=False,
                )
                for entry in range(ENTRIES_PER_SESSION)
            ]
            evidence.store(config, "pi", f"s-bench{number}-{session}", f"p_bench{number}", entries)

    # The global pages every project shares, and one session whose directory answered no
    # project (DESIGN 7): the tiered search reaches both, and the scoped one it replaces
    # reached the first alone.
    global_dir = config.vault / "global"
    global_dir.mkdir(exist_ok=True)
    (global_dir / "preferences.md").write_text(GLOBAL_TEXT, encoding="utf-8")
    entries = [
        Entry(
            id=f"e-{entry}",
            time=base + timedelta(seconds=entry),
            role="user" if entry % 2 == 0 else "assistant",
            text=entry_text(PROJECTS, PROJECTS, entry),
            is_error=False,
        )
        for entry in range(ENTRIES_PER_SESSION)
    ]
    evidence.store(config, "pi", "s-bench-unfiled", None, entries)
    unfiled = config.vault / "digests/_unfiled/2026/09/2026-09-28-pi-s-bench-unfiled.md"
    unfiled.parent.mkdir(parents=True, exist_ok=True)
    unfiled.write_text(digest_text(PROJECTS, 0), encoding="utf-8")
    report = index.reindex(config)
    if report:
        print(f"index build reported {len(report)} problem(s):", file=sys.stderr)
        for line in report[:5]:
            print(f"  {line}", file=sys.stderr)
    print(f"built the index in {time.perf_counter() - started:.1f} s")


def report_rows(config: Config) -> None:
    """Print the row count per collection, so the run says what it timed."""
    conn = sqlite3.connect(config.home / index.INDEX_NAME)
    try:
        counts = {
            name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (index.PAGES, index.DIGESTS, index.EVIDENCE)
        }
    finally:
        conn.close()
    total = sum(counts.values())
    named = ", ".join(f"{name} {count}" for name, count in counts.items())
    print(f"index rows: {named}, total {total}")


def time_scenarios(
    config: Config, repeats: int
) -> list[tuple[str, list[tuple[str, list[str], float]]]]:
    """Time each scenario's queries two ways, best of ``repeats`` whole passes.

    One pass times `aft search` whole, the way the command pays for it, with the index
    check it runs first. The other times the query alone, with the check stubbed, so a
    change in the query itself is visible beside the check both builds share.
    """
    first_slug, first_directory = "bench0", config.vault / "projects" / "bench0"
    other = f"bench{PROJECTS // 2}"
    outside = Path(tempfile.mkdtemp(prefix="aft-bench-nowhere-"))
    scenarios = [
        ("default, from a project's directory", first_directory, {}),
        (f"--project {first_slug}", first_directory, {"project": first_slug}),
        ("--all (accepted, the default itself)", first_directory, {"all_projects": True}),
        ("--archive", first_directory, {"archive": True}),
        ("default, from a directory of no project", outside, {}),
    ]
    # Words chosen the way a person asks: a rare word beside this project's name, the same
    # rare word beside another project's name (the question F50 exists to answer), a
    # material a few hundred rows share, the word every digest's handoff section carries,
    # and a two-word ask of this project.
    queries = [
        f"tantalum {first_slug}",
        f"tantalum {other}",
        "molybdenum",
        "handoff",
        f"{first_slug} gasket",
    ]
    passes = [
        ("aft search, with the index check", None),
        ("the query alone, index check stubbed", _stub_index_check()),
    ]
    timings: list[tuple[str, list[tuple[str, list[str], float]]]] = []
    original = Path.cwd()
    try:
        for label, restore in passes:
            lines = []
            for name, directory, kwargs in scenarios:
                os.chdir(directory)
                best = float("inf")
                for _ in range(repeats):
                    started = time.perf_counter()
                    for query in queries:
                        search.search(config, query, **kwargs)
                    best = min(best, time.perf_counter() - started)
                lines.append((name, queries, best))
            timings.append((label, lines))
            if restore is not None:
                restore()
    finally:
        os.chdir(original)
        shutil.rmtree(outside, ignore_errors=True)
    return timings


def _stub_index_check() -> Callable[[], None]:
    """Stub the index check inside search for one timing pass, and hand back the restore.

    `aft search` runs `index.update` before every query, which is the cost both builds pay
    and the reason the query's own cost can hide inside it. The stub returns an empty
    report, which is what an index already in step returns.
    """
    real_update = index.update
    index.update = lambda config: []
    return lambda: setattr(index, "update", real_update)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=REPEATS)
    parser.add_argument("--keep", action="store_true", help="keep the temporary home and vault")
    args = parser.parse_args(argv)

    home = Path(tempfile.mkdtemp(prefix="aft-bench-home-"))
    (home / "config.toml").write_text(
        f'vault = "{home / "vault"}"\nhost = "aft-bench"\n',
        encoding="utf-8",
    )
    # Set before load() is called, so the run reads the scratch home and its vault.
    os.environ[HOME_ENV] = str(home)
    # A scratch git identity, so the vault's initialization commit reads nobody's config.
    gitconfig = home / "gitconfig"
    gitconfig.write_text(
        "[user]\n\tname = Bench\n\temail = bench@example.com\n[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    os.environ["GIT_CONFIG_GLOBAL"] = str(gitconfig)
    os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
    # The system temporary directory moves under the home, as tests/conftest.py moves it:
    # the vault and its projects then sit outside the directories that never become
    # projects (DESIGN 7), while the run's own scratch directories stay inside them.
    scratch = home / "scratch"
    scratch.mkdir()
    original_temp = tempfile.tempdir
    tempfile.tempdir = str(scratch)

    config = load()
    print(f"home:  {config.home}")
    print(f"vault: {config.vault}")
    try:
        build(config)
        report_rows(config)
        for label, lines in time_scenarios(config, args.repeats):
            print(f"\n{label}")
            print(f"{'scenario':44} {'queries':>7} {'best of N':>10} {'ms/query':>9}")
            for name, queries, seconds in lines:
                per_query = seconds / len(queries) * 1000.0
                print(f"{name:44} {len(queries):>7} {args.repeats:>10} {per_query:>9.1f}")
    finally:
        tempfile.tempdir = original_temp
        if args.keep:
            print(f"kept: {home}")
        else:
            shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
