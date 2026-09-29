"""``aft work``: the run that digests the queue, inside the day's budget (BUILD-PLAN M2.6).

Two steps, and the order is the point. The sweep goes first, so a session whose hook never
fired is in this run's queue rather than the next one's (DESIGN 8); then the queue itself,
one job at a time and oldest first, until the work runs out or the day's tokens are spent.
Nothing here decides what a digest says - only how far one run goes.

A run stops between jobs, never inside one. The jobs it did not reach are still queued with
their sessions' watermarks unmoved, so the next run picks them up where this one left off
(DESIGN 13: "when a budget runs out, work waits in the queue"). A job that belongs to one
session and cannot be digested is left behind as the record of why, and the run moves on to
the next session. A failure that would repeat for every job left - an endpoint that is down,
a vault that is not there, a queue another run has locked - ends the run instead, because
working through the queue would stamp each of the day's jobs with the same sentence and
leave no work behind to retry once a person has fixed the endpoint (DESIGN 14).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from aft import capture, digest, model, queue, vault
from aft.config import Config
from aft.evidence import EvidenceError
from aft.gitutil import GitError
from aft.model import ModelError
from aft.readers import ReaderError
from aft.registry import RegistryError
from aft.vault import VaultError

# What a run reports as the reason it stopped early.
STOP_BUDGET = "the day's token budget is spent"
STOP_UNREACHABLE = "the model or the vault could not be reached"
STOP_QUEUE = "the queue could not be used"

# The queue's own failures: a row this process no longer holds, or a database another
# worker keeps locked. Both are ordinary the moment two runs overlap, and while they last
# no job can be opened or closed at all, so they belong to the run rather than to a session.
QUEUE_LOST = (queue.QueueError, sqlite3.Error)

# The failures that belong to the endpoint or the vault rather than to a session: every job
# left in the queue would meet the same one, so the run ends on the first of them and what is
# left stays queued, claimed only by a lease that the next run reclaims.
STOPS_RUN = (ModelError, VaultError, GitError, RegistryError, *QUEUE_LOST)

# The failures that belong to one session: the job is left behind with its reason and the
# next session gets its turn.
FAILS_JOB = (digest.DigestError, ReaderError, EvidenceError, OSError)


@dataclass(frozen=True)
class Report:
    """One run of the queue, as the command, the timer's log and a test all read it."""

    swept: int = 0
    digested: int = 0
    empty: int = 0
    failures: tuple[str, ...] = ()
    stopped: str = ""

    def line(self) -> str:
        """The one line both a person and a log read."""
        said = [
            f"{self.swept} swept",
            f"{self.digested} digested",
            f"{self.empty} with nothing to digest",
        ]
        if self.failures:
            said.append(f"{len(self.failures)} failed")
        if self.stopped:
            said.append(f"stopped: {self.stopped}")
        return ", ".join(said)


def run(config: Config, now: datetime | None = None) -> Report:
    """Sweep for the sessions no hook reported, then digest queued jobs until told to stop.

    A missing vault stops here rather than failing every job in the queue against it.
    ``now`` is the moment the run measures itself at: the sweep's window and the day the
    budget counts are both taken from it, so a run and a test can name the same instant.
    """
    vault.require(config)
    moment = now or datetime.now(UTC)
    swept = digested = empty = 0
    failures: list[str] = []
    stopped = ""
    budget = config.settings.work.daily_token_budget
    # The sweep, the claim and the close all touch the queue's own database, and a run that
    # cannot use it reports the queue's words on the line the timer reads rather than dying
    # with a traceback (DESIGN 14).
    try:
        swept = len(capture.sweep(config, moment))
        while True:
            if model.tokens_used_today(config, moment) >= budget:
                stopped = STOP_BUDGET
                break
            job = queue.next_job(config)
            if job is None:
                break
            try:
                if digest.digest_job(config, job, moment) is None:
                    empty += 1
                else:
                    digested += 1
            except STOPS_RUN as exc:
                # The job stays claimed only by a lease: its session's watermark has not moved,
                # so the next run reads the same chunk again once the endpoint answers.
                failures.append(f"job {job.id} ({job.session_id}): {exc}")
                stopped = STOP_QUEUE if isinstance(exc, QUEUE_LOST) else STOP_UNREACHABLE
                break
            except FAILS_JOB as exc:
                failures.append(f"job {job.id} ({job.session_id}): {exc}")
                try:
                    queue.fail(config, job, str(exc))
                except QUEUE_LOST as lost:
                    # Writing a failure down is a queue call like the sweep and the close: while
                    # the queue cannot be used no row can be left behind, so the run stops with
                    # its own reason rather than dying in the middle of reporting (DESIGN 14).
                    failures.append(f"the queue: {lost}")
                    stopped = STOP_QUEUE
                    break
    except QUEUE_LOST as exc:
        failures.append(f"the queue: {exc}")
        stopped = STOP_QUEUE
    return Report(
        swept=swept, digested=digested, empty=empty, failures=tuple(failures), stopped=stopped
    )
