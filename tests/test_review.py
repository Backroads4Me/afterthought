"""F48: ``aft review``, clearing what memory should not keep (BUILD-PLAN F48).

The list is saved and no page changes; the withdrawal is the user's order, by number or
all; a changed line and a noted line are skipped and named; a number not on the list
refuses the whole withdrawal; the budget is counted (DESIGN 13). The model only judges
(R9): every scripted reply is what a real model could say under the schema."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import config as configmod
from aft import page, pagewrite, review, vault
from aft.registry import Registry, RegistryError
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

SEED_FRONTMATTER = """---
type: Project
title: {slug}
description: The {slug} project.
sources:
  - id: s1
    resource: file:planning/DESIGN.md#9. Dreaming
    quote: "Ask the model for one decision per candidate, as JSON:"
aft:
  project: {project}
---
"""

RECENT_BODY = """## State

- The digest run is paused until the model server is back. [observed] {date} [^s1]
"""


def register(cfg: configmod.Config, slug: str = "widget") -> None:
    registry = Registry.load(cfg.registry_path)
    registry.add(slug, slug.capitalize())
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], f"register {slug}")


def seed_page(
    cfg: configmod.Config,
    rel: str,
    body: str,
    *,
    project: str = RUN_PROJECT,
    slug: str = "widget",
    frontmatter_template: str | None = None,
) -> Path:
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    template = frontmatter_template or FRONTMATTER
    text = template.format(slug=slug, project=project)
    path.write_text(f"{text}\n\n{body}", encoding="utf-8")
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
    assert "The other 19 line(s) the model saw stay as they are." in body_text


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


def test_the_slug_global_is_reserved(initialized, home, fake):
    """The literal scope ``global`` reviews the global pages, so no project may take
    the slug: the registry refuses it, and the global pages still review (review fix 5)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "global/preferences.md", GLOBAL_BODY, project="", slug="preferences")
    registry = Registry.load(cfg.registry_path)
    with pytest.raises(RegistryError) as raised:
        registry.add("global")
    assert "reserved" in str(raised.value)
    with pytest.raises(RegistryError):
        registry.rename("widget", "global")

    fake.script = [withdraw_reply("L1")]
    outcome = review.review(cfg, "global", now=NOW)
    assert outcome.reviewed == 1
    assert outcome.findings[0].page == "global/preferences.md"
    assert "review-global.md" in str(review.report_path(cfg.vault, "global"))
    _, body = read_report(cfg, "global")
    assert "# Review global" in body


def test_a_seeded_recent_line_is_reviewed_like_any_other(initialized, home, fake):
    """``recent.md`` is short-term memory, and a line written the way
    ``scripts/seed_afterthought.py`` writes one, with its ``file:`` source, is reviewed
    like every other line (BUILD-PLAN F48)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(
        cfg,
        "projects/widget/recent.md",
        RECENT_BODY.format(date=TODAY),
        frontmatter_template=SEED_FRONTMATTER,
    )
    fake.script = [withdraw_reply("L1")]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 1
    assert outcome.findings[0].page == "projects/widget/recent.md"
    assert outcome.findings[0].section == "State"
    assert outcome.findings[0].tag == "observed", "the seed writes an observed tag"
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
        "a future session would make a mistake, ask the user again, or give less "
        "fitting help without it",
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


def test_an_order_to_do_work_is_never_a_fact_in_digest_decide_or_review():
    """BUILD-PLAN F88: an order to do a piece of work fails the keep test, whether or not
    the work was carried out."""
    from aft import digest
    from aft.dream import decide

    order = (
        "Also never a fact: a one-time order to do a piece of work, given or approved by the"
        " user, whether or not it was carried out. A standing rule, a permission, and the"
        " choice an approval settles are not such orders."
    )
    prompts = {
        "digest.md": digest.prompt_text(),
        "decide.md": decide.prompt(),
        "review.md": review.prompt(),
    }
    for name, text in prompts.items():
        assert order in " ".join(text.split()), f"{name} drops the order rule"


def test_the_review_prompt_keeps_facts_about_the_users_life():
    """F73: the user's life, tastes and plans pass the keep test, and a dated event is a
    durable record kept long-term with its date, so age alone withdraws nothing."""
    words = " ".join(review.prompt().split())
    assert "A fact about the user's life, interests, tastes or plans passes it." in words
    assert (
        'A dated event is a durable record ("On 2024-03-02 the user adopted a dog"), '
        "not a fact that stops being true, so it is kept long-term with its date in the "
        "statement." in words
    )


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


def test_a_line_that_only_moved_is_withdrawn(initialized, home, fake):
    """A dream add above a saved line moves every line below it; the withdrawal still
    finds the line by what it says, not by the number it was saved at (review fix 1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L3")]
    review.review(cfg, "widget", now=NOW)
    page_rel = "projects/widget/overview.md"
    with vault.locked(cfg):
        doc = pagewrite.PageDoc.read(cfg.vault / page_rel, vault=cfg.vault)
        doc.add_fact("state", "The digest run moved to the GPU host.", "observed", TODAY, ["s1"])
        (cfg.vault / page_rel).write_text(doc.render(), encoding="utf-8")

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [1]
    text = (cfg.vault / page_rel).read_text(encoding="utf-8")
    assert "~~Capture runs at session end.~~ [stated] 2026-09-20 [^s1] withdrawn 2026-10-05" in text
    assert "- The digest run moved to the GPU host." in text, "the line the dream added stands"


def test_identical_lines_are_told_apart_by_the_saved_number(initialized, home, fake):
    """Two identical lines are told apart by the saved line number: the one nearest it
    is struck, and its twin stands (review fix 1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    body = OVERVIEW_BODY.replace(
        "- Capture runs at session end. [stated] 2026-09-20 [^s1]",
        "- Capture runs at session end. [stated] 2026-09-20 [^s1]\n"
        "- Capture runs at session end. [stated] 2026-09-20 [^s1]",
    )
    seed_page(cfg, "projects/widget/overview.md", body)
    fake.script = [withdraw_reply("L3")]
    review.review(cfg, "widget", now=NOW)
    page_rel = "projects/widget/overview.md"
    with vault.locked(cfg):
        doc = pagewrite.PageDoc.read(cfg.vault / page_rel, vault=cfg.vault)
        doc.add_fact("state", "The digest run moved to the GPU host.", "observed", TODAY, ["s1"])
        (cfg.vault / page_rel).write_text(doc.render(), encoding="utf-8")

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [1]
    text = (cfg.vault / page_rel).read_text(encoding="utf-8")
    assert text.count("~~Capture runs at session end.~~") == 1, "only one twin is struck"
    assert text.count("- Capture runs at session end. [stated]") == 1, "the twin stands"


def test_a_number_with_a_result_is_left_as_it_is(initialized, home, fake):
    """A number whose finding already carries its result is left as it is, as ``all``
    leaves it: the dated record stands and nothing is committed (review fix 2)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1", "L3")]
    review.review(cfg, "widget", now=NOW)
    review.withdraw(cfg, "widget", "2", now=NOW)
    after_first = review.report_path(cfg.vault, "widget").read_text(encoding="utf-8")
    page_before = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")

    outcome = review.withdraw(cfg, "widget", "2", now=NOW)

    assert outcome.withdrawn == ()
    assert outcome.skipped == ()
    assert not outcome.committed, "nothing changed, so nothing is committed"
    assert review.report_path(cfg.vault, "widget").read_text(encoding="utf-8") == after_first
    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == page_before
    frontmatter, _ = read_report(cfg, "widget")
    results = {f["number"]: f["result"] for f in frontmatter["aft"]["review"]["findings"]}
    assert results == {1: None, 2: "withdrawn 2026-10-05"}

    later = review.withdraw(cfg, "widget", "1,2", now=NOW)

    assert [f.number for f in later.withdrawn] == [1], "the line with a result is passed over"
    frontmatter, _ = read_report(cfg, "widget")
    results = {f["number"]: f["result"] for f in frontmatter["aft"]["review"]["findings"]}
    assert results == {1: "withdrawn 2026-10-05", 2: "withdrawn 2026-10-05"}


def test_a_list_saved_before_the_lock_is_not_lost(initialized, home, fake, monkeypatch):
    """The saved list is read and the numbers resolved under the vault lock (review fix
    3): a review that saves a new list before the withdrawal takes the lock is acted
    on, not overwritten by the withdrawal's stale copy."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L2")]
    review.review(cfg, "widget", now=NOW)
    decisions = next(
        fact
        for fact in page.parse(
            (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8"),
            Path("projects/widget/overview.md"),
        ).facts
        if fact.statement == "Capture runs at session end."
    )
    newer = review.ReviewOutcome(
        scope="widget",
        reviewed=3,
        pages_read=1,
        findings=(
            review.Finding(
                number=1,
                page="projects/widget/overview.md",
                line=decisions.line,
                section="Decisions",
                statement="Capture runs at session end.",
                tag="stated",
                date="2026-09-20",
                refs=("s1",),
                reason="progress on the work",
            ),
        ),
        calls=1,
        shown=3,
    )

    from aft import vault as vaultmod

    real_locked = vaultmod.locked

    @contextmanager
    def locked(config):
        with real_locked(config):
            # A concurrent review saved a new list while this withdrawal waited for the
            # lock; the withdrawal reads it under the lock, never writes the old copy back.
            path = review.report_path(config.vault, "widget")
            path.write_text(review.report_text(newer, NOW), encoding="utf-8")
            vaultmod.commit(config.vault, ["reports/review-widget.md"], "aft: review widget")
            yield

    monkeypatch.setattr(vaultmod, "locked", locked)

    outcome = review.withdraw(cfg, "widget", "1", now=NOW)

    assert [f.number for f in outcome.withdrawn] == [1]
    assert outcome.withdrawn[0].statement == "Capture runs at session end."
    text = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    assert "~~Capture runs at session end.~~" in text
    assert "~~The GPU host runs vLLM" not in text, "the stale copy's line is left alone"
    frontmatter, _ = read_report(cfg, "widget")
    assert (
        frontmatter["aft"]["review"]["findings"][0]["statement"] == "Capture runs at session end."
    )
    assert frontmatter["aft"]["review"]["findings"][0]["result"] == "withdrawn 2026-10-05"


def test_a_noted_line_is_reviewed_but_the_withdrawal_skips_it(initialized, home, fake):
    """A line with an open note is active memory too (DESIGN 6 injects it), so the
    review asks about it; the withdrawal still skips it for ``aft settle`` (review
    fix 4)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    page_rel = "projects/widget/overview.md"
    with vault.locked(cfg):
        doc = pagewrite.PageDoc.read(cfg.vault / page_rel, vault=cfg.vault)
        doc.add_callout(
            doc.fact("The capture hook enqueues work and returns."),
            "conflict",
            "A session says the hook runs at session start.",
            TODAY,
        )
        doc.add_callout(
            doc.fact("The GPU host runs vLLM for the digest model."),
            "question",
            "Which card does the digest model run on?",
            TODAY,
        )
        (cfg.vault / page_rel).write_text(doc.render(), encoding="utf-8")
    fake.script = [withdraw_reply("L1")]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.reviewed == 3, "the noted lines are active memory and are asked about"
    user = fake.requests[0]["messages"][1]["content"]
    assert "L1 (state, observed, overview.md" in user
    assert "L2 (state, observed, overview.md" in user
    assert outcome.findings[0].statement == "The capture hook enqueues work and returns."

    withdrawal = review.withdraw(cfg, "widget", "1", now=NOW)

    assert withdrawal.withdrawn == ()
    assert withdrawal.skipped[0][1] == "it has an open note; aft settle settles it"
    text = (cfg.vault / page_rel).read_text(encoding="utf-8")
    assert "~~The capture hook" not in text


def test_a_page_outside_the_fact_pages_refuses_the_withdrawal(initialized, home, fake):
    """A saved finding's page is validated the way ``aft settle`` validates the page it
    is given (review fix 6): inside the vault, under a directory fact pages live in."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1")]
    review.review(cfg, "widget", now=NOW)
    page_before = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")
    report = review.report_path(cfg.vault, "widget")
    escaped = report.read_text(encoding="utf-8").replace(
        "projects/widget/overview.md", "projects/widget/../widget/overview.md"
    )
    report.write_text(escaped, encoding="utf-8")

    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "widget", "1", now=NOW)
    assert "not a fact page" in str(raised.value)

    outside = report.read_text(encoding="utf-8").replace(
        "projects/widget/../widget/overview.md", "digests/widget/2026/10/05.md"
    )
    report.write_text(outside, encoding="utf-8")
    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "widget", "1", now=NOW)
    assert "not a fact page" in str(raised.value)
    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == page_before


def test_a_list_saved_for_another_scope_is_refused(initialized, home, fake):
    """The saved list is checked to be the requested scope's list (review fix 6): a
    copy filed under another slug is refused, not withdrawn as that scope's memory."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    register(cfg, "gadget")
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    seed_page(cfg, "projects/gadget/overview.md", OVERVIEW_BODY, project="p_gadget", slug="gadget")
    fake.script = [withdraw_reply("L1")]
    review.review(cfg, "widget", now=NOW)
    saved = review.report_path(cfg.vault, "widget").read_text(encoding="utf-8")
    copy = review.report_path(cfg.vault, "gadget")
    copy.write_text(saved, encoding="utf-8")
    vault.commit(cfg.vault, ["reports/review-gadget.md"], "copy the list by hand")

    with pytest.raises(review.ReviewError) as raised:
        review.withdraw(cfg, "gadget", "1", now=NOW)
    assert "is for widget, not gadget" in str(raised.value)


def test_the_cli_names_unreadable_pages_when_nothing_is_active(initialized, home, fake, capsys):
    """A scope with no active lines but an unreadable page names the page, instead of
    only saying there is nothing to review (review fix 7)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    scratch = cfg.vault / "projects/widget/scratch.md"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    scratch.write_text("# Scratch\n\n- Not a fact page.\n", encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/scratch.md"], "seed scratch")
    from aft.cli import main

    assert main(["review", "widget"]) == 0
    out = capsys.readouterr().out
    assert "no active memory lines in widget" in out
    assert "projects/widget/scratch.md" in out, "the unreadable page is named"
    assert "not a fact page" in out


def test_a_part_that_judged_nothing_is_not_counted_as_kept(initialized, home, fake):
    """A review whose every part failed says so: no line was judged, and the Kept
    sentence counts only the lines the model saw (review fix 8)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [Reply(status=500)]

    outcome = review.review(cfg, "widget", now=NOW)

    assert outcome.calls == 0
    assert outcome.shown == 0
    _, body = read_report(cfg, "widget")
    assert "Nothing. No line was judged; see Incomplete for why." in body
    assert "The other 0 line(s) the model saw stay as they are." in body


def test_a_withdrawn_report_keeps_the_review_date(initialized, home, fake):
    """A report a withdrawal rewrites keeps the review date in its body: the sentence
    counts the review's model calls, not the withdrawal's day (review fix 8)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg)
    seed_page(cfg, "projects/widget/overview.md", OVERVIEW_BODY)
    fake.script = [withdraw_reply("L1")]
    review.review(cfg, "widget", now=NOW)

    review.withdraw(cfg, "widget", "1", now=NOW + timedelta(days=1))

    _, body = read_report(cfg, "widget")
    assert "in 1 model call(s) on 2026-10-05" in body
    assert "call(s) on 2026-10-06" not in body, "the withdrawal's day is not the review's"
