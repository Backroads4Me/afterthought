"""``aft run`` against an endpoint that is cooling down: one line, no calls, nothing done.

The cooldown is the record an earlier failed call left behind, so a run that reads it
stops before the sweep and leaves every job queued for the run after the deadline
(BUILD-PLAN F66).
"""

from __future__ import annotations

from aft import capture, model, queue, work


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
