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
    reports/<stamp>-<variant>-<backend>.json  what a run scored against the key

Each call record holds the answer as the model returned it, before the script's checks,
with the token usage the endpoint reported. Each session summary holds what the pipeline
kept, what it refused, its parts, its summaries and handoff, the tokens and the time.
``compare`` matches facts across backends by quote overlap for the lead to settle into the
key; ``score`` scores a run against the key: wrong facts, missed facts, wrong tags,
questions recorded as decisions, and tokens. A synthetic session with no key file is
scored against its own ``expected.json``.

The variant ``base`` is today's pipeline; a later variant scores against the same key, so
the two runs' numbers are comparable.
"""

from __future__ import annotations

import argparse
import json
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

    def __call__(self, cfg: Config, system: str, user: str, schema: dict, max_tokens: int) -> dict:
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
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
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
# Scoring a run against the key


def score_run(data: Path, variant: str, backend: str, synthetic: Path) -> dict:
    """Score one backend's run of one variant against the key, and write a dated report.

    A run fact that matches no key fact is wrong; a key fact no run fact matches was
    missed; a matched pair whose source, kind, term or scope disagree is a wrong tag; a
    kept fact whose kind is a decision and whose quote asks a question is a question
    recorded as a decision. Tokens and time come from the run's own records.
    """
    if backend not in BACKENDS:
        raise EvalError(f"unknown backend {backend!r}; the backends are {', '.join(BACKENDS)}")
    root = backend_root(data, backend, variant)
    if not root.is_dir():
        raise EvalError(f"no answers for backend {backend!r} under {root}")
    session_dirs = [path for path in sorted(root.iterdir()) if (path / "summary.json").is_file()]
    if not session_dirs:
        raise EvalError(f"no session summaries for backend {backend!r} under {root}")
    sessions = []
    unkeyed = []
    totals = {
        "sessions": 0,
        "facts_kept": 0,
        "key_facts": 0,
        "wrong_facts": 0,
        "missed_facts": 0,
        "wrong_tags": 0,
        "question_decisions": 0,
        "refused_facts": 0,
        "refused_parts": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "seconds": 0.0,
    }
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
        scored = score_session(summary, key)
        scored["session"] = session_id
        scored["key_from"] = source
        sessions.append(scored)
        totals["sessions"] += 1
        totals["facts_kept"] += len(summary.get("facts", []))
        totals["key_facts"] += len(key)
        for name in ("wrong_facts", "missed_facts", "wrong_tags", "question_decisions"):
            totals[name] += scored[name]
        totals["refused_facts"] += len(summary.get("dropped", []))
        totals["refused_parts"] += len(summary.get("refused_parts", []))
        usage = summary.get("usage") or {}
        totals["prompt_tokens"] += usage.get("prompt_tokens", 0)
        totals["completion_tokens"] += usage.get("completion_tokens", 0)
        totals["seconds"] += summary.get("seconds", 0.0)
    report = {
        "at": datetime.now(UTC).isoformat(),
        "variant": variant,
        "backend": backend,
        "totals": totals,
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


def score_session(summary: dict, key: list[dict]) -> dict:
    """One session's numbers against its key facts."""
    run_facts = [fact for fact in summary.get("facts", []) if fact.get("quote")]
    groups = match_facts({KEY_BACKEND: key, summary["backend"]: run_facts})
    wrong = [group.facts[summary["backend"]] for group in groups if KEY_BACKEND not in group.facts]
    missed = [group.facts[KEY_BACKEND] for group in groups if summary["backend"] not in group.facts]
    tags = []
    for group in groups:
        run_fact = group.facts.get(summary["backend"])
        key_fact = group.facts.get(KEY_BACKEND)
        if run_fact is None or key_fact is None:
            continue
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
    usage = summary.get("usage") or {}
    return {
        "facts_kept": len(run_facts),
        "key_facts": len(key),
        "wrong_facts": len(wrong),
        "missed_facts": len(missed),
        "wrong_tags": len(tags),
        "question_decisions": len(questions),
        "wrong": wrong,
        "missed": missed,
        "tags": tags,
        "questions": questions,
        "refused_facts": len(summary.get("dropped", [])),
        "refused_parts": len(summary.get("refused_parts", [])),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "seconds": summary.get("seconds", 0.0),
        "nothing_to_digest": summary.get("nothing_to_digest", False),
        "fully_refused": summary.get("fully_refused", False),
    }


def _print_report(report: dict) -> None:
    totals = report["totals"]
    print(
        f"{report['backend']} ({report['variant']}): {totals['sessions']} sessions, "
        f"{totals['facts_kept']} facts kept against {totals['key_facts']} key facts: "
        f"{totals['wrong_facts']} wrong, {totals['missed_facts']} missed, "
        f"{totals['wrong_tags']} wrong tags, {totals['question_decisions']} questions "
        f"as decisions, {totals['refused_facts']} refused facts, "
        f"{totals['refused_parts']} refused parts, "
        f"tokens={totals['prompt_tokens'] + totals['completion_tokens']}, "
        f"{totals['seconds']:.1f}s",
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
_STATEMENT_WORD_MIN = 4


def _content_words(text: str) -> set[str]:
    return {
        w for w in quote_tokens(text or "") if len(w) >= _STATEMENT_WORD_MIN and w not in _COMMON
    }


def same_item(left: dict, right: dict) -> bool:
    """Whether two items name the same settled thing: their quotes overlap to half their
    words, or their statements share ``STATEMENT_MATCH_RATIO`` of their content words."""
    if left.get("quote") and right.get("quote"):
        if quote_overlap(left["quote"], right["quote"]) >= 0.5:
            return True
    return (
        statement_overlap(left.get("statement", ""), right.get("statement", ""))
        >= STATEMENT_MATCH_RATIO
    )


def score_turns(data: Path, variant: str, backend: str) -> dict:
    """A run's facts against the per-turn key: of the key's durable items, how many the run
    found (any source), and how many of the run's [stated] facts the key does not hold. The
    key labels the user's turns only, so [observed] facts are counted but not judged."""
    key_path = data / KEY_TURNS
    if not key_path.exists():
        raise EvalError(f"{key_path}: no per-turn key")
    key = json.loads(key_path.read_text(encoding="utf-8"))
    expected: dict[str, list[dict]] = {}
    for name, turn in key.items():
        expected.setdefault(name.rsplit("#", 1)[0], []).extend(turn["durable"])
    totals = {
        "sessions": 0,
        "key_items": 0,
        "found": 0,
        "stated": 0,
        "stated_wrong": 0,
        "observed": 0,
    }
    sessions = []
    for session_id, items in sorted(expected.items()):
        summary_path = run_dir(data, backend, variant, session_id) / "summary.json"
        if not summary_path.exists():
            continue
        facts = json.loads(summary_path.read_text(encoding="utf-8")).get("facts", [])
        stated = [fact for fact in facts if fact.get("source") == "stated"]
        found = [item for item in items if any(same_item(item, fact) for fact in facts)]
        wrong = [fact for fact in stated if not any(same_item(fact, item) for item in items)]
        totals["sessions"] += 1
        totals["key_items"] += len(items)
        totals["found"] += len(found)
        totals["stated"] += len(stated)
        totals["stated_wrong"] += len(wrong)
        totals["observed"] += sum(1 for fact in facts if fact.get("source") == "observed")
        sessions.append(
            {
                "session": session_id,
                "key_items": len(items),
                "found": len(found),
                "stated": len(stated),
                "wrong": [fact["statement"] for fact in wrong],
            }
        )
    return {"variant": variant, "backend": backend, "totals": totals, "sessions": sessions}


def _cmd_score_turns(args: argparse.Namespace) -> int:
    data = _data_dir(args)
    report = score_turns(data, args.variant, args.backend)
    t = report["totals"]
    out = (
        data
        / "reports"
        / f"{time.strftime('%Y%m%dT%H%M%S')}-turns-{args.variant}-{args.backend}.json"
    )
    _write_json(out, report)
    print(
        f"{args.variant}/{args.backend}: found {t['found']} of {t['key_items']} key items;"
        f" {t['stated_wrong']} of {t['stated']} [stated] facts not in the key;"
        f" {t['observed']} [observed] facts, not judged by this key; report {out}"
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
    score_run(_data_dir(args), args.variant, args.backend, _synthetic_dir(args))
    return 0


# ---------------------------------------------------------------------------
# F60: current truth evaluation (BUILD-PLAN F60)


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
    current: tuple[str, ...]
    superseded: tuple[str, ...]
    sessions: tuple[ScenarioSession, ...]
    slug: str = "afterthought"
    config_overrides: dict[str, Any] = field(default_factory=dict)


def score_block(block: str, current: Sequence[str], superseded: Sequence[str]) -> dict[str, Any]:
    """Score a memory block against expected current and superseded phrases.

    The block states each current value, and holds no superseded one.
    A superseded value appearing in the block is recorded as a contradiction.
    """
    found_current = [item for item in current if item in block]
    missed_current = [item for item in current if item not in block]
    found_superseded = [item for item in superseded if item in block]
    current_present = len(missed_current) == 0
    superseded_absent = len(found_superseded) == 0
    contradiction = not superseded_absent
    passed = current_present and superseded_absent
    return {
        "current_present": current_present,
        "superseded_absent": superseded_absent,
        "contradiction": contradiction,
        "passed": passed,
        "current": list(current),
        "superseded": list(superseded),
        "found_current": found_current,
        "missed_current": missed_current,
        "found_superseded": found_superseded,
    }


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

    def _to_tuple(val: object) -> tuple[str, ...]:
        if val is None:
            return ()
        if isinstance(val, str):
            return (val,)
        if isinstance(val, list):
            return tuple(str(x) for x in val)
        return ()

    current = _to_tuple(raw.get("current"))
    superseded = _to_tuple(raw.get("superseded"))
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

            score = score_block(block, scenario.current, scenario.superseded)
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
                "current": list(scenario.current),
                "superseded": list(scenario.superseded),
                "found_current": [],
                "missed_current": list(scenario.current),
                "found_superseded": [],
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
    variant: str = BASE_VARIANT,
) -> dict[str, Any]:
    """Run a list of scenarios and summarize totals per model and scenario."""
    results = []
    for scenario in scenarios:
        results.append(run_scenario(scenario, cfg, backend, variant=variant))

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
    scenarios_dir = args.scenarios_dir or DEFAULT_CURRENT_SCENARIOS
    scenarios = load_scenarios(scenarios_dir, args.scenario)
    if not scenarios:
        raise EvalError(f"{scenarios_dir}: no scenarios found")
    report = run_scenarios(scenarios, cfg, backend, variant=args.variant)
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
