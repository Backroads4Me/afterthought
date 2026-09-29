"""Capture: the sweep that finds the sessions no hook ever reported.

Work reaches the queue two ways (DESIGN 8). A hook fires and hands one event to
`queue.enqueue`, which writes a row and returns inside its budget. A timer runs `sweep`,
which walks both agents' session directories and enqueues what the hooks missed: a
session whose hook never fired, and a long-lived session that has grown enough to be
worth digesting in a chunk while it is still going.

`enqueue` opens no transcript, so the hook stays cheap and never blocks on a file an
agent is still writing. The sweep reads a session file itself: the records at its head,
for the directory the session was started in, and its entries past the watermark, to
ask whether anything is left to digest.

Nothing here resolves a session to a project, filters, digests, or advances a
watermark. Resolution and digestion are the digest step's, and the watermark moves
only when a digest is written (BUILD-PLAN M2.2).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aft import readers
from aft.config import Config
from aft.events import COMPACTION, SESSION_END, SessionEvent
from aft.queue import (
    SessionState,
    begin_watching,
    enqueue,
    note_observed,
    session_states,
    watching_since,
)
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

# How far back the sweep looks. A session whose file has not moved within this window is
# left alone whatever its watermark says (BUILD-PLAN M2.2).
SESSION_WINDOW = timedelta(days=7)

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
    """Enqueue the sessions of both agents that this sweep found uncaptured.

    Each session is compared with its own watermark, not with when the last sweep ran, so
    a session whose hook never fired is found whatever the timer's cadence (BUILD-PLAN
    M2.2). It is capture work when the queue holds no work in flight for it and is not
    holding it off another try on a widening pause, its file has moved within the last
    seven days, and it holds entries past its watermark that it has either gone quiet on
    for 30 minutes or outgrown by 200 KB.

    Before this home has ever been watched there is nothing to measure its sessions
    against, and up to seven days of sessions that predate afterthought would fill the
    queue. So the opening sweep records that the watching has begun and captures nothing,
    and from then on a session the queue has never digested is capture work only once its
    file has moved (BUILD-PLAN M2.2).

    The queue is read once for the whole walk, whatever files it walks: every session is
    asked the same questions of the same rows, and a file goes to the reader only once its
    bytes have moved since something last read them to their end (BUILD-PLAN M2.2).

    A swept session's event carries the time its transcript last moved, not the time of
    the sweep, so that later quiet-period checks measure session activity (DESIGN 9).
    """
    watching = watching_since(config)
    if watching is None:
        begin_watching(config, now)
        return []
    states = session_states(config)
    found: list[SessionEvent] = []
    for agent, directory in SESSION_DIRS.items():
        for path in _session_files(directory):
            try:
                event = _candidate(config, agent, path, now, watching, states)
            except OSError:
                # An agent's own cleanup can remove a transcript between the scan and the
                # read of it. The session it held is gone, and the sweep moves on.
                continue
            if event is None:
                continue
            enqueue(config, event)
            found.append(event)
    return found


def _candidate(
    config: Config,
    agent: str,
    path: Path,
    now: datetime,
    watching: datetime,
    states: dict[tuple[str, str], SessionState],
) -> SessionEvent | None:
    """The event one session file earns from this sweep, or None for no work.

    The checks run cheapest first: the file's own timestamps, the queue's facts that the
    sweep read once for the whole walk, a read of the file's tail, the entries inside it,
    and last the head scan that finds its directory.
    """
    stat = path.stat()
    touched = datetime.fromtimestamp(stat.st_mtime, UTC)
    if now - touched > SESSION_WINDOW:
        # Past the seven days the sweep looks over, whatever its watermark holds.
        return None
    session_id = _session_id(agent, path)
    state = states.get((agent, session_id), SessionState())
    if state.has_open_job:
        return None
    if state.waiting_on_retry(now):
        # A chunk of this session did not land, so it waits out the pause that failure set:
        # doubling with every try, then RETRY_INTERVAL apart for as long as its chunks keep
        # failing. It is named on `attention_needed` where a person can read why (BUILD-PLAN
        # M2.1, M2.2).
        return None
    if state.watermark is None and touched <= watching:
        # Nothing has ever digested this session and its file predates the watching, so it
        # is history from before afterthought rather than a hook it missed.
        return None
    if state.seen == (stat.st_mtime, stat.st_size):
        # Something already read these same bytes to their end and called them finished. The
        # observation the queue holds is the file's modification time and size, and a
        # transcript only ever grows, so the same pair holds the same answer - and parsing
        # them is the sweep's one expensive check.
        return None
    idle = now - touched >= IDLE_AFTER
    if not idle and not _grown_since_watermark(path, stat.st_size, state.watermark, agent):
        return None
    if not _has_new_entries(agent, path, state.watermark):
        # The watermark names the session's last entry, so there is nothing left to digest
        # however long the session has been quiet or however big the file has become.
        note_observed(config, agent, session_id, stat.st_mtime, stat.st_size)
        return None

    cwd = _cwd_of(path)
    if cwd is None:
        # Nothing in the file names the directory it was started in, so there is no cwd
        # for the digest to resolve and no event to hand it (BUILD-PLAN M2.6).
        note_observed(config, agent, session_id, stat.st_mtime, stat.st_size)
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


def _grown_since_watermark(path: Path, size: int, watermark: str | None, agent: str) -> bool:
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
    if size < GROWTH_AFTER:
        return False
    if watermark is None:
        return True
    with open(path, "rb") as handle:
        handle.seek(size - GROWTH_AFTER)
        tail = handle.read()
    record = re.escape(watermark.partition("#")[0].encode())
    return re.search(RECORD_ID_KEYS[agent] + rb"\s*:\s*\"" + record + rb"\"", tail) is None


def _has_new_entries(agent: str, path: Path, watermark: str | None) -> bool:
    """Whether a session holds entries its watermark does not answer for.

    What an entry is, and the order they come in, belongs to the readers, so this asks
    `aft.readers.read` for the entries after the watermark and stops at the first one it
    answers with. The check above is one of size; this is the one of content, and a
    session whose watermark already names its last entry is no work however long it has
    sat quiet (BUILD-PLAN M2.2).

    A transcript the reader cannot read is capture work rather than a session with
    nothing to say: the file holds something a digest has to account for, and the job row
    is how that failure reaches the report (BUILD-PLAN M2.1, DESIGN 14).
    """
    try:
        return next(readers.read(agent, path, watermark), None) is not None
    except readers.ReaderError:
        return True
