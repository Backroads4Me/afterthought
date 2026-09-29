"""Capture: the sweep that finds the sessions no hook ever reported.

Work reaches the queue two ways (DESIGN 8). A hook fires and hands one event to
`queue.enqueue`, which writes a row and returns inside its budget. A timer runs `sweep`,
which walks both agents' session directories and enqueues what the hooks missed: a
session whose hook never fired, and a long-lived session that has grown enough to be
worth digesting in a chunk while it is still going.

`enqueue` opens no transcript, so the hook stays cheap and never blocks on a file an
agent is still writing. The sweep does read a session file, but never its conversation:
the first records, for the directory the session was started in, and the end of it, for
the watermark's record.

Nothing here resolves a session to a project, reads a transcript's entries, filters, or
advances a watermark. Resolution and digestion are the digest step's, and the watermark
moves only when a digest is written (BUILD-PLAN M2.2).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft.config import Config
from aft.events import COMPACTION, SESSION_END, SessionEvent
from aft.queue import enqueue, ensure_schema, last_sweep, record_sweep, session_state
from aft.readers import CLAUDE, PI

# Where each agent keeps its session files, and the layout of one: Claude Code writes
# ``<session id>.jsonl`` under a directory named for the working directory, and pi writes
# ``<timestamp>_<session id>.jsonl`` under a directory named the same way. Both layouts
# are recorded in `aft.readers`. These are module constants rather than configuration
# because they are facts about the two agents, not choices afterthought makes.
SESSION_DIRS = {
    CLAUDE: Path("~/.claude/projects"),
    PI: Path("~/.pi/agent/sessions"),
}

# A session that has not written for this long is done talking, whatever its hook said.
IDLE_AFTER = timedelta(minutes=30)

# A session that has added this much past its watermark holds enough new conversation to
# be worth a digest now, rather than whenever it happens to end (DESIGN 8: running
# sessions are processed in chunks so long-lived ones are not digested all at once).
GROWTH_AFTER = 200 * 1024

# The key each agent files a record's own id under: pi writes ``id``, Claude Code
# ``uuid``. A watermark is looked up by that key because the same id is quoted inside
# other records as a reference (pi's ``parentId``, Claude Code's ``parentUuid``), and a
# reference to a record is not the record itself.
RECORD_ID_KEYS = {PI: b'"id"', CLAUDE: b'"uuid"'}

# The record budget of the head scan that recovers a session's working directory. Both
# agents record it early: pi in its opening session record, Claude Code in its first
# conversation records.
CWD_SCAN_RECORDS = 50

# What a swept session says about what set its capture off. The agent fired nothing; the
# sweep found the session, and the kind records which of its two tests the session met.
SWEEP_DETAIL = "sweep"


def sweep(config: Config, now: datetime) -> list[SessionEvent]:
    """Enqueue the sessions of both agents that this sweep window turned up.

    A session is capture work when the queue has no work in flight for it and it has
    either gone quiet or outgrown its watermark, and only for files touched since the
    last sweep: an untouched session cannot have new conversation in it.

    Before the first sweep there is no window, and every transcript on the host would
    fill it, spending the model budget on sessions that predate afterthought. So the
    first sweep records where the window starts and enqueues nothing (BUILD-PLAN M2.2).

    A swept session's event carries the time its transcript last moved, not the time of
    the sweep, so that later quiet-period checks measure session activity (DESIGN 9).
    """
    since = last_sweep(config)
    if since is None:
        record_sweep(config, now)
        return []
    ensure_schema(config)
    found: list[SessionEvent] = []
    for agent, directory in SESSION_DIRS.items():
        for path in _session_files(directory):
            try:
                event = _candidate(config, agent, path, since, now)
            except OSError:
                # An agent's own cleanup can remove a transcript between the scan and the
                # read of it. The session it held is gone, and the sweep moves on.
                continue
            if event is None:
                continue
            enqueue(config, event)
            found.append(event)
    record_sweep(config, now)
    return found


def _candidate(
    config: Config, agent: str, path: Path, since: datetime, now: datetime
) -> SessionEvent | None:
    """The event one session file earns from this sweep, or None for no work.

    The checks run cheapest first: a timestamp, the queue, and only then the file.
    """
    touched = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    if touched <= since:
        return None
    session_id = _session_id(agent, path)
    state = session_state(config, agent, session_id)
    if state.has_open_job:
        return None
    idle = now - touched >= IDLE_AFTER
    if not idle and not _grown_since_watermark(path, state.watermark, agent):
        return None
    cwd = _cwd_of(path)
    if cwd is None:
        # Nothing in the file names the directory it was started in, so there is no cwd
        # for the digest to resolve and no event to hand it (BUILD-PLAN M2.6).
        return None
    return SessionEvent(
        agent=agent,
        # A session that has gone quiet is over, whatever its end hook did; one that is
        # merely large is mid-conversation, which is what a compaction captures.
        kind=SESSION_END if idle else COMPACTION,
        session_id=session_id,
        cwd=cwd,
        transcript_path=path,
        time=touched,
        detail=SWEEP_DETAIL,
    )


def _session_files(directory: Path) -> list[Path]:
    """Every session file of one agent, in a name order that keeps runs comparable.
    A directory that is not there is an agent that is not installed, not an error: the
    sweep runs for both and reports on neither.
    """
    root = directory.expanduser()
    return sorted(root.glob("*/*.jsonl")) if root.is_dir() else []


def _session_id(agent: str, path: Path) -> str:
    """The session a file holds the transcript of, from its name.

    Claude Code names the file after the session; pi prefixes its start time to pi's own
    session id. Both layouts are the ones `aft.readers` reads.
    """
    stem = path.stem
    if agent == CLAUDE:
        return stem
    return stem.partition("_")[2] or stem


def _cwd_of(path: Path) -> Path | None:
    """The working directory a session file names in its own opening records.

    A directory name cannot answer this: each agent folds the separators of the working
    directory into one dash, which is a name that cannot be read back.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        for index, line in enumerate(handle):
            if index >= CWD_SCAN_RECORDS:
                break
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and isinstance(record.get("cwd"), str):
                cwd = record["cwd"]
                if cwd:
                    return Path(cwd)
    return None


def _grown_since_watermark(path: Path, watermark: str | None, agent: str) -> bool:
    """Whether a transcript has grown past the record its watermark names.

    The watermark is the last entry a digest read, so its record sits near the end of the
    file as that digest saw it. The check therefore reads the last `GROWTH_AFTER` bytes
    and asks whether the record is missing from them: a record that is not there is one
    `GROWTH_AFTER` older than the end of the file, or one this file never held. The
    second case is a session continued into a new file, which a reader answers by reading
    it whole (`aft.readers.read`), so the whole file counts as grown.

    A watermark set on a tool result names that result inside a record
    (`readers/claude.py`), so the record's own id is the part to look for, anchored on
    the key that holds it: the same id is quoted as a reference inside other records -
    pi's `parentId` after a `/tree` jump, Claude Code's `parentUuid` - and a reference to
    a record does not answer for the record itself.
    """
    size = path.stat().st_size
    if size < GROWTH_AFTER:
        return False
    if watermark is None:
        return True
    with open(path, "rb") as handle:
        handle.seek(size - GROWTH_AFTER)
        tail = handle.read()
    record = re.escape(watermark.partition("#")[0].encode())
    return re.search(RECORD_ID_KEYS[agent] + rb"\s*:\s*\"" + record + rb"\"", tail) is None
