"""Sessions of no project (BUILD-PLAN F35; DESIGN 7).

A session whose directory belongs to no project is digested and filed as unfiled. Dreaming
takes only its global facts into memory: its project facts have no project page, stay in
the digest, and are reported.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from aft import config as configmod
from aft import digest, evidence, lint, page, queue
from aft.dream import apply, schedule
from aft.readers import USER, Entry
from aft.registry import Registry, path_key
from fakes.model_server import Reply
from test_apply import NOW, TODAY, config_with_fake, fake, reply, write_digest

__all__ = ["fake"]  # the fixture, imported so pytest finds it here

EMPTY_ANSWER = {
    "summary": "Talked about punctuation.",
    "handoff": {"in_flight": {"text": "", "entry": ""}, "failed": [], "open": []},
    "facts": [],
}


def unfiled_digest(cfg) -> None:
    write_digest(
        cfg,
        digest.UNFILED,
        "2026-10-05-c-u1",
        facts=[
            {
                "statement": "The user wants plain hyphens, never em dashes.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
                "term": "long",
            },
            {
                "statement": "The inference stack runs vLLM on port 8080.",
                "kind": "state",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
                "scope": "project",
                "term": "long",
            },
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/u1#e1",
                "quote": "The user wants plain hyphens, never em dashes.",
            },
            {
                "id": "s2",
                "resource": "aft://session/c/u1#e2",
                "quote": "The inference stack runs vLLM on port 8080.",
            },
        ],
    )


def test_an_unfiled_digest_gives_memory_its_global_facts_only(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    unfiled_digest(cfg)
    fake.script = [reply("add")]
    rep = schedule.run(cfg, now=NOW).reports[digest.UNFILED]
    assert len(fake.requests) == 1, "a project fact of no project was put to the model"
    prefs = (cfg.vault / "global" / "preferences.md").read_text()
    assert "The user wants plain hyphens, never em dashes. [stated]" in prefs
    assert [(s.statement, s.reason) for s in rep.skips] == [
        ("The inference stack runs vLLM on port 8080.", apply.UNFILED_SKIP)
    ]
    assert Registry.load(cfg.registry_path).projects == []
    assert not (cfg.vault / "projects" / digest.UNFILED).exists()
    assert lint.lint_vault(cfg.vault) == []


def test_redigest_files_a_session_of_no_project_as_unfiled(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    entries = [
        Entry(id="e1", time=NOW, role=USER, text="Keep plain hyphens.", is_error=False),
        Entry(id="e2", time=NOW, role=USER, text="Thanks, that works.", is_error=False),
    ]
    evidence.store(cfg, agent="claude", session_id="loose-1", project_id=None, entries=entries)
    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "loose-1", now=NOW)
    assert wrote.path.startswith(f"digests/{digest.UNFILED}/")
    frontmatter = page.split_frontmatter((cfg.vault / wrote.path).read_text())[0]
    assert frontmatter["title"].startswith("Unfiled session ")
    assert "project" not in frontmatter["aft"]


def test_redigest_files_an_unregistered_repository_under_its_own_name(
    initialized, home, fake, tmp_path
):
    """A session in a repository the registry has not met has no project id in the
    archive, and its directory still names it."""
    from aft import queue
    from aft.events import SESSION_END, SessionEvent
    from conftest import make_repo

    cfg = config_with_fake(home, initialized.vault, fake)
    repo = make_repo(tmp_path / "gizmo")
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="repo-1",
            cwd=repo,
            transcript_path=tmp_path / "t.jsonl",
            time=NOW,
            detail=None,
        ),
    )
    entries = [
        Entry(id="e1", time=NOW, role=USER, text="Keep plain hyphens.", is_error=False),
        Entry(id="e2", time=NOW, role=USER, text="Thanks, that works.", is_error=False),
    ]
    evidence.store(cfg, agent="claude", session_id="repo-1", project_id=None, entries=entries)
    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "repo-1", now=NOW)
    assert wrote.path.startswith("digests/gizmo/")


def test_an_unfiled_dream_waits_while_an_unfiled_session_is_active(
    initialized, home, fake, monkeypatch
):
    """The quiet period holds for unfiled sessions too, so a dream never takes up a session
    that is still talking."""
    from aft import queue

    cfg = config_with_fake(home, initialized.vault, fake)
    cfg = config_with_fake(home, initialized.vault, fake)
    # The session of an unfiled digest ran in a directory that resolves to none, which is
    # one under the system temporary directory (DESIGN 7, F49).
    nowhere = (Path(tempfile.gettempdir()) / "loose").resolve()
    nowhere.mkdir(exist_ok=True)
    unfiled_digest(cfg)
    monkeypatch.setattr(queue, "last_events", lambda config: {nowhere: NOW})
    run = schedule.run(cfg, now=NOW)
    assert digest.UNFILED in run.active
    assert digest.UNFILED not in run.ran
    assert fake.requests == []


# The unfiled backfill (DESIGN 7, F49, aft redigest --unfiled)


def _unfiled_record(cfg, tmp_path, name: str = "fieldwork", session: str = "u1") -> Path:
    """A session that ran in ``name`` before any of it was registered: an unfiled digest
    stands, the evidence holds its words with no project, and the queue record names the
    directory it ran in."""
    plain = tmp_path / name
    plain.mkdir()
    # The evidence's resource urls name the agent "c" (aft://session/c/u1#e1), so the
    # record the backfill resolves is recorded under that agent too.
    queue.record_project(cfg, "c", session, None, name, plain)
    unfiled_digest(cfg)
    return plain


def test_the_backfill_files_a_session_under_its_registered_project(
    initialized, home, fake, tmp_path
):
    """The backfill rebuilds an unfiled session to file under the project its recorded
    directory now resolves to. The directory is gone and the registry's path answers for
    it; the unfiled digest the session stood under is removed."""
    cfg = config_with_fake(home, initialized.vault, fake)
    plain = _unfiled_record(cfg, tmp_path)
    registry = Registry.load(initialized.registry_path)
    project = registry.add("fieldwork", "Fieldwork")
    registry.add_path(project, path_key("testhost", plain))
    registry.save()
    shutil.rmtree(plain)
    assert digest.unfiled_sessions(cfg) == ["u1"]

    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "u1", now=NOW, unfiled=True)
    assert wrote.path.startswith("digests/fieldwork/")
    assert not (cfg.vault / "digests" / digest.UNFILED).exists()
    frontmatter = page.split_frontmatter((cfg.vault / wrote.path).read_text())[0]
    assert frontmatter["aft"]["project"] == project.id
    assert lint.lint_vault(cfg.vault) == []


def test_the_backfill_files_a_session_whose_directory_is_still_plain(
    initialized, home, fake, tmp_path
):
    """A recorded directory that no registry path answers for files under the name it
    settles (DESIGN 7, F49): the backfill gives the session the digest directory a plain
    directory gets, the way the digest was built for it."""
    cfg = config_with_fake(home, initialized.vault, fake)
    plain = _unfiled_record(cfg, tmp_path)

    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "u1", now=NOW, unfiled=True)
    assert wrote.path.startswith("digests/fieldwork/")
    assert not (cfg.vault / "digests" / digest.UNFILED).exists()
    record = queue.recorded_project(cfg, "c", "u1")
    assert (record.slug, record.path) == ("fieldwork", str(plain))


def test_the_backfill_never_refiles_a_home_subdirectory_or_the_downloads_into_home(
    initialized, home, fake, tmp_path, monkeypatch
):
    """home is the home directory itself and matches nothing below it, and the refusals are
    answered before the registry is looked at (DESIGN 7, F49): the backfill refiles a
    session in a subdirectory of a registered home under that subdirectory's own project,
    and leaves the sessions of ~/Downloads and an excluded path unfiled instead of filing
    them into home."""
    from aft import vault

    cfg = config_with_fake(home, initialized.vault, fake)
    home_dir = tmp_path / "homedir"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    notes = home_dir / "notes"
    downloads = home_dir / "Downloads" / "x"
    private = home_dir / "private"
    for directory in (notes, downloads, private):
        directory.mkdir(parents=True)
    registry = Registry.load(cfg.registry_path)
    project = registry.add("home", "Home")
    registry.add_path(project, path_key("testhost", home_dir))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "aft: register home")

    _unfiled_session(cfg, "u1", "notes", notes)
    _unfiled_session(cfg, "u2", "x", downloads)
    _unfiled_session(cfg, "u3", "private", private)
    text = (cfg.home / "config.toml").read_text(encoding="utf-8")
    (cfg.home / "config.toml").write_text(text + f'\n[capture]\nexclude_paths = ["{private}"]\n')
    cfg = configmod.load()

    assert digest.stays_unfiled(cfg, "u2")
    assert digest.stays_unfiled(cfg, "u3"), "an excluded path under home is not home"
    assert not digest.stays_unfiled(cfg, "u1")

    fake.script = [Reply(content=EMPTY_ANSWER)]
    wrote = digest.redigest(cfg, "u1", now=NOW, unfiled=True)
    assert wrote.path.startswith("digests/notes/"), "the subdirectory files as its own project"
    assert not (cfg.vault / "digests/home").exists(), "nothing was refiled into home"
    record = queue.recorded_project(cfg, "c", "u1")
    assert (record.slug, record.path) == ("notes", str(notes))
    names = [p.name for p in (cfg.vault / "digests" / digest.UNFILED).rglob("*.md")]
    assert f"{TODAY}-c-u2.md" in names and f"{TODAY}-c-u3.md" in names


def test_the_backfill_names_the_sessions_that_still_have_an_archive(initialized, tmp_path):
    """A session `aft forget` removed has no evidence left, so the backfill does not list
    it; a digest with no session behind it is not listed either."""
    plain = _unfiled_record(initialized, tmp_path)
    write_digest(
        initialized,
        digest.UNFILED,
        f"{TODAY}-claude-u2",
        facts=[],
        sources=[],
        store_evidence=False,
    )
    shutil.rmtree(plain)
    evidence.forget(initialized, "u1")
    assert digest.unfiled_sessions(initialized) == []


def _unfiled_session(cfg, session: str, name: str, directory: Path | None) -> None:
    """An unfiled digest of ``session`` and a session-start record naming ``directory``."""
    write_digest(
        cfg,
        digest.UNFILED,
        f"{TODAY}-c-{session}",
        facts=[
            {
                "statement": "The user wants plain hyphens, never em dashes.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
                "term": "long",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": f"aft://session/c/{session}#e1",
                "quote": "The user wants plain hyphens, never em dashes.",
            }
        ],
    )
    if directory is not None:
        queue.record_project(cfg, "c", session, None, name, directory)


def test_the_backfill_leaves_a_session_whose_directory_answers_no_project(
    initialized, home, fake, tmp_path, capsys, monkeypatch
):
    """A session whose directory resolves to no project has nowhere to move its digest to:
    the backfill lists it as staying unfiled and skips it, so the digest is not rebuilt
    onto the same ``_unfiled`` path and charged for again on every run (DESIGN 7, F49)."""
    import tempfile

    from aft.cli import main

    cfg = config_with_fake(home, initialized.vault, fake)
    # The system temporary directory answers no project (DESIGN 7, F49), and a removed
    # directory with no registered path answers none either.
    nowhere = (Path(tempfile.gettempdir()) / "loose-stay1").resolve()
    nowhere.mkdir(exist_ok=True)
    _unfiled_session(cfg, "stay1", "stay1", nowhere)
    gone = tmp_path / "gonefield"
    gone.mkdir()
    _unfiled_session(cfg, "stay2", "stay2", gone)
    shutil.rmtree(gone)
    fieldwork = tmp_path / "fieldwork"
    fieldwork.mkdir()
    _unfiled_session(cfg, "move1", "fieldwork", fieldwork)
    assert digest.unfiled_sessions(cfg) == ["move1", "stay1", "stay2"]
    assert digest.stays_unfiled(cfg, "stay1")
    assert digest.stays_unfiled(cfg, "stay2")
    assert not digest.stays_unfiled(cfg, "move1")

    monkeypatch.setattr(
        digest,
        "redigest_estimate",
        lambda config, session_id: 900 if session_id == "move1" else None,
    )
    assert main(["redigest", "--unfiled", "--dry-run"]) == 0
    assert capsys.readouterr().out == (
        "move1\t~900 tokens\n"
        "stay1\tstays unfiled: its directory answers no project\n"
        "stay2\tstays unfiled: its directory answers no project\n"
        "1 session(s) would rebuild, ~900 tokens; 2 staying unfiled; 0 cannot rebuild\n"
    )

    rebuilt: list[str] = []
    monkeypatch.setattr(
        digest,
        "redigest",
        lambda config, session_id, now=None, unfiled=False: (
            rebuilt.append(session_id)
            or digest.Digest(path="digests/fieldwork/x.md", facts=(), dropped=(), watermark="e9")
        ),
    )
    assert main(["redigest", "--unfiled"]) == 0
    assert capsys.readouterr().out == (
        "rebuilt digest at digests/fieldwork/x.md\n"
        "stay1\tstays unfiled: its directory answers no project\n"
        "stay2\tstays unfiled: its directory answers no project\n"
    )
    assert rebuilt == ["move1"], "a session with nowhere to move was put to the model"
    assert digest.unfiled_sessions(cfg) == ["move1", "stay1", "stay2"]


def test_the_backfill_skips_and_names_a_session_the_archive_no_longer_covers(
    initialized, home, fake, tmp_path, capsys, monkeypatch
):
    """A rebuild replaces the session's digests with one built from the entries the archive
    still holds, so a session whose digests cite entries an `aft forget` has since taken is
    not rebuilt at all: the run names it with the reason, and the digests stand (DESIGN 7,
    F49). A session the archive still covers is rebuilt as before."""
    import sqlite3

    from aft.cli import main
    from aft.readers import USER, Entry

    cfg = config_with_fake(home, initialized.vault, fake)
    fieldwork = tmp_path / "fieldwork"
    fieldwork.mkdir()
    _unfiled_session(cfg, "u1", "fieldwork", fieldwork)
    other = tmp_path / "otherfield"
    other.mkdir()
    _unfiled_session(cfg, "u2", "otherfield", other)
    # The archive holds another entry of u1's, so the session is still archived at all -
    # what is gone is the one entry its digest cites.
    evidence.store(
        cfg,
        agent="c",
        session_id="u1",
        project_id=None,
        entries=[Entry(id="e2", time=NOW, role=USER, text="still archived", is_error=False)],
    )
    with sqlite3.connect(cfg.home / "evidence.sqlite") as conn:
        conn.execute("DELETE FROM evidence WHERE session_id = 'u1' AND entry_id = 'e1'")
    conn.close()

    assert digest.unfiled_sessions(cfg) == ["u2"]
    assert digest.uncovered_unfiled(cfg) == [
        ("u1", "its digests cite 1 entry the archive no longer holds (e1)")
    ]

    fake.script = [Reply(content=EMPTY_ANSWER)]
    assert main(["redigest", "--unfiled", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "u1\tcannot rebuild: its digests cite 1 entry the archive no longer holds (e1)" in out
    assert "u2\t~" in out and "1 session(s) would rebuild, ~" in out
    assert "0 staying unfiled; 1 cannot rebuild" in out
    assert not (cfg.vault / "digests/otherfield").exists(), "the dry run wrote nothing"

    assert main(["redigest", "--unfiled"]) == 0
    out = capsys.readouterr().out
    assert "u1\tcannot rebuild: its digests cite 1 entry the archive no longer holds (e1)" in out
    assert "rebuilt digest at digests/otherfield/" in out
    unfiled = [p.name for p in (cfg.vault / "digests" / digest.UNFILED).rglob("*.md")]
    assert f"{TODAY}-c-u1.md" in unfiled, "the uncovered digest was rebuilt over"
    assert f"{TODAY}-c-u2.md" not in unfiled, "the covered session was not rebuilt"
