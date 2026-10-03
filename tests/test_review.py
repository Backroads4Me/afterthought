"""F48: ``aft review``, clearing what memory should not keep (BUILD-PLAN F48).

The list is saved and no page changes; the withdrawal is the user's order, by number or
all; a changed line and a noted line are skipped and named; a number not on the list
refuses the whole withdrawal; the budget is counted (DESIGN 13). The model only judges
(R9): every scripted reply is what a real model could say under the schema."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import config as configmod
from aft import page, pagewrite, review, vault
from aft.registry import Registry
from conftest import run_git
from fakes.model_server import Reply
from test_apply import NOW, TODAY, config_with_fake, fake

__all__ = ["fake"]  # the fixture, imported so pytest finds it here

RUN_PROJECT = "p_widget"

FRONTMATTER = """---
type: Project
title: {slug}
description: The {slug} project.
sources:
  - id: s1
    resource: aft://session/claude/2c1e9d0a#entry-148
    title: Design session
    quote: "the capture hook enqueues and returns"
aft:
  project: {project}
---
"""

OVERVIEW_BODY = """## State

- The capture hook enqueues work and returns. [observed] 2026-09-30 [^s1]
- The GPU host runs vLLM for the digest model. [observed] 2026-09-30 [^s1]

## Decisions

- Capture runs at session end. [stated] 2026-09-20 [^s1]

## Candidates

- The GPU host needs a second card. [inferred] 2026-09-30 [^s1] (signals 1/3, sessions 1/2)
"""

GLOBAL_BODY = """## Preferences

- No RGB lighting on the GPU host. [stated] 2026-09-01 [^s1]
"""

RECENT_BODY = """## State

- The digest run is paused until the model server is back. [stated] {date} [^s1]
"""


def register(cfg: configmod.Config, slug: str = "widget") -> None:
    registry = Registry.load(cfg.registry_path)
    registry.add(slug, slug.capitalize())
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], f"register {slug}")


def seed_page(
    cfg: configmod.Config, rel: str, body: str, *, project: str = RUN_PROJECT, slug: str = "widget"
) -> Path:
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = FRONTMATTER.format(slug=slug, project=project)
    path.write_text(f"{frontmatter}\n\n{body}", encoding="utf-8")
    vault.commit(cfg.vault, [rel], f"seed {rel}")
    return path


def withdraw_reply(*labels: str) -> Reply:
    return Reply(
        content={
            "withdraw": [
                {
                    "line": label,
                    "reason": f"line {label} is progress on the work",
                }
                for label in labels
            ]
        }
    )


def spend(cfg: configmod.Config, tokens: int) -> None:
    """Record tokens of model use earlier today, as aft.model logs a call.

    The budget tests run the review without a frozen ``now``, so the day's ledger is the
    real one and a call's own usage counts against the cap the way the digests' tests
    exercise it (test_digest's ``test_the_daily_budget_stops_the_work``)."""
    at = datetime.now(UTC) - timedelta(minutes=1)
    line = {"at": at.isoformat(), "prompt_tokens": tokens, "completion_tokens": 0}
    with (cfg.home / "usage.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")


def read_report(cfg: configmod.Config, scope: str) -> tuple[dict, str]:
    text = review.report_path(cfg.vault, scope).read_text(encoding="utf-8")
    frontmatter, body, _, error = page.split_frontmatter(text)
    assert error is None
    assert frontmatter is not None
    return frontmatter, body


def test_the_list_is_saved_and_no_page_changes(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    before = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    fake.script = [withdraw_reply("L2")]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 3, "the candidate is not memory a withdrawal would touch"
    assert [f.number for f in outcome.findings] == [1]
    assert outcome.findings[0].page == "projects/widget/overview.md"
    assert outcome.committed
    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == before
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    frontmatter, body = read_report(cfg, "widget")
    assert frontmatter["aft"]["review"]["scope"] == "widget"
    assert frontmatter["aft"]["review"]["reviewed"] == 3
    assert frontmatter["aft"]["review"]["calls"] == 1
    record = frontmatter["aft"]["review"]["findings"][0]
    assert record["line"] == outcome.findings[0].line
    assert record["statement"] == "The GPU host runs vLLM for the digest model."
    assert record["result"] is None
    assert "1. projects/widget/overview.md" in body
    assert "Reason: line L2 is progress on the work" in body
    assert "withdraw 1" in body
    assert run_git(["log", "--format=%s", "-1"], cfg.vault) == "aft: review widget"


def test_the_review_shows_the_model_lines_not_quotes(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [Reply(content={"withdraw": []})]

    review.review(cfg, "widget", now=NOW)

    body = fake.requests[0]
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert system == review.prompt()
    hook = (
        "L1 (state, observed, overview.md, 2026-09-30): The capture hook enqueues work and returns."
    )
    assert hook in user
    assert "L3 (decision, stated, overview.md, 2026-09-20): Capture runs at session end." in user
    assert "the capture hook enqueues and returns" not in user, "the quote is not shown"
    assert "The GPU host needs a second card" not in user, "a candidate is not asked about"
    assert body["max_tokens"] == review.MAX_TOKENS


def test_withdraw_by_number_strikes_and_records(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1", "L3")]
    review.review(cfg, "widget", now=NOW)

    outcome = review.withdraw(cfg, "widget", "2", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [2]
    assert outcome.committed
    text = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    assert "~~Capture runs at session end.~~ [stated] 2026-09-20 [^s1] withdrawn 2026-10-05" in text
    assert "- The capture hook enqueues work and returns." in text, "the unchosen line stands"
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    frontmatter, body = read_report(cfg, "widget")
    results = {f["number"]: f["result"] for f in frontmatter["aft"]["review"]["findings"]}
    assert results == {1: None, 2: "withdrawn 2026-10-05"}
    assert "Withdrawn 2026-10-05." in body
    assert run_git(["log", "--format=%s", "-1"], cfg.vault) == (
        "aft: withdraw 1 review line(s) in widget"
    )


def test_withdraw_all_strikes_every_line_without_a_result(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1", "L3")]
    review.review(cfg, "widget", now=NOW)

    outcome = review.withdraw(cfg, "widget", "all", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [1, 2]
    text = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    assert text.count("withdrawn 2026-10-05") == 2

    again = review.withdraw(cfg, "widget", "all", now=NOW)

    assert again.withdrawn == ()
    assert again.message is None, "a rerun changes nothing; every line has its result"
    assert not again.committed
    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == text


def test_a_changed_line_is_skipped_and_named(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1")]
    review.review(cfg, "widget", now=NOW)
    page_path = cfg.vault / "projects/widget/overview.md"
    page_path.write_text(
        page_path.read_text(encoding="utf-8").replace(
            "- The capture hook enqueues work and returns.",
            "- The capture hook enqueues work at session start.",
        ),
        encoding="utf-8",
    )

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert outcome.withdrawn == ()
    assert outcome.skipped[0][1] == "the line changed since the list was saved"
    assert outcome.committed, "the report records what became of the line"
    frontmatter, body = read_report(cfg, "widget")
    record = frontmatter["aft"]["review"]["findings"][0]
    assert record["result"] == "skipped: the line changed since the list was saved"
    assert "Skipped: the line changed since the list was saved." in body


def test_a_noted_line_is_skipped_and_named(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1")]
    review.review(cfg, "widget", now=NOW)
    page_rel = "projects/widget/overview.md"
    with vault.locked(cfg):
        doc = pagewrite.PageDoc.read(cfg.vault / page_rel, vault=cfg.vault)
        doc.add_callout(
            doc.fact("The capture hook enqueues work and returns."),
            "conflict",
            "A session says the hook runs at session start.",
            TODAY,
        )
        (cfg.vault / page_rel).write_text(doc.render(), encoding="utf-8")

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert outcome.withdrawn == ()
    assert outcome.skipped[0][1] == "it has an open note; aft settle settles it"
    text = (cfg.vault / page_rel).read_text(encoding="utf-8")
    assert "~~The capture hook" not in text


def test_a_number_not_on_the_list_refuses_the_whole_withdrawal(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1", "L2")]
    review.review(cfg, "widget", now=NOW)
    before = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")

    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "widget", "1,99", now=NOW)
    assert "carries no line 99" in str(raised.value)
    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "widget", "two", now=NOW)
    assert "--withdraw takes the numbers of the saved list or 'all'" in str(raised.value)

    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == before
    frontmatter, _ = read_report(cfg, "widget")
    assert all(f["result"] is None for f in frontmatter["aft"]["review"]["findings"])


def test_the_budget_is_counted(initialized, home, fake):
    """A budget already spent refuses the review; one spent part-way leaves the rest
    waiting (DESIGN 13), the digests' wording."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    spend(cfg, cfg.settings.work.daily_token_budget)

    with pytest.raises(review.ReviewError) as raised:
        review.review(cfg, "widget")
    assert "token budget" in str(raised.value)
    assert "waits for a day with room in it" in str(raised.value)
    assert fake.requests == [], "the review called the model with the day's budget spent"
    assert not review.report_path(cfg.vault, "widget").exists()


def test_a_budget_spent_partway_leaves_the_rest_waiting(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    body = "\n".join(
        ["## State", ""]
        + [f"- Line {n} stands in memory. [observed] 2026-09-30 [^s1]" for n in range(1, 22)]
    )
    seed_page(cfg, "projects/widget/overview.md", body)
    seed_page(cfg, "projects/widget/overview.md", body)
    spend(cfg, cfg.settings.work.daily_token_budget - 250)  # one call costs 200 + 50
    fake.script = [withdraw_reply("L1"), withdraw_reply("L1")]

    outcome = review.review(cfg, "widget")

    assert len(fake.requests) == 1, "the second part waits, the budget is spent"
    assert outcome.waiting == 1
    assert outcome.calls == 1
    assert outcome.committed
    frontmatter, body_text = read_report(cfg, "widget")
    assert frontmatter["aft"]["review"]["waiting"] == 1
    assert "Incomplete" in body_text
    assert "1 part(s) wait, the day's token budget is spent" in body_text


def test_lines_are_reviewed_in_parts_of_twenty_with_continuing_numbers(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    body = "\n".join(
        ["## State", ""]
        + [f"- Line {n} stands in memory. [observed] 2026-09-30 [^s1]" for n in range(1, 22)]
    )
    seed_page(cfg, "projects/widget/overview.md", body)
    fake.script = [withdraw_reply("L2"), withdraw_reply("L1")]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 21
    assert outcome.calls == 2
    first, second = (request["messages"][1]["content"] for request in fake.requests)
    assert "L1 (state, observed, overview.md, 2026-09-30): Line 1 stands in memory." in first
    assert "L20 (state, observed, overview.md, 2026-09-30): Line 20 stands in memory." in first
    assert "L21" not in first
    assert "L1 (state, observed, overview.md, 2026-09-30): Line 21 stands in memory." in second
    assert [f.number for f in outcome.findings] == [1, 2], "the numbers continue across parts"
    assert outcome.findings[0].line == 18, "the page's own line number, not the label"
    assert outcome.findings[1].line == 37


def test_a_model_failure_keeps_the_findings_so_far(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    body = "\n".join(
        ["## State", ""]
        + [f"- Line {n} stands in memory. [observed] 2026-09-30 [^s1]" for n in range(1, 22)]
    )
    seed_page(cfg, "projects/widget/overview.md", body)
    fake.script = [withdraw_reply("L2"), Reply(status=500)]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.calls == 1
    assert [f.number for f in outcome.findings] == [1]
    assert outcome.failure is not None and "part 2 of 2 failed" in outcome.failure
    _, body_text = read_report(cfg, "widget")
    assert "part 2 of 2 failed" in body_text


def test_no_lines_makes_no_call(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", "")

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 0
    assert outcome.report is None
    assert fake.requests == []
    assert not review.report_path(cfg.vault, "widget").exists()


def test_the_global_scope_reviews_the_global_pages(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "global/preferences.md", GLOBAL_BODY, project="", slug="preferences")
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1")]

    outcome = review.review(cfg, "global", now=NOW)

    assert outcome.reviewed == 1
    assert outcome.findings[0].page == "global/preferences.md"
    assert "Lines of memory in the global pages" in fake.requests[0]["messages"][1]["content"]
    assert "review-global.md" in str(review.report_path(cfg.vault, "global"))
    _, body = read_report(cfg, "global")
    assert "# Review global" in body


def test_a_project_named_global_loses_to_the_literal(initialized, home, fake):
    """A project whose slug is ``global`` is reached by its slug in the registry, but
    the literal scope ``global`` always reviews the global pages."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    register(cfg, "global")
    seed_page(cfg, "global/preferences.md", GLOBAL_BODY, project="", slug="preferences")
    seed_page(cfg, "projects/global/overview.md", OVERVIEW_BODY, slug="global")
    fake.script = [withdraw_reply("L1"), withdraw_reply("L1")]

    assert review.review(cfg, "global", now=NOW).reviewed == 1
    assert review.review(cfg, "widget", now=NOW).reviewed == 0


def test_a_seeded_recent_line_is_reviewed_like_any_other(initialized, home, fake):
    """``recent.md`` is short-term memory, and its lines are reviewed like every
    other line (BUILD-PLAN F48)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/recent.md", RECENT_BODY.format(date=TODAY))
    fake.script = [withdraw_reply("L1")]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 1
    assert outcome.findings[0].page == "projects/widget/recent.md"
    assert outcome.findings[0].section == "State"
    review.withdraw(cfg, "widget", "1", now=NOW)
    text = (cfg.vault / "projects/widget/recent.md").read_text(encoding="utf-8")
    assert "withdrawn 2026-10-05" in text


def test_an_unregistered_slug_is_refused(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    with pytest.raises(review.ReviewError) as raised:
        review.review(cfg, "nowhere", now=NOW)
    assert "no project 'nowhere' is registered" in str(raised.value)
    assert fake.requests == []


def test_a_page_that_is_not_a_page_is_named_not_reviewed(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    notes = cfg.vault / "projects/widget/scratch.md"
    notes.write_text("# Scratch\n\n- Not a fact page.\n", encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/scratch.md"], "seed scratch")
    fake.script = [Reply(content={"withdraw": []})]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 3
    assert any("scratch.md" in entry and "not a fact page" in entry for entry in outcome.unreadable)
    _, body = read_report(cfg, "widget")
    assert "Not read" in body


def test_the_withdrawal_needs_a_saved_list(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "widget", "1", now=NOW)
    assert "no saved review list" in str(raised.value)


def test_the_withdrawal_reads_the_list_back_from_the_report(initialized, home, fake):
    """The report's frontmatter carries what the withdrawal needs; the body is prose."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L3")]
    review.review(cfg, "widget", now=NOW)
    saved = review.report_path(cfg.vault, "widget").read_text(encoding="utf-8")
    rewritten = saved.replace(
        "- Capture runs at session end.", "- Capture runs at a session's end."
    )
    review.report_path(cfg.vault, "widget").write_text(rewritten, encoding="utf-8")

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [1]
    assert outcome.skipped == (), "the body a person edited is prose; the frontmatter decides"
    text = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    assert "~~Capture runs at session end.~~" in text
    frontmatter, _ = read_report(cfg, "widget")
    assert (
        frontmatter["aft"]["review"]["findings"][0]["statement"] == "Capture runs at session end."
    )


def test_labels_no_part_showed_are_ignored_and_duplicates_taken_once(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [
        Reply(
            content={
                "withdraw": [
                    {"line": "L1", "reason": "progress on the work"},
                    {"line": "L1", "reason": "said again"},
                    {"line": "L9", "reason": "no such line was shown"},
                ]
            }
        )
    ]

    outcome = review.review(cfg, "widget", now=NOW)

    assert [f.number for f in outcome.findings] == [1]
    assert outcome.findings[0].reason == "progress on the work"
    assert outcome.unshown == ("L9",)


def test_the_review_prompt_wording_matches_the_digest_and_decide_prompts():
    """F46's one test and never-a-fact list, and the short-term definition, read the
    same in every prompt that applies them (BUILD-PLAN F48)."""
    from aft import digest
    from aft.dream import decide

    prompts = {
        "digest.md": digest.prompt_text(),
        "decide.md": decide.prompt(),
        "review.md": review.prompt(),
    }
    shared = [
        "a future session would make a mistake, or ask the user again, without it",
        "Never a fact, long or short term: the status of a pull request, task, branch or"
        " commit; who is working on what; a test's name or result; a review's outcome;"
        " what a change or fix did to the code; and anything the repository's own"
        " documents already say",
        "a temporary condition that changes how an agent should act",
        "(something paused, a temporary setting, a freeze)",
        "not progress on the work",
    ]
    for name, text in prompts.items():
        words = " ".join(text.split())
        for sentence in shared:
            assert sentence in words, f"{name} drops part of the shared wording"


def test_the_schema_names_twenty_lines():
    schema = review.schema()
    items = schema["properties"]["withdraw"]["items"]
    assert schema["required"] == ["withdraw"]
    assert schema["properties"]["withdraw"]["maxItems"] == review.PART
    assert items["properties"]["line"]["enum"] == [f"L{n}" for n in range(1, review.PART + 1)]
    assert items["properties"]["reason"]["maxLength"] == 200
    assert items["required"] == ["line", "reason"]
    assert items["additionalProperties"] is False


def test_the_cli_parses_review_and_the_error_is_named():
    from aft.cli import _CLI_ERROR_NAMES, _parser

    parser = _parser()
    args = parser.parse_args(["review", "widget"])
    assert args.scope == "widget"
    assert args.withdraw is None
    args = parser.parse_args(["review", "global", "--withdraw", "1,2"])
    assert args.scope == "global"
    assert args.withdraw == "1,2"
    assert "ReviewError" in _CLI_ERROR_NAMES
