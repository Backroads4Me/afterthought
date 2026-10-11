"""Search phrases, kept as vault files (BUILD-PLAN F85).

For each active line of a ``pages`` row and each section of a ``digests`` row, the configured
model writes up to 8 short phrases a person might search with - synonyms, the question the
text answers. The phrases are model output, so they are files in the vault, never index-only
state: ``phrases/<the page's or digest's own path>.json``, one JSON object mapping a key to
its phrases. The index stays a cache rebuilt from files with no model call in it, and its
``phrases`` column is filled from these files by ``aft.index``.

A page line's key is the SHA-256 of the line's statement text - the text the index row holds -
so an edited line is a new key with no phrases until new ones are written. A digest section's
key is the SHA-256 of its heading and its section text. ``aft run``'s phrases step writes
phrases for indexed rows that lack them, under the daily token budget and the endpoint's
cooldown the digests answer to, and prunes entries whose key no longer matches a current row:
a withdrawn, superseded or edited line, a digest that is gone. The files it changes are
committed with the run's other vault changes.

Phrases are search aids and nothing else: they are never injected, never shown as a fact,
never read by the digest, dream, review or reports, and never a source; ``aft read`` does not
show them. ``aft forget`` reaches them like every other vault file (DESIGN 8): a forgotten
session's lines and digests lose their phrases, and ``aft forget pattern`` redacts them. The
phrases are generated from indexed text, which capture already redacted, so capture's own
redaction needs no new path.
"""

from __future__ import annotations

import functools
import hashlib
import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aft import inject, model, page, vault
from aft.config import Config
from aft.model import ModelError

#: The vault directory the phrase files are kept in, one JSON object per phrased file.
PHRASES_DIR = "phrases"

PROMPTS = Path(__file__).resolve().parent / "prompts"

#: Rows per model call. A phrase is short and the answers come back as one JSON object, so
#: twenty rows fit one response with room to spare for the schema around them (BUILD-PLAN
#: F85: "several rows per call where they fit").
BATCH = 20

#: The most phrases one row gets (BUILD-PLAN F85).
MAX_PHRASES = 8

#: The response's token ceiling: twenty rows of up to eight short phrases each, plus the
#: JSON around them.
MAX_TOKENS = 2048


def line_key(statement: str) -> str:
    """A page line's key: the hash of the statement text the index row holds."""
    return hashlib.sha256(statement.encode("utf-8")).hexdigest()


def section_key(heading: str, text: str) -> str:
    """A digest section's key: the hash of its heading and its section text."""
    return hashlib.sha256(f"{heading}\n{text}".encode()).hexdigest()


def file_rel(rel: str) -> str:
    """The vault-relative path of the phrase file for the file at ``rel``."""
    return f"{PHRASES_DIR}/{rel}.json"


def file_path(config: Config, rel: str) -> Path:
    """Where the phrases of the file at ``rel`` live in the vault."""
    return config.vault / file_rel(rel)


def read_file(path: Path) -> dict[str, list[str]]:
    """The phrases one phrase file holds, keyed as the index looks them up.

    A file that is missing, unreadable or not the shape the writer leaves is no phrases at
    all rather than an error: a damaged phrase file must not take a page's rows, a search
    or a run down with it, and the next write of the file repairs what is read. An entry
    whose list holds nothing but blanks is dropped the same way.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    held: dict[str, list[str]] = {}
    for key, phrases in data.items():
        if not isinstance(key, str) or not isinstance(phrases, list):
            continue
        kept = [one for one in phrases if isinstance(one, str) and one.strip()]
        if kept:
            held[key] = kept
    return held


def write_file(path: Path, phrases: dict[str, list[str]]) -> None:
    """Write one phrase file: its keys sorted, one trailing newline, so it diffs cleanly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: phrases[key] for key in sorted(phrases)}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class Row:
    """One indexed row the phrases step can phrase: the file it came from and its key.

    ``key`` is the phrase key the row's own text decides - ``line_key`` for a page's line,
    ``section_key`` for a digest's section - so the row and its entry in the phrase file
    meet without the index having to say what a phrase file means.
    """

    path: str
    key: str
    title: str
    heading: str
    body: str


def _row_key(collection: str, heading: str, body: str) -> str:
    """The phrase key one indexed row earns, by the collection it answers from."""
    if collection == "digests":
        return section_key(heading, body)
    return line_key(body)


def _rows(config: Config) -> dict[str, list[Row]]:
    """The current pages and digest rows, grouped by the file each row came from.

    The step phrases only what a search can answer from, and a search answers from the
    index, so the rows are read from the index rather than re-derived from the files: the
    caller has just brought the index in step with the vault, and what it holds is what
    this run's own ``index.update`` will serve.
    """
    from aft import index

    grouped: dict[str, list[Row]] = {}
    with index._reading(config) as conn:
        for collection in (index.PAGES, index.DIGESTS):
            for path, title, heading, body in conn.execute(
                f"SELECT path, title, heading, body FROM {collection}"
            ):
                grouped.setdefault(path, []).append(
                    Row(
                        path=path,
                        key=_row_key(collection, heading, body),
                        title=title,
                        heading=heading,
                        body=body,
                    )
                )
    return grouped


def _ask_text(row: Row) -> str:
    """What one row shows the model: its title, its heading and the text itself."""
    parts = []
    if row.title:
        parts.append(f"Title: {row.title}")
    if row.heading:
        parts.append(f"Heading: {row.heading}")
    if row.body:
        parts.append(f"Content: {row.body}")
    return "\n".join(parts)


@functools.cache
def _system_prompt() -> str:
    """The phrases prompt, as the file in ``aft.prompts`` holds it."""
    return (PROMPTS / "phrases.md").read_text(encoding="utf-8")


@functools.cache
def _schema() -> dict:
    """The phrases answer schema, as the file in ``aft.prompts`` holds it."""
    try:
        return json.loads((PROMPTS / "phrases.schema.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"the phrases schema file cannot be read: {exc}") from exc


def _generate(config: Config, batch: list[Row]) -> dict[str, list[str]]:
    """One batch's phrases, keyed by the row's number in the batch.

    An answer that skips a row, or answers a row with nothing usable, leaves that row
    without phrases rather than inventing them: the row simply waits for the next run.
    """
    numbered = "\n\n".join(
        f"[{position}] {_ask_text(row)}" for position, row in enumerate(batch, 1)
    )
    reply = model.complete_json(
        config,
        system=_system_prompt(),
        user=numbered,
        schema=_schema(),
        max_tokens=MAX_TOKENS,
    )
    answered = reply.get("phrases") if isinstance(reply, dict) else None
    if not isinstance(answered, dict):
        return {}
    phrases_by_row: dict[str, list[str]] = {}
    for number, phrases in answered.items():
        if not isinstance(phrases, list):
            continue
        kept = [one.strip() for one in phrases if isinstance(one, str) and one.strip()]
        if kept:
            phrases_by_row[str(number)] = kept[:MAX_PHRASES]
    return phrases_by_row


def _missing(config: Config, grouped: dict[str, list[Row]]) -> list[Row]:
    """The rows that have no phrases yet, in file order and then key order.

    A row whose key the file already holds is current: those phrases were written for this
    text, whatever run wrote them. Only a key the file does not hold - a line just added, a
    section just written, a file never phrased - is work.
    """
    targets: list[Row] = []
    for rel in sorted(grouped):
        held = read_file(file_path(config, rel))
        targets.extend(row for row in grouped[rel] if row.key not in held)
    targets.sort(key=lambda row: (row.path, row.key))
    return targets


def _batches(targets: list[Row]) -> Iterator[list[Row]]:
    """The rows in model-call-sized groups of ``BATCH``."""
    for start in range(0, len(targets), BATCH):
        yield targets[start : start + BATCH]


def _store(
    config: Config, batch: list[Row], answered: dict[str, list[str]], changed: list[str]
) -> int:
    """Write one batch's answers into the rows' phrase files; return the rows phrased.

    ``changed`` collects the phrase-file paths the step has touched, which the caller
    commits with the run's other vault changes.
    """
    by_file: dict[str, dict[str, list[str]]] = {}
    written = 0
    for position, row in enumerate(batch, 1):
        phrases = answered.get(str(position))
        if not phrases:
            continue
        by_file.setdefault(row.path, {})[row.key] = phrases
        written += 1
    for rel, additions in by_file.items():
        path = file_path(config, rel)
        held = read_file(path)
        held.update(additions)
        write_file(path, held)
        if file_rel(rel) not in changed:
            changed.append(file_rel(rel))
    return written


def _prune(config: Config, grouped: dict[str, list[Row]], changed: list[str]) -> int:
    """Remove the entries whose key no longer matches a current row; return how many went.

    A file's kept keys are the keys of the rows the index now holds for it, which is what
    the caller read. A phrase file left holding nothing - its digest deleted, its page
    rewritten line by line - is deleted rather than kept as an empty object, so the
    ``phrases/`` tree names exactly the files that have phrases.
    """
    current = {rel: {row.key for row in rows} for rel, rows in grouped.items()}
    root = config.vault / PHRASES_DIR
    if not root.is_dir():
        return 0
    pruned = 0
    for path in sorted(root.rglob("*.json")):
        rel = path.relative_to(config.vault).as_posix()
        file_rel_path = _file_of(rel)
        if file_rel_path is None:
            continue
        held = read_file(path)
        keys = current.get(file_rel_path, set())
        kept = {key: phrases for key, phrases in held.items() if key in keys}
        if len(kept) == len(held):
            continue
        pruned += len(held) - len(kept)
        if kept:
            write_file(path, kept)
        else:
            path.unlink()
        if rel not in changed:
            changed.append(rel)
    return pruned


def _file_of(phrase_rel: str) -> str | None:
    """The vault file a phrase file phrases, or None when this is not one.

    ``phrases/<rel>.json`` phrases the file at ``<rel>`` (BUILD-PLAN F85), so the mapping
    back is the name with its directory and suffix taken off.
    """
    prefix = f"{PHRASES_DIR}/"
    if not phrase_rel.startswith(prefix) or not phrase_rel.endswith(".json"):
        return None
    return phrase_rel[len(prefix) : -len(".json")]


def prune_paths(config: Config, rels: Iterable[str]) -> tuple[int, list[str]]:
    """Prune the phrase files of these vault files against the files themselves.

    ``aft forget session`` calls this after it has rewritten or deleted the pages and
    digests of the session it forgets: the kept keys are read from the files as they now
    stand - a page's active lines, nothing at all for a file that is gone - because the
    index is brought in step only after the forget's commit. A page whose parse finds
    problems is left unpruned: the forget has already left the page standing, and a phrase
    file holding one line's phrases is harmless next to a rewrite that failed.

    Returns the entries removed and the phrase files changed or deleted, which the caller
    commits with its own changes.
    """
    pruned = 0
    changed: list[str] = []
    for rel in sorted(dict.fromkeys(rels)):
        path = file_path(config, rel)
        held = read_file(path)
        if not held:
            continue  # nothing to prune: no file, or one that already holds nothing
        keys = _file_keys(config, rel)
        if keys is None:
            continue
        kept = {key: phrases for key, phrases in held.items() if key in keys}
        if len(kept) == len(held):
            continue
        pruned += len(held) - len(kept)
        if kept:
            write_file(path, kept)
        else:
            path.unlink()
        changed.append(file_rel(rel))
    return pruned, changed


def _file_keys(config: Config, rel: str) -> set[str] | None:
    """The phrase keys the file at ``rel`` still earns, read from the file itself.

    A page's kept keys are its active lines' statements - the lines ``pages`` rows are
    built from - so a line a forget withdrew or rewrote earns no phrases any more. A file
    that cannot be read keeps everything, since nothing says its lines changed.
    """
    try:
        text = (config.vault / rel).read_text(encoding="utf-8")
    except OSError:
        return set()
    parsed = page.parse(text, config.vault / rel)
    if parsed.errors:
        return None
    return {line_key(fact.statement) for fact in parsed.facts if fact.state in inject.INJECTED}


@dataclass(frozen=True)
class Report:
    """What one phrases step did.

    ``written`` counts the rows the model gave phrases to and ``pruned`` the entries whose
    key no longer matched a current row. ``stopped`` says why the work that is left waits
    for the next run; a spent budget is not named there, because the digests' own budget
    line already says the day's tokens are gone.
    """

    written: int = 0
    pruned: int = 0
    stopped: str = ""


def update(config: Config, now: datetime | None = None) -> Report:
    """Write phrases for the rows that lack them and prune the entries whose rows left.

    ``aft run`` calls this after its digests (BUILD-PLAN F85): the model's own words cost
    tokens, so the step answers to the same daily budget the digests do and to the same
    cooldown - an endpoint an earlier failure left cooling down gets no calls from this
    step either. The phrase files it changed are committed before it returns, under the
    vault lock the run's other writers take, so the vault never holds uncommitted model
    output between runs.

    Pruning runs whatever the endpoint is doing: it is file work, and entries naming text
    the vault no longer holds have no business waiting on the model.
    """
    vault.require(config)
    from aft import index

    moment = now or datetime.now(UTC)
    index.update(config)
    grouped = _rows(config)
    written = pruned = 0
    changed: list[str] = []
    stopped = ""
    with vault.locked(config):
        pruned = _prune(config, grouped, changed)
        cooling, reason = model.is_in_cooldown(config)
        if cooling:
            until = model.cooldown_until(config)
            stopped = f"the model endpoint is in cooldown until {until:%H:%M}Z: {reason}"
        else:
            budget = config.settings.work.daily_token_budget
            for batch in _batches(_missing(config, grouped)):
                if model.tokens_used_today(config, moment) >= budget:
                    break  # the day's tokens are spent; what is left waits for the next run
                try:
                    answered = _generate(config, batch)
                except ModelError as exc:
                    stopped = str(exc)
                    break
                written += _store(config, batch, answered, changed)
        if changed:
            committable = _committable(config.vault, changed)
            if committable:
                vault.commit(config.vault, committable, "aft: phrases")
    return Report(written=written, pruned=pruned, stopped=stopped)


def _committable(vault_root: Path, rels: list[str]) -> list[str]:
    """The changed phrase paths git can stage: present on disk, or already tracked.

    A file the step deleted can still need staging (its removal is the change), but only
    if an earlier commit recorded it; a path that is neither on disk nor tracked has
    nothing for git to act on. If git cannot answer, the step still commits what exists.
    """
    from aft.gitutil import GitError, git

    try:
        tracked = set(git(["ls-files", "--", *rels], cwd=vault_root).stdout.split())
    except GitError:
        tracked = set()
    return [rel for rel in rels if (vault_root / rel).exists() or rel in tracked]
