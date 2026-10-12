"""Retrieval and the decision call (BUILD-PLAN M4.3, design 9 steps 2 and 3).

Every model call goes to the fake endpoint and every page is written into a temporary
vault. The tests name the decision the model gives and check what reaches the rules: the
action, the line it names, and the outcome ``rules.check`` returns for them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aft import config as configmod
from aft import schema as schemamod
from aft.dream import decide, rules
from fakes.model_server import FakeEndpoint, Reply

RUN_PROJECT = "p_widget"
OTHER_PROJECT = "p_other"
TODAY = "2026-10-05"
NEWER = "2026-10-04"

S9 = "aft://session/claude/9999aaaa#entry-4"

PROJECT_PAGE = f"""---
type: Project
title: widget
description: The widget project.
sources:
  - id: s1
    resource: aft://session/claude/2c1e9d0a#entry-148
    title: Design session
    quote: "the capture hook enqueues and returns"
  - id: s2
    resource: aft://session/pi/01a0e256#entry-88
    title: GPU session
    quote: "the gpu host runs vllm"
  - id: s3
    resource: aft://session/pi/01a0e256#entry-90
    title: Old index
    quote: "store the index as json"
aft:
  project: {RUN_PROJECT}
---

## State

- The capture hook enqueues work and returns. [observed] 2026-09-30 [^s1]
- The GPU host runs vLLM for the digest model. [observed] 2026-09-30 [^s2]

## Decisions

- ~~The index is stored as JSON files.~~ [stated] 2026-09-01 [^s3] superseded 2026-09-20 by [^s1]
- Capture runs at session end. [stated] 2026-09-20 [^s1] (stale 2026-09-25: words gone)

## Candidates

- The GPU host needs a second card. [inferred] 2026-09-30 [^s2] (signals 1/3, sessions 1/2)
"""

GLOBAL_PAGE = """---
type: Preferences
title: Preferences
description: What Sam wants everywhere.
sources:
  - id: s1
    resource: aft://session/claude/2c1e9d0a#entry-7
    title: Setup
    quote: "no rgb lighting on the gpu host"
---

## Preferences

- No RGB lighting on the GPU host. [stated] 2026-09-01 [^s1]
"""

OTHER_PAGE = f"""---
type: Project
title: other
description: Another project.
sources:
  - id: s1
    resource: aft://session/claude/3333#entry-1
    title: Other
    quote: "the gpu host runs sglang"
aft:
  project: {OTHER_PROJECT}
---

## State

- The GPU host runs SGLang for the other project. [observed] 2026-09-30 [^s1]
"""

REPORT_PAGE = """---
type: Dream Report
title: 2026-10-04
---

## Added

- The GPU host runs vLLM. [observed] 2026-09-30 [^s1]
"""

VLLM = "The GPU host runs vLLM for the digest model."
CAPTURE = "The capture hook enqueues work and returns."
CARD = "The GPU host needs a second card."
RGB = "No RGB lighting on the GPU host."


@pytest.fixture
def vault(cfg) -> Path:
    root = cfg.vault
    for rel, text in {
        "projects/widget/overview.md": PROJECT_PAGE,
        "projects/other/overview.md": OTHER_PAGE,
        "global/preferences.md": GLOBAL_PAGE,
        "reports/2026/10/2026-10-04.md": REPORT_PAGE,
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


@pytest.fixture
def fake():
    endpoint = FakeEndpoint()
    endpoint.start()
    yield endpoint
    endpoint.stop()


def _config(home: Path, vault: Path, fake: FakeEndpoint) -> configmod.Config:
    (home / "config.toml").write_text(
        f'vault = "{vault}"\nhost = "testhost"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
    )
    return configmod.load()


def _fact(statement: str, *, kind: str = "state", scope: str = "project") -> rules.NewFact:
    return rules.NewFact(statement=statement, kind=kind, source="stated", date=NEWER, scope=scope)


def _evidence() -> tuple[rules.Evidence, ...]:
    return (
        rules.Evidence(
            source="s9",
            resource=S9,
            origin=rules.PRIMARY,
            date=NEWER,
            verified=True,
            yours=True,
        ),
    )


def _label(proposal: decide.Proposal, statement: str) -> str:
    return next(line.label for line in proposal.lines if line.fact.statement == statement)


# The decision call


@pytest.mark.parametrize(
    ("action", "named", "outcome"),
    [
        ("add", None, rules.apply("add")),
        ("confirm", VLLM, rules.apply("confirm")),
        ("supersede", VLLM, rules.apply("supersede")),
        ("conflict", VLLM, rules.apply("conflict")),
        ("skip", None, rules.apply("skip")),
    ],
)
def test_each_action_reaches_the_rules_with_its_target(home, vault, fake, action, named, outcome):
    cfg = _config(home, vault, fake)
    new_fact = _fact("The GPU host runs SGLang for the digest model.")
    lines = decide.related(cfg.vault, new_fact, "widget")
    label = next((line.label for line in lines if line.fact.statement == named), None)
    fake.script = [fake_reply(action, label)]

    proposal = decide.decide(cfg, new_fact, "switched the gpu host to sglang", "widget")

    assert proposal.decision == rules.Decision(action, label, "because")
    if named is None:
        assert proposal.target is None
    else:
        assert proposal.target is not None
        assert proposal.target.fact.statement == named
        assert proposal.target.path == "projects/widget/overview.md"
        assert proposal.target.project == RUN_PROJECT
        assert set(proposal.target.sources) == {"s2"}
    context = proposal.context(TODAY, _evidence(), RUN_PROJECT)
    assert context.page_project == RUN_PROJECT
    assert proposal.check(context) == outcome


def test_a_candidate_line_is_carried_as_the_target(home, vault, fake):
    cfg = _config(home, vault, fake)
    new_fact = _fact("The GPU host needs a second card.")
    lines = decide.related(cfg.vault, new_fact, "widget")
    fake.script = [fake_reply("confirm", next(x.label for x in lines if x.fact.statement == CARD))]

    proposal = decide.decide(cfg, new_fact, "it needs a second card", "widget")

    assert proposal.target is not None
    assert proposal.target.fact.kind is None
    assert proposal.target.fact.statement == CARD


def test_a_named_line_outside_the_list_is_rejected(home, vault, fake):
    cfg = _config(home, vault, fake)
    new_fact = _fact("The capture hook enqueues work.")
    lines = decide.related(cfg.vault, new_fact, "widget")
    assert len(lines) < decide.LIMIT
    outside = f"L{len(lines) + 1}"
    fake.script = [fake_reply("confirm", outside)]

    proposal = decide.decide(cfg, new_fact, "the hook enqueues", "widget")

    assert proposal.decision.line == outside
    assert proposal.target is None
    context = proposal.context(TODAY, _evidence(), RUN_PROJECT)
    assert proposal.check(context) == rules.reject("M4.3 decision schema")


@pytest.mark.parametrize("action", ["confirm", "supersede", "conflict"])
def test_an_action_that_edits_a_line_without_naming_one_is_rejected(home, vault, fake, action):
    cfg = _config(home, vault, fake)
    fake.script = [fake_reply(action, None)]

    proposal = decide.decide(cfg, _fact(CAPTURE), "the hook enqueues", "widget")

    assert proposal.target is None
    context = proposal.context(TODAY, _evidence(), RUN_PROJECT)
    assert proposal.check(context) == rules.reject("M4.3 decision schema")


def test_a_line_named_on_add_or_skip_is_not_a_target(home, vault, fake):
    cfg = _config(home, vault, fake)
    fake.script = [fake_reply("add", "L1")]

    proposal = decide.decide(cfg, _fact(CAPTURE), "the hook enqueues", "widget")

    assert proposal.decision.line == "L1"
    assert proposal.target is None


def test_the_call_sends_the_prompt_the_schema_and_the_numbered_lines(home, vault, fake):
    cfg = _config(home, vault, fake)
    fake.script = [fake_reply("skip", None)]
    new_fact = _fact("The GPU host runs SGLang for the digest model.")

    proposal = decide.decide(cfg, new_fact, 'switched to "sglang"', "widget")

    body = fake.requests[0]
    system, user = (message["content"] for message in body["messages"])
    assert system == decide.PROMPT_PATH.read_text()
    assert body["response_format"]["json_schema"]["schema"] == json.loads(
        decide.SCHEMA_PATH.read_text()
    )
    assert (
        "New fact ([stated], state, long term, 2026-10-04):"
        " The GPU host runs SGLang for the digest model." in user
    )
    assert 'Quote: "switched to \\"sglang\\""' in user
    for line in proposal.lines:
        kind = line.fact.kind or "candidate"
        assert (
            f"{line.label} ([{line.fact.source}], {kind}, {line.fact.date}):"
            f" {line.fact.statement}\n" in user
        )


def test_the_context_shows_each_lines_tag_date_and_quote(home, vault, fake):
    """M14: every existing line shows its tag, date and one quote, so the model can tell
    newer evidence about the same thing from a conflict without reading the page."""
    cfg = _config(home, vault, fake)
    fake.script = [fake_reply("skip", None)]

    proposal = decide.decide(cfg, _fact(VLLM), "the gpu host runs vllm", "widget")

    user = fake.requests[0]["messages"][1]["content"]
    observed = next(line for line in proposal.lines if line.fact.statement == VLLM)
    assert (
        f"{observed.label} ([observed], state, 2026-09-30): {VLLM}\n"
        '  Quote: "the gpu host runs vllm"\n' in user
    )
    candidate = next(line for line in proposal.lines if line.fact.statement == CARD)
    assert (
        f"{candidate.label} ([inferred], candidate, 2026-09-30): {CARD}\n"
        '  Quote: "the gpu host runs vllm"\n' in user
    )


def test_a_long_source_quote_is_cut_to_the_limit(vault):
    """The context shows the words a line stands on, not the whole source entry."""
    long_quote = "the gpu host runs vllm and the digest model follows it " * 5
    assert len(long_quote) > decide.QUOTE_LIMIT
    (vault / "projects" / "widget" / "overview.md").write_text(
        PROJECT_PAGE.replace('quote: "the gpu host runs vllm"', f'quote: "{long_quote.strip()}"')
    )

    new_fact = _fact(VLLM)
    rendered = decide.render(new_fact, "quote", decide.related(vault, new_fact, "widget"))

    expected = "  Quote: " + json.dumps(
        long_quote.strip()[: decide.QUOTE_LIMIT].rstrip() + "...", ensure_ascii=False
    )
    assert expected in rendered


def test_a_line_whose_footnote_has_no_entry_shows_no_quote(vault):
    """A footnote the page's frontmatter does not name gives no quote; the line still
    shows its tag and date."""
    (vault / "projects" / "widget" / "overview.md").write_text(
        PROJECT_PAGE.replace("[^s2]", "[^s9]")
    )

    new_fact = _fact(VLLM)
    lines = decide.related(vault, new_fact, "widget")
    vllm = next(line for line in lines if line.fact.statement == VLLM)
    rendered = decide.render(new_fact, "quote", lines)

    assert f"{vllm.label} ([observed], state, 2026-09-30): {VLLM}\n  Quote: none.\n" in rendered


def test_a_fact_with_no_related_lines_is_still_asked(home, vault, fake):
    cfg = _config(home, vault, fake)
    fake.script = [fake_reply("add", None)]

    proposal = decide.decide(cfg, _fact("Zebras xylophone quietly."), "zebras", "widget")

    assert proposal.lines == ()
    assert "Existing lines: none." in fake.requests[0]["messages"][1]["content"]
    assert proposal.check(proposal.context(TODAY, _evidence(), RUN_PROJECT)) == rules.apply("add")


def test_an_answer_that_fails_the_schema_twice_raises(home, vault, fake):
    cfg = _config(home, vault, fake)
    fake.script = [Reply(content={"action": "merge", "line": None, "reason": "no"})]

    with pytest.raises(decide.model.ModelError):
        decide.decide(cfg, _fact(CAPTURE), "the hook enqueues", "widget")
    assert len(fake.requests) == 2


def test_a_reason_over_200_characters_fails_the_schema(home, vault, fake):
    cfg = _config(home, vault, fake)
    fake.script = [Reply(content={"action": "add", "line": None, "reason": "x" * 201})]

    with pytest.raises(decide.model.ModelError):
        decide.decide(cfg, _fact(CAPTURE), "the hook enqueues", "widget")


# The schema and the prompt


def test_the_schema_is_the_one_the_rules_expect():
    loaded = decide.schema()
    schemamod.check(loaded)
    properties = loaded["properties"]
    assert tuple(properties["action"]["enum"]) == rules.MODEL_ACTIONS
    assert properties["line"]["enum"] == [f"L{n}" for n in range(1, decide.LIMIT + 1)] + [None]
    assert properties["reason"]["maxLength"] == 200
    assert set(loaded["required"]) == {"action", "line", "reason"}
    for answer in (
        {"action": "add", "line": None, "reason": "new"},
        {"action": "confirm", "line": "L10", "reason": "same"},
    ):
        schemamod.validate(answer, loaded)
    for answer in (
        {"action": "confirm", "line": "L11", "reason": "same"},
        {"action": "confirm", "line": "L1"},
        {"action": "add", "line": None, "reason": ""},
    ):
        with pytest.raises(schemamod.SchemaError):
            schemamod.validate(answer, loaded)


# Retrieval


def test_retrieval_ranks_the_closest_line_first(vault):
    lines = decide.related(vault, _fact("The capture hook enqueues work."), "widget")

    assert lines[0].fact.statement == CAPTURE
    assert [line.label for line in lines] == [f"L{n}" for n in range(1, len(lines) + 1)]


def test_retrieval_stems_so_pausing_finds_paused(vault):
    """Design 10: retrieval stems its words like the search index, so "pausing" finds the
    line that says "paused"; without the stemmer the two share no word."""
    text = PROJECT_PAGE.replace(
        "- The capture hook enqueues work and returns. [observed] 2026-09-30 [^s1]",
        "- The capture queue is paused. [observed] 2026-09-30 [^s1]",
    )
    (vault / "projects" / "widget" / "overview.md").write_text(text)

    lines = decide.related(vault, _fact("Pausing was deliberate."), "widget")

    assert [line.fact.statement for line in lines] == ["The capture queue is paused."]


def test_a_resumed_fact_is_shown_the_paused_lines_date(vault):
    """M14's example: "resumed" against a "paused" line matches on the thing itself, and
    the dates in the context are what let the model resolve it as a supersede."""
    text = PROJECT_PAGE.replace(
        "- The capture hook enqueues work and returns. [observed] 2026-09-30 [^s1]",
        "- The search indexer is paused. [observed] 2026-09-30 [^s1]",
    )
    (vault / "projects" / "widget" / "overview.md").write_text(text)

    new_fact = _fact("The user resumed the search indexer.")
    lines = decide.related(vault, new_fact, "widget")

    assert "The search indexer is paused." in {line.fact.statement for line in lines}
    rendered = decide.render(new_fact, "resumed the search indexer", lines)
    assert "New fact ([stated], state, long term, 2026-10-04):" in rendered
    assert "([observed], state, 2026-09-30): The search indexer is paused." in rendered


def test_retrieval_takes_active_and_candidate_lines_only(vault):
    lines = decide.related(vault, _fact("Index JSON files capture session end."), "widget")
    found = {line.fact.statement for line in lines}

    assert "The index is stored as JSON files." not in found  # superseded
    assert "Capture runs at session end." not in found  # stale
    assert CAPTURE in found

    candidates = decide.related(vault, _fact("A second card for the GPU host."), "widget")
    assert CARD in {line.fact.statement for line in candidates}


def test_a_project_fact_sees_only_its_own_project_pages(vault):
    lines = decide.related(vault, _fact("The GPU host runs SGLang."), "widget")
    paths = {line.path for line in lines}

    assert paths == {"projects/widget/overview.md"}


def test_a_global_fact_sees_only_global_pages(vault):
    lines = decide.related(vault, _fact(RGB, kind="preference", scope="global"), "widget")
    by_statement = {line.fact.statement: line for line in lines}

    assert lines[0].fact.statement == RGB
    assert by_statement[RGB].path == "global/preferences.md"
    assert by_statement[RGB].project is None
    assert all(line.path.startswith("global/") for line in lines)
    others = decide.related(vault, _fact(VLLM, scope="global"), "widget")
    assert [line.fact.statement for line in others] == [RGB]


def test_a_global_add_targets_a_global_page(vault):
    new_fact = _fact(RGB, kind="preference", scope="global")
    proposal = decide.Proposal(new_fact, (), rules.Decision("add", None, "new"), None)

    assert proposal.page_project(RUN_PROJECT) is None
    assert proposal.check(proposal.context(TODAY, _evidence(), RUN_PROJECT)) == rules.apply("add")


def test_retrieval_never_reads_reports(vault):
    lines = decide.related(vault, _fact("The GPU host runs vLLM.", scope="global"), "widget")

    assert not any(line.path.startswith("reports/") for line in lines)


def test_retrieval_returns_at_most_ten_lines(tmp_path):
    body = "\n".join(
        f"- The widget cache holds entry {n}. [observed] 2026-09-30 [^s1]" for n in range(15)
    )
    page_text = PROJECT_PAGE.split("## State")[0] + "## State\n\n" + body + "\n"
    path = tmp_path / "projects" / "widget" / "overview.md"
    path.parent.mkdir(parents=True)
    path.write_text(page_text)

    lines = decide.related(tmp_path, _fact("The widget cache holds entry 3."), "widget")

    assert len(lines) == decide.LIMIT
    assert lines[0].fact.statement == "The widget cache holds entry 3."
    assert lines[-1].label == "L10"


def test_a_project_with_no_pages_has_no_lines(tmp_path):
    assert decide.related(tmp_path, _fact(CAPTURE), None) == ()
    assert decide.related(tmp_path, _fact(CAPTURE), "missing") == ()


def test_a_page_whose_frontmatter_does_not_parse_gives_no_lines(tmp_path):
    path = tmp_path / "projects" / "widget" / "overview.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nsources: [unclosed\n---\n\n## State\n\n" + PROJECT_PAGE.split("## State")[1]
    )

    assert decide.related(tmp_path, _fact(CAPTURE), "widget") == ()


def fake_reply(action: str, line: str | None) -> Reply:
    return Reply(content={"action": action, "line": line, "reason": "because"})


def test_the_label_named_is_the_line_carried(home, vault, fake):
    cfg = _config(home, vault, fake)
    new_fact = _fact("The GPU host runs SGLang for the digest model.")
    lines = decide.related(cfg.vault, new_fact, "widget")
    assert len(lines) >= 3
    for line in lines[1:]:
        fake.script = [fake_reply("confirm", line.label)]
        proposal = decide.decide(cfg, new_fact, "switched to sglang", "widget")
        assert proposal.target == line


def test_a_global_fact_carries_a_global_line_as_its_target(home, vault, fake):
    cfg = _config(home, vault, fake)
    new_fact = _fact(RGB, kind="preference", scope="global")
    lines = decide.related(cfg.vault, new_fact, "widget")
    fake.script = [fake_reply("confirm", next(x.label for x in lines if x.fact.statement == RGB))]

    proposal = decide.decide(cfg, new_fact, "still no rgb", "widget")

    assert proposal.target is not None
    assert proposal.target.path == "global/preferences.md"
    context = proposal.context(TODAY, _evidence(), RUN_PROJECT)
    assert context.page_project is None
    assert proposal.check(context) == rules.apply("confirm")


def test_a_project_page_without_its_project_gives_no_lines(vault):
    text = PROJECT_PAGE.replace(f"aft:\n  project: {RUN_PROJECT}\n", "")
    (vault / "projects" / "widget" / "overview.md").write_text(text)

    for scope in ("project", "global"):
        lines = decide.related(vault, _fact(CAPTURE, scope=scope), "widget")
        assert not any(line.path.startswith("projects/") for line in lines)


def test_a_page_is_global_by_its_path(vault):
    path = vault / "global" / "preferences.md"
    path.write_text(GLOBAL_PAGE.replace("---\n\n##", f"aft:\n  project: {RUN_PROJECT}\n---\n\n##"))

    lines = decide.related(vault, _fact(RGB, kind="preference", scope="global"), "widget")

    assert next(x for x in lines if x.fact.statement == RGB).project is None


def test_the_decide_prompt_keeps_conflict_to_the_same_thing():
    """A conflict is two claims about one thing that cannot both hold; anything else is new."""
    words = " ".join(decide.prompt().split())
    assert "say different things about the same thing, so both cannot be true" in words
    assert "Before naming a line for supersede or conflict, check that it is about" in words
    assert "is not a supersede or a conflict: choose confirm if a line already" in words


def test_the_decide_prompt_keeps_the_plans_text():
    """The plan's own words still ship (BUILD-PLAN M4.3). M14 extends the prompt between
    the plan's lines; it never rewords or drops one."""
    plan = Path(__file__).resolve().parents[1] / "planning" / "BUILD-PLAN.md"
    lines = plan.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if "(`src/aft/prompts/decide.md`)" in line)
    fences = [i for i in range(start, len(lines)) if lines[i].strip() in ("```text", "```")][:2]
    plan_prompt = lines[fences[0] + 1 : fences[1]]

    shipped = decide.prompt().splitlines(keepends=True)
    cursor = 0
    for plan_line in plan_prompt:
        while cursor < len(shipped) and shipped[cursor] != plan_line:
            cursor += 1
        assert cursor < len(shipped), f"the plan's line is gone from the prompt: {plan_line!r}"
        cursor += 1


def test_the_decide_prompt_explains_the_fields():
    """M14: the prompt explains the tag, date and quote fields, and dates decide a
    supersede from a conflict."""
    words = " ".join(decide.prompt().split())
    assert (
        "Each line shows its source tag in brackets, its date, and one quote from its"
        " evidence; the new fact shows its own tag, date and quote." in words
    )
    assert "[stated] the user, [observed] a session, [inferred] or [suggested] the model" in words
    assert (
        "Newer evidence about the same thing that says something different means the thing"
        " has changed: supersede." in words
    )
    assert (
        "Evidence that is not newer does not replace a line: confirm when the two say the"
        " same thing, conflict when they say different things that cannot both be true." in words
    )


def test_the_decide_prompt_says_questions_proposals_requests_are_not_decisions():
    """F52: a question, proposal or request from the user is never a decision or preference."""
    words = " ".join(decide.prompt().split())
    assert (
        "A question, a proposal or a request from the user is never a decision or a preference."
        in words
    )


def test_the_decide_prompt_says_new_fact_changing_how_something_is_done_supersedes():
    """F52: a new fact changing how something is done supersedes an existing line."""
    words = " ".join(decide.prompt().split())
    assert (
        "A new fact that changes how something is done, which an existing line describes, "
        "supersedes that line rather than adding beside it." in words
    )


def test_the_decide_prompt_skips_by_the_value_test_and_the_never_a_fact_list():
    """F46: skip applies the digest's one test and never-a-fact list to the fact."""
    words = " ".join(decide.prompt().split())
    assert (
        "a future session would make a mistake, ask the user again, or give less "
        "fitting help without it" in words
    )
    assert "Never a fact, long or short term" in words
    for item in (
        "the status of a pull request, task, branch or commit",
        "who is working on what",
        "a test's name or result",
        "a review's outcome",
        "what a change or fix did to the code",
        "anything the repository's own documents already say",
    ):
        assert item in words
    assert "it fails that test, is on the never-a-fact list above" in words


def test_the_decide_prompt_keeps_facts_about_the_users_life():
    """F73: a fact about the user's life, interests, tastes or plans passes the test, and
    a dated event is a durable record kept long-term with its date in the statement."""
    words = " ".join(decide.prompt().split())
    assert "A fact about the user's life, interests, tastes or plans passes it." in words
    assert (
        'A dated event is a durable record ("On 2024-03-02 the user adopted a dog"), '
        "not a fact that stops being true, so it is kept long-term with its date in the "
        "statement." in words
    )


def test_the_decide_prompt_defines_short_term_as_a_temporary_condition():
    """F46: short-term memory is a temporary condition, not progress on the work."""
    words = " ".join(decide.prompt().split())
    assert "a temporary condition that changes how an agent should act" in words
    assert "not progress on the work" in words
    assert "last few sessions were working with" not in words
