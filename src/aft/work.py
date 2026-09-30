"""``aft work``: the run that digests the queue, inside the day's budget (BUILD-PLAN M2.6).

Two steps, and the order is the point. The sweep goes first, so a session whose hook never
fired is in this run's queue rather than the next one's (DESIGN 8); then the queue itself,
one job at a time and oldest first, until the work runs out or the day's tokens are spent.
Nothing here decides what a digest says - only how far one run goes.

A run stops between jobs, never inside one. The jobs it did not reach are still queued with
their sessions' watermarks unmoved, so the next run picks them up where this one left off
(DESIGN 13: "when a budget runs out, work waits in the queue"). A job that belongs to one
session and cannot be digested is marked failed with its reason as the record of why, its
watermark unmoved, and the run moves on to the next session. A failure that would repeat for
every job left - an endpoint that is down, a vault that is not there, a queue another run has
locked - ends the run instead and leaves the jobs queued (DESIGN 14), because working through
the queue would stamp each of the day's jobs with the same sentence and leave no work behind
to retry once a person has fixed the endpoint.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from aft import capture, digest, index, model, queue, vault
from aft.config import Config
from aft.evidence import EvidenceError
from aft.gitutil import GitError
from aft.index import SearchIndexError
from aft.model import ModelError
from aft.readers import ReaderError
from aft.registry import RegistryError
from aft.vault import VaultError

# What a run reports as the reason it stopped early.
STOP_BUDGET = "the day's token budget is spent"
STOP_UNREACHABLE = "the model or the vault could not be reached"
STOP_QUEUE = "the queue could not be used"
STOP_INDEX = "the search index refused to be written"

# The queue's own failures: a row this process no longer holds, or a database another
# worker keeps locked. Both are ordinary the moment two runs overlap, and while they last
# no job can be opened or closed at all, so they belong to the run rather than to a session.
QUEUE_LOST = (queue.QueueError, sqlite3.Error)

# The failures that belong to the endpoint, the vault, or the host's own state rather than to
# a session: every job left in the queue would meet the same one, so the run ends on the first
# of them and what is left stays queued, claimed only by a lease that the next run reclaims.
# An index that will not be written - another build's rows, or a file that is not an index at
# all - is one of these: the digest is the worker's job and the index is the host's state, and
# DESIGN 14 says the host's state ends the run with its own words rather than stamping job
# after job failed for something no session caused. Its words are its own word in
# ``STOP_INDEX``, not the endpoint's, because a person who reads the timer's line cannot tell
# a model that is down from a stale index file otherwise.
STOPS_RUN = (ModelError, VaultError, GitError, RegistryError, SearchIndexError, *QUEUE_LOST)

# The failures that belong to one session: the job is marked failed with its reason and the
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


def _stop_for(exc: BaseException) -> str:
    """Which of the run's reasons this failure is told as.

    Three of them are distinct happenings and the timer's line is the only place anyone reads
    them: a queue that cannot be used, an index that refuses to be written, and an endpoint or
    vault that did not answer. The index gets its own word rather than borrowing the
    endpoint's, because "the model or the vault could not be reached" sends whoever sees it
    looking at the wrong machine (BUILD-PLAN M3.1, lead's ruling on #39).
    """
    if isinstance(exc, QUEUE_LOST):
        return STOP_QUEUE
    if isinstance(exc, SearchIndexError):
        return STOP_INDEX
    return STOP_UNREACHABLE


def _archive_worker(config: Config, job: queue.Job, failures: list[str]) -> str:
    """Park one worker session in the evidence archive and the index.

    Returns the reason the run stops, or "" when the session was parked - or when the
    failure was only this job's, which is said and left at that.
    """
    try:
        digest.archive_worker_job(config, job)
    except FAILS_JOB as exc:
        failures.append(f"job {job.id} ({job.session_id}): {exc}")
    except STOPS_RUN as exc:
        failures.append(f"job {job.id} ({job.session_id}): {exc}")
        return _stop_for(exc)
    return ""


def _archive_workers(config: Config, failures: list[str]) -> str:
    """Park every waiting worker session in the evidence archive and the index.

    A worker's words stay searchable while it waits for budget, and archiving moves no
    watermark, so the digest that budget eventually buys still reads the chunk this run
    skipped. One job's failure is that job's own; a failure that stops the run stops this
    loop too, rather than being logged once per worker (BUILD-PLAN M2.7).

    Returns the reason the run stops, or "" when every worker was parked.
    """
    for job in queue.pending_jobs(config, role="worker"):
        reason = _archive_worker(config, job, failures)
        if reason:
            return reason
    return ""


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
            job = queue.next_job(config, role="primary")
            if job is None:
                break
            try:
                if digest.digest_job(config, job, moment) is None:
                    empty += 1
                else:
                    digested += 1
            except STOPS_RUN as exc:
                # The job stays claimed only by a lease: its session's watermark has not moved,
                # so the next run reads the same chunk again once the host works again.
                failures.append(f"job {job.id} ({job.session_id}): {exc}")
                stopped = _stop_for(exc)
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

        # Worker sessions: digested only with budget left after primary sessions. With none
        # left they are archived and indexed without digesting, which spends no token - so it
        # happens even when the endpoint is down or a primary stopped the run, because no
        # reason to call a model is a reason to leave a worker's words out of the archive.
        # Only a queue that cannot be used stops this: nothing can be read or written then.
        if stopped != STOP_QUEUE:
            if stopped or model.tokens_used_today(config, moment) >= budget:
                stopped = _archive_workers(config, failures) or stopped or STOP_BUDGET
            else:
                # A worker session the archive stored on an earlier run is owed a digest, and
                # this is the moment the design gives it one: the primaries are done and the
                # day's budget still has room. Filing those debts as rows is what lets the
                # loop below claim them like any other work (BUILD-PLAN M2.7).
                queue.redeem_digest_debts(config)
                while True:
                    if model.tokens_used_today(config, moment) >= budget:
                        stopped = _archive_workers(config, failures) or STOP_BUDGET
                        break
                    job = queue.next_job(config, role="worker")
                    if job is None:
                        break
                    try:
                        if digest.digest_job(config, job, moment) is None:
                            empty += 1
                        else:
                            digested += 1
                    except STOPS_RUN as exc:
                        failures.append(f"job {job.id} ({job.session_id}): {exc}")
                        stopped = _stop_for(exc)
                        if stopped != STOP_QUEUE:
                            # A model that will not answer is no reason to leave a worker's
                            # words out of the archive: storing them spends no token, and this
                            # job's text was read as far as the digest could get with it.
                            _archive_worker(config, job, failures)
                            _archive_workers(config, failures)
                        break
                    except FAILS_JOB as exc:
                        failures.append(f"job {job.id} ({job.session_id}): {exc}")
                        try:
                            queue.fail(config, job, str(exc))
                        except QUEUE_LOST as lost:
                            failures.append(f"the queue: {lost}")
                            stopped = STOP_QUEUE
                            break

        # What this run committed is indexed here, and what the index could not read is said
        # out loud rather than dropped: the digest step's own update runs before its commit,
        # so the page just written is only searchable after this one, and a report line that
        # nobody read would leave a file unreadable on every run without ever naming it.
        try:
            failures += [f"the index: {line}" for line in index.update(config)]
        except SearchIndexError as exc:
            # The digest is committed and the queue is settled; an index this run cannot
            # write - another build's stamp, a file that is not the index - is reported and
            # does not undo the work (DESIGN 14, BUILD-PLAN M3.1).
            failures.append(f"the index: {exc}")
    except QUEUE_LOST as exc:
        failures.append(f"the queue: {exc}")
        stopped = STOP_QUEUE
    return Report(
        swept=swept, digested=digested, empty=empty, failures=tuple(failures), stopped=stopped
    )
