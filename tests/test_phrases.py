"""Search phrases (BUILD-PLAN F85): vault files the index reads, never injected memory.

The phrases are model output, so they live in the vault as ``phrases/<rel>.json`` files keyed
by the hash of the text they phrase. The tests cover the step that writes and prunes them,
the index column they fill, the snippet rule, the never-injected rule, and the forget and
redaction paths that reach them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aft import config as configmod
from aft import evidence, forget, index, inject, keys, model, phrases, registry, search, work
from aft.config import Config
from aft.readers import Entry
from aft.registry import path_key
from conftest import make_repo, run_git
from fakes.model_server import FakeEndpoint

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

SESSION = "s-1"

STATEMENT = "The retry waits three seconds."
SUMMARY = "The session tuned the retry backoff."

PAGE = """\
---
type: Project
title: Widget
description: A widget that turns water into widgets.
sources:
  - id: s1
    resource: aft://session/claude/{session}
    quote: the retry waits three seconds
aft:
  project: p_widget
---

## Facts

- {statement} [stated] 2026-10-01 [^s1]
"""

DIGEST = """\
---
type: Session Digest
title: Widget session
description: What the session did.
sources: []
---

## Summary

{summary}
"""

PAGE_REL = "projects/widget/overview.md"
DIGEST_REL = f"digests/widget/2026/10/2026-10-01-claude-{SESSION}.md"


def serve(cfg: Config, *answers: object) -> tuple[FakeEndpoint, Config]:
    """Point the config at the fake endpoint returning these answers in order."""
    fake = FakeEndpoint(list(answers))
    fake.start()
    (cfg.home / configmod.CONFIG_NAME).write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n',
        encoding="utf-8",
    )
    return fake, configmod.load()


def answer(rows: dict[int, list[str]]) -> str:
    """A phrases answer keyed by row number, in the shape the schema asks for."""
    return json.dumps({"phrases": {str(number): value for number, value in rows.items()}})


def write_page(cfg: Config, rel: str, text: str) -> Path:
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    run_git(["add", rel], cfg.vault)
    run_git(["commit", "-q", "-m", f"add {rel}"], cfg.vault)
    return path


def phrases_column(cfg: Config, collection: str = index.PAGES) -> list[tuple]:
    conn = sqlite3.connect(cfg.home / index.INDEX_NAME)
    try:
        return conn.execute(f"SELECT path, body, phrases FROM {collection}").fetchall()
    finally:
        conn.close()


@pytest.fixture
def widget(initialized: Config, tmp_path) -> Config:
    """A registered project whose overview carries one active line, with one digest."""
    reg = registry.Registry.load(initialized.registry_path)
    project = reg.add("widget", "Widget")
    reg.add_path(project, path_key(initialized.host, make_repo(tmp_path / "widget").resolve()))
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)
    write_page(initialized, PAGE_REL, PAGE.format(session=SESSION, statement=STATEMENT))
    write_page(initialized, DIGEST_REL, DIGEST.format(summary=SUMMARY))
    return initialized


def test_phrases_are_written_for_rows_that_lack_them_and_filled_into_the_index(widget):
    fake, cfg = serve(
        widget, answer({1: ["backoff tuning"], 2: ["how long until it retries", "retry delay"]})
    )
    report = phrases.update(cfg, NOW)

    assert report.written == 2
    assert len(fake.requests) == 1, "the step batches the rows into one call"
    path = cfg.vault / phrases.file_rel(PAGE_REL)
    held = phrases.read_file(path)
    assert held == {phrases.line_key(STATEMENT): ["how long until it retries", "retry delay"]}
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert list(json.loads(text)) == sorted(json.loads(text)), "the keys are sorted"
    digest_held = phrases.read_file(cfg.vault / phrases.file_rel(DIGEST_REL))
    assert digest_held == {phrases.section_key("Summary", SUMMARY): ["backoff tuning"]}

    index.update(cfg)
    rows = {(row[0], row[1]): row[2] for row in phrases_column(cfg)}
    digest_rows = {(row[0], row[1]): row[2] for row in phrases_column(cfg, index.DIGESTS)}
    assert rows[(PAGE_REL, STATEMENT)] == "how long until it retries retry delay"
    assert digest_rows[(DIGEST_REL, SUMMARY)] == "backoff tuning"


def test_phrases_are_removed_for_rows_that_left(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.update(cfg, NOW)
    calls = len(fake.requests)
    (cfg.vault / DIGEST_REL).unlink()

    report = phrases.update(cfg, NOW)

    assert report.pruned == 1
    assert not (cfg.vault / phrases.file_rel(DIGEST_REL)).exists()
    assert (cfg.vault / phrases.file_rel(PAGE_REL)).exists()
    assert len(fake.requests) == calls, "pruning is file work and asks the model for nothing"


def test_an_edited_line_loses_its_stale_phrases(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.update(cfg, NOW)
    old_key = phrases.line_key(STATEMENT)
    edited = PAGE.format(session=SESSION, statement="The retry waits three seconds exactly.")
    write_page(cfg, PAGE_REL, edited)

    report = phrases.update(cfg, NOW)

    held = phrases.read_file(cfg.vault / phrases.file_rel(PAGE_REL))
    assert old_key not in held
    assert phrases.line_key("The retry waits three seconds exactly.") in held
    assert report.pruned == 1
    assert report.written == 1


def test_a_rebuild_reproduces_the_phrases_column_without_a_model_call(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.update(cfg, NOW)
    calls = len(fake.requests)

    index.reindex(cfg)

    rows = {(row[0], row[1]): row[2] for row in phrases_column(cfg)}
    digest_rows = {(row[0], row[1]): row[2] for row in phrases_column(cfg, index.DIGESTS)}
    assert rows[(PAGE_REL, STATEMENT)] == "retry delay"
    assert digest_rows[(DIGEST_REL, SUMMARY)] == "backoff tuning"
    assert len(fake.requests) == calls, "the rebuild reads the files and calls no model"


def test_a_hit_is_found_by_its_phrases_and_snippet_shows_the_row_not_the_phrases(widget):
    fake, cfg = serve(widget, answer({1: ["retry backoff"], 2: ["delay before retrying"]}))
    phrases.update(cfg, NOW)

    hits = search.search(cfg, "delay before retrying")

    assert hits, "the phrases column answers a query the body's own words do not"
    hit = next(h for h in hits if h.collection == index.PAGES)
    assert hit.path == PAGE_REL
    assert "delay" not in hit.snippet, "the snippet is drawn from the body, never the phrases"
    assert "retry waits" in hit.snippet


def test_inject_and_aft_read_never_show_phrases(widget, tmp_path):
    fake, cfg = serve(widget, answer({1: ["retry backoff"], 2: ["delay before retrying"]}))
    phrases.update(cfg, NOW)
    index.update(cfg)
    calls = len(fake.requests)

    reg = registry.Registry.load(cfg.registry_path)
    resolution = keys.resolve(tmp_path / "widget", reg, cfg.host)
    block = inject.build(cfg, resolution)
    read = search.read_page(cfg, PAGE_REL)

    assert "delay before retrying" not in block
    assert "backoff tuning" not in block
    assert "delay before retrying" not in read
    assert "backoff tuning" not in read
    assert len(fake.requests) == calls, "inject and read ask the model for nothing"


def test_forget_session_takes_the_phrases_of_what_it_removes(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.update(cfg, NOW)
    calls = len(fake.requests)
    project = registry.Registry.load(cfg.registry_path).by_slug("widget")
    assert project is not None
    evidence.store(
        cfg, "claude", SESSION, project.id, [Entry("e1", NOW, "user", STATEMENT.lower(), False)]
    )

    report = forget.forget_session(cfg, SESSION)

    assert not (cfg.vault / phrases.file_rel(PAGE_REL)).exists(), (
        "the line's phrases go with the line"
    )
    assert not (cfg.vault / phrases.file_rel(DIGEST_REL)).exists(), (
        "the digest's phrases go with the digest"
    )
    assert report.phrases_pruned == 2
    assert report.vault_committed
    assert len(fake.requests) == calls, "the forget prunes files and calls no model"


def test_a_forget_drops_every_phrase_of_a_page_it_cannot_parse(widget):
    """Which lines of a page that fails to parse are still active cannot be read, so the
    forget's prune errs toward removing: the page's phrase file goes, and a later run writes
    phrases again for the rows the index holds."""
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.update(cfg, NOW)
    assert (cfg.vault / phrases.file_rel(PAGE_REL)).exists()
    calls = len(fake.requests)
    path = cfg.vault / PAGE_REL
    path.write_text(path.read_text(encoding="utf-8").replace("---", "", 2), encoding="utf-8")

    pruned, changed = phrases.prune_paths(cfg, [PAGE_REL])

    assert pruned >= 1
    assert changed == [phrases.file_rel(PAGE_REL)]
    assert not (cfg.vault / phrases.file_rel(PAGE_REL)).exists()
    assert len(fake.requests) == calls, "pruning calls no model"


def test_forget_pattern_redacts_the_phrase_files_too(widget):
    secret = "deploy-token-9f2-for-tests"
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: [f"the deploy key {secret}"]}))
    phrases.update(cfg, NOW)
    calls = len(fake.requests)

    report = forget.forget_pattern(cfg, secret)

    assert phrases.file_rel(PAGE_REL) in report.pages_redacted
    held = phrases.read_file(cfg.vault / phrases.file_rel(PAGE_REL))
    assert all(secret not in phrase for value in held.values() for phrase in value)
    assert any("[redacted]" in phrase for value in held.values() for phrase in value)
    rows = {(row[0], row[1]): row[2] for row in phrases_column(cfg)}
    assert secret not in rows[(PAGE_REL, STATEMENT)]
    assert len(fake.requests) == calls


def spend(cfg: Config, at: datetime, tokens: int) -> None:
    line = {"at": at.isoformat(), "prompt_tokens": tokens, "completion_tokens": 0}
    with model.usage_path(cfg).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def test_a_spent_budget_stops_the_phrases_step(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    phrases.write_file(
        phrases.file_path(cfg, PAGE_REL),
        {phrases.line_key("A line the page no longer holds."): ["stale phrase"]},
    )
    spend(cfg, NOW, cfg.settings.work.daily_token_budget)

    report = phrases.update(cfg, NOW)

    assert report.written == 0
    assert report.pruned == 1, "pruning is file work and runs whatever the budget says"
    assert fake.requests == [], "a spent budget buys no model calls"


def test_a_cooling_endpoint_gets_no_calls_from_the_phrases_step(widget):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    model.set_cooldown(cfg, "the endpoint answered 500")

    report = phrases.update(cfg, NOW)

    assert "cooldown" in report.stopped
    assert report.written == 0
    assert fake.requests == []


def test_the_run_writes_phrases_after_the_digests_and_reports_the_counts(widget, monkeypatch):
    fake, cfg = serve(widget, answer({1: ["backoff tuning"], 2: ["retry delay"]}))
    monkeypatch.setattr(work.capture, "sweep", lambda config, moment: [])

    report = work.run(cfg, NOW)

    assert report.phrases_written == 2
    assert "2 phrases written" in report.line()
    assert (cfg.vault / phrases.file_rel(PAGE_REL)).is_file()
    assert (cfg.vault / phrases.file_rel(DIGEST_REL)).is_file()
    assert fake.requests, "the run's phrases step reaches the endpoint"
    assert len(fake.requests) == 1
