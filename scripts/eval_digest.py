#!/usr/bin/env python3
"""F54: measure what the digest gets right, over a fixed test set (BUILD-PLAN F54).

The harness runs today's digest pipeline, unmodified, over each session of a fixed test
set: the shipped prompt, the shipped schema, the chunking, the answer checks and the quote
verification all come from ``src/aft/digest.py``. The one seam is the answer itself, which
comes from a backend:

- ``local``: the configured endpoint, as ``aft`` calls it (endpoint, model and extra_body
  given by flags). This is the pipeline exactly as production runs it.
- ``gemini``: the same prompt, shelled out to ``agy --print``; the JSON object in the
  output is checked against the shipped schema like any answer.
- ``claude``: the same prompt, shelled out to ``claude -p --model claude-opus-5-5``.

The gemini and claude backends are run by the lead, never by this builder; tests exercise
them only through a fake command. The harness issues at most one model call at a time:
there is no thread, no pool, and no second call before the previous one has returned.

Sessions come from two places. A real session is one the lead exported to the eval data
directory: its ``evidence.jsonl`` holds the filtered entries the digest reads today, and
the harness digests exactly those rows. It never re-runs the filter over a real
transcript (a re-run with another config's redaction would not be what today's digest
reads) and it never sends ``transcript.jsonl`` to a model: a model only ever receives what
afterthought's own filter and digest code produce. A synthetic session is a made-up
transcript in the repository under ``eval/digest/synthetic/``, each with its own expected
answer; the harness reads it with the shipped reader and filter, which is the code that
would produce its entries in production, and no memory block is echoed into one.

Nothing of the pipeline is changed and nothing is written to any vault: the harness needs
only the afterthought home named by ``AFT_HOME``, where it keeps the evidence archive that
quote verification reads and the usage ledger it reads token counts from. It refuses
outright to run against the real home or the real vault.

Layout under the data directory (``--data``, default from ``AFT_EVAL_DATA``):

    answers/<backend>/<session>/          the baseline run (variant ``base``)
    answers/<backend>/<variant>/<session>/  a run of a later pipeline variant
    answers/<backend>/[<variant>/]run.json  what was run, one merged record per backend
    compare/<variant>/<session>.json      where the backends' facts agree and differ
    key/<session>.json                    the answer key the lead settles
    key-turns.json                        the per-turn key the lead settles (F55)
    split.json                            the frozen dev/held-out split of the per-turn key (M16)
    judge-cache.json                      the judge's verdicts, keyed by pair (M16)
    judge-check.json                      the planted pairs the judge is checked against (M16)
    reports/<stamp>-<variant>-<backend>.json  what a run scored against the key

Each call record holds the answer as the model returned it, before the script's checks,
with the token usage the endpoint reported. Each session summary holds what the pipeline
kept, what it refused, its parts, its summaries and handoff, the tokens and the time.
``compare`` matches facts across backends by quote overlap for the lead to settle into the
key; ``score`` and ``score-turns`` pair run facts with key facts one to one and judge each
pair (BUILD-PLAN M16): the names and numbers of the run statement are checked against the
key text without a model call, and what survives is judged by the model on the configured
endpoint, which sees the key statement, the key quote and the run statement and answers
whether the run statement says what the key text says. The judge prompt and its schema sit
beside this script as ``judge.md`` and ``judge.schema.json``; every verdict is cached under
the data directory. A synthetic session with no key file is scored against its own
``expected.json``.

The variant ``base`` is today's pipeline; a later variant scores against the same key, so
the two runs' numbers are comparable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from aft import capture, cli, evidence, gitutil, model, queue, readers, vault, work
from aft import config as configmod
from aft import digest as digestmod
from aft import filter as filtermod
from aft.config import Config, ConfigError
from aft.dream import rules, schedule
from aft.events import SESSION_END, SessionEvent
from aft.registry import Registry, path_key
from aft.schema import SchemaError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYNTHETIC = REPO_ROOT / "eval" / "digest" / "synthetic"
DEFAULT_CURRENT_SCENARIOS = REPO_ROOT / "eval" / "current"

BACKENDS = ("local", "gemini", "claude")
BASE_VARIANT = "base"
EXPORT = "export"
SYNTHETIC = "synthetic"
KEY_BACKEND = "key"

# The local endpoint, its model, and what tells it not to think. The endpoint comes from
# AFT_EVAL_ENDPOINT when set, so a run names its own server without a flag.
LOCAL_ENDPOINT = os.environ.get("AFT_EVAL_ENDPOINT", "http://localhost:8000/v1")
LOCAL_MODEL = "GLM-5.3-Flash"
LOCAL_EXTRA_BODY = '{"chat_template_kwargs": {"enable_thinking": false}}'

# The CLI each shell backend answers through; the lead runs them for real, tests run a
# fake command in their place.
GEMINI_COMMAND = ("agy", "--print")
CLAUDE_COMMAND = ("claude", "-p", "--model", "claude-opus-5-5")

# A CLI answered in its own time or not at all; nothing here waits forever.
SHELL_TIMEOUT_SECONDS = 600.0

# What an endpoint's or CLI's own text contributes to a message shown to a person.
REPORT_CHARS = 500

# Two quotes name the same fact when their words agree to this much, measured over the
# normalized tokens of both (see ``quote_overlap``). Quotes are copied from the same
# entries, so models that read the same words land near 1.0; copies of different sentences
# land far below.
QUOTE_MATCH_RATIO = 0.6

# Two statements name the same fact when they share this share of their content words
# (see ``statement_overlap``). Models quote different lines for one fact often enough that
# quotes alone split a single fact into two apparent disagreements.
STATEMENT_MATCH_RATIO = 0.4

# Words too common to say which fact a statement is about.
_COMMON = frozenset(
    "the a an and or but of to in on at for from with by is are was were be been it its this"
    " that these those as not no so than then when which who what user agent session"
    " uses use used has have had can will would should must may into over".split()
)


# ---------------------------------------------------------------------------
# The judge of one pair (BUILD-PLAN M16)

JUDGE_PROMPT_PATH = REPO_ROOT / "scripts" / "judge.md"
JUDGE_SCHEMA_PATH = REPO_ROOT / "scripts" / "judge.schema.json"
JUDGE_CACHE = "judge-cache.json"
JUDGE_CHECK = "judge-check.json"
SPLIT_FILE = "split.json"
VERDICTS = frozenset({"same", "narrower", "broader", "different"})
# The verdicts that count a key item found: the run statement says what the key says, part
# of it, or all of it and more, and contradicts nothing.
FOUND = frozenset({"same", "narrower", "broader"})
SPLIT_NAMES = ("dev", "held-out")
JUDGE_MAX_TOKENS = 200
# The judge is asked at temperature 0 whatever the config says, so a stored run scores
# the same twice.
JUDGE_TEMPERATURE = 0.0
# The judge-check bars, as the share of each planted set that must land right: 38 of 40
# planted wrong judged ``different``, 36 of 40 planted same and 36 of 40 planted broader
# judged found (BUILD-PLAN M16).
JUDGE_CHECK_WRONG_SHARE = 0.95
JUDGE_CHECK_SAME_SHARE = 0.90
# The rule split.json is built by, stated for the reader of the file.
SPLIT_RULE = "sha256 of the turn id, first 8 bytes read big-endian: even dev, odd held-out"

# How a backend's answers are shaped: the local endpoint enforces the schema through
# ``response_format``; a CLI only reads the prompt, so the schema is written into it (a
# standing decision a report states).
BACKEND_SHAPE = {"local": "endpoint-schema", "gemini": "prompt-schema", "claude": "prompt-schema"}

# The digest pipeline's own prompts, hashed into every report so a report says which
# wording produced its numbers.
DIGEST_PROMPT_FILES = (
    digestmod.PROMPT_FILE,
    digestmod.TURNS_PROMPT_FILE,
    digestmod.OBSERVED_PROMPT_FILE,
    digestmod.FINAL_PROMPT_FILE,
    digestmod.SUMMARY_PROMPT_FILE,
    digestmod.VERIFY_PROMPT_FILE,
)

# The model call the judge makes, captured at import: a run of a CLI backend replaces
# ``model.complete_json`` to shell its answers out, and the judge is not the backend's
# to answer, it is the configured endpoint's.
_MODEL_COMPLETE = model.complete_json


class EvalError(Exception):
    """A test set, run, key or report the harness refuses rather than guesses about."""


# ---------------------------------------------------------------------------
# The home the harness is allowed to run against


def load_config(environ: dict[str, str] | None = None) -> Config:
    """The config of the sandbox home ``AFT_HOME`` names, refused anywhere else.

    The harness never guesses a home: ``aft.config.load`` would fall back to
    ``~/.afterthought``, which is the user's real one. A home under ``~/afterthought`` or
    ``~/.afterthought``, and a vault under either, are refused the same way.
    """
    env = os.environ if environ is None else environ
    raw = env.get(configmod.HOME_ENV)
    if not raw:
        raise EvalError(
            f"{configmod.HOME_ENV} is not set; the harness runs against the sandbox home it names"
        )
    _refuse_real(str(raw), configmod.HOME_ENV)
    cfg = configmod.load(environ)
    _refuse_real(str(cfg.vault), "vault")
    return cfg


def _refuse_real(path: str, what: str) -> None:
    """Refuse a path that is, or sits below, the user's real home or real vault."""
    home = Path(path).expanduser().resolve()
    for banned in (Path("~/.afterthought"), Path("~/afterthought")):
        real = banned.expanduser().resolve()
        if home == real or real in home.parents:
            raise EvalError(f"refusing {what} {path}: the harness runs only against a sandbox home")


def with_model(cfg: Config, endpoint: str, name: str, extra_body: str) -> Config:
    """The config with the model settings the flags give, for the local backend."""
    try:
        extra = json.loads(extra_body)
    except ValueError as exc:
        raise EvalError(f"--extra-body is not JSON: {exc}") from exc
    if not isinstance(extra, dict):
        raise EvalError("--extra-body must be a JSON object")
    settings = replace(
        cfg.settings,
        model=replace(cfg.settings.model, endpoint=endpoint, name=name, extra_body=extra),
        # The scenario runs measure digests against scripted answers; the phrases step's
        # own calls would consume the script (BUILD-PLAN F85).
        work=replace(cfg.settings.work, phrases=False),
    )
    return replace(cfg, settings=settings)


# ---------------------------------------------------------------------------
# The sessions of the test set


@dataclass(frozen=True)
class SessionInput:
    """One session of the test set, with the entries its digest call would read."""

    session_id: str
    agent: str
    source: str  # EXPORT: the lead's export of a real session; SYNTHETIC: a made-up one
    case: str | None  # the rare case a synthetic session exercises
    cwd: str | None
    project_id: str | None
    entries: tuple[readers.Entry, ...]
    # The unfiltered read, for the digest's observed check alone (BUILD-PLAN F78): it is
    # never archived or sent to a model. None when the raw transcript cannot be read.
    raw: tuple[readers.Entry, ...] | None = None


def real_sessions(data: Path) -> list[SessionInput]:
    """The exported sessions of the data directory, in the order its index lists them."""
    index_path = data / "index.json"
    records = _read_json(index_path)
    if not isinstance(records, list):
        raise EvalError(f"{index_path}: expected a list of session records")
    sessions = []
    for number, record in enumerate(records, start=1):
        where = f"{index_path}: record {number}"
        if not isinstance(record, dict):
            raise EvalError(f"{where}: expected an object")
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not digestmod.SESSION_ID.match(session_id):
            raise EvalError(f"{where}: session_id {session_id!r} is not a session id")
        agent = record.get("agent")
        if not isinstance(agent, str) or not agent.strip():
            raise EvalError(f"{where}: agent must be a non-empty string")
        path = data / "sessions" / session_id / "evidence.jsonl"
        entries = _export_entries(path)
        raw = _raw_read(agent, data / "sessions" / session_id / "transcript.jsonl")
        sessions.append(
            SessionInput(
                session_id=session_id,
                agent=agent,
                source=EXPORT,
                case=None,
                cwd=_optional(record.get("cwd"), where),
                project_id=_optional(record.get("project_id"), where),
                entries=entries,
                raw=raw,
            )
        )
    return sessions


def _raw_read(agent: str, path: Path) -> tuple[readers.Entry, ...] | None:
    """The session's unfiltered entries, as digest_job reads them, or None when the raw
    transcript is missing or the reader refuses it (BUILD-PLAN F78)."""
    if not path.is_file():
        return None
    try:
        return tuple(readers.read(agent, path))
    except Exception:
        return None


def _export_entries(path: Path) -> tuple[readers.Entry, ...]:
    """The filtered entries of one exported session, in conversation order.

    Each row is one entry as the archive holds it. The rows are sorted by their own
    timestamp, files first among equals, so a row written out of time order cannot cut a
    conversation into pieces; for an export in order the sort changes nothing.
    """
    if not path.is_file():
        raise EvalError(f"{path}: the session's filtered entries are missing")
    entries: list[tuple[readers.Entry, int]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = _read_json_line(path, line, number)
        entry_id = row.get("entry_id")
        role = row.get("role")
        text = row.get("text")
        is_error = row.get("is_error")
        when = readers.parse_time(row.get("time"))
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise EvalError(f"{path}: line {number} has no entry id")
        if role not in (readers.USER, readers.ASSISTANT, readers.TOOL):
            raise EvalError(f"{path}: line {number} has unknown role {role!r}")
        if not isinstance(text, str):
            raise EvalError(f"{path}: line {number} has no text")
        if not isinstance(is_error, bool):
            raise EvalError(f"{path}: line {number} has a non-boolean is_error")
        if when is None:
            raise EvalError(f"{path}: line {number} has no readable time")
        entries.append((readers.Entry(entry_id, when, role, text, is_error), number))
    entries.sort(key=lambda pair: (pair[0].time, pair[1]))
    return tuple(entry for entry, _ in entries)


def synthetic_sessions(directory: Path, cfg: Config) -> list[SessionInput]:
    """The made-up sessions of the repository, read and filtered the way capture reads them.

    The filter and the reader are the shipped ones, so a model sees only what afterthought's
    own code would hand it. A synthetic session carries no memory block, so the echo step
    has nothing to remove.
    """
    if not directory.is_dir():
        raise EvalError(f"{directory}: no synthetic sessions directory")
    sessions = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir():
            continue
        expected = _read_json(path / "expected.json")
        if not isinstance(expected, dict):
            raise EvalError(f"{path / 'expected.json'}: expected an object")
        session_id = expected.get("session_id")
        if not isinstance(session_id, str) or not digestmod.SESSION_ID.match(session_id):
            raise EvalError(f"{path}: session_id {session_id!r} is not a session id")
        agent = expected.get("agent")
        if agent not in (readers.CLAUDE, readers.PI):
            raise EvalError(f"{path}: agent must be {readers.CLAUDE!r} or {readers.PI!r}")
        try:
            read = tuple(readers.read(agent, path / "transcript.jsonl"))
            kept, _ = filtermod.filter_entries(read, cfg, False)
        except readers.ReaderError as exc:
            raise EvalError(f"{path}: {exc}") from exc
        sessions.append(
            SessionInput(
                session_id=session_id,
                agent=agent,
                source=SYNTHETIC,
                case=_optional(expected.get("case"), str(path)),
                cwd=None,
                project_id=None,
                entries=tuple(kept),
                raw=read,
            )
        )
    return sessions


def _optional(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvalError(f"{where}: expected a string or null, got {value!r}")
    return value


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise EvalError(f"{path} cannot be read: {exc}") from exc
    except ValueError as exc:
        raise EvalError(f"{path} is not JSON: {exc}") from exc


def _read_json_line(path: Path, line: str, number: int) -> dict:
    try:
        row = json.loads(line)
    except ValueError as exc:
        raise EvalError(f"{path}: line {number} is not JSON: {exc}") from exc
    if not isinstance(row, dict):
        raise EvalError(f"{path}: line {number} is not an object")
    return row


def select_sessions(sessions: list[SessionInput], wanted: list[str]) -> list[SessionInput]:
    """The sessions the run was asked for, or all of them."""
    if not wanted:
        return sessions
    known = {session.session_id: session for session in sessions}
    missing = [name for name in wanted if name not in known]
    if missing:
        available = ", ".join(session.session_id for session in sessions)
        raise EvalError(f"no session(s) {', '.join(missing)}; the set holds {available}")
    return [known[name] for name in wanted]


# ---------------------------------------------------------------------------
# Backends


@dataclass
class Backend:
    """One place an answer comes from.

    A backend without a command is the configured endpoint, called the way production
    calls it. A backend with one is a CLI the prompt is shelled out to. ``raw_text``
    holds the last CLI's own output, kept by the call record for auditing; the endpoint
    backend answers in JSON already and leaves it None.
    """

    name: str
    command: tuple[str, ...] | None = None
    raw_text: str | None = None

    def complete_json(
        self, cfg: Config, system: str, user: str, schema: dict, max_tokens: int
    ) -> dict:
        """One answer that satisfies ``schema``, asked for the way this backend asks."""
        if self.command is None:
            return model.complete_json(cfg, system, user, schema, max_tokens)
        return self._shell(system + "\n\n" + user + _schema_note(schema), schema)

    def _shell(self, prompt: str, schema: dict) -> dict:
        assert self.command is not None
        program = self.command[0]
        try:
            ran = subprocess.run(
                [*self.command, prompt],
                capture_output=True,
                text=True,
                timeout=SHELL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise model.ModelError(
                f"{program} did not answer within {SHELL_TIMEOUT_SECONDS:.0f}s"
            ) from exc
        except OSError as exc:
            raise model.ModelError(f"cannot run {program}: {exc}") from exc
        self.raw_text = ran.stdout
        if ran.returncode != 0:
            raise model.ModelError(
                f"{program} exited {ran.returncode}: {ran.stderr.strip()[:REPORT_CHARS]}"
            )
        found = json_object(ran.stdout)
        if found is None:
            # A CLI that prints prose around its answer is this part's problem, the same
            # way a schema refusal is: the pipeline asks the part once more.
            raise model.ModelError(
                f"{digestmod.ANSWER_REFUSED}: {program} printed no JSON object"
            ) from None
        try:
            return model._checked(found, schema)
        except SchemaError as exc:
            raise model.ModelError(f"{digestmod.ANSWER_REFUSED}: {exc}") from None


def _schema_note(schema: dict) -> str:
    """What a shell backend is told about the answer's form.

    The local endpoint enforces the schema through ``response_format``; a CLI only reads
    the prompt, so the same schema is written into it, with the instruction to answer in
    one JSON object and nothing else.
    """
    return (
        "\n\nAnswer with exactly one JSON object that satisfies this JSON schema, and"
        " nothing else: no prose, no code fence.\n\n" + json.dumps(schema)
    )


def build_backend(name: str, command: tuple[str, ...] | None = None) -> Backend:
    """The backend a run asks, or an error naming the backends there are."""
    if name == "local":
        return Backend(name)
    if name == "gemini":
        return Backend(name, command=GEMINI_COMMAND if command is None else command)
    if name == "claude":
        return Backend(name, command=CLAUDE_COMMAND if command is None else command)
    raise EvalError(f"unknown backend {name!r}; the backends are {', '.join(BACKENDS)}")


def json_object(text: str) -> str | None:
    """The first balanced JSON object in ``text``, or None when it holds none.

    A CLI wraps its answer in prose or code fences often enough that the answer is looked
    for rather than expected. Strings are skipped whole, so a brace inside one never
    opens or closes anything.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for position in range(start, len(text)):
            character = text[position]
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
            elif character == '"':
                in_string = True
            elif character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    return text[start : position + 1]
        start = text.find("{", start + 1)
    return None


# ---------------------------------------------------------------------------
# Recording every raw answer


class CallRecorder:
    """Stands in for the model client while one session is digested.

    The pipeline asks ``model.complete_json`` for every answer; the recorder takes its
    place for the duration of one session and hands each call to the backend it wraps,
    saving the raw answer - the answer as the model returned it, before the script's
    checks - as one call record beside the token usage the endpoint reported.
    """

    def __init__(
        self,
        answer: Callable[[Config, str, str, dict, int], dict],
        cfg: Config,
        session: SessionInput,
        backend: Backend,
        variant: str,
        out: Path,
    ) -> None:
        self.answer = answer
        self.cfg = cfg
        self.session = session
        self.backend = backend
        self.variant = variant
        self.out = out
        self.count = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def __call__(
        self,
        cfg: Config,
        system: str,
        user: str,
        schema: dict,
        max_tokens: int,
        raise_cut: bool = False,
    ) -> dict:
        self.count += 1
        record: dict[str, Any] = {
            "session": self.session.session_id,
            "backend": self.backend.name,
            "variant": self.variant,
            "call": self.count,
            "at": datetime.now(UTC).isoformat(),
            "system_chars": len(system),
            "user_chars": len(user),
            "entries": _labels(user),
            "model": None if self.backend.command else (self.cfg.settings.model.name or None),
            "command": list(self.backend.command) if self.backend.command else None,
        }
        before = _ledger(self.cfg)
        started = time.monotonic()
        try:
            # The local backend asks the endpoint client, which splits a length cut for a
            # digest chunk (BUILD-PLAN T54); a shell backend has no such answer to give.
            if self.backend.command is None:
                answer = self.answer(cfg, system, user, schema, max_tokens, raise_cut=raise_cut)
            else:
                answer = self.answer(cfg, system, user, schema, max_tokens)
        except model.ModelError as exc:
            record.update(
                {
                    "answer": None,
                    "error": str(exc),
                    "seconds": round(time.monotonic() - started, 3),
                    "usage": _usage_delta(self.cfg, before),
                    "raw_text": self.backend.raw_text,
                }
            )
            _write_json(self.out / f"call-{self.count:03d}.json", record)
            raise
        record.update(
            {
                "answer": answer,
                "error": None,
                "seconds": round(time.monotonic() - started, 3),
                "usage": _usage_delta(self.cfg, before),
                "raw_text": self.backend.raw_text,
            }
        )
        _write_json(self.out / f"call-{self.count:03d}.json", record)
        usage = record["usage"]
        if usage is not None:
            self.usage["prompt_tokens"] += usage["prompt_tokens"]
            self.usage["completion_tokens"] += usage["completion_tokens"]
        return answer


def _labels(user: str) -> dict[str, Any] | None:
    """The entry labels a chunk was shown under, for reading a call record back."""
    found = _LABEL.findall(user)
    if not found:
        return None
    return {"first": f"#{found[0]}", "last": f"#{found[-1]}", "count": len(found)}


_LABEL = re.compile(r"(?m)^\[#(\d+)\]")


def _ledger(cfg: Config) -> list[tuple[int, int]]:
    """The usable token counts of the home's usage ledger, in the order written."""
    try:
        text = model.usage_path(cfg).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records = []
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        prompt = model._token_count(row, "prompt_tokens")
        completion = model._token_count(row, "completion_tokens")
        if prompt is None or completion is None:
            continue
        records.append((prompt, completion))
    return records


def _usage_delta(cfg: Config, before: list[tuple[int, int]]) -> dict[str, int] | None:
    """What the calls since ``before`` were billed, or None when nothing was.

    The ledger is appended to by the endpoint client alone, one call at a time under the
    vault lock, so the records after ``before`` are this call's. A shell backend answers
    outside the client and is billed nowhere, which reads as None.
    """
    after = _ledger(cfg)
    fresh = after[len(before) :] if len(after) > len(before) else []
    if not fresh:
        return None
    return {
        "prompt_tokens": sum(prompt for prompt, _ in fresh),
        "completion_tokens": sum(completion for _, completion in fresh),
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Running the pipeline over the set


def run_sessions(
    cfg: Config,
    backend: Backend,
    sessions: list[SessionInput],
    data: Path,
    variant: str,
) -> None:
    """Digest every session in turn, one model call at a time, and write what each gave."""
    _write_manifest(cfg, backend, data, variant, sessions)
    for session in sessions:
        summary = run_session(cfg, backend, session, data, variant)
        counts = (
            f"parts={summary['parts']} calls={summary['calls']} "
            f"facts={len(summary['facts'])} dropped={len(summary['dropped'])}"
        )
        if summary["nothing_to_digest"]:
            counts = "nothing to digest"
        elif summary["fully_refused"]:
            counts += " refused"
        tokens = summary["usage"]["prompt_tokens"] + summary["usage"]["completion_tokens"]
        print(
            f"{session.session_id} {session.agent} {session.source} {counts} "
            f"tokens={tokens} {summary['seconds']}s",
            flush=True,
        )


def run_session(
    cfg: Config, backend: Backend, session: SessionInput, data: Path, variant: str
) -> dict:
    """One session through the digest pipeline, its answers and its verdicts written."""
    out = run_dir(data, backend.name, variant, session.session_id)
    # A rerun replaces the session's previous answers wholesale.
    try:
        shutil.rmtree(out)
    except OSError:
        pass
    out.mkdir(parents=True, exist_ok=True)
    summary = {
        "session": session.session_id,
        "agent": session.agent,
        "source": session.source,
        "case": session.case,
        "backend": backend.name,
        "variant": variant,
        "model": None if backend.command else (cfg.settings.model.name or None),
        "endpoint": None if backend.command else cfg.settings.model.endpoint,
        "command": list(backend.command) if backend.command else None,
        "at": datetime.now(UTC).isoformat(),
        "entries": len(session.entries),
        "parts": 0,
        "calls": 0,
        "facts": [],
        "dropped": [],
        "refused_parts": [],
        "summaries": [],
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "usage": {"prompt_tokens": 0, "completion_tokens": 0},
        "seconds": 0.0,
        "nothing_to_digest": False,
        "fully_refused": False,
        "refused_reason": None,
    }
    if not session.entries:
        summary["nothing_to_digest"] = True
        _write_json(out / "summary.json", summary)
        return summary

    # Quote verification reads the archive, so the session's entries are archived first,
    # the way the digest archives a chunk before the model sees it.
    evidence.store(cfg, session.agent, session.session_id, session.project_id, session.entries)
    chunks = digestmod.parts(session.entries)
    recorder = CallRecorder(_answer_source(backend), cfg, session, backend, variant, out)
    summary["parts"] = len(chunks)
    started = time.monotonic()
    original = model.complete_json
    model.complete_json = recorder
    digested: digestmod.Digested | None = None
    try:
        raws = digestmod._raw_spans(session.raw, chunks) if session.raw else None
        digested = digestmod._digest_parts(cfg, session.session_id, chunks, rules.PRIMARY, raws)
    except digestmod.AnswerRefused as exc:
        # Every part of the session was refused; the calls were recorded, and the
        # summary holds the refusals the pipeline reported.
        summary["fully_refused"] = True
        summary["refused_reason"] = str(exc)
    finally:
        model.complete_json = original
    summary["seconds"] = round(time.monotonic() - started, 3)
    if digested is not None:
        summary["facts"] = [asdict(fact) for fact in digested.facts]
        summary["dropped"] = [asdict(dropped) for dropped in digested.dropped]
        summary["refused_parts"] = digested.refused_parts
        summary["summaries"] = digested.summaries
        summary["handoff"] = digested.handoff
    summary["calls"] = recorder.count
    summary["usage"] = dict(recorder.usage)
    _write_json(out / "summary.json", summary)
    return summary


def _answer_source(backend: Backend) -> Callable[[Config, str, str, dict, int], dict]:
    """The callable an answer is asked from, captured before the pipeline is patched.

    The local backend asks the endpoint client itself, held as the function object so the
    recorder's patch of ``model.complete_json`` cannot send it through the recorder again.
    """
    if backend.command is None:
        return model.complete_json
    return backend.complete_json


def _write_manifest(
    cfg: Config, backend: Backend, data: Path, variant: str, sessions: list[SessionInput]
) -> None:
    """One record per backend of what has been run, merged over earlier runs of it."""
    path = backend_root(data, backend.name, variant) / "run.json"
    manifest: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                manifest = loaded
        except (OSError, ValueError):
            manifest = {}
    manifest.update(
        {
            "backend": backend.name,
            "variant": variant,
            "model": None if backend.command else (cfg.settings.model.name or None),
            "endpoint": None if backend.command else cfg.settings.model.endpoint,
            "command": list(backend.command) if backend.command else None,
            "at": datetime.now(UTC).isoformat(),
        }
    )
    known = set(manifest.get("sessions") or [])
    known.update(session.session_id for session in sessions)
    manifest["sessions"] = sorted(known)
    _write_json(path, manifest)


def variant_dir(base: Path, variant: str) -> Path:
    """Where a variant's runs sit: the baseline directly under, a later one in its own
    directory, so the baseline keeps the layout the answers were first written to."""
    if not digestmod.SESSION_ID.match(variant):
        raise EvalError(f"variant {variant!r} is not a name to file a run under")
    return base / variant if variant != BASE_VARIANT else base


def backend_root(data: Path, backend: str, variant: str) -> Path:
    return variant_dir(data / "answers" / backend, variant)


def run_dir(data: Path, backend: str, variant: str, session_id: str) -> Path:
    return backend_root(data, backend, variant) / session_id


# ---------------------------------------------------------------------------
# Matching facts by quote overlap


@dataclass
class Group:
    """Facts of several lists that name the same thing, matched by quote overlap."""

    overlap: float | None
    facts: dict[str, dict] = field(default_factory=dict)


def quote_tokens(quote: str) -> list[str]:
    """The words of a quote, folded the way a comparison folds them."""
    return _TOKENS.findall(quote.casefold())


_TOKENS = re.compile(r"[a-z0-9_]+")


def quote_overlap(left: str, right: str) -> float:
    """How much two quotes' words agree, 0.0 to 1.0.

    Both sides are copied from the same entries, so the words that matter are the words
    themselves: punctuation and case are set aside, and the order the words stood in is
    what two copies of one sentence keep.
    """
    if not left.strip() or not right.strip():
        return 0.0
    return SequenceMatcher(None, quote_tokens(left), quote_tokens(right)).ratio()


def statement_overlap(left: str, right: str) -> float:
    """How many content words two statements share, 0.0 to 1.0 (Jaccard over words of four
    letters or more that are not in ``_COMMON``)."""
    words = [
        {w for w in quote_tokens(text) if len(w) >= 4 and w not in _COMMON}
        for text in (left, right)
    ]
    if not words[0] or not words[1]:
        return 0.0
    return len(words[0] & words[1]) / len(words[0] | words[1])


def fact_overlap(left: dict, right: dict) -> float | None:
    """How strongly two facts name the same thing, or None when they do not: their quotes
    overlap to ``QUOTE_MATCH_RATIO`` or their statements to ``STATEMENT_MATCH_RATIO``. The
    larger of the two measures, each scaled to its threshold, orders the pairs."""
    quote = quote_overlap(left["quote"], right["quote"])
    statement = statement_overlap(left.get("statement", ""), right.get("statement", ""))
    if quote < QUOTE_MATCH_RATIO and statement < STATEMENT_MATCH_RATIO:
        return None
    return max(quote / QUOTE_MATCH_RATIO, statement / STATEMENT_MATCH_RATIO)


def match_facts(by_backend: dict[str, list[dict]]) -> list[Group]:
    """Group facts that name the same thing across the given lists.

    Two facts are paired when ``fact_overlap`` names them the same thing. The
    strongest pairs are taken first, and a fact joins at most one group, so a fact is
    never counted as agreeing with two others at once. A fact no pair claimed stands as a
    group of its own, which is how a difference reads: the lists do not hold the same
    fact.
    """
    names = list(by_backend)
    candidates: list[tuple[float, int, int, int, int]] = []
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            for left_index, left_fact in enumerate(by_backend[names[left]]):
                for right_index, right_fact in enumerate(by_backend[names[right]]):
                    ratio = fact_overlap(left_fact, right_fact)
                    if ratio is not None:
                        candidates.append((ratio, left, left_index, right, right_index))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2], item[3], item[4]))
    groups: list[Group] = []
    placed: dict[tuple[str, int], Group] = {}
    for ratio, left, left_index, right, right_index in candidates:
        left_name, right_name = names[left], names[right]
        left_key, right_key = (left_name, left_index), (right_name, right_index)
        left_group, right_group = placed.get(left_key), placed.get(right_key)
        if left_group is None and right_group is None:
            group = Group(overlap=ratio)
            group.facts[left_name] = by_backend[left_name][left_index]
            group.facts[right_name] = by_backend[right_name][right_index]
            groups.append(group)
            placed[left_key] = placed[right_key] = group
        elif left_group is not None and right_group is None:
            if right_name not in left_group.facts:
                left_group.facts[right_name] = by_backend[right_name][right_index]
                left_group.overlap = max(left_group.overlap or 0.0, ratio)
                placed[right_key] = left_group
        elif right_group is not None and left_group is None:
            if left_name not in right_group.facts:
                right_group.facts[left_name] = by_backend[left_name][left_index]
                right_group.overlap = max(right_group.overlap or 0.0, ratio)
                placed[left_key] = right_group
    for name in names:
        for index, fact in enumerate(by_backend[name]):
            if (name, index) not in placed:
                groups.append(Group(overlap=None, facts={name: fact}))
    return groups


# ---------------------------------------------------------------------------
# Comparing the backends


def compare_runs(data: Path, variant: str, backends: list[str]) -> list[Path]:
    """Write, per session, where the given backends' facts agree and differ.

    A group holding a fact from every backend is an agreement; a group missing a backend
    is a difference for the lead to settle. What a backend refused, and why, is listed
    beside the groups, because a disagreement is often one backend's quote not verifying.
    """
    summaries = _summaries(data, variant, backends)
    sessions = sorted({sid for per_backend in summaries.values() for sid in per_backend})
    missing = {
        backend: [sid for sid in sessions if sid not in summaries[backend]]
        for backend in backends
        if any(sid not in summaries[backend] for sid in sessions)
    }
    out_dir = data / "compare" / variant
    written = []
    for session_id in sessions:
        per_backend = {
            name: summaries[name][session_id] for name in backends if session_id in summaries[name]
        }
        facts = {
            name: [fact for fact in summary.get("facts", []) if fact.get("quote")]
            for name, summary in per_backend.items()
        }
        report = {
            "session": session_id,
            "variant": variant,
            "backends": list(per_backend),
            "groups": [
                {"overlap": group.overlap, "facts": group.facts} for group in match_facts(facts)
            ],
            "dropped": {name: summary.get("dropped", []) for name, summary in per_backend.items()},
            "refused_parts": {
                name: summary.get("refused_parts", []) for name, summary in per_backend.items()
            },
            "summaries": {
                name: summary.get("summaries", []) for name, summary in per_backend.items()
            },
            "nothing_to_digest": {
                name: summary.get("nothing_to_digest", False)
                for name, summary in per_backend.items()
            },
        }
        if missing:
            report["not_run"] = {
                name: [sid for sid in missing[name] if sid == session_id]
                for name in backends
                if session_id in missing.get(name, [])
            }
        path = out_dir / f"{session_id}.json"
        _write_json(path, report)
        written.append(path)
        agreed = sum(1 for group in report["groups"] if len(group["facts"]) == len(per_backend))
        print(
            f"{session_id}: {agreed} agreed of {len(report['groups'])} groups "
            f"across {', '.join(per_backend)}",
            flush=True,
        )
    if not written:
        raise EvalError(f"no answers under {data / 'answers'} for variant {variant!r}; run first")
    return written


def _summaries(data: Path, variant: str, backends: list[str]) -> dict[str, dict[str, dict]]:
    """Each backend's session summaries, keyed by session id, from the answers on disk."""
    summaries: dict[str, dict[str, dict]] = {}
    for backend in backends:
        root = backend_root(data, backend, variant)
        if not root.is_dir():
            raise EvalError(f"no answers for backend {backend!r} under {root}")
        per_session: dict[str, dict] = {}
        for path in sorted(root.iterdir()):
            summary_path = path / "summary.json" if path.is_dir() else None
            if summary_path is None or not summary_path.is_file():
                continue
            summary = _read_json(summary_path)
            if not isinstance(summary, dict):
                raise EvalError(f"{summary_path}: expected an object")
            per_session[path.name] = summary
        if not per_session:
            raise EvalError(f"no session summaries for backend {backend!r} under {root}")
        summaries[backend] = per_session
    return summaries


def backends_with_answers(data: Path, variant: str) -> list[str]:
    """The backends that hold answers for this variant, in the order they are tried."""
    present = []
    for backend in BACKENDS:
        root = backend_root(data, backend, variant)
        if root.is_dir() and any(
            (path / "summary.json").is_file() for path in root.iterdir() if path.is_dir()
        ):
            present.append(backend)
    if not present:
        raise EvalError(f"no answers under {data / 'answers'} for variant {variant!r}")
    return present


# ---------------------------------------------------------------------------
# The answer key


KEY_SOURCES = frozenset({"stated", "observed", "inferred", "suggested"})
KEY_TERMS = frozenset({"long", "short"})
KEY_SCOPES = frozenset({"project", "global"})


def load_key(data: Path, session_id: str, synthetic: Path) -> tuple[list[dict], str]:
    """The key facts of one session, from the lead's file or a synthetic session's own.

    A synthetic session carries its expected answer in the repository, which stands in
    for the key until the lead settles one; every other session without a key file is
    unkeyed, and the scorer says so rather than scoring it against nothing.
    """
    path = data / "key" / f"{session_id}.json"
    if path.is_file():
        return _key_facts(_read_json(path), path), "lead"
    expected = synthetic / session_id / "expected.json"
    if expected.is_file():
        return _key_facts(_read_json(expected), expected), "expected"
    raise EvalError(
        f"no key for {session_id}: settle {data / 'key' / (session_id + '.json')} first"
    )


def _key_facts(document: object, path: Path) -> list[dict]:
    """The facts of a key or an expected answer, each checked before it scores anything."""
    if not isinstance(document, dict):
        raise EvalError(f"{path}: expected an object")
    facts = document.get("facts")
    if not isinstance(facts, list):
        raise EvalError(f"{path}: expected a list of facts")
    checked = []
    for number, fact in enumerate(facts, start=1):
        where = f"{path}: fact {number}"
        if not isinstance(fact, dict):
            raise EvalError(f"{where}: expected an object")
        quote = fact.get("quote")
        if not isinstance(quote, str) or not quote.strip():
            raise EvalError(f"{where}: a key fact carries the quote it is matched by")
        checked.append(
            {
                "statement": _key_text(fact.get("statement"), where, required=False),
                "quote": quote,
                "source": _key_choice(fact.get("source"), KEY_SOURCES, "source", where),
                "kind": _key_choice(fact.get("kind"), frozenset(page_kinds()), "kind", where),
                "term": _key_choice(fact.get("term"), KEY_TERMS, "term", where),
                "scope": _key_choice(fact.get("scope", "project"), KEY_SCOPES, "scope", where),
            }
        )
    return checked


def _key_text(value: object, where: str, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{where}: expected non-empty text")
    return value


def _key_choice(value: object, choices: frozenset[str], name: str, where: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise EvalError(f"{where}: {name} must be one of {', '.join(sorted(choices))}")
    return value


def page_kinds() -> list[str]:
    """The fact kinds the pipeline accepts, as the shipped page grammar names them."""
    from aft import page

    return sorted(set(page.KIND_SECTIONS.values()))


# ---------------------------------------------------------------------------
# The judge of one pair, its cache, and the files built around it (BUILD-PLAN M16)


class Judge:
    """The judge of one pair of statements, with its verdict cache.

    The judge is the configured endpoint, asked at temperature 0 with the schema that
    sits beside this script. It sees the key statement, the key quote and the run
    statement, and never which backend wrote the run statement. Every verdict is cached
    under the data directory, keyed by the hash of the judge prompt, the model, the key
    statement, the key quote and the run statement, so scoring a stored run twice calls the model
    once and gives the same numbers.
    """

    def __init__(self, cfg: Config, cache_path: Path | None) -> None:
        model_settings = replace(cfg.settings.model, temperature=JUDGE_TEMPERATURE)
        self.cfg = replace(cfg, settings=replace(cfg.settings, model=model_settings))
        try:
            self.prompt = JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
            self.schema = json.loads(JUDGE_SCHEMA_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise EvalError(f"the judge prompt or schema cannot be read: {exc}") from exc
        self.prompt_hash = hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()[:16]
        self.cache_path = cache_path
        self.cache = _load_judge_cache(cache_path)
        self.calls = 0

    def verdict(self, key_statement: str, key_quote: str, run_statement: str) -> dict:
        """The judge's answer on one pair, from the cache or one call to the endpoint."""
        key = _judge_key(
            self.prompt_hash, self.cfg.settings.model.name, key_statement, key_quote, run_statement
        )
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        answer = _MODEL_COMPLETE(
            self.cfg,
            self.prompt,
            _judge_user(key_statement, key_quote, run_statement),
            self.schema,
            JUDGE_MAX_TOKENS,
            purpose="eval",
        )
        verdict = {"differs": list(answer["differs"]), "verdict": answer["verdict"]}
        self.cache[key] = verdict
        self.calls += 1
        if self.cache_path is not None:
            _write_json(self.cache_path, self.cache)
        return verdict


def _judge_user(key_statement: str, key_quote: str, run_statement: str) -> str:
    """What the judge is shown: the key text and the run statement, and nothing else."""
    return (
        f"Key statement: {key_statement or '(none)'}\n"
        f"Key quote: {key_quote or '(none)'}\n"
        f"Run statement: {run_statement}"
    )


def _judge_key(
    prompt_hash: str, model: str, key_statement: str, key_quote: str, run_statement: str
) -> str:
    """The cache key of one pair: the judge prompt's hash and the model with the three
    texts, so a verdict is reused only by the judge that gave it."""
    payload = json.dumps(
        [prompt_hash, model, key_statement, key_quote, run_statement], ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _load_judge_cache(path: Path | None) -> dict[str, dict]:
    """The verdicts cached under the data directory, refused when one is not a verdict."""
    if path is None or not path.is_file():
        return {}
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise EvalError(f"{path}: expected an object of verdicts")
    for key, verdict in raw.items():
        if (
            not isinstance(verdict, dict)
            or verdict.get("verdict") not in VERDICTS
            or not isinstance(verdict.get("differs"), list)
        ):
            raise EvalError(f"{path}: {key} does not hold a verdict")
    return raw


def run_evidence(fact: dict) -> str:
    """The verified words a run fact rests on: its quote, and the proposal's words when the
    fact is an approval the digest grounded in the proposal it answers."""
    parts = [fact.get("quote") or ""]
    proposal = fact.get("proposal")
    if isinstance(proposal, list) and len(proposal) == 2 and isinstance(proposal[1], str):
        parts.append(proposal[1])
    return "\n".join(part for part in parts if part)


def judge_pair(
    judge: Judge, key_statement: str, key_quote: str, run_statement: str, run_quote: str = ""
) -> dict:
    """The judge's verdict on one pair (BUILD-PLAN M16): ``same``, ``narrower`` and
    ``broader`` count as found, a ``different`` verdict is a wrong statement.

    The names and numbers of the run statement that neither the key text nor the run
    fact's own evidence (``run_evidence``) holds are recorded beside the verdict as
    ``unheld``, for the reader of the report; they do not decide it. A summary keeps one
    quote per fact, while the digest grounded the statement in the whole turn, so a true
    detail can be missing from the quote; a name the run changed is the judge's to catch.
    """
    said = "\n".join(part for part in (key_statement, key_quote, run_quote) if part)
    answer = judge.verdict(key_statement, key_quote, run_statement)
    return {
        "differs": answer["differs"],
        "verdict": answer["verdict"],
        "unheld": digestmod.ungrounded(run_statement, said),
    }


def digest_prompt_hash() -> str:
    """The hash of the digest prompts, so a report says which wording produced it."""
    joined = "\n".join(
        f"{name}\x00{(Path(digestmod.__file__).parent / name).read_text(encoding='utf-8')}"
        for name in DIGEST_PROMPT_FILES
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def build_split(data: Path, force: bool = False) -> Path:
    """Freeze the dev/held-out split of the per-turn key's turns (BUILD-PLAN M16).

    Each turn id is hashed and read as an integer, so the split is about half each and
    a turn lands on the same side every time the file is rebuilt. The file names real
    sessions, so it stays out of git.
    """
    path = data / SPLIT_FILE
    if path.exists() and not force:
        raise EvalError(f"{path} exists and is frozen; rebuild it only with --force")
    key = _read_json(data / KEY_TURNS)
    if not isinstance(key, dict) or not key:
        raise EvalError(f"{data / KEY_TURNS}: expected a non-empty object of turns")
    turns = {}
    for turn_id in key:
        number = int.from_bytes(hashlib.sha256(turn_id.encode("utf-8")).digest()[:8], "big")
        turns[turn_id] = SPLIT_NAMES[number % 2]
    _write_json(path, {"built": datetime.now(UTC).isoformat(), "rule": SPLIT_RULE, "turns": turns})
    return path


def load_split(data: Path) -> dict[str, str] | None:
    """The frozen split of turn ids, or None when the data directory holds none."""
    path = data / SPLIT_FILE
    if not path.is_file():
        return None
    raw = _read_json(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("turns"), dict):
        raise EvalError(f"{path}: expected an object holding a turns object")
    for turn_id, name in raw["turns"].items():
        if name not in SPLIT_NAMES:
            raise EvalError(
                f"{path}: turn {turn_id!r} names split {name!r}; the splits are"
                f" {', '.join(SPLIT_NAMES)}"
            )
    return raw["turns"]


def split_record(data: Path) -> dict[str, Any] | None:
    """What a report records about the split it read: the counts and the file's hash."""
    turns = load_split(data)
    if turns is None:
        return None
    counts = {name: 0 for name in SPLIT_NAMES}
    for name in turns.values():
        counts[name] += 1
    return {
        "rule": SPLIT_RULE,
        "turns": counts,
        "sha256": hashlib.sha256((data / SPLIT_FILE).read_bytes()).hexdigest()[:16],
    }


def load_judge_check(data: Path) -> dict[str, list[dict]]:
    """The lead's planted pairs the judge is checked against (BUILD-PLAN M16).

    ``wrong`` holds key statements with one name, number or negation changed, which the
    judge must call ``different``; ``same`` holds key statements rewritten in other words
    with the same meaning, which the judge must call ``same`` or ``narrower``; ``broader``
    holds key statements with a true detail added, which the judge must count found.
    Planted wrong statements include supersets that hide one changed name, number or
    negation inside added detail, the case a ``broader`` verdict must not let through.
    """
    path = data / JUDGE_CHECK
    if not path.is_file():
        raise EvalError(f"{path}: no judge check; the lead writes one first")
    raw = _read_json(path)
    if not isinstance(raw, dict):
        raise EvalError(f"{path}: expected an object")
    check: dict[str, list[dict]] = {}
    for name in ("wrong", "same", "broader"):
        entries = raw.get(name)
        if not isinstance(entries, list) or not entries:
            raise EvalError(f"{path}: expected a non-empty {name!r} list")
        checked = []
        for number, entry in enumerate(entries, start=1):
            where = f"{path}: {name} entry {number}"
            if not isinstance(entry, dict):
                raise EvalError(f"{where}: expected an object")
            checked.append(
                {
                    "statement": _key_text(entry.get("statement"), where, required=True),
                    "quote": _key_text(entry.get("quote"), where, required=False),
                    "planted": _key_text(entry.get("planted"), where, required=True),
                }
            )
        check[name] = checked
    return check


# ---------------------------------------------------------------------------
# Scoring a run against the key


def score_run(data: Path, variant: str, backend: str, synthetic: Path, judge: Judge) -> dict:
    """Score one backend's run of one variant against the key, and write a dated report.

    Run facts and key facts are paired one to one and each pair is judged (BUILD-PLAN
    M16): a pair judged ``different`` is a wrong statement, and its key fact counts as
    missed. A run fact that matches no key fact is wrong; a key fact no run fact matches
    was missed; a pair judged ``same`` or ``narrower`` whose source, kind, term or scope
    disagree is a wrong tag; a kept fact whose kind is a decision and whose quote asks a
    question is a question recorded as a decision. Every ``[observed]`` fact is judged
    against its own cited quote. When the data directory holds a split of the per-turn
    key, the key-side numbers are reported for both splits, each key fact sitting in the
    split of the key-turn item it names the same thing as. Tokens and time come from the
    run's own records.
    """
    if backend not in BACKENDS:
        raise EvalError(f"unknown backend {backend!r}; the backends are {', '.join(BACKENDS)}")
    root = backend_root(data, backend, variant)
    if not root.is_dir():
        raise EvalError(f"no answers for backend {backend!r} under {root}")
    session_dirs = [path for path in sorted(root.iterdir()) if (path / "summary.json").is_file()]
    if not session_dirs:
        raise EvalError(f"no session summaries for backend {backend!r} under {root}")
    split = load_split(data)
    labelled = load_turn_items(data)
    sessions = []
    unkeyed = []
    totals = {
        "sessions": 0,
        "facts_kept": 0,
        "key_facts": 0,
        "wrong_facts": 0,
        "missed_facts": 0,
        "narrower": 0,
        "broader": 0,
        "wrong_tags": 0,
        "question_decisions": 0,
        "observed": 0,
        "observed_supported": 0,
        "observed_narrower": 0,
        "observed_broader": 0,
        "observed_unsupported": 0,
        "refused_facts": 0,
        "refused_parts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "seconds": 0.0,
    }
    splits = _split_buckets() if split else None
    for path in session_dirs:
        summary = _read_json(path / "summary.json")
        if not isinstance(summary, dict):
            raise EvalError(f"{path / 'summary.json'}: expected an object")
        session_id = path.name
        try:
            key, source = load_key(data, session_id, synthetic)
        except EvalError:
            unkeyed.append(session_id)
            continue
        scored = score_session(
            summary, key, judge, key_splits=_key_splits(key, labelled.get(session_id), split)
        )
        scored["session"] = session_id
        scored["key_from"] = source
        sessions.append(scored)
        totals["sessions"] += 1
        totals["facts_kept"] += len(summary.get("facts", []))
        totals["key_facts"] += len(key)
        for name in (
            "wrong_facts",
            "missed_facts",
            "narrower",
            "broader",
            "wrong_tags",
            "question_decisions",
            "observed",
            "observed_supported",
            "observed_narrower",
            "observed_broader",
            "observed_unsupported",
        ):
            totals[name] += scored[name]
        totals["refused_facts"] += len(summary.get("dropped", []))
        totals["refused_parts"] += len(summary.get("refused_parts", []))
        usage = summary.get("usage") or {}
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += usage.get("completion_tokens", 0)
        totals["seconds"] += summary.get("seconds", 0.0)
        if splits is not None:
            _add_session_splits(splits, scored)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "variant": variant,
        "backend": backend,
        "judge_prompt": judge.prompt_hash,
        "digest_prompts": digest_prompt_hash(),
        "model": judge.cfg.settings.model.name or None,
        "temperature": JUDGE_TEMPERATURE,
        "judge_calls": judge.calls,
        "prompt_shapes": {backend: BACKEND_SHAPE[backend]},
        "split": split_record(data),
        "totals": totals,
        "splits": splits,
        "unkeyed": sorted(unkeyed),
        "sessions": sessions,
    }
    path = (
        data
        / "reports"
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{variant}-{backend}.json"
    )
    _write_json(path, report)
    _print_report(report)
    return report


def score_session(
    summary: dict, key: list[dict], judge: Judge, key_splits: dict[int, str] | None = None
) -> dict:
    """One session's numbers against its key facts, each pair judged (BUILD-PLAN M16)."""
    placed = key_splits or {}
    run_facts = [dict(fact) for fact in summary.get("facts", []) if fact.get("quote")]
    groups = match_facts({KEY_BACKEND: key, summary["backend"]: run_facts})
    wrong: list[dict] = []
    missed: list[dict] = []
    tags = []
    pairs = []
    narrower = 0
    broader = 0
    for group in groups:
        run_fact = group.facts.get(summary["backend"])
        key_fact = group.facts.get(KEY_BACKEND)
        if key_fact is not None and run_fact is None:
            missed.append({**key_fact, "split": placed.get(id(key_fact))})
            continue
        if run_fact is not None and key_fact is None:
            wrong.append(dict(run_fact))
            continue
        assert run_fact is not None and key_fact is not None
        answer = judge_pair(
            judge,
            key_fact.get("statement", ""),
            key_fact.get("quote", ""),
            run_fact.get("statement", ""),
            run_evidence(run_fact),
        )
        split = placed.get(id(key_fact))
        pairs.append(
            {
                "key": key_fact.get("statement", ""),
                "run": run_fact.get("statement", ""),
                "verdict": answer["verdict"],
                "differs": answer["differs"],
                "unheld": answer["unheld"],
                "split": split,
            }
        )
        if answer["verdict"] == "different":
            wrong.append({**run_fact, "split": split})
            missed.append({**key_fact, "split": split})
            continue
        if answer["verdict"] == "narrower":
            narrower += 1
        if answer["verdict"] == "broader":
            broader += 1
        for name in ("source", "kind", "term", "scope"):
            if run_fact.get(name) != key_fact.get(name):
                tags.append(
                    {
                        "statement": run_fact.get("statement", ""),
                        "tag": name,
                        "run": run_fact.get(name),
                        "key": key_fact.get(name),
                    }
                )
    questions = [
        fact
        for fact in run_facts
        if fact.get("kind") == "decision" and "?" in fact.get("quote", "")
    ]
    observed = _judge_observed(judge, run_facts)
    usage = summary.get("usage") or {}
    return {
        "facts_kept": len(run_facts),
        "key_facts": len(key),
        "wrong_facts": len(wrong),
        "missed_facts": len(missed),
        "narrower": narrower,
        "broader": broader,
        "wrong_tags": len(tags),
        "question_decisions": len(questions),
        "observed": observed["count"],
        "observed_supported": observed["supported"],
        "observed_narrower": observed["narrower"],
        "observed_broader": observed["broader"],
        "observed_unsupported": observed["unsupported"],
        "wrong": wrong,
        "missed": missed,
        "tags": tags,
        "questions": questions,
        "pairs": pairs,
        "refused_facts": len(summary.get("dropped", [])),
        "refused_parts": len(summary.get("refused_parts", [])),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "seconds": summary.get("seconds", 0.0),
        "nothing_to_digest": summary.get("nothing_to_digest", False),
        "fully_refused": summary.get("fully_refused", False),
    }


def _judge_observed(judge: Judge, run_facts: list[dict]) -> dict[str, int]:
    """Each ``[observed]`` fact judged against its own cited quote (BUILD-PLAN M16).

    The judge answers whether the quote says what the statement says; the report gives
    how many observed facts are supported, narrower, broader (the statement says more than
    its quote) and unsupported. Observed recall
    needs a key of agent entries, which does not exist.
    """
    counts = {"count": 0, "supported": 0, "narrower": 0, "broader": 0, "unsupported": 0}
    buckets = {
        "same": "supported",
        "narrower": "narrower",
        "broader": "broader",
        "different": "unsupported",
    }
    for fact in run_facts:
        if fact.get("source") != "observed" or not fact.get("quote"):
            continue
        answer = judge_pair(judge, "", fact["quote"], fact.get("statement", ""))
        counts["count"] += 1
        counts[buckets[answer["verdict"]]] += 1
    return counts


def _split_buckets() -> dict[str, dict[str, int]]:
    """The per-split key-side counts a report carries, ``unsplit`` making them add up."""
    return {
        name: {"key_facts": 0, "found": 0, "narrower": 0, "broader": 0, "missed": 0, "wrong": 0}
        for name in (*SPLIT_NAMES, "unsplit")
    }


def _split_bucket(splits: dict[str, dict[str, int]], name: str | None) -> dict[str, int]:
    """The bucket one split name counts in; a turn the split file does not name is unsplit."""
    return splits[name if name in splits else "unsplit"]


def _add_session_splits(splits: dict[str, dict[str, int]], scored: dict) -> None:
    """Add one session's key-side results to the split buckets they sit in."""
    for record in scored["missed"]:
        bucket = _split_bucket(splits, record.get("split"))
        bucket["key_facts"] += 1
        bucket["missed"] += 1
    for record in scored["pairs"]:
        if record["verdict"] == "different":
            continue
        bucket = _split_bucket(splits, record.get("split"))
        bucket["key_facts"] += 1
        bucket["found"] += 1
        if record["verdict"] in ("narrower", "broader"):
            bucket[record["verdict"]] += 1
    for record in scored["wrong"]:
        if record.get("split") is None:
            continue
        _split_bucket(splits, record["split"])["wrong"] += 1


def load_turn_items(data: Path) -> dict[str, list[tuple[str, dict]]]:
    """The per-turn key as session -> its labelled (turn id, item) pairs; {} without one."""
    path = data / KEY_TURNS
    if not path.is_file():
        return {}
    key = _read_json(path)
    if not isinstance(key, dict):
        raise EvalError(f"{path}: expected an object of turns")
    return _turn_items(key, path)


def _turn_items(key: dict, path: Path) -> dict[str, list[tuple[str, dict]]]:
    """The labelled durable items of a per-turn key: session -> (turn id, item) pairs."""
    labelled: dict[str, list[tuple[str, dict]]] = {}
    for turn_id, turn in key.items():
        where = f"{path}: turn {turn_id!r}"
        if not isinstance(turn, dict) or not isinstance(turn.get("durable"), list):
            raise EvalError(f"{where}: expected an object with a durable list")
        for item in turn["durable"]:
            labelled.setdefault(turn_id.rsplit("#", 1)[0], []).append(
                (turn_id, _key_item(item, where))
            )
    return labelled


def _key_item(item: object, where: str) -> dict:
    """One durable item of the per-turn key, which must say something to be judged."""
    if not isinstance(item, dict):
        raise EvalError(f"{where}: expected an object")
    normalized = dict(item)
    for name in ("statement", "quote"):
        value = normalized.get(name)
        if value is not None and not isinstance(value, str):
            raise EvalError(f"{where}: {name} must be a string")
        if value is None:
            normalized[name] = ""
    if not normalized["statement"].strip() and not normalized["quote"].strip():
        raise EvalError(f"{where}: a durable item carries a statement or a quote")
    return normalized


def _key_splits(
    key: list[dict], labelled: list[tuple[str, dict]] | None, split: dict[str, str] | None
) -> dict[int, str]:
    """The split each key fact of a session sits in, through the key-turn item it names.

    A key fact sits in the split of the labelled item it names the same thing as; a key
    fact the per-turn key does not hold sits in no split.
    """
    if not labelled or not split:
        return {}
    groups = match_facts({KEY_BACKEND: key, "turns": [item for _, item in labelled]})
    turn_of = {id(item): turn_id for turn_id, item in labelled}
    splits = {}
    for group in groups:
        key_fact = group.facts.get(KEY_BACKEND)
        item = group.facts.get("turns")
        if key_fact is None or item is None:
            continue
        name = split.get(turn_of.get(id(item), ""))
        if name is not None:
            splits[id(key_fact)] = name
    return splits


def _print_report(report: dict) -> None:
    totals = report["totals"]
    print(
        f"{report['backend']} ({report['variant']}): {totals['sessions']} sessions, "
        f"{totals['facts_kept']} facts kept against {totals['key_facts']} key facts: "
        f"{totals['wrong_facts']} wrong, {totals['missed_facts']} missed, "
        f"{totals['narrower']} narrower, {totals['broader']} broader, "
        f"{totals['wrong_tags']} wrong tags, {totals['question_decisions']} questions "
        f"as decisions, observed {totals['observed_supported']} supported, "
        f"{totals['observed_narrower']} narrower, {totals['observed_broader']} broader, "
        f"{totals['observed_unsupported']} "
        f"unsupported of {totals['observed']}, "
        f"{totals['refused_facts']} refused facts, "
        f"{totals['refused_parts']} refused parts, "
        f"tokens={totals['prompt_tokens'] + totals['completion_tokens']}, "
        f"{totals['seconds']:.1f}s",
        flush=True,
    )
    for name, bucket in (report.get("splits") or {}).items():
        print(
            f"  split {name}: {bucket['found']} of {bucket['key_facts']} key facts found"
            f" ({bucket['narrower']} narrower, {bucket['broader']} broader),"
            f" {bucket['missed']} missed,"
            f" {bucket['wrong']} wrong statements",
            flush=True,
        )
    if report["unkeyed"]:
        print(f"unkeyed, not scored: {', '.join(report['unkeyed'])}", file=sys.stderr)


# ---------------------------------------------------------------------------
# The command line


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except (EvalError, ConfigError, model.ModelError, evidence.EvidenceError) as exc:
        print(f"eval-digest: {exc}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eval_digest",
        description="Run the digest pipeline over a fixed test set and score it.",
    )
    sub = parser.add_subparsers(metavar="command", required=True)

    p = sub.add_parser("run", help="digest every session through one backend")
    p.add_argument("--data", help="the eval data directory (default from AFT_EVAL_DATA)")
    p.add_argument("--backend", choices=BACKENDS, default="local")
    p.add_argument("--variant", default=BASE_VARIANT)
    p.add_argument(
        "--endpoint", default=LOCAL_ENDPOINT, help="local backend endpoint ($AFT_EVAL_ENDPOINT)"
    )
    p.add_argument("--model", dest="model_name", default=LOCAL_MODEL, help="local backend model")
    p.add_argument(
        "--extra-body", default=LOCAL_EXTRA_BODY, help="local backend extra_body, as JSON"
    )
    p.add_argument("--session", action="append", default=[], help="run only this session, by id")
    p.add_argument("--synthetic", type=Path, default=None, help="the synthetic sessions directory")
    p.set_defaults(handler=_cmd_run)

    p = sub.add_parser("compare", help="list where the backends' facts agree and differ")
    p.add_argument("--data")
    p.add_argument("--variant", default=BASE_VARIANT)
    p.add_argument(
        "--backend", action="append", default=None, help="default: every backend with answers"
    )
    p.set_defaults(handler=_cmd_compare)

    p = sub.add_parser("score", help="score a run against the answer key")
    p.add_argument("--data")
    p.add_argument("--variant", default=BASE_VARIANT)
    p.add_argument("--backend", default="local")
    p.add_argument("--synthetic", type=Path, default=None)
    p.set_defaults(handler=_cmd_score)

    p = sub.add_parser("score-turns", help="score a run against the per-turn key")
    p.add_argument("--data")
    p.add_argument("--variant", default=BASE_VARIANT)
    p.add_argument("--backend", default="local")
    p.set_defaults(handler=_cmd_score_turns)

    p = sub.add_parser("split", help="freeze the dev/held-out split of the per-turn key")
    p.add_argument("--data")
    p.add_argument("--force", action="store_true", help="rewrite an existing split.json")
    p.set_defaults(handler=_cmd_split)

    p = sub.add_parser("judge-check", help="check the judge against the lead's planted pairs")
    p.add_argument("--data")
    p.add_argument(
        "--endpoint", default=LOCAL_ENDPOINT, help="local backend endpoint ($AFT_EVAL_ENDPOINT)"
    )
    p.add_argument("--model", dest="model_name", default=LOCAL_MODEL, help="local backend model")
    p.add_argument(
        "--extra-body", default=LOCAL_EXTRA_BODY, help="local backend extra_body, as JSON"
    )
    p.set_defaults(handler=_cmd_judge_check)
    p = sub.add_parser("current", help="run end-to-end current-truth eval over scenarios")
    p.add_argument("--data", help="eval data directory for output reports")
    p.add_argument("--backend", choices=BACKENDS, default="local")
    p.add_argument("--variant", default=BASE_VARIANT)
    p.add_argument(
        "--endpoint", default=LOCAL_ENDPOINT, help="local backend endpoint ($AFT_EVAL_ENDPOINT)"
    )
    p.add_argument("--model", dest="model_name", default=LOCAL_MODEL, help="local backend model")
    p.add_argument(
        "--extra-body", default=LOCAL_EXTRA_BODY, help="local backend extra_body, as JSON"
    )
    p.add_argument(
        "--scenario", action="append", default=[], help="run only this scenario, by name"
    )
    p.add_argument(
        "--scenarios-dir",
        type=Path,
        default=None,
        help="scenarios directory (default: eval/current)",
    )
    p.add_argument("--report", type=Path, default=None, help="write JSON report to this file")
    p.set_defaults(handler=_cmd_current)
    return parser


def with_pipeline(cfg: Config, variant: str) -> Config:
    """The settings a variant runs under: a variant named after a digest pipeline runs that
    pipeline (BUILD-PLAN F55); any other variant runs the settings as they are."""
    if variant not in configmod.DIGEST_PIPELINES:
        return cfg
    digest = replace(cfg.settings.digest, pipeline=variant)
    return replace(cfg, settings=replace(cfg.settings, digest=digest))


# ---------------------------------------------------------------------------
# Scoring a run against the per-turn key (BUILD-PLAN F55)

KEY_TURNS = "key-turns.json"
USER_NAMES = "user-names.json"


def quote_contains(item_quote: str, fact_quote: str) -> bool:
    """True when the key item's quote stands whole and in order inside the run fact's quote."""
    sub = quote_tokens(item_quote)
    if not sub:
        return False
    full = quote_tokens(fact_quote)
    n = len(sub)
    m = len(full)
    if n > m:
        return False
    for i in range(m - n + 1):
        if full[i : i + n] == sub:
            return True
    return False


def turn_fact_overlap(item: dict, fact: dict) -> float | None:
    """How strongly a key item and a run fact pair in score-turns, or None when they do not.

    A key item pairs with a run fact when fact_overlap names them the same thing (score
    >= 1.0), or when the key item's quote stands whole and in order inside the run fact's
    quote (score < 1.0). Containment pairs rank below every fact_overlap pair.
    """
    overlap = fact_overlap(item, fact)
    if overlap is not None:
        return overlap
    if quote_contains(item.get("quote", ""), fact.get("quote", "")):
        quote = quote_overlap(item.get("quote", ""), fact.get("quote", ""))
        statement = statement_overlap(item.get("statement", ""), fact.get("statement", ""))
        return max(quote / QUOTE_MATCH_RATIO, statement / STATEMENT_MATCH_RATIO)
    return None


def pair_turn_facts(key_items: list[dict], run_facts: list[dict]) -> list[tuple[int, int]]:
    """Pair each key item with at most one run fact, its strongest.

    A run fact may answer several key items. Each key item takes its strongest matching
    run fact; ties are broken by earlier run fact index. Returns a list of (item_index,
    fact_index) pairs.
    """
    pairs: list[tuple[int, int]] = []
    for item_index, item in enumerate(key_items):
        best_overlap: float | None = None
        best_fact_index: int | None = None
        for fact_index, fact in enumerate(run_facts):
            score = turn_fact_overlap(item, fact)
            if score is None:
                continue
            if best_overlap is None or score > best_overlap:
                best_overlap = score
                best_fact_index = fact_index
        if best_fact_index is not None:
            pairs.append((item_index, best_fact_index))
    return pairs


def score_turns(data: Path, variant: str, backend: str, judge: Judge) -> dict:
    """A run's facts against the per-turn key (BUILD-PLAN F55, T56).

    Key items and run facts are paired through ``pair_turn_facts``, each key item taking
    at most one run fact, its strongest, while a run fact may answer several key items.
    Each pair is judged: the names and numbers of the run statement are checked against
    the key text with no model call, and what survives is judged. A key item counts as
    found only when its pair is judged ``same``, ``narrower``, or ``broader``; a
    ``[stated]`` fact left unpaired, or judged ``different`` in every pair it is in, is
    wrong. Every ``[observed]`` fact is judged against its own cited quote; the key labels
    the user's turns only, so it holds no observed recall. When the data directory holds
    a frozen split, the key-side numbers are reported for both splits.
    """
    if backend not in BACKENDS:
        raise EvalError(f"unknown backend {backend!r}; the backends are {', '.join(BACKENDS)}")
    key_path = data / KEY_TURNS
    if not key_path.exists():
        raise EvalError(f"{key_path}: no per-turn key")
    labelled = _turn_items(_read_json(key_path), key_path)
    split = load_split(data)
    names_path = data / USER_NAMES
    user_names = [str(n) for n in _read_json(names_path)] if names_path.exists() else None
    totals = {
        "sessions": 0,
        "key_items": 0,
        "found": 0,
        "narrower": 0,
        "broader": 0,
        "missed": 0,
        "stated": 0,
        "stated_wrong": 0,
        "observed": 0,
        "observed_supported": 0,
        "observed_narrower": 0,
        "observed_broader": 0,
        "observed_unsupported": 0,
    }
    if user_names is not None:
        totals["naming_user"] = 0
    buckets = _split_buckets() if split else None
    sessions = []
    for session_id, items in sorted(labelled.items()):
        summary_path = run_dir(data, backend, variant, session_id) / "summary.json"
        if not summary_path.exists():
            continue
        summary = _read_json(summary_path)
        facts = [dict(fact) for fact in summary.get("facts", [])]
        scored = _score_turns_session(judge, backend, items, facts, split, user_names)
        totals["sessions"] += 1
        for name, value in scored["totals"].items():
            totals[name] += value
        sessions.append({"session": session_id, **scored["record"]})
        if buckets is not None and scored["splits"] is not None:
            for name, bucket in scored["splits"].items():
                for field, value in bucket.items():
                    buckets[name][field] += value
    return {
        "variant": variant,
        "backend": backend,
        "judge_prompt": judge.prompt_hash,
        "digest_prompts": digest_prompt_hash(),
        "model": judge.cfg.settings.model.name or None,
        "temperature": JUDGE_TEMPERATURE,
        "judge_calls": judge.calls,
        "prompt_shapes": {backend: BACKEND_SHAPE[backend]},
        "split": split_record(data),
        "totals": totals,
        "splits": buckets,
        "sessions": sessions,
    }


def _score_turns_session(
    judge: Judge,
    backend: str,
    items: list[tuple[str, dict]],
    facts: list[dict],
    split: dict[str, str] | None,
    user_names: list[str] | None = None,
) -> dict:
    """One session against its labelled key items, each pair judged (BUILD-PLAN M16, T56)."""
    key_fact_list = [item for _, item in items]
    matched = dict(pair_turn_facts(key_fact_list, facts))
    counts = {
        "key_items": len(items),
        "found": 0,
        "narrower": 0,
        "broader": 0,
        "missed": 0,
        "stated": 0,
        "stated_wrong": 0,
    }
    buckets = _split_buckets() if split else None
    pairs = []
    wrong_statements = []
    missed_statements = []
    fact_verdicts: list[list[str]] = [[] for _ in facts]
    # The split bucket of the first key item each run fact is paired with, where its
    # wrong [stated] count goes; an unpaired fact belongs to no split.
    fact_buckets: list[dict[str, int] | None] = [None for _ in facts]
    for item_index, (turn_id, item) in enumerate(items):
        where = split.get(turn_id) if split else None
        bucket = _split_bucket(buckets, where) if buckets is not None else None
        if bucket is not None:
            bucket["key_facts"] += 1
        if item_index not in matched:
            counts["missed"] += 1
            missed_statements.append(item.get("statement", ""))
            if bucket is not None:
                bucket["missed"] += 1
            continue
        fact_index = matched[item_index]
        fact = facts[fact_index]
        answer = judge_pair(
            judge,
            item.get("statement", ""),
            item.get("quote", ""),
            fact.get("statement", ""),
            run_evidence(fact),
        )
        fact_verdicts[fact_index].append(answer["verdict"])
        if fact_buckets[fact_index] is None:
            fact_buckets[fact_index] = bucket
        pairs.append(
            {
                "key": item.get("statement", ""),
                "run": fact.get("statement", ""),
                "verdict": answer["verdict"],
                "differs": answer["differs"],
                "unheld": answer["unheld"],
                "split": where,
            }
        )
        if answer["verdict"] == "different":
            counts["missed"] += 1
            missed_statements.append(item.get("statement", ""))
            if bucket is not None:
                bucket["missed"] += 1
            continue
        counts["found"] += 1
        if bucket is not None:
            bucket["found"] += 1
        if answer["verdict"] in ("narrower", "broader"):
            counts[answer["verdict"]] += 1
            if bucket is not None:
                bucket[answer["verdict"]] += 1
    for fact_index, fact in enumerate(facts):
        if fact.get("source") != "stated":
            continue
        verdicts = fact_verdicts[fact_index]
        if not verdicts or all(v == "different" for v in verdicts):
            counts["stated_wrong"] += 1
            wrong_statements.append(fact.get("statement", ""))
            if verdicts and fact_buckets[fact_index] is not None:
                fact_buckets[fact_index]["wrong"] += 1
    observed = _judge_observed(judge, facts)
    naming_user_statements = _naming_user_statements(facts, user_names)
    if user_names is not None:
        counts["naming_user"] = len(naming_user_statements)
    counts["stated"] = sum(1 for fact in facts if fact.get("source") == "stated")
    counts["observed"] = observed["count"]
    counts["observed_supported"] = observed["supported"]
    counts["observed_narrower"] = observed["narrower"]
    counts["observed_broader"] = observed["broader"]
    counts["observed_unsupported"] = observed["unsupported"]
    record = {
        "key_items": len(items),
        "found": counts["found"],
        "narrower": counts["narrower"],
        "broader": counts["broader"],
        "missed": counts["missed"],
        "stated": counts["stated"],
        "wrong": wrong_statements,
        "missed_items": missed_statements,
        "pairs": pairs,
    }
    if user_names is not None:
        record["naming_user"] = len(naming_user_statements)
        record["naming_user_statements"] = naming_user_statements
    return {"record": record, "totals": counts, "splits": buckets}


def _naming_user_statements(facts: list[dict], names: list[str] | None) -> list[str]:
    """Statements that call the user by a listed name their quote does not hold (F96)."""
    if not names:
        return []
    statements = []
    for fact in facts:
        statement = fact.get("statement", "")
        held = [n for n in names if re.search(rf"\b{re.escape(n)}\b", statement)]
        if not held:
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", fact.get("quote", "")) for n in held):
            continue
        statements.append(statement)
    return statements


def _cmd_score_turns(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    cfg = load_config()
    judge = Judge(cfg, data / JUDGE_CACHE)
    report = score_turns(data, args.variant, args.backend, judge)
    t = report["totals"]
    out = (
        data
        / "reports"
        / f"{time.strftime('%Y%m%dT%H%M%S')}-turns-{args.variant}-{args.backend}.json"
    )
    _write_json(out, report)
    stated_txt = f"{t['stated_wrong']} of {t['stated']} [stated] facts not in the key"
    if "naming_user" in t:
        stated_txt += f"; {t['naming_user']} naming_user"
    print(
        f"{args.variant}/{args.backend}: found {t['found']} of {t['key_items']} key items"
        f" ({t['narrower']} narrower, {t['broader']} broader); {stated_txt};"
        f" observed {t['observed_supported']} supported,"
        f" {t['observed_narrower']} narrower, {t['observed_broader']} broader,"
        f" {t['observed_unsupported']} unsupported of"
        f" {t['observed']}; judge calls {report['judge_calls']}; report {out}"
    )
    for name, bucket in (report.get("splits") or {}).items():
        print(
            f"  split {name}: found {bucket['found']} of {bucket['key_facts']}"
            f" key items ({bucket['narrower']} narrower, {bucket['broader']} broader)"
        )
    return 0


def _data_dir(args: argparse.Namespace) -> Path:
    raw = args.data or os.environ.get("AFT_EVAL_DATA")
    if not raw:
        raise EvalError("no data directory: give --data or set AFT_EVAL_DATA")
    data = Path(raw).expanduser()
    if not data.is_dir():
        raise EvalError(f"{data}: no eval data directory")
    return data


def _synthetic_dir(args: argparse.Namespace) -> Path:
    return DEFAULT_SYNTHETIC if args.synthetic is None else args.synthetic


def _cmd_run(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    cfg = with_model(load_config(), args.endpoint, args.model_name, args.extra_body)
    cfg = with_pipeline(cfg, args.variant)
    variant_dir(data / "answers" / args.backend, args.variant)
    backend = build_backend(args.backend)
    sessions = select_sessions(
        real_sessions(data) + synthetic_sessions(_synthetic_dir(args), cfg), args.session
    )
    with vault.locked(cfg):
        run_sessions(cfg, backend, sessions, data, args.variant)
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    backends = args.backend or backends_with_answers(data, args.variant)
    unknown = [name for name in backends if name not in BACKENDS]
    if unknown:
        raise EvalError(f"unknown backend(s) {', '.join(unknown)}")
    compare_runs(data, args.variant, backends)
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    cfg = load_config()
    score_run(
        data, args.variant, args.backend, _synthetic_dir(args), Judge(cfg, data / JUDGE_CACHE)
    )
    return 0


def _cmd_split(args: argparse.Namespace) -> int:
    path = build_split(_data_dir(args), force=args.force)
    turns = load_split(path.parent) or {}
    counts = {name: 0 for name in SPLIT_NAMES}
    for name in turns.values():
        counts[name] += 1
    print(
        f"{path}: {counts[SPLIT_NAMES[0]]} {SPLIT_NAMES[0]}, {counts[SPLIT_NAMES[1]]}"
        f" {SPLIT_NAMES[1]} of {len(turns)} turns"
    )
    return 0


def _cmd_judge_check(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    cfg = with_model(load_config(), args.endpoint, args.model_name, args.extra_body)
    judge = Judge(cfg, data / JUDGE_CACHE)
    check = load_judge_check(data)
    judged = {}
    for name, entries in check.items():
        judged[name] = [
            {**entry, **judge.verdict(entry["statement"], entry["quote"], entry["planted"])}
            for entry in entries
        ]
    wrong_hits = sum(1 for row in judged["wrong"] if row["verdict"] == "different")
    same_hits = sum(1 for row in judged["same"] if row["verdict"] in ("same", "narrower"))
    broader_hits = sum(1 for row in judged["broader"] if row["verdict"] in FOUND)
    need_wrong = math.ceil(JUDGE_CHECK_WRONG_SHARE * len(judged["wrong"]))
    need_same = math.ceil(JUDGE_CHECK_SAME_SHARE * len(judged["same"]))
    need_broader = math.ceil(JUDGE_CHECK_SAME_SHARE * len(judged["broader"]))
    passed = wrong_hits >= need_wrong and same_hits >= need_same and broader_hits >= need_broader
    report = {
        "at": datetime.now(UTC).isoformat(),
        "judge_prompt": judge.prompt_hash,
        "model": judge.cfg.settings.model.name or None,
        "temperature": JUDGE_TEMPERATURE,
        "judge_calls": judge.calls,
        "wrong": judged["wrong"],
        "same": judged["same"],
        "broader": judged["broader"],
        "wrong_different": wrong_hits,
        "wrong_needed": need_wrong,
        "same_ok": same_hits,
        "same_needed": need_same,
        "broader_found": broader_hits,
        "broader_needed": need_broader,
        "passed": passed,
    }
    out = data / "reports" / f"{time.strftime('%Y%m%dT%H%M%S')}-judge-check.json"
    _write_json(out, report)
    print(
        f"judge check: {wrong_hits} of {len(judged['wrong'])} planted wrong judged"
        f" different (need {need_wrong}), {same_hits} of {len(judged['same'])} planted"
        f" same judged same or narrower (need {need_same}), {broader_hits} of"
        f" {len(judged['broader'])} planted broader judged found (need {need_broader}) ->"
        f" {'PASS' if passed else 'FAIL'}; report {out}"
    )
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# F60: current truth evaluation (BUILD-PLAN F60)


@dataclass(frozen=True)
class ScenarioValue:
    """One expected value of a scenario and the tokens that tell it apart (BUILD-PLAN M16)."""

    value: str
    tokens: tuple[str, ...]


@dataclass(frozen=True)
class ScenarioSession:
    """One session in a scenario sequence."""

    session_id: str
    agent: str
    transcript: Path
    time: datetime
    dream_time: datetime | None = None
    dream_only: bool = False


@dataclass(frozen=True)
class Scenario:
    """One evaluation scenario composed of scripted sessions and expectations."""

    name: str
    description: str
    name: str
    description: str
    current: tuple[ScenarioValue, ...]
    superseded: tuple[ScenarioValue, ...]
    sessions: tuple[ScenarioSession, ...]
    slug: str = "afterthought"
    config_overrides: dict[str, Any] = field(default_factory=dict)


def score_block(
    block: str,
    current: Sequence[ScenarioValue],
    superseded: Sequence[ScenarioValue],
    judge: Judge,
) -> dict[str, Any]:
    """Score a memory block against a scenario's values, each judged (BUILD-PLAN M16).

    A value is present when a block line holds all the tokens that tell the value apart,
    matched casefolded with punctuation set aside, and the judge, given the line as the
    key, answers ``same`` or ``narrower`` for the value: word overlap alone never counts,
    because a current and a superseded value share most of their words. A present
    superseded value is recorded as a contradiction.
    """
    lines = [line for line in block.splitlines() if line.strip()]

    def value_result(value: ScenarioValue) -> dict[str, Any]:
        candidates = [line for line in lines if _holds_tokens(line, value.tokens)]
        judged = []
        present = False
        line_found = None
        for line in candidates:
            # The line is the key and the value the statement judged against it: a line
            # holding the value and more answers narrower, where the other way round the
            # line's extra words would read as a difference.
            answer = judge.verdict(line, "", value.value)
            judged.append({"line": line, **answer})
            if answer["verdict"] in FOUND:
                present = True
                line_found = line
                break
        return {
            "value": value.value,
            "tokens": list(value.tokens),
            "present": present,
            "line": line_found,
            "judged": judged,
        }

    current_results = [value_result(value) for value in current]
    superseded_results = [value_result(value) for value in superseded]
    current_present = all(result["present"] for result in current_results)
    superseded_absent = not any(result["present"] for result in superseded_results)
    contradiction = not superseded_absent
    passed = current_present and superseded_absent
    return {
        "current_present": current_present,
        "superseded_absent": superseded_absent,
        "contradiction": contradiction,
        "passed": passed,
        "current": current_results,
        "superseded": superseded_results,
    }


def _holds_tokens(line: str, tokens: Sequence[str]) -> bool:
    """Whether a line holds every token, casefolded with punctuation set aside.

    A token may be a short phrase: its words must stand together in the line's words.
    """
    words = _TOKENS.findall(line.casefold())
    for token in tokens:
        wanted = _TOKENS.findall(token.casefold())
        if not wanted or not any(
            words[start : start + len(wanted)] == wanted
            for start in range(len(words) - len(wanted) + 1)
        ):
            return False
    return True


def load_scenario(path: Path) -> Scenario:
    """Load one scenario directory holding scenario.json and transcripts."""
    scenario_file = path / "scenario.json"
    if not scenario_file.is_file():
        raise EvalError(f"{path}: no scenario.json")
    raw = _read_json(scenario_file)
    if not isinstance(raw, dict):
        raise EvalError(f"{scenario_file}: expected JSON object")

    name = raw.get("name") or path.name
    description = raw.get("description") or ""
    slug = raw.get("slug") or "afterthought"

    def _to_values(val: object, where: str) -> tuple[ScenarioValue, ...]:
        if val is None:
            return ()
        if not isinstance(val, list):
            raise EvalError(f"{where}: expected a list of values")
        values = []
        for number, entry in enumerate(val, start=1):
            place = f"{where}: value {number}"
            if not isinstance(entry, dict):
                raise EvalError(f"{place}: expected an object with a value and its tokens")
            value = entry.get("value")
            tokens = entry.get("tokens")
            if not isinstance(value, str) or not value.strip():
                raise EvalError(f"{place}: expected a non-empty value")
            if (
                not isinstance(tokens, list)
                or not tokens
                or not all(isinstance(token, str) and token.strip() for token in tokens)
            ):
                raise EvalError(f"{place}: expected a non-empty list of distinguishing tokens")
            values.append(ScenarioValue(value=value, tokens=tuple(tokens)))
        return tuple(values)

    current = _to_values(raw.get("current"), str(scenario_file))
    superseded = _to_values(raw.get("superseded"), str(scenario_file))
    config_overrides = raw.get("config") or {}

    sessions_raw = raw.get("sessions") or []
    sessions: list[ScenarioSession] = []
    for idx, sess in enumerate(sessions_raw, start=1):
        if not isinstance(sess, dict):
            continue
        sess_id = sess.get("session_id") or f"{name}-{idx}"
        agent = sess.get("agent") or readers.CLAUDE
        transcript_rel = sess.get("transcript")
        transcript_path = path / transcript_rel if transcript_rel else path / f"session-{idx}.jsonl"
        time_raw = sess.get("time")
        if time_raw:
            sess_time = datetime.fromisoformat(time_raw)
            if sess_time.tzinfo is None:
                sess_time = sess_time.replace(tzinfo=UTC)
        else:
            sess_time = datetime.now(UTC)

        dream_time_raw = sess.get("dream_time")
        dream_time = None
        if dream_time_raw:
            dream_time = datetime.fromisoformat(dream_time_raw)
            if dream_time.tzinfo is None:
                dream_time = dream_time.replace(tzinfo=UTC)

        dream_only = bool(sess.get("dream_only", False))
        sessions.append(
            ScenarioSession(
                session_id=sess_id,
                agent=agent,
                transcript=transcript_path,
                time=sess_time,
                dream_time=dream_time,
                dream_only=dream_only,
            )
        )

    return Scenario(
        name=name,
        description=description,
        current=current,
        superseded=superseded,
        sessions=tuple(sessions),
        slug=slug,
        config_overrides=config_overrides,
    )


def load_scenarios(directory: Path, wanted: list[str] | None = None) -> list[Scenario]:
    """Load all scenarios from directory, optionally filtered by name."""
    if not directory.is_dir():
        raise EvalError(f"{directory}: no scenarios directory")
    scenarios = []
    for path in sorted(directory.iterdir()):
        if path.is_dir() and (path / "scenario.json").is_file():
            scenarios.append(load_scenario(path))

    if not wanted:
        return scenarios

    by_name = {s.name: s for s in scenarios}
    selected = []
    for name in wanted:
        if name not in by_name:
            raise EvalError(
                f"unknown scenario {name!r}; scenarios are {', '.join(sorted(by_name))}"
            )
        selected.append(by_name[name])
    return selected


def _toml_value(val: Any) -> str:
    """Format a Python value as a TOML scalar or inline table."""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return json.dumps(val)
    if isinstance(val, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in val) + "]"
    if isinstance(val, dict):
        pairs = [f"{k} = {_toml_value(v)}" for k, v in val.items()]
        return "{ " + ", ".join(pairs) + " }"
    return json.dumps(val)


def run_scenario(
    scenario: Scenario,
    cfg: Config,
    backend: Backend,
    judge: Judge,
    variant: str = BASE_VARIANT,
) -> dict[str, Any]:
    """Run one scenario end-to-end through capture, digest, dream, and inject."""
    with tempfile.TemporaryDirectory(prefix=f"aft-current-{scenario.name}-") as tmpdir:
        tmp = Path(tmpdir)
        scenario_home = tmp / "home"
        scenario_vault = tmp / "vault"
        scenario_repo = tmp / "repo"
        scenario_home.mkdir(parents=True)
        scenario_vault.mkdir(parents=True)
        scenario_repo.mkdir(parents=True)

        _refuse_real(str(scenario_home), "scenario home")
        _refuse_real(str(scenario_vault), "scenario vault")

        gitutil.git(["init", "-q"], cwd=scenario_repo)
        (scenario_repo / "README.md").write_text(f"{scenario.slug}\n", encoding="utf-8")
        gitutil.git(["add", "README.md"], cwd=scenario_repo)
        gitutil.git(["commit", "-q", "-m", "init"], cwd=scenario_repo)

        pipeline_name = (
            variant if variant in configmod.DIGEST_PIPELINES else cfg.settings.digest.pipeline
        )
        lines = [
            f'vault = "{scenario_vault}"',
            'host = "evalhost"',
            "",
            "[model]",
            f'endpoint = "{cfg.settings.model.endpoint}"',
            f'name = "{cfg.settings.model.name}"',
            f"extra_body = {_toml_value(cfg.settings.model.extra_body)}",
            "",
            "[digest]",
            f'pipeline = "{pipeline_name}"',
            "",
            # The scenario runs measure digests against scripted answers; the phrases
            # step's own calls would consume the script (BUILD-PLAN F85).
            "[work]",
            "phrases = false",
            "",
            "[dream]",
            "quiet_minutes = 0",
            f"short_term_days = {cfg.settings.dream.short_term_days}",
        ]
        if "dream" in scenario.config_overrides:
            for k, v in scenario.config_overrides["dream"].items():
                lines.append(f"{k} = {_toml_value(v)}")
        (scenario_home / configmod.CONFIG_NAME).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )

        empty_agents = tmp / "empty_agents"
        empty_agents.mkdir(parents=True)
        orig_session_dirs = capture.SESSION_DIRS
        capture.SESSION_DIRS = {readers.CLAUDE: empty_agents, readers.PI: empty_agents}

        old_home = os.environ.get(configmod.HOME_ENV)
        git_author = os.environ.get("GIT_AUTHOR_NAME")
        git_email = os.environ.get("GIT_AUTHOR_EMAIL")
        git_committer = os.environ.get("GIT_COMMITTER_NAME")
        git_committer_email = os.environ.get("GIT_COMMITTER_EMAIL")
        try:
            os.environ[configmod.HOME_ENV] = str(scenario_home)
            os.environ["GIT_AUTHOR_NAME"] = "eval"
            os.environ["GIT_AUTHOR_EMAIL"] = "eval@example.com"
            os.environ["GIT_COMMITTER_NAME"] = "eval"
            os.environ["GIT_COMMITTER_EMAIL"] = "eval@example.com"

            scenario_cfg = configmod.load()
            vault.init(scenario_cfg)

            registry = Registry.load(scenario_cfg.registry_path)
            proj = registry.add(scenario.slug)
            registry.add_path(proj, path_key(scenario_cfg.host, scenario_repo))
            registry.save()
            vault.commit(scenario_cfg.vault, ["registry.yaml"], f"aft: project add {scenario.slug}")

            original_complete = model.complete_json
            if backend.command is not None:

                def _dispatch_complete(c, s, u, sc, m):
                    return backend.complete_json(c, s, u, sc, m)

                model.complete_json = _dispatch_complete

            try:
                for sess in scenario.sessions:
                    if sess.dream_only or not sess.transcript.is_file():
                        schedule.run(scenario_cfg, now=sess.time)
                        continue

                    event = SessionEvent(
                        agent=sess.agent,
                        kind=SESSION_END,
                        session_id=sess.session_id,
                        cwd=scenario_repo,
                        transcript_path=sess.transcript,
                        time=sess.time,
                        detail="other",
                    )
                    queue.enqueue(scenario_cfg, event)

                    work_time = sess.time + timedelta(minutes=5)
                    work.run(scenario_cfg, now=work_time)

                    dream_now = sess.dream_time or (sess.time + timedelta(hours=1))
                    schedule.run(scenario_cfg, now=dream_now)

                block = cli._injection_block(scenario_repo)
            finally:
                if backend.command is not None:
                    model.complete_json = original_complete

            score = score_block(block, scenario.current, scenario.superseded, judge)
            score["scenario"] = scenario.name
            score["description"] = scenario.description
            score["block"] = block
            return score
        except Exception as exc:
            return {
                "scenario": scenario.name,
                "description": scenario.description,
                "current_present": False,
                "superseded_absent": False,
                "contradiction": False,
                "passed": False,
                "error": str(exc),
                "current": [
                    {"value": value.value, "tokens": list(value.tokens), "present": False}
                    for value in scenario.current
                ],
                "superseded": [
                    {"value": value.value, "tokens": list(value.tokens), "present": False}
                    for value in scenario.superseded
                ],
                "block": "",
            }
        finally:
            if old_home is not None:
                os.environ[configmod.HOME_ENV] = old_home
            else:
                os.environ.pop(configmod.HOME_ENV, None)
            if git_author is not None:
                os.environ["GIT_AUTHOR_NAME"] = git_author
            else:
                os.environ.pop("GIT_AUTHOR_NAME", None)
            if git_email is not None:
                os.environ["GIT_AUTHOR_EMAIL"] = git_email
            else:
                os.environ.pop("GIT_AUTHOR_EMAIL", None)
            if git_committer is not None:
                os.environ["GIT_COMMITTER_NAME"] = git_committer
            else:
                os.environ.pop("GIT_COMMITTER_NAME", None)
            if git_committer_email is not None:
                os.environ["GIT_COMMITTER_EMAIL"] = git_committer_email
            else:
                os.environ.pop("GIT_COMMITTER_EMAIL", None)
            capture.SESSION_DIRS = orig_session_dirs


def run_scenarios(
    scenarios: list[Scenario],
    cfg: Config,
    backend: Backend,
    judge: Judge,
    variant: str = BASE_VARIANT,
) -> dict[str, Any]:
    """Run a list of scenarios and summarize totals per model and scenario."""
    results = []
    for scenario in scenarios:
        results.append(run_scenario(scenario, cfg, backend, judge, variant=variant))

    count = len(results)
    current_present = sum(1 for r in results if r["current_present"])
    contradictions = sum(1 for r in results if r["contradiction"])
    passed = sum(1 for r in results if r["passed"])

    totals = {
        "scenarios": count,
        "current_present": current_present,
        "current_rate": round(current_present / count, 3) if count else 0.0,
        "superseded_absent": count - contradictions,
        "contradictions": contradictions,
        "contradiction_rate": round(contradictions / count, 3) if count else 0.0,
        "passed": passed,
        "pass_rate": round(passed / count, 3) if count else 0.0,
    }

    return {
        "command": "current",
        "backend": backend.name,
        "variant": variant,
        "model": None if backend.command else (cfg.settings.model.name or None),
        "endpoint": None if backend.command else cfg.settings.model.endpoint,
        "judge_prompt": judge.prompt_hash,
        "digest_prompts": digest_prompt_hash(),
        "temperature": JUDGE_TEMPERATURE,
        "prompt_shapes": {backend.name: BACKEND_SHAPE[backend.name]},
        "at": datetime.now(UTC).isoformat(),
        "totals": totals,
        "scenarios": results,
    }


def print_current_report(report: dict[str, Any]) -> None:
    """Print the human-readable summary of current truth scoring."""
    backend_label = report["backend"]
    if report.get("model"):
        backend_label += f" ({report['model']})"
    t = report["totals"]
    print(f"\n{backend_label} - Current truth evaluation:")
    print(f"Scenarios: {t['scenarios']}")
    for s in report["scenarios"]:
        cur_str = "yes" if s["current_present"] else "NO"
        sup_str = "absent" if s["superseded_absent"] else "PRESENT (contradiction)"
        status = "PASS" if s["passed"] else "FAIL"
        err = f" [error: {s['error']}]" if "error" in s else ""
        print(f"  {s['scenario']}: current={cur_str}, superseded={sup_str} -> {status}{err}")
    print(
        f"Total: {t['current_present']}/{t['scenarios']} current present "
        f"({t['current_rate'] * 100:.1f}%), "
        f"{t['contradictions']}/{t['scenarios']} contradictions "
        f"({t['contradiction_rate'] * 100:.1f}% contradiction rate). "
        f"{t['passed']}/{t['scenarios']} passed."
    )


def _cmd_current(args: argparse.Namespace) -> int:
    cfg = with_model(load_config(), args.endpoint, args.model_name, args.extra_body)
    cfg = with_pipeline(cfg, args.variant)
    backend = build_backend(args.backend)
    judge = Judge(cfg, Path(args.data) / JUDGE_CACHE if args.data else None)
    scenarios_dir = args.scenarios_dir or DEFAULT_CURRENT_SCENARIOS
    scenarios = load_scenarios(scenarios_dir, args.scenario)
    if not scenarios:
        raise EvalError(f"{scenarios_dir}: no scenarios found")
    report = run_scenarios(scenarios, cfg, backend, judge, variant=args.variant)
    print_current_report(report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        _write_json(args.report, report)
    elif args.data:
        data = _data_dir(args)
        out = data / "reports" / f"{time.strftime('%Y%m%dT%H%M%S')}-current-{args.backend}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        _write_json(out, report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
