"""``aft run``: queue processing, poison job quarantine, and retry limits."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from aft import capture, model, queue, work
from aft.config import Config
from aft.events import SESSION_END, SessionEvent
from aft.readers import CLAUDE

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def queue_event(
    session_id: str = "s1",
    agent: str = CLAUDE,
    kind: str = SESSION_END,
    transcript: str = "/home/user/.claude/projects/-home-ted/s1.jsonl",
    at: datetime = NOW,
) -> SessionEvent:
    return SessionEvent(
        agent=agent,
        kind=kind,
        session_id=session_id,
        cwd=Path("/home/user/dev/app"),
        transcript_path=Path(transcript),
        time=at,
        detail=None,
    )


def query_jobs(config: Config, session_id: str) -> list[tuple]:
    conn = sqlite3.connect(config.home / queue.QUEUE_NAME)
    try:
        return conn.execute(
            "SELECT status, attempts, error FROM jobs WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def test_a_run_in_cooldown_exits_early_without_touching_the_queue(cfg, monkeypatch):
    model.set_cooldown(cfg, "cannot reach http://endpoint: connection refused")

    def forbidden(*args, **kwargs):
        raise AssertionError("a run in cooldown must not reach the sweep or the queue")

    monkeypatch.setattr(capture, "sweep", forbidden)
    monkeypatch.setattr(queue, "next_job", forbidden)
    report = work.run(cfg)
    assert (report.swept, report.digested, report.empty) == (0, 0, 0)
    assert report.failures == ()
    assert "cooldown" in report.stopped
    assert "connection refused" in report.stopped


def test_the_run_stop_line_names_when_the_cooldown_ends(cfg):
    model.set_cooldown(cfg, "the endpoint answered 500")
    report = work.run(cfg)
    line = report.line()
    assert line.startswith("0 swept, 0 digested, 0 with nothing to digest, stopped:")
    assert "in cooldown until" in line
    assert "the endpoint answered 500" in line


def test_fatal_400_chunk_error_is_quarantined_and_subsequent_jobs_are_processed(
    initialized: Config, monkeypatch
) -> None:
    """A job hitting a 400 context error is quarantined/failed and subsequent jobs are
    processed (H6)."""
    cfg = initialized
    monkeypatch.setattr(work.capture, "sweep", lambda config, moment: [])
    queue.enqueue(cfg, queue_event(session_id="poison-400"))
    queue.enqueue(cfg, queue_event(session_id="valid-job"))
    with queue._writing(cfg) as conn:
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T10:00:00+00:00' WHERE session_id = ?",
            ("poison-400",),
        )
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T11:00:00+00:00' WHERE session_id = ?",
            ("valid-job",),
        )

    def mock_digest(config: Config, job: queue.Job, moment: datetime):
        if job.session_id == "poison-400":
            raise model.ModelError("http://endpoint returned 400: context length exceeded")
        queue.complete(config, job, "entry-1", off_record=False)
        return object()

    monkeypatch.setattr(work.digest, "digest_job", mock_digest)
    report = work.run(cfg)

    assert report.digested == 1
    assert len(report.failures) == 1
    assert "poison-400" in report.failures[0]
    assert "400" in report.failures[0]
    assert report.stopped == ""

    rows = query_jobs(cfg, "poison-400")
    assert rows[0][0] == queue.FAILED

    valid_rows = query_jobs(cfg, "valid-job")
    assert valid_rows[0][0] == queue.DONE


def test_fatal_413_and_422_errors_are_quarantined_and_allow_progress(
    initialized: Config, monkeypatch
) -> None:
    """413 Payload Too Large and 422 Unprocessable Entity errors fail only that job (H6)."""
    cfg = initialized
    monkeypatch.setattr(work.capture, "sweep", lambda config, moment: [])
    queue.enqueue(cfg, queue_event(session_id="payload-413"))
    queue.enqueue(cfg, queue_event(session_id="unprocessable-422"))
    queue.enqueue(cfg, queue_event(session_id="valid-job"))
    with queue._writing(cfg) as conn:
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T10:00:00+00:00' WHERE session_id = ?",
            ("payload-413",),
        )
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T11:00:00+00:00' WHERE session_id = ?",
            ("unprocessable-422",),
        )
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T12:00:00+00:00' WHERE session_id = ?",
            ("valid-job",),
        )

    def mock_digest(config: Config, job: queue.Job, moment: datetime):
        if job.session_id == "payload-413":
            raise model.ModelError("http://endpoint returned 413: payload too large")
        if job.session_id == "unprocessable-422":
            raise model.ModelError("http://endpoint returned 422: unprocessable entity")
        queue.complete(config, job, "entry-1", off_record=False)
        return object()

    monkeypatch.setattr(work.digest, "digest_job", mock_digest)
    report = work.run(cfg)

    assert report.digested == 1
    assert len(report.failures) == 2
    assert query_jobs(cfg, "payload-413")[0][0] == queue.FAILED
    assert query_jobs(cfg, "unprocessable-422")[0][0] == queue.FAILED
    assert query_jobs(cfg, "valid-job")[0][0] == queue.DONE


def test_transient_error_failing_three_times_is_marked_failed_and_unblocks_queue(
    initialized: Config, monkeypatch
) -> None:
    """A job failing 3 times is marked failed and no longer blocks the queue (H6)."""
    cfg = initialized
    monkeypatch.setattr(work.capture, "sweep", lambda config, moment: [])
    queue.enqueue(cfg, queue_event(session_id="timeout-job"))
    queue.enqueue(cfg, queue_event(session_id="healthy-job"))
    with queue._writing(cfg) as conn:
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T10:00:00+00:00' WHERE session_id = ?",
            ("timeout-job",),
        )
        conn.execute(
            "UPDATE jobs SET created_time = '2026-10-01T11:00:00+00:00' WHERE session_id = ?",
            ("healthy-job",),
        )

    def mock_digest(config: Config, job: queue.Job, moment: datetime):
        if job.session_id == "timeout-job":
            raise model.ModelError("the model endpoint timed out")
        queue.complete(config, job, "entry-1", off_record=False)
        return object()

    monkeypatch.setattr(work.digest, "digest_job", mock_digest)

    def expire_lease(session_id: str):
        with queue._writing(cfg) as conn:
            conn.execute(
                "UPDATE jobs SET updated_time = '2026-10-01T00:00:00+00:00' WHERE session_id = ?",
                (session_id,),
            )

    # Attempt 1
    rep1 = work.run(cfg)
    assert rep1.digested == 0
    assert rep1.stopped == work.STOP_UNREACHABLE
    assert query_jobs(cfg, "timeout-job")[0][0] == queue.RUNNING
    assert query_jobs(cfg, "timeout-job")[0][1] == 1

    # Attempt 2 (after lease expires)
    expire_lease("timeout-job")
    rep2 = work.run(cfg)
    assert rep2.digested == 0
    assert rep2.stopped == work.STOP_UNREACHABLE
    assert query_jobs(cfg, "timeout-job")[0][0] == queue.RUNNING
    assert query_jobs(cfg, "timeout-job")[0][1] == 2

    # Attempt 3 (reaches MAX_ATTEMPTS = 3 and fails)
    expire_lease("timeout-job")
    rep3 = work.run(cfg)
    assert rep3.digested == 0
    assert rep3.stopped == work.STOP_UNREACHABLE
    assert query_jobs(cfg, "timeout-job")[0][0] == queue.FAILED
    assert query_jobs(cfg, "timeout-job")[0][1] == 3

    # Attempt 4 / Next run: timeout-job is FAILED, healthy-job is now processed
    rep4 = work.run(cfg)
    assert rep4.digested == 1
    assert rep4.stopped == ""
    assert query_jobs(cfg, "healthy-job")[0][0] == queue.DONE
