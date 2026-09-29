"""Tests for M4.5: apply, maintenance, and commit.

DESIGN 9, steps 5 to 7, DESIGN 15 M4 fixture suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aft import config as configmod
from aft import evidence, inject, page, pagewrite, vault
from aft.dream import apply, rules, schedule
from aft.registry import Registry, path_key
from conftest import make_repo, run_git
from fakes.model_server import FakeEndpoint, Reply

TODAY = "2026-10-05"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=95)
YESTERDAY = "2026-10-04"
RUN_PROJECT = "p_widget"


@pytest.fixture
def fake():
    endpoint = FakeEndpoint()
    endpoint.start()
    yield endpoint
    endpoint.stop()


def config_with_fake(home: Path, vault_path: Path, fake: FakeEndpoint) -> configmod.Config:
    (home / "config.toml").write_text(
        f'vault = "{vault_path}"\nhost = "testhost"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
    )
    return configmod.load()


def reply(action: str, line: str | None = None, reason: str = "because") -> Reply:
    return Reply(content={"action": action, "line": line, "reason": reason})


def write_digest(
    cfg: configmod.Config,
    slug: str,
    filename: str,
    *,
    facts: list[dict],
    sources: list[dict],
    role: str = "primary",
    at: str = "2026-10-05T08:00:00Z",
    consumed: str | None = None,
    store_evidence: bool = True,
) -> Path:
    if store_evidence:
        from aft.readers import USER, Entry

        db_path = cfg.home / "evidence.sqlite"
        existing_keys: set[tuple[str, str]] = set()
        if db_path.exists():
            with evidence._reading(cfg) as conn:
                rows = conn.execute("SELECT session_id, entry_id FROM evidence").fetchall()
                existing_keys = {(str(r[0]), str(r[1])) for r in rows}

        by_session: dict[str, list[tuple[str, Entry]]] = {}
        for s in sources:
            res = s.get("resource", "")
            q = s.get("quote", "")
            if res.startswith("aft://session/") and "#" in res:
                uri = res.removeprefix("aft://session/")
                parts = uri.split("#", 1)
                agent_sess = parts[0].split("/")
                agent = agent_sess[0] if len(agent_sess) > 1 else "claude"
                sess_id = agent_sess[-1]
                entry_id = parts[1]
                if (sess_id, entry_id) in existing_keys:
                    continue
                by_session.setdefault(sess_id, []).append(
                    (agent, Entry(id=entry_id, time=NOW, role=USER, text=q, is_error=False))
                )
        for sess_id, entries_with_agent in by_session.items():
            agent = entries_with_agent[0][0]
            entries = [e for _, e in entries_with_agent]
            evidence.store(
                cfg,
                agent=agent,
                session_id=sess_id,
                project_id=f"p_{slug}",
                entries=entries,
            )

    rel = f"digests/{slug}/2026/10/{filename}.md"
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)

    aft_dict: dict[str, object] = {"project": f"p_{slug}", "role": role}
    if consumed:
        aft_dict["consumed"] = consumed
    aft_dict["facts"] = facts

    fm = {
        "type": "Session Digest",
        "title": f"Digest {filename}",
        "description": "Summary of session.",
        "generated": {"by": "aft", "at": at},
        "sources": sources,
        "aft": aft_dict,
    }
    fm_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()

    body_lines = [""]
    by_section: dict[str, list[dict]] = {}
    for f in facts:
        kind = f.get("kind", "fact")
        section = pagewrite.SECTION_FOR_KIND.get(kind, kind.capitalize())
        if section not in by_section:
            by_section[section] = []
        by_section[section].append(f)

    for section, sec_facts in by_section.items():
        body_lines.append(f"## {section}")
        body_lines.append("")
        for f in sec_facts:
            body_lines.append(
                f"- {f['statement']} [{f['source']}] {f.get('date', TODAY)} [^{f.get('ref', 's1')}]"
            )
        body_lines.append("")

    path.write_text(f"---\n{fm_str}\n---\n" + "\n".join(body_lines), encoding="utf-8")
    vault.commit(cfg.vault, [rel], f"seed digest {filename}")
    return path


def seed_overview(
    cfg: configmod.Config,
    slug: str,
    *,
    body: str,
    sources: list[dict] | None = None,
    anchors: dict | None = None,
) -> Path:
    overview = cfg.vault / "projects" / slug / "overview.md"
    overview.parent.mkdir(parents=True, exist_ok=True)
    registry = Registry.load(cfg.registry_path)
    proj = registry.by_slug(slug)
    proj_id = proj.id if proj else f"p_{slug}"

    aft_dict: dict[str, object] = {"project": proj_id}
    if anchors:
        aft_dict["anchors"] = anchors

    fm = {
        "type": "Project",
        "title": slug,
        "description": f"The {slug} project.",
        "sources": sources or [],
        "aft": aft_dict,
    }
    fm_str = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    overview.write_text(f"---\n{fm_str}\n---\n\n{body}", encoding="utf-8")
    vault.commit(cfg.vault, [f"projects/{slug}/overview.md"], f"seed overview {slug}")
    return overview


# M4 Fixture Suite from DESIGN 15


def test_no_op_run_makes_no_commit(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    before_sha = run_git(["rev-parse", "HEAD"], cfg.vault)
    result = schedule.run(cfg, now=NOW)
    assert result.waiting == {}
    assert result.ran == []
    assert run_git(["rev-parse", "HEAD"], cfg.vault) == before_sha


def test_add(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Existing line. [observed] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "existing quote"}],
    )

    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": "The queue uses SQLite WAL mode.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s10",
            }
        ],
        sources=[
            {
                "id": "s10",
                "resource": "aft://session/c/2#e5",
                "quote": "The queue uses SQLite WAL mode.",
            }
        ],
    )

    fake.script = [reply("add", None, "new decision")]
    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.ran
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].statement == "The queue uses SQLite WAL mode."

    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "- The queue uses SQLite WAL mode. [stated] 2026-10-05" in overview_text
    assert report.commit is not None


def test_confirm(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- The GPU host serves vLLM. [observed] 2026-09-20 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "serves vllm"}],
    )

    write_digest(
        cfg,
        "widget",
        "d2",
        facts=[
            {
                "statement": "The GPU host serves vLLM.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e3", "quote": "serves vllm"}],
    )

    fake.script = [reply("confirm", "L1", "confirmed")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.confirmed) == 1
    assert report.confirmed[0].statement == "The GPU host serves vLLM."

    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert f"- The GPU host serves vLLM. [observed] {TODAY} [^s1] [^s2]" in overview_text


@pytest.mark.parametrize(
    ("kind", "target_kind", "source", "replaces"),
    [
        ("preference", "Preferences", "stated", True),
        ("decision", "Decisions", "stated", True),
        ("state", "State", "observed", True),
        ("fact", "Facts", "observed", True),
        ("lesson", "Lessons", "observed", True),
        ("procedure", "Procedures", "observed", True),
        ("reference", "References", "observed", True),
    ],
)
def test_allowed_supersede_for_each_kind(
    initialized, home, fake, kind, target_kind, source, replaces
):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body=f"## {target_kind}\n\n- Old statement for {kind}. [{source}] 2026-09-01 [^s1]\n",
        sources=[
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": f"Old statement for {kind}."}
        ],
    )

    write_digest(
        cfg,
        "widget",
        f"d-{kind}",
        facts=[
            {
                "statement": f"New statement for {kind}.",
                "kind": kind,
                "source": source,
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {"id": "s2", "resource": "aft://session/c/2#e2", "quote": f"New statement for {kind}."}
        ],
    )

    fake.script = [reply("supersede", "L1", "updated configuration")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.superseded) == 1
    assert report.superseded[0].new_statement == f"New statement for {kind}."

    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert f"- New statement for {kind}. [{source}] {TODAY} [^s2]" in overview_text
    assert (
        f"- ~~Old statement for {kind}.~~ [{source}] 2026-09-01 [^s1] superseded {TODAY} by [^s2]"
        in overview_text
    )


def test_refused_supersede_becomes_conflict_or_needs_you(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    # Preference contradicted by observed evidence -> needs-you
    seed_overview(
        cfg,
        "widget",
        body=(
            "## Preferences\n\n"
            "- No RGB lighting. [stated] 2026-09-01 [^s1]\n\n"
            "## State\n\n"
            "- Server has 16 cores. [observed] 2026-09-20 [^s1]\n"
        ),
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "serves"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-conflict",
        facts=[
            {
                "statement": "RGB lighting is present on the server.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            },
            {
                "statement": "Server has 8 cores.",
                "kind": "state",
                "source": "observed",
                "date": "2026-09-10",  # older date than standing line (2026-09-20)
                "ref": "s3",
            },
        ],
        sources=[
            {"id": "s2", "resource": "aft://session/c/2#e1", "quote": "RGB lighting is present"},
            {"id": "s3", "resource": "aft://session/c/2#e2", "quote": "8 cores"},
        ],
    )

    # First call tries to supersede preference with observed -> needs-you
    # Second call tries to supersede state with older date -> conflict
    fake.script = [
        reply("supersede", "L1", "observed RGB"),
        reply("supersede", "L1", "seen 8 cores"),
    ]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.questions) == 1
    assert report.questions[0].new_statement == "RGB lighting is present on the server."
    assert len(report.conflicts) == 1
    assert report.conflicts[0].new_statement == "Server has 8 cores."

    # Standing lines untouched
    text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "- No RGB lighting. [stated] 2026-09-01 [^s1]" in text
    assert "- Server has 16 cores. [observed] 2026-09-20 [^s1]" in text


def test_tentative_statement_stays_suggested(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- The server runs Linux. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "linux"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-suggested",
        facts=[
            {
                "statement": "Maybe switch the server to BSD.",
                "kind": "state",
                "source": "suggested",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e2", "quote": "maybe switch"}],
    )

    fake.script = [reply("supersede", "L1", "maybe bsd")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    # suggested never replaces anything -> becomes conflict/needs-you
    assert len(report.conflicts) == 1
    assert report.conflicts[0].new_statement == "Maybe switch the server to BSD."


def test_inferred_fact_stays_a_candidate_below_the_gate(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Server runs Linux. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "linux"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-inferred",
        facts=[
            {
                "statement": "Workloads are mostly CPU-bound.",
                "kind": "fact",
                "source": "inferred",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e2", "quote": "mostly cpu"}],
    )

    fake.script = [reply("add", None, "inferred pattern")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.candidates) == 1
    assert len(report.downgrades) == 1
    assert report.downgrades[0].to_action == "candidate"

    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "## Candidates" in overview_text
    assert (
        "- Workloads are mostly CPU-bound. [inferred] 2026-10-05 [^s2] (signals 1/3, sessions 1/2)"
        in overview_text
    )


def test_worker_only_fact_stays_a_candidate(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- The server is online. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "online"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-worker",
        role="worker",
        facts=[
            {
                "statement": "Test runner completed in 40s.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            },
            {
                "statement": "The server is online.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s3",
            },
        ],
        sources=[
            {"id": "s2", "resource": "aft://session/w/1#e1", "quote": "runner completed"},
            {"id": "s3", "resource": "aft://session/w/1#e2", "quote": "server is online"},
        ],
    )

    fake.script = [
        reply("add", None, "test fact"),
        reply("confirm", "L1", "confirmed by worker"),
    ]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.candidates) == 1
    assert report.candidates[0].statement == "Test runner completed in 40s."
    # Worker confirm is rejected
    assert any("worker" in r.rule.lower() for r in report.rejected)


def test_cross_project_edit_refused(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    p2 = registry.add("gadget", "Gadget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Widget state. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "widget state"}],
    )
    # Page belonging to another project
    gadget_page = cfg.vault / "projects" / "gadget" / "overview.md"
    gadget_page.parent.mkdir(parents=True, exist_ok=True)
    gadget_page.write_text(f"""---
type: Project
title: gadget
sources:
  - id: s1
    resource: aft://session/c/9#e1
    quote: "gadget line"
aft:
  project: {p2.id}
---

## State

- Gadget line. [observed] 2026-09-01 [^s1]
""")
    vault.commit(cfg.vault, ["projects/gadget/overview.md"], "seed gadget")

    write_digest(
        cfg,
        "widget",
        "d-cross",
        facts=[
            {
                "statement": "New fact for widget.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e2", "quote": "quote"}],
    )

    # Retrieval in decide for project fact only retrieves widget pages;
    # If the proposal targeted gadget line, R6 rejects it
    fake.script = [reply("supersede", "L1", "cross edit")]
    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.reports
    # The proposal sees only widget lines, but if target is somehow outside, R6 rejects
    overview_text = gadget_page.read_text()
    assert "- Gadget line." in overview_text  # Gadget page untouched


def test_bad_quote_rejected(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state line"}],
    )

    from aft.readers import USER, Entry

    evidence.store(
        cfg,
        agent="claude",
        session_id="sess_quote",
        project_id="p_widget",
        entries=[
            Entry(id="e1", time=NOW, role=USER, text="the actual transcript words", is_error=False)
        ],
    )

    # Digest with fabricated quote that does NOT match the entry
    write_digest(
        cfg,
        "widget",
        "d-badquote",
        facts=[
            {
                "statement": "Fabricated statement.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_quote#e1",
                "quote": "words never spoken in the transcript",
            }
        ],
    )

    fake.script = [reply("add", None, "bad quote")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.rejected) == 1
    assert report.rejected[0].rule == "R1"
    assert (
        "Fabricated statement."
        not in (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    )


def test_broken_anchor_marked_stale(initialized, home, fake, tmp_path):
    cfg = config_with_fake(home, initialized.vault, fake)
    repo = make_repo(tmp_path / "widget")
    (repo / "code.py").write_text("def run(): pass\n")
    run_git(["add", "code.py"], repo)
    run_git(["commit", "-m", "init code"], repo)
    sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(cfg.host, repo))
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- The code has anchors. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "anchors"}],
        anchors={"s1": {"path": "code.py", "all_of": ["nonexistent_keyword"], "commit": sha}},
    )

    write_digest(
        cfg,
        "widget",
        "d-anchor",
        facts=[],
        sources=[],
    )

    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.broken_anchors) == 1
    assert report.broken_anchors[0].reported_only is False

    text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "(stale 2026-10-05: anchor words gone from code.py)" in text


def test_mid_edit_project_skipped(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "line"}],
    )

    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[],
        sources=[],
    )

    # Hand edit within quiet period (5 minutes ago)
    page_file = cfg.vault / "projects" / "widget" / "overview.md"
    page_file.write_text(page_file.read_text() + "\n- Mid edit.\n")
    import os

    recent = NOW - timedelta(minutes=5)
    os.utime(page_file, (recent.timestamp(), recent.timestamp()))

    res = schedule.run(cfg, now=NOW)
    assert res.edited == ["widget"]
    assert res.ran == []


def test_older_hand_edit_checkpointed(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "line"}],
    )

    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[],
        sources=[],
    )

    page_file = cfg.vault / "projects" / "widget" / "overview.md"
    page_file.write_text(page_file.read_text() + "\n- Older hand edit.\n")
    import os

    os.utime(page_file, (LONG_AGO.timestamp(), LONG_AGO.timestamp()))

    res = schedule.run(cfg, now=NOW)
    assert res.checkpoint is not None
    assert "widget" in res.ran
    log = run_git(["log", "--format=%s"], cfg.vault).splitlines()
    assert schedule.CHECKPOINT_MESSAGE in log


def test_second_run_on_same_day(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- First. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "first"}],
    )

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    schedule.run(cfg, now=NOW)

    write_digest(cfg, "widget", "d2", facts=[], sources=[])
    schedule.run(cfg, now=NOW + timedelta(hours=1))

    log_content = (cfg.vault / "log.md").read_text()
    assert log_content.count("dream widget") == 2


def test_reports_never_reach_injection_default_search_or_dream_input(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.save()

    # Create a report file
    report_file = cfg.vault / "reports" / "2026" / "10" / "2026-10-04.md"
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text("""---
type: Dream Report
title: 2026-10-04
---

## Added

- Secret report memory not for injection. [observed] 2026-10-04 [^s1]
""")
    vault.commit(cfg.vault, ["reports/2026/10/2026-10-04.md"], "add report")

    # 1. Injection never includes reports
    from aft.keys import PATH, Resolution

    res = Resolution(
        kind=PATH,
        cwd=cfg.vault,
        project=proj,
        slug="widget",
        root=cfg.vault,
        alias_added=False,
    )
    injected = inject.build(cfg, res)
    assert "Secret report memory" not in injected

    # 2. Dream decide.related never retrieves from reports
    from aft.dream import decide

    new_f = rules.NewFact(
        statement="Secret report memory", kind="fact", source="observed", date=TODAY
    )
    lines = decide.related(cfg.vault, new_f, "widget")
    assert all("reports" not in cand.path for cand in lines)


# Additional Behaviors and Refusal Tests


def test_creates_overview_and_registers_project_on_first_curated_fact(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    # widget is UNREGISTERED
    assert Registry.load(cfg.registry_path).by_slug("unregistered-widget") is None
    assert not (cfg.vault / "projects" / "unregistered-widget" / "overview.md").exists()

    write_digest(
        cfg,
        "unregistered-widget",
        "d1",
        facts=[
            {
                "statement": "First curated fact for project.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "first curated"}],
    )

    fake.script = [reply("add", None, "first fact")]
    res = schedule.run(cfg, now=NOW)
    assert "unregistered-widget" in res.ran

    # Now registered!
    reg = Registry.load(cfg.registry_path)
    proj = reg.by_slug("unregistered-widget")
    assert proj is not None

    overview = cfg.vault / "projects" / "unregistered-widget" / "overview.md"
    assert overview.exists()
    text = overview.read_text()
    assert f"project: {proj.id}" in text
    assert "- First curated fact for project. [observed] 2026-10-05" in text


def test_unregistered_project_with_only_skips_is_not_registered(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    write_digest(
        cfg,
        "transient-widget",
        "d1",
        facts=[
            {
                "statement": "Routine build step.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "routine build"}],
    )

    fake.script = [reply("skip", None, "routine")]
    res = schedule.run(cfg, now=NOW)
    assert "transient-widget" in res.ran
    assert Registry.load(cfg.registry_path).by_slug("transient-widget") is None
    assert not (cfg.vault / "projects" / "transient-widget" / "overview.md").exists()


def test_broken_anchor_cleared_when_words_appear_again(initialized, home, fake, tmp_path):
    cfg = config_with_fake(home, initialized.vault, fake)
    repo = make_repo(tmp_path / "widget")
    (repo / "db.py").write_text("import sqlite3\n")
    run_git(["add", "db.py"], repo)
    run_git(["commit", "-m", "init db"], repo)
    sha = run_git(["rev-parse", "HEAD"], repo)

    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.add_path(proj, path_key(cfg.host, repo))
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body=(
            "## State\n\n"
            "- The DB is SQLite. [observed] 2026-09-20 [^s1] (stale 2026-09-25: words gone)\n"
        ),
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "sqlite"}],
        anchors={"s1": {"path": "db.py", "all_of": ["sqlite3"], "commit": sha}},
    )

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.cleared_anchors) == 1
    assert report.cleared_anchors[0].statement == "The DB is SQLite."

    text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "- The DB is SQLite. [observed] 2026-09-20 [^s1]\n" in text


def test_moves_superseded_lines_older_than_90_days_to_archive(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="""## Decisions

- Current index. [stated] 2026-09-20 [^s2]
- ~~Old index.~~ [stated] 2026-01-01 [^s1] superseded 2026-06-01 by [^s2]
""",
        sources=[
            {"id": "s1", "resource": "aft://session/c/1#e1", "quote": "old"},
            {"id": "s2", "resource": "aft://session/c/2#e2", "quote": "current"},
        ],
    )

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.archived_lines) == 1
    assert report.archived_lines[0].statement == "Old index."

    archive_file = cfg.vault / "archive" / "projects" / "widget" / "overview.md"
    assert archive_file.exists()
    assert "Old index." in archive_file.read_text()
    assert "Old index." not in (cfg.vault / "projects" / "widget" / "overview.md").read_text()


def test_flags_pages_over_12000_chars(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )
    big_page = cfg.vault / "projects" / "widget" / "big.md"
    body_lines = ["## State", ""]
    for i in range(250):
        body_lines.append(
            f"- Line {i:04d} with plenty of text to pad the page. [observed] 2026-09-01 [^s1]"
        )
    body_lines.append("")
    big_fm = (
        "---\n"
        "type: Concept\n"
        "title: Big\n"
        "description: Big page\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/c/1#e1\n"
        "    quote: line\n"
        f"aft:\n  project: {proj.id}\n---\n\n"
    )
    big_page.write_text(big_fm + "\n".join(body_lines), encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/big.md"], "add big")

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert "projects/widget/big.md" in report.oversize_pages


def test_regenerates_index_and_appends_log(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    proj = registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )
    api_page = cfg.vault / "projects" / "widget" / "api.md"
    api_fm = (
        "---\n"
        "type: Concept\n"
        "title: API\n"
        "description: API\n"
        "sources: []\n"
        f"aft:\n  project: {proj.id}\n---\n\n## State\n"
    )
    api_page.write_text(api_fm, encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/api.md"], "add api")

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    schedule.run(cfg, now=NOW)

    index_text = (cfg.vault / "projects" / "widget" / "index.md").read_text()
    assert "- overview.md" in index_text
    assert "- api.md" in index_text

    log_text = (cfg.vault / "log.md").read_text()
    assert f"- {TODAY}: dream widget" in log_text


def test_lint_failure_aborts_run_without_commit(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )

    digest_path = write_digest(cfg, "widget", "d1", facts=[], sources=[])
    before_sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    # Corrupt a file to make lint fail
    corrupt_page = cfg.vault / "projects" / "widget" / "broken.md"
    corrupt_page.write_text("---\nstatus: bad_status\n---\n")

    with pytest.raises(apply.ApplyError, match="lint failed"):
        apply.run_project(cfg, "widget", [digest_path], now=NOW)

    # Aborted without commit!
    assert run_git(["rev-parse", "HEAD"], cfg.vault) == before_sha
    # The pre-existing corrupt file is untouched; run does not destroy uncommitted files
    assert corrupt_page.exists()
    assert run_git(["status", "--porcelain"], cfg.vault) == "?? projects/widget/broken.md"


def test_marks_consumed_digests(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )

    digest_path = write_digest(cfg, "widget", "d1", facts=[], sources=[])
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert report.commit is not None

    d_text = digest_path.read_text()
    fm, _, _, _ = page.split_frontmatter(d_text)
    assert fm["aft"]["consumed"] != ""

    # Second run finds no waiting digests
    res2 = schedule.run(cfg, now=NOW)
    assert res2.waiting == {}
    assert res2.ran == []


def test_makes_one_commit_per_project_run(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register widget")

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )

    write_digest(cfg, "widget", "d1", facts=[], sources=[])
    before_count = len(run_git(["log", "--format=%s"], cfg.vault).splitlines())

    res = schedule.run(cfg, now=NOW)
    after_log = run_git(["log", "--format=%s"], cfg.vault).splitlines()

    # Exactly one new commit was made
    assert len(after_log) == before_count + 1
    assert after_log[0] == "aft: dream widget"
    assert res.reports["widget"].commit == run_git(["rev-parse", "HEAD"], cfg.vault)


def test_refusal_r2_stated_not_yours(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- S. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "s"}],
    )

    # Put assistant entry in evidence archive (not user turn)
    from aft.readers import ASSISTANT, Entry

    evidence.store(
        cfg,
        agent="claude",
        session_id="sess_assist",
        project_id="p_widget",
        entries=[
            Entry(id="e2", time=NOW, role=ASSISTANT, text="I recommend PostgreSQL.", is_error=False)
        ],
    )

    write_digest(
        cfg,
        "widget",
        "d-r2",
        facts=[
            {
                "statement": "We use PostgreSQL.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_assist#e2",
                "quote": "I recommend PostgreSQL.",
            }
        ],
    )

    fake.script = [reply("add", None, "stated decision")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(r.rule == "R2" for r in report.rejected)
    assert "PostgreSQL" not in (cfg.vault / "projects" / "widget" / "overview.md").read_text()


def test_refusal_r5_derivable_statement(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- S. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "s"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-r5",
        facts=[
            {
                "statement": "Code block ```print(1)``` is used.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e2", "quote": "Code block"}],
    )

    fake.script = [reply("add", None, "code snippet")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.rejected) == 1
    assert report.rejected[0].rule == "R5"


def test_refusal_r6_global_fact_not_stated(initialized, home, fake):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- S. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "s"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-r6",
        facts=[
            {
                "statement": "The global python version is 3.13.",
                "kind": "state",
                "source": "observed",  # Not stated!
                "date": TODAY,
                "scope": "global",
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e2", "quote": "global python"}],
    )

    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.skips) == 1
    assert "global page changes only from [stated]" in report.skips[0].reason
    assert "global python" not in (cfg.vault / "global" / "environment.md").read_text()


def test_quote_not_in_archive_is_rejected(initialized, home, fake):
    """A fact whose quote is not in the archive is rejected under R1."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state line"}],
    )

    # Digest citing an entry that is not in the archive
    write_digest(
        cfg,
        "widget",
        "d-missing-quote",
        facts=[
            {
                "statement": "An unevidenced fact.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_missing#e99",
                "quote": "words never captured",
            }
        ],
        store_evidence=False,
    )

    fake.script = [reply("add", None, "add fact")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.rejected) == 1
    assert report.rejected[0].rule == "R1"
    assert report.rejected[0].reason == "quote not verified"
    assert (
        "An unevidenced fact."
        not in (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    )


def test_stated_fact_whose_evidence_entry_has_role_assistant_is_not_yours(initialized, home, fake):
    """A [stated] fact whose evidence entry has role assistant is not yours."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state line"}],
    )

    from aft.readers import ASSISTANT, Entry

    # Store entry with assistant role
    evidence.store(
        cfg,
        agent="claude",
        session_id="sess_assist_test",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1", time=NOW, role=ASSISTANT, text="The system runs on Redis.", is_error=False
            )
        ],
    )

    # Stated fact citing assistant words
    write_digest(
        cfg,
        "widget",
        "d-assist",
        facts=[
            {
                "statement": "The system runs on Redis.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_assist_test#e1",
                "quote": "The system runs on Redis.",
            }
        ],
        store_evidence=False,
    )

    fake.script = [reply("add", None, "add stated fact")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(r.rule == "R2" for r in report.rejected)
    assert (
        "The system runs on Redis."
        not in (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    )


def test_fact_with_two_refs_where_only_second_verifies_is_written_with_second_source(
    initialized, home, fake
):
    """A fact with two refs where only the second verifies is written with the second source."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state line"}],
    )

    from aft.readers import USER, Entry

    # Store only the second entry in evidence archive
    evidence.store(
        cfg,
        agent="claude",
        session_id="sess_second",
        project_id="p_widget",
        entries=[
            Entry(id="e2", time=NOW, role=USER, text="Second verified quote.", is_error=False)
        ],
    )

    # Digest has a fact with two refs: s1 (unverified) and s2 (verified)
    rel = "digests/widget/2026/10/d-two-refs.md"
    dpath = cfg.vault / rel
    dpath.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "Session Digest",
        "title": "Digest two refs",
        "description": "Two refs test.",
        "generated": {"by": "aft", "at": "2026-10-05T08:00:00Z"},
        "sources": [
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_first#e1",
                "quote": "First unverified quote.",
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_second#e2",
                "quote": "Second verified quote.",
            },
        ],
        "aft": {
            "project": "p_widget",
            "role": "primary",
            "facts": [
                {
                    "statement": "Dual ref fact.",
                    "kind": "fact",
                    "source": "observed",
                    "date": TODAY,
                    "refs": ["s1", "s2"],
                }
            ],
        },
    }
    body = "## Facts\n\n- Dual ref fact. [observed] 2026-10-05 [^s1] [^s2]\n"
    dpath.write_text(f"---\n{yaml.safe_dump(fm, sort_keys=False)}---\n\n{body}", encoding="utf-8")
    vault.commit(cfg.vault, [rel], "seed digest two refs")

    fake.script = [reply("add", None, "dual ref")]
    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.ran
    report = res.reports["widget"]
    assert len(report.added) == 1
    added = report.added[0]
    assert added.statement == "Dual ref fact."

    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    overview_doc = pagewrite.PageDoc.read(cfg.vault / "projects" / "widget" / "overview.md")

    # The unverified first source must not be added to sources
    all_quotes = [s.get("quote") for s in overview_doc.sources.values()]
    all_resources = [s.get("resource") for s in overview_doc.sources.values()]
    assert "First unverified quote." not in all_quotes
    assert "aft://session/claude/sess_first#e1" not in all_resources

    # The second verified source must be present
    assert "Second verified quote." in all_quotes
    assert "aft://session/claude/sess_second#e2" in all_resources

    # Find the sid for the second source in overview_doc
    second_sid = next(
        k
        for k, v in overview_doc.sources.items()
        if v.get("resource") == "aft://session/claude/sess_second#e2"
    )
    assert added.refs == (second_sid,)
    assert f"- Dual ref fact. [observed] 2026-10-05 [^{second_sid}]" in overview_text


def test_lint_failure_leaves_unrelated_uncommitted_file_untouched(
    initialized, home, fake, monkeypatch
):
    """A lint failure restores modified files while leaving unrelated uncommitted files
    untouched.
    """
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State line. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state line"}],
    )

    # Create unrelated uncommitted files in the vault
    unrelated_file = cfg.vault / "unrelated.txt"
    unrelated_file.write_text("Unrelated work that must remain untouched.\n")

    unrelated_dir = cfg.vault / "projects" / "other_project"
    unrelated_dir.mkdir(parents=True, exist_ok=True)
    unrelated_page = unrelated_dir / "notes.md"
    unrelated_page.write_text("Other project notes.\n")

    dpath = write_digest(
        cfg,
        "widget",
        "d-lint-fail",
        facts=[
            {
                "statement": "New fact that triggers lint error.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_lint#e1",
                "quote": "New fact quote.",
            }
        ],
    )

    # Force lint_vault to fail
    monkeypatch.setattr(apply.lint, "lint_vault", lambda _vault: ["simulated lint failure"])

    fake.script = [reply("add", None, "add fact")]
    with pytest.raises(apply.ApplyError, match="lint failed"):
        apply.run_project(cfg, "widget", [dpath], now=NOW)

    # Unrelated files must still exist with exact content
    assert unrelated_file.exists()
    assert unrelated_file.read_text() == "Unrelated work that must remain untouched.\n"
    assert unrelated_page.exists()
    assert unrelated_page.read_text() == "Other project notes.\n"

    # Overview.md must be restored to its clean state without the new fact
    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "New fact that triggers lint error." not in overview_text
