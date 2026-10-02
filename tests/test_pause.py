"""Pausing background work (BUILD-PLAN F38).

A pause is a marker in the home directory. While it stands, `aft work` and `aft dream` do
nothing whatever started them, and a session's end starts no run; capture still queues
every session, and `aft resume` ends the pause and starts a run for the backlog.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aft import cli, pause, queue
from aft.events import SESSION_END, SessionEvent
from aft.install import systemd

NOW = datetime(2026, 10, 1, 21, 40, tzinfo=UTC)


def test_pause_records_when_and_why_and_keeps_the_first(cfg):
    made, new = pause.pause(cfg, "the digest step invents sessions", now=NOW)
    assert new and made.since == "2026-10-01 21:40Z"
    again, new = pause.pause(cfg, "another reason")
    assert not new and again == made
    assert pause.paused(cfg) == made


def test_an_unreadable_marker_still_pauses(cfg):
    (cfg.home / pause.PAUSE_FILE).write_text("not json")
    assert pause.paused(cfg) is not None


def test_work_and_dream_do_nothing_while_paused(initialized, capsys, monkeypatch):
    ran = []
    monkeypatch.setattr("aft.work.run", lambda config: ran.append("work"))
    monkeypatch.setattr("aft.cli._dream", lambda config: ran.append("dream"))
    pause.pause(initialized, "maintenance")
    assert cli.main(["work"]) == 0
    assert cli.main(["dream"]) == 0
    assert ran == []
    out = capsys.readouterr().out
    assert "aft work: paused since" in out and "maintenance" in out
    assert "aft dream: paused since" in out


def test_force_runs_once_despite_the_pause(initialized, monkeypatch):
    ran = []
    monkeypatch.setattr("aft.cli._dream", lambda config: ran.append("dream"))
    pause.pause(initialized)
    assert cli.main(["dream", "--force"]) == 0
    assert ran == ["dream"]
    assert pause.paused(initialized) is not None, "--force does not end the pause"


def test_a_session_end_while_paused_queues_and_starts_nothing(cfg, tmp_path):
    started = []
    pause.pause(cfg)
    assert not systemd.trigger_early(cfg, runner=started.append)
    assert started == []
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="s-paused",
            cwd=tmp_path,
            transcript_path=tmp_path / "t.jsonl",
            time=NOW,
            detail=None,
        ),
    )
    assert queue.next_job(cfg) is not None, "capture stopped queuing while paused"


def test_resume_ends_the_pause_and_starts_a_run(cfg, capsys, monkeypatch):
    started = []
    monkeypatch.setattr(
        systemd,
        "trigger_early",
        lambda config, force=False: started.append(force) or True,
    )
    pause.pause(cfg)
    assert cli.main(["resume"]) == 0
    assert pause.paused(cfg) is None
    assert started == [True]
    assert "resumed" in capsys.readouterr().out
    assert cli.main(["resume"]) == 0
    assert "was not paused" in capsys.readouterr().out


def test_resume_starts_a_run_however_recent_the_last_trigger(cfg):
    started = []
    assert systemd.trigger_early(cfg, runner=started.append)
    assert not systemd.trigger_early(cfg, runner=started.append), "the rate limit holds"
    assert systemd.trigger_early(cfg, runner=started.append, force=True)
    assert len(started) == 2
