"""Tests for M4.5: apply, maintenance, and commit.

DESIGN 9, steps 5 to 7, DESIGN 15 M4 fixture suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from aft import config as configmod
from aft import evidence, inject, keys, lint, model, page, pagewrite, queue, vault
from aft.dream import apply, rules, schedule
from aft.events import SESSION_END, SessionEvent
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


@pytest.fixture(autouse=True)
def _commit_registry_after_save(monkeypatch):
    """Keep test-registered projects committed, matching aft register behavior."""
    orig_save = Registry.save

    def save(self):
        orig_save(self)
        try:
            vault.commit(self.path.parent, ["registry.yaml"], "register widget")
        except Exception:
            pass

    monkeypatch.setattr(Registry, "save", save)


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
    with_project: bool = True,
    with_check: bool = False,
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
            source_role = s.get("role", USER)
            source_text = s.get("text", q)
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
                    (
                        agent,
                        Entry(
                            id=entry_id,
                            time=NOW,
                            role=source_role,
                            text=source_text,
                            is_error=False,
                        ),
                    )
                )
        for sess_id, entries_with_agent in by_session.items():
            agent = entries_with_agent[0][0]
            entries = [e for _, e in entries_with_agent]
            evidence.store(
                cfg,
                agent=agent,
                session_id=sess_id,
                project_id=f"p_{slug}" if with_project else None,
                entries=entries,
            )

    rel = f"digests/{slug}/2026/10/{filename}.md"
    path = cfg.vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)

    aft_dict: dict[str, object] = {"role": role}
    if with_project:
        aft_dict["project"] = f"p_{slug}"
    if consumed:
        aft_dict["consumed"] = consumed
    aft_dict["facts"] = facts

    cleaned_sources = []
    for s in sources:
        cs = {k: v for k, v in s.items() if k not in ("text",)}
        if with_check:
            res = cs.get("resource", "")
            q = cs.get("quote", "")
            if res.startswith("aft://session/"):
                if "role" not in cs:
                    cs["role"] = s.get("role", "user")
                if "said" not in cs:
                    cs["said"] = s.get("said", TODAY)
                if "verified" not in cs:
                    cs["verified"] = s.get("verified", TODAY)
                if "check" not in cs and res:
                    parsed = evidence.parse_resource(res)
                    if parsed:
                        cs["check"] = evidence.quote_check(
                            parsed[0], parsed[1], cs["role"], cs["said"], q
                        )
            else:
                if "said" not in cs:
                    cs["said"] = s.get("said", TODAY)
                if "verified" not in cs:
                    cs["verified"] = s.get("verified", TODAY)
                if "check" not in cs and res:
                    parsed = evidence.parse_resource(res)
                    if parsed:
                        cs["check"] = evidence.library_quote_check(parsed[0], parsed[1], q)
        else:
            if "check" not in s:
                cs.pop("role", None)
        cleaned_sources.append(cs)
    fm = {
        "type": "Session Digest",
        "title": f"Digest {filename}",
        "description": "Summary of session.",
        "generated": {"by": "aft", "at": at},
        "sources": cleaned_sources,
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
            refs = f.get("refs") or [f.get("ref", "s1")]
            ref_str = " ".join(f"[^{r}]" for r in refs)
            body_lines.append(
                f"- {f['statement']} [{f['source']}] {f.get('date', TODAY)} {ref_str}"
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


def test_an_archived_projects_digest_is_dreamed(initialized, home, fake):
    """Archiving takes a project out of injection and the idle report, not out of the
    dream (DESIGN 7, F49): its waiting digest is applied to its pages like any other."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.set_archived("widget", TODAY)
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
    overview_text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "- The queue uses SQLite WAL mode. [stated] 2026-10-05" in overview_text


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
    from aft.readers import USER, Entry

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

    # Each quote sits in the archive on the day it was said: the RGB sighting is from today,
    # the 8-cores one from 2026-09-10, the date its digest line carries.
    evidence.store(
        cfg,
        agent="c",
        session_id="2",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1",
                time=datetime(2026, 10, 5, 9, 0, tzinfo=UTC),
                role=USER,
                text="RGB lighting is present",
                is_error=False,
            ),
            Entry(
                id="e2",
                time=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
                role=USER,
                text="8 cores",
                is_error=False,
            ),
        ],
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
        store_evidence=False,
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

    # Standing lines untouched and callouts written to the page
    text = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "- No RGB lighting. [stated] 2026-09-01 [^s1]" in text
    assert "- Server has 16 cores. [observed] 2026-09-20 [^s1]" in text
    assert "> [!question]" in text
    assert "RGB lighting is present on the server. ([^s2])" in text
    assert "> [!conflict]" in text
    assert "Server has 8 cores. ([^s3])" in text
    assert "observed RGB" not in text
    assert "seen 8 cores" not in text
    assert report.conflicts[0].refs == ("s3",)
    assert report.conflicts[0].path == "projects/widget/overview.md"
    assert report.questions[0].refs == ("s2",)
    assert report.questions[0].path == "projects/widget/overview.md"


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


def test_a_registration_midrun_keeps_one_run_id(initialized, home, fake, tmp_path):
    """A registration that renames the group mid-run makes no new run id: the digests
    consumed before it and the run's commit name the same id (DESIGN 9)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    # The group's two sessions ran in yard/widget, and their records carry the path; they
    # were written while the name widget was free.
    yard_widget = tmp_path / "yard" / "widget"
    for session in ("1", "2"):
        queue.record_project(
            cfg,
            "c",
            session,
            None,
            "widget",
            yard_widget,
            registry=Registry.load(cfg.registry_path),
        )
    # Another directory registered the name in the meantime.
    registry = Registry.load(cfg.registry_path)
    other = registry.add("widget", "Widget")
    registry.add_path(other, path_key(cfg.host, tmp_path / "elsewhere" / "widget"))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "aft: register widget")

    write_digest(
        cfg,
        "widget",
        "2026-10-05-c-1",
        facts=[
            {
                "statement": "The user prefers the morning standup at nine.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
                "term": "long",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "standup at nine"}],
        at="2026-10-05T08:00:00Z",
    )
    write_digest(
        cfg,
        "widget",
        "2026-10-05-c-2",
        facts=[
            {
                "statement": "The yard build runs on the GPU host.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
                "scope": "project",
                "term": "long",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e1", "quote": "runs on the GPU host"}],
        at="2026-10-05T09:00:00Z",
    )
    fake.script = [reply("add", None, "preference"), reply("add", None, "state")]
    res = schedule.run(cfg, now=NOW)
    assert res.failures == {}

    # The first project fact registered the directory under the name the registry had
    # free, and the run id stayed the one the group filed its digests under.
    reg = Registry.load(cfg.registry_path)
    project = reg.by_path_key(path_key(cfg.host, yard_widget))
    assert project is not None and project.slug == "yard-widget"

    consumed = page.split_frontmatter(
        (cfg.vault / "digests/widget/2026/10/2026-10-05-c-1.md").read_text()
    )[0]["aft"]["consumed"]
    named = run_git(["log", "--grep", consumed, "--format=%H"], cwd=cfg.vault)
    assert named, f"no commit's message names the consumed id {consumed}"
    trailer = run_git(["log", "--grep", "Run: dream-widget", "--format=%B", "-1"], cwd=cfg.vault)
    assert f"Run: {consumed}" in trailer


def test_a_contested_group_slug_leaves_the_holding_projects_pages_alone(
    initialized, home, fake, tmp_path
):
    """A group whose slug a registered project holds writes no short-term line of that
    project's: the page under the group slug belongs to the holder, and the run neither
    reconciles nor lifts from it (DESIGN 7, F49)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    from aft import evidence
    from aft.readers import USER, Entry

    # Session c/8 spoke the line the holder's recent.md carries.
    evidence.store(
        cfg,
        agent="c",
        session_id="8",
        project_id=None,
        entries=[
            Entry(id="e1", time=NOW, role=USER, text="The hook rewrite is paused.", is_error=False)
        ],
    )
    # The contested group's session c/9 ran in work/widget, and its record was written
    # while the name was free.
    work_widget = tmp_path / "work" / "widget"
    queue.record_project(
        cfg, "c", "9", None, "widget", work_widget, registry=Registry.load(cfg.registry_path)
    )
    # Another directory registered the name, and its recent.md carries an active line.
    registry = Registry.load(cfg.registry_path)
    holder = registry.add("widget", "Widget")
    registry.add_path(holder, path_key(cfg.host, tmp_path / "elsewhere" / "widget"))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "aft: register widget")
    holder_id = Registry.load(cfg.registry_path).by_slug("widget").id
    holder_recent = cfg.vault / "projects/widget/recent.md"
    holder_recent.parent.mkdir(parents=True, exist_ok=True)
    fm = {
        "type": "Project",
        "title": "Widget recent",
        "description": "Short-term memory.",
        "sources": [
            {"id": "r0", "resource": "aft://session/c/8#e1", "quote": "The hook rewrite is paused."}
        ],
        "aft": {"project": holder_id},
    }
    holder_recent.write_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()}\n---\n\n"
        "## State\n\n- The hook rewrite is paused. [observed] 2026-10-04 [^r0]\n"
    )
    vault.commit(cfg.vault, ["projects/widget/recent.md"], "seed holder recent")

    # The contested group's digest holds only a global fact, so the run registers no
    # project of its own and its slug stays the held name.
    write_digest(
        cfg,
        "widget",
        "2026-10-05-c-9",
        facts=[
            {
                "statement": "The user prefers the morning standup at nine.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s9",
                "scope": "global",
                "term": "long",
            }
        ],
        sources=[{"id": "s9", "resource": "aft://session/c/9#e1", "quote": "standup at nine"}],
        at="2026-10-05T08:00:00Z",
    )
    fake.script = [reply("add", None, "preference")]
    res = schedule.run(cfg, now=NOW)
    assert res.failures == {}

    # The holder's line stands untouched: the newest session of the contested group never
    # withdraws it, and no lift moves it into a project of the group's own.
    body = holder_recent.read_text()
    assert "~~The hook rewrite is paused.~~" not in body
    assert "- The hook rewrite is paused. [observed] 2026-10-04 [^r0]" in body
    # No project was registered for the group's directory: the run registered nothing,
    # because its digests held only a global fact.
    reg = Registry.load(cfg.registry_path)
    assert reg.by_path_key(path_key(cfg.host, work_widget)) is None


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


def test_lint_failure_aborts_run_without_commit(initialized, home, fake, monkeypatch):
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

    # An unparsable hand edit does NOT stop the run (per-page lint)
    corrupt_page = cfg.vault / "projects" / "widget" / "broken.md"
    corrupt_page.write_text("---\nstatus: bad_status\n---\n")

    rep = apply.run_project(cfg, "widget", [digest_path], now=NOW)
    assert rep is not None
    assert corrupt_page.exists()
    assert run_git(["rev-parse", "HEAD"], cfg.vault) != before_sha

    # But a lint error on a page the run wrote aborts without commit
    digest_path2 = write_digest(
        cfg,
        "widget",
        "d2",
        facts=[
            {
                "statement": "Fact triggers lint.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e1", "quote": "quote"}],
    )
    fake.script = [reply("add", None, "add fact")]
    current_sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    def fail_written(vault, rel_paths, reg=None):
        return [
            apply.lint.Finding(
                "projects/widget/overview.md", 1, "simulated lint failure on written page"
            )
        ]

    monkeypatch.setattr(apply.lint, "lint_pages", fail_written)
    with pytest.raises(apply.ApplyError, match="lint failed"):
        apply.run_project(cfg, "widget", [digest_path2], now=NOW)

    # Aborted without commit!
    assert run_git(["rev-parse", "HEAD"], cfg.vault) == current_sha
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


def test_makes_one_commit_per_project_run(initialized, home, fake, tmp_path):
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register widget")
    keep_out_of_the_idle_report(cfg, tmp_path)

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

    # Dream commit and report commit were made
    assert len(after_log) == before_count + 2
    assert after_log[:2] == ["aft: report widget", "aft: dream widget"]
    assert res.reports["widget"].commit == run_git(["rev-parse", "HEAD~1"], cfg.vault)


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

    fake.script = [reply("add", None, "global python")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.rejected) == 1
    assert report.rejected[0].rule == "R6"
    assert "global python" not in (cfg.vault / "global" / "environment.md").read_text()


def test_overview_created_when_project_already_registered_without_overview(initialized, home, fake):
    """When a project is already registered without overview.md, it is created before write."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    overview_path = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview_path.exists()

    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": "First fact on new overview.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/1#e1",
                "quote": "First fact quote.",
            }
        ],
    )

    fake.script = [reply("add", None, "first fact")]
    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.ran
    assert overview_path.exists()
    assert "First fact on new overview." in overview_path.read_text()


def test_overview_created_for_project_registered_with_project_add(
    initialized, home, fake, tmp_path
):
    """A project registered with aft project add has no overview.md initially;
    it is created before the first curated fact is written.
    """
    from aft.cli import main

    repo = make_repo(tmp_path / "custom-repo")

    cfg = config_with_fake(home, initialized.vault, fake)
    ret = main(["project", "add", str(repo), "--title", "Custom Repo"])
    assert ret == 0

    overview_path = cfg.vault / "projects" / "custom-repo" / "overview.md"
    assert not overview_path.exists()

    write_digest(
        cfg,
        "custom-repo",
        "d-cli-add",
        facts=[
            {
                "statement": "Fact for project registered with CLI.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/c/1#e1",
                "quote": "Fact for project registered with CLI.",
            }
        ],
    )

    fake.script = [reply("add", None, "add fact")]
    res = schedule.run(cfg, now=NOW)
    assert "custom-repo" in res.ran
    assert overview_path.exists()
    assert "Fact for project registered with CLI." in overview_path.read_text()
    doc = pagewrite.PageDoc.read(overview_path)
    assert doc.fact("Fact for project registered with CLI.").state == page.ACTIVE


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
    monkeypatch.setattr(
        apply.lint,
        "lint_pages",
        lambda _vault, _paths, _reg=None: [
            apply.lint.Finding("dummy.md", 1, "simulated lint failure")
        ],
    )

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


def test_model_failure_mid_run_rolls_back_every_write(initialized, home, fake, tmp_path):
    """A failure partway through a run restores every path it wrote, leaves the digests
    unconsumed and re-raises; the next run finds a clean vault and makes no
    checkpoint: human edits commit."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")
    keep_out_of_the_idle_report(cfg, tmp_path)

    overview = seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Existing line. [observed] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "existing quote"}],
    )

    first = write_digest(
        cfg,
        "widget",
        "d-a",
        facts=[
            {
                "statement": "First fact.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/2#e1", "quote": "first fact quote"}],
    )
    second = write_digest(
        cfg,
        "widget",
        "d-b",
        facts=[
            {
                "statement": "Second fact.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s3",
            }
        ],
        sources=[{"id": "s3", "resource": "aft://session/c/2#e2", "quote": "second fact quote"}],
    )

    before_sha = run_git(["rev-parse", "HEAD"], cfg.vault)
    fake.script = [reply("add", None, "first"), Reply(status=500)]

    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.failures
    assert "500" in res.failures["widget"]

    # The run's own writes are gone and the work tree is clean. The only commit since is the
    # report section that names the failure, which is how the failure reaches the person.
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    subjects = run_git(["log", "--format=%s", f"{before_sha}..HEAD"], cfg.vault).splitlines()
    assert subjects == ["aft: report widget"]
    assert "First fact." not in overview.read_text()
    for digest in (first, second):
        fm, _, _, _ = page.split_frontmatter(digest.read_text())
        assert not fm["aft"].get("consumed")

    # Both digests still wait, and their bytes carry no trace of the failed run, so the
    # next run has no human edit to checkpoint and dreams each fact exactly once.
    # The 500s left a cooldown stamped with the wall clock, which a test cannot wait out;
    # the endpoint answers again, so the record is cleared the way its deadline would be.
    model.clear_cooldown(cfg)
    fake.script = [reply("add", None, "first again"), reply("add", None, "second again")]
    res = schedule.run(cfg, now=NOW + timedelta(minutes=45))
    assert res.checkpoint is None
    assert res.tagged == []
    assert res.ran == ["widget"]
    text = overview.read_text()
    assert text.count("- First fact.") == 1
    assert text.count("- Second fact.") == 1


def test_gate_counts_line_footnotes_by_evidence_date_not_line_date(initialized, home, fake):
    """R3 weighs a target line's own footnotes by the date of their evidence entry, not
    the date on the line: the aged signal of session A counts half, so the candidate's
    raw counts reach the gate's numbers while its weighted score does not, and it
    stays a candidate."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    from aft.readers import USER, Entry

    evidence.store(
        cfg,
        agent="claude",
        session_id="sessA",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1",
                time=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
                role=USER,
                text="checkpoint quote one",
                is_error=False,
            )
        ],
    )
    evidence.store(
        cfg,
        agent="claude",
        session_id="sessB",
        project_id="p_widget",
        entries=[
            Entry(
                id="e2",
                time=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                role=USER,
                text="checkpoint quote two",
                is_error=False,
            )
        ],
    )

    seed_overview(
        cfg,
        "widget",
        body=(
            "## Candidates\n\n"
            "- Sessions need checkpoint capture. [inferred] 2026-10-01 [^s1] [^s2] "
            "(signals 2/3, sessions 2/2)\n"
        ),
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sessA#e1",
                "quote": "checkpoint quote one",
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sessB#e2",
                "quote": "checkpoint quote two",
            },
        ],
    )

    # The new quote is from session A and dated today: the raw counts reach 3 signals
    # across 2 sessions while the aged s1 pulls the weighted score to 2.5.
    write_digest(
        cfg,
        "widget",
        "d-gate",
        facts=[
            {
                "statement": "Sessions need checkpoint capture.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s10",
            }
        ],
        sources=[
            {
                "id": "s10",
                "resource": "aft://session/claude/sessA#e9",
                "quote": "checkpoint capture again",
            }
        ],
    )

    fake.script = [reply("confirm", "L1", "stated again")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert report.promoted == []
    assert len(report.confirmed) == 1

    overview = cfg.vault / "projects" / "widget" / "overview.md"
    text = overview.read_text()
    # The footnotes now reach the gate's raw numbers, and the line still sits in
    # Candidates: the weighted score of its evidence does not.
    assert "(signals 3/3, sessions 2/2)" in text
    parsed = page.parse(text, overview.relative_to(cfg.vault))
    line = next(f for f in parsed.facts if f.statement == "Sessions need checkpoint capture.")
    assert line.state == page.CANDIDATE


def test_line_footnote_of_a_worker_session_does_not_count_toward_the_gate(initialized, home, fake):
    """A target line's footnote reads its origin from the evidence entry and the digest of
    its session: a worker session's quote adds nothing to the gate, so the candidate
    stays below it."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    from aft.readers import USER, Entry

    evidence.store(
        cfg,
        agent="claude",
        session_id="sessW",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1",
                time=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                role=USER,
                text="checkpoint quote w",
                is_error=False,
            )
        ],
    )
    evidence.store(
        cfg,
        agent="claude",
        session_id="sessB",
        project_id="p_widget",
        entries=[
            Entry(
                id="e2",
                time=datetime(2026, 10, 1, 9, 0, tzinfo=UTC),
                role=USER,
                text="checkpoint quote b",
                is_error=False,
            )
        ],
    )

    # The digest of session sessW marks it a worker session; it is consumed, so it only
    # stands as the record of how that session was classified.
    write_digest(
        cfg,
        "widget",
        "2026-09-30-claude-sessW",
        facts=[],
        sources=[],
        role="worker",
        consumed="dream-widget-20260930T120000Z",
    )

    seed_overview(
        cfg,
        "widget",
        body=(
            "## Candidates\n\n"
            "- Sessions need checkpoint capture. [inferred] 2026-10-01 [^s1] [^s2] "
            "(signals 2/3, sessions 2/2)\n"
        ),
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sessW#e1",
                "quote": "checkpoint quote w",
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sessB#e2",
                "quote": "checkpoint quote b",
            },
        ],
    )

    write_digest(
        cfg,
        "widget",
        "d-worker-gate",
        facts=[
            {
                "statement": "Sessions need checkpoint capture.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s10",
            }
        ],
        sources=[
            {
                "id": "s10",
                "resource": "aft://session/claude/sessD#e5",
                "quote": "checkpoint capture stated",
            }
        ],
    )

    fake.script = [reply("confirm", "L1", "stated again")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert report.promoted == []

    overview = cfg.vault / "projects" / "widget" / "overview.md"
    parsed = page.parse(overview.read_text(), overview.relative_to(cfg.vault))
    line = next(f for f in parsed.facts if f.statement == "Sessions need checkpoint capture.")
    assert line.state == page.CANDIDATE


NOTES_PAGE = (
    "---\n"
    "type: Concept\n"
    "title: Notes\n"
    "description: Project notes.\n"
    "sources:\n"
    "  - id: s1\n"
    "    resource: aft://session/c/1#e1\n"
    '    quote: "existing quote"\n'
    "aft:\n"
    "  project: p_widget\n"
    "---\n\n"
    "## Facts\n\n"
    "- A noted fact. [observed] 2026-09-30 [^s1]\n"
)


def test_dream_commit_carries_only_the_paths_the_run_wrote(initialized, home, fake, monkeypatch):
    """A page edited while the run waits on the model stays out of the dream commit and
    keeps the person's bytes."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Existing line. [observed] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "existing quote"}],
    )

    p_id = registry.by_slug("widget").id
    notes = cfg.vault / "projects" / "widget" / "notes.md"
    notes.write_text(NOTES_PAGE.replace("p_widget", p_id), encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/notes.md"], "add notes")

    write_digest(
        cfg,
        "widget",
        "d-scope",
        facts=[
            {
                "statement": "The index uses FTS5.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {"id": "s2", "resource": "aft://session/c/9#e1", "quote": "fts5 it is"},
        ],
    )

    real_decide = apply.decide.decide

    def deciding(config, new_fact, quote, slug):
        """The page changes after the model call, before any write of the run."""
        proposal = real_decide(config, new_fact, quote, slug)
        extra = "- No RGB on the desk lamp. [stated] 2026-10-04 [^s1]\n"
        notes.write_text(notes.read_text() + extra)
        return proposal

    monkeypatch.setattr(apply.decide, "decide", deciding)

    fake.script = [reply("add", None, "new decision")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert report.commit is not None

    files = run_git(["show", "--name-only", "--format=", report.commit], cfg.vault).splitlines()
    assert "projects/widget/overview.md" in files
    assert "projects/widget/notes.md" not in files

    # The edit is untouched and still uncommitted: the run neither staged it nor wrote
    # over it.
    assert "- No RGB on the desk lamp. [stated] 2026-10-04 [^s1]" in notes.read_text()
    assert run_git(["diff", "--name-only"], cfg.vault) == "projects/widget/notes.md"
    assert run_git(["diff", "--cached", "--name-only"], cfg.vault) == ""


def test_lint_rollback_restores_run_writes_not_unrelated_edits(
    initialized, home, fake, monkeypatch
):
    """The lint rollback restores the paths this run wrote from the content this run
    captured, so a tracked page the person edited stays byte-identical and
    uncommitted."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Existing line. [observed] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "existing quote"}],
    )

    p_id = registry.by_slug("widget").id
    notes = cfg.vault / "projects" / "widget" / "notes.md"
    notes.write_text(NOTES_PAGE.replace("p_widget", p_id), encoding="utf-8")
    vault.commit(cfg.vault, ["projects/widget/notes.md"], "add notes")
    extra = "- No RGB on the desk lamp. [stated] 2026-10-04 [^s1]\n"
    notes.write_text(notes.read_text() + extra)

    dpath = write_digest(
        cfg,
        "widget",
        "d-lint",
        facts=[
            {
                "statement": "The index uses FTS5.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {"id": "s2", "resource": "aft://session/c/9#e1", "quote": "fts5 it is"},
        ],
    )

    monkeypatch.setattr(
        apply.lint,
        "lint_pages",
        lambda _vault, _paths, _reg=None: [
            apply.lint.Finding("dummy.md", 1, "simulated lint failure")
        ],
    )
    fake.script = [reply("add", None, "new decision")]
    before_sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    with pytest.raises(apply.ApplyError, match="lint failed"):
        apply.run_project(cfg, "widget", [dpath], now=NOW)

    assert run_git(["rev-parse", "HEAD"], cfg.vault) == before_sha
    assert "- No RGB on the desk lamp. [stated] 2026-10-04 [^s1]" in notes.read_text()
    assert run_git(["diff", "--name-only"], cfg.vault) == "projects/widget/notes.md"
    assert run_git(["diff", "--cached", "--name-only"], cfg.vault) == ""
    fm, _, _, _ = page.split_frontmatter(dpath.read_text())
    assert not fm["aft"].get("consumed")


def test_global_confirm_writes_to_the_targets_page(initialized, home, fake):
    """A global fact that confirms a line is applied to the page that line stands on, not
    to a global page picked from the new fact's kind."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    prefs = cfg.vault / "global" / "preferences.md"
    prefs.write_text(
        "---\n"
        "type: Preferences\n"
        "title: Preferences\n"
        "description: Your preferences that apply in every project.\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/claude/sessP#e1\n"
        '    quote: "no rgb please"\n'
        "---\n\n"
        "## Preferences\n\n"
        "- No RGB lighting. [stated] 2026-09-01 [^s1]\n",
        encoding="utf-8",
    )
    vault.commit(cfg.vault, ["global/preferences.md"], "seed preferences")
    environment = cfg.vault / "global" / "environment.md"
    environment_before = environment.read_text()

    write_digest(
        cfg,
        "widget",
        "d-global",
        facts=[
            {
                "statement": "No RGB lighting.",
                "kind": "preference",
                "source": "stated",
                "date": TODAY,
                "ref": "s10",
                "scope": "global",
            }
        ],
        sources=[
            {
                "id": "s10",
                "resource": "aft://session/claude/sessQ#e1",
                "quote": "still no rgb lighting",
            }
        ],
    )

    fake.script = [reply("confirm", "L1", "stated again")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.confirmed) == 1
    assert report.confirmed[0].path == "global/preferences.md"

    assert "- No RGB lighting. [stated] 2026-10-05 [^s1] [^s2]" in prefs.read_text()
    assert environment.read_text() == environment_before


def test_global_fact_writes_to_profile_md(initialized, home, fake):
    """A new global fact of kind 'fact' routes to global/profile.md."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    profile = cfg.vault / "global" / "profile.md"
    environment = cfg.vault / "global" / "environment.md"
    environment_before = environment.read_text()
    prefs = cfg.vault / "global" / "preferences.md"
    prefs_before = prefs.read_text()

    write_digest(
        cfg,
        "widget",
        "d-profile",
        facts=[
            {
                "statement": "Lives in Seattle, WA.",
                "kind": "fact",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": "I live in Seattle, WA.",
            }
        ],
    )

    fake.script = [reply("add", None, "profile location fact")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].path == "global/profile.md"

    profile_text = profile.read_text()
    assert "Lives in Seattle, WA." in profile_text
    assert environment.read_text() == environment_before
    assert prefs.read_text() == prefs_before


def test_global_fact_creates_profile_md_when_missing(initialized, home, fake):
    """When global/profile.md is absent, adding a global fact creates it."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    profile = cfg.vault / "global" / "profile.md"
    if profile.exists():
        profile.unlink()
        vault.commit(cfg.vault, ["global/profile.md"], "remove profile.md for test")

    assert not profile.exists()

    write_digest(
        cfg,
        "widget",
        "d-profile-create",
        facts=[
            {
                "statement": "Works as a senior platform engineer.",
                "kind": "fact",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "scope": "global",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": "I work as a senior platform engineer.",
            }
        ],
    )

    fake.script = [reply("add", None, "profile work fact")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].path == "global/profile.md"
    assert profile.exists()
    profile_text = profile.read_text()
    assert "type: Profile" in profile_text
    assert "Works as a senior platform engineer." in profile_text


def test_edit_quote_matches_only_added_lines(initialized, home):
    cfg = config_with_fake(home, initialized.vault, FakeEndpoint())
    test_file = cfg.vault / "test.txt"
    test_file.write_text("initial line to be removed\ncontext line\n")
    vault.commit(cfg.vault, ["test.txt"], "initial commit")

    test_file.write_text("brand new added line\ncontext line\n")
    vault.commit(cfg.vault, ["test.txt"], "update commit with added line")
    sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    # Added line matches
    v, origin, dt, yours = apply._lookup_evidence(cfg, f"aft://edit/{sha}", "brand new added line")
    assert v is True
    assert yours is False
    assert apply._is_quote_yours(cfg, f"aft://edit/{sha}") is False

    # Removed line does NOT match
    v, _, _, _ = apply._lookup_evidence(cfg, f"aft://edit/{sha}", "initial line to be removed")
    assert v is False

    # Commit message does NOT match
    v, _, _, _ = apply._lookup_evidence(cfg, f"aft://edit/{sha}", "update commit with added line")
    assert v is False

    # Argument injection / non-hex sha is rejected
    v, _, _, _ = apply._lookup_evidence(
        cfg, "aft://edit/--output=/tmp/evil", "brand new added line"
    )
    assert v is False
    v, _, _, _ = apply._lookup_evidence(
        cfg, "aft://edit/not_a_valid_hex_sha!", "brand new added line"
    )
    assert v is False

    # Non-consecutive added lines across files/hunks do NOT match
    f1 = cfg.vault / "file1.txt"
    f2 = cfg.vault / "file2.txt"
    f1.write_text("No RGB\n")
    f2.write_text("lighting.\n")
    vault.commit(cfg.vault, ["file1.txt", "file2.txt"], "two files added")
    multi_sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    v, _, _, _ = apply._lookup_evidence(cfg, f"aft://edit/{multi_sha}", "No RGB lighting.")
    assert v is False

    # Consecutive lines within a single added run DO match
    f3 = cfg.vault / "file3.txt"
    f3.write_text("No RGB\nlighting.\n")
    vault.commit(cfg.vault, ["file3.txt"], "single file with consecutive added lines")
    single_sha = run_git(["rev-parse", "HEAD"], cfg.vault)

    v, _, _, _ = apply._lookup_evidence(cfg, f"aft://edit/{single_sha}", "No RGB lighting.")
    assert v is True


test_evidence_source_edit_uri = test_edit_quote_matches_only_added_lines


def test_a_settled_replacements_statement_verifies_inside_its_fact_line(initialized, home):
    """T43: the settle cites the replacement's statement as ``aft://edit/<sha>``, which the
    settle commit added as a whole fact line; the lookup finds the statement in the added
    line and dates the source by the commit."""
    cfg = config_with_fake(home, initialized.vault, FakeEndpoint())
    test_file = cfg.vault / "test.txt"
    test_file.write_text("before\n")
    vault.commit(cfg.vault, ["test.txt"], "seed")
    test_file.write_text(
        "- The daemon listens on 9000. [stated] 2026-10-06 [^s2] [^s3]\n"
        "~~The daemon listens on 8123.~~ [observed] 2026-09-20 [^s1]"
        " superseded 2026-10-06 by [^s2]\n"
    )
    vault.commit(
        cfg.vault,
        ["test.txt"],
        "aft: settle projects/widget/overview.md:1 (replace): The daemon listens on 8123.",
    )
    sha = run_git(["rev-parse", "HEAD"], cfg.vault)
    day = run_git(["show", "-s", "--format=%cs", sha], cfg.vault)
    v, origin, dt, yours = apply._lookup_evidence(
        cfg, f"aft://edit/{sha}", "The daemon listens on 9000."
    )
    assert (v, origin, dt, yours) == (True, rules.PRIMARY, day, False)


def test_settle_file_edit_question_note_keep_leaves_stated_active(initialized, home, fake):
    """Settling a file-edit question note via notes.settle(config, rel, line, notes.KEEP)
    clears the question callout and leaves the fact as active [stated]."""
    from aft import notes

    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## Decisions\n\n- Line. [stated] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "line"}],
    )
    write_digest(cfg, "widget", "d1", facts=[], sources=[])

    page_file = cfg.vault / "projects" / "widget" / "overview.md"
    page_file.write_text(page_file.read_text() + "- Releases are cut from main only.\n")
    import os

    os.utime(page_file, (LONG_AGO.timestamp(), LONG_AGO.timestamp()))

    res = schedule.run(cfg, now=NOW)
    assert res.checkpoint is not None
    assert "projects/widget/overview.md" in res.tagged

    parsed = page.parse(page_file.read_text(), page_file)
    fact = next(f for f in parsed.facts if f.statement == "Releases are cut from main only.")
    assert fact.state == page.NEEDS_YOU
    assert fact.source == "stated"
    assert len(fact.callouts) == 1
    assert fact.callouts[0].type == "question"
    assert fact.callouts[0].text == ["File edit in checkpoint. Confirm to keep this fact."]

    rel = "projects/widget/overview.md"
    notes.settle(cfg, rel, fact.line, notes.KEEP, now=NOW)

    parsed_after = page.parse(page_file.read_text(), page_file)
    fact_after = next(
        f for f in parsed_after.facts if f.statement == "Releases are cut from main only."
    )
    assert fact_after.state == page.ACTIVE
    assert fact_after.source == "stated"
    assert fact_after.callouts == []


def test_lookup_evidence_worker_digest_exact_match_no_leak(initialized, home):
    from aft.readers import USER, Entry

    cfg = config_with_fake(home, initialized.vault, FakeEndpoint())
    sess_full = "worker-session-abcdef12345678"
    entry_id = "e1"

    # Worker digest in nested directory with full session ID in filename
    d_dir = cfg.vault / "digests" / "widget" / "2026" / "09"
    d_dir.mkdir(parents=True, exist_ok=True)
    d_path = d_dir / f"2026-09-29-claude-{sess_full}.md"
    d_path.write_text("---\ntype: Session Digest\naft:\n  project: p_widget\n  role: worker\n---\n")

    # 1. Query with prefix/substring of session ID: must NOT leak worker role
    sess_sub = "abcdef12"
    evidence.store(
        cfg,
        agent="claude",
        session_id=sess_sub,
        project_id="p_widget",
        entries=[Entry(id=entry_id, time=NOW, role=USER, text="primary statement", is_error=False)],
    )
    res_sub = f"aft://session/claude/{sess_sub}#{entry_id}"
    v, origin, _, _ = apply._lookup_evidence(cfg, res_sub, "primary statement")
    assert v is True
    assert origin == rules.PRIMARY  # No leak!

    # 2. Query with different agent: must NOT leak worker role
    evidence.store(
        cfg,
        agent="pi",
        session_id=sess_full,
        project_id="p_widget",
        entries=[Entry(id=entry_id, time=NOW, role=USER, text="pi statement", is_error=False)],
    )
    res_pi = f"aft://session/pi/{sess_full}#{entry_id}"
    v, origin, _, _ = apply._lookup_evidence(cfg, res_pi, "pi statement")
    assert v is True
    assert origin == rules.PRIMARY  # No cross-agent leak!

    # 3. Exact session and agent match: returns WORKER
    evidence.store(
        cfg,
        agent="claude",
        session_id=sess_full,
        project_id="p_widget",
        entries=[Entry(id=entry_id, time=NOW, role=USER, text="worker statement", is_error=False)],
    )
    res_exact = f"aft://session/claude/{sess_full}#{entry_id}"
    v, origin, _, _ = apply._lookup_evidence(cfg, res_exact, "worker statement")
    assert v is True
    assert origin == rules.WORKER

    # 4. Multipart worker digest: returns WORKER
    d_part2 = d_dir / f"2026-09-29-claude-{sess_full}-2.md"
    d_part2.write_text(
        "---\ntype: Session Digest\naft:\n  project: p_widget\n  role: worker\n---\n"
    )
    v, origin, _, _ = apply._lookup_evidence(cfg, res_exact, "worker statement")
    assert v is True
    assert origin == rules.WORKER


def test_new_fact_evidence_date_uses_quote_date_not_digest_date(initialized, home, fake):
    """The new fact's evidence date comes from the quote's entry time, not the digest's end date."""
    from aft.readers import USER, Entry

    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    # Standing line has evidence from 2026-09-15
    quote_time_old = datetime(2026, 9, 15, 10, 0, 0, tzinfo=UTC)
    evidence.store(
        cfg,
        agent="claude",
        session_id="standing_sess",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1", time=quote_time_old, role=USER, text="Server has 16 cores.", is_error=False
            )
        ],
    )
    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Server has 16 cores. [observed] 2026-09-15 [^s1]\n",
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/standing_sess#e1",
                "quote": "Server has 16 cores.",
            }
        ],
    )

    # User says "Server has 8 cores." on 2026-09-10 in a session whose last entry is on 2026-09-20.
    quote_time_new = datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC)
    evidence.store(
        cfg,
        agent="claude",
        session_id="new_sess",
        project_id="p_widget",
        entries=[
            Entry(
                id="e2", time=quote_time_new, role=USER, text="Server has 8 cores.", is_error=False
            )
        ],
    )

    # Digest filename date is 2026-09-20 (the session's end date)
    write_digest(
        cfg,
        "widget",
        "2026-09-20-claude-new_sess",
        facts=[
            {
                "statement": "Server has 8 cores.",
                "kind": "state",
                "source": "observed",
                "date": "2026-09-20",
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/new_sess#e2",
                "quote": "Server has 8 cores.",
            }
        ],
    )

    fake.script = [reply("supersede", "L1", "server now has 8 cores")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    # The ref is dated by its own quote's day, 2026-09-10, not by the digest's filename day of
    # 2026-09-20. 09-10 is older than the standing line's 2026-09-15, so R4 refuses the
    # supersede and the fact lands as a conflict instead.
    assert len(report.conflicts) == 1
    assert report.conflicts[0].new_statement == "Server has 8 cores."
    assert report.conflicts[0].refs == ("s2",)
    assert report.conflicts[0].path == "projects/widget/overview.md"


def test_real_per_page_lint_catches_invalid_written_page_and_rolls_back(
    initialized, home, fake, monkeypatch, tmp_path
):
    """The real per-page lint refuses a page this run wrote, and the run rolls back."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    keep_out_of_the_idle_report(cfg, tmp_path)

    write_digest(
        cfg,
        "widget",
        "2026-10-04-claude-sess1",
        facts=[
            {
                "statement": "Valid fact statement.",
                "kind": "state",
                "source": "observed",
                "date": "2026-10-04",
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": "Valid fact statement.",
            }
        ],
    )
    fake.script = [reply("add", None, "add fact")]

    # The page this run writes fails the real lint: the appended bullet is not a fact line.
    orig_render = pagewrite.PageDoc.render

    def render_with_an_unlintable_line(self):
        return orig_render(self) + "\n- Broken fact line missing required metadata\n"

    monkeypatch.setattr(pagewrite.PageDoc, "render", render_with_an_unlintable_line)

    # A hand edit stands uncommitted beside the run, and the checkpoint commits it alone.
    hand_notes = cfg.vault / "hand-notes.md"
    hand_notes.write_text("a hand note\n", encoding="utf-8")
    import os

    os.utime(hand_notes, (LONG_AGO.timestamp(), LONG_AGO.timestamp()))

    before_sha = run_git(["rev-parse", "HEAD"], cfg.vault)
    res = schedule.run(cfg, now=NOW)

    # run_project refuses the run and rolls its writes back; schedule.run keeps the failure to
    # this project and names it in the day's report.
    assert "widget" not in res.ran
    assert "lint failed" in res.failures["widget"]
    assert res.reports["widget"].error is not None
    assert not (cfg.vault / "projects" / "widget" / "overview.md").exists()
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    report_text = (cfg.vault / "reports" / "2026" / "10" / "2026-10-05.md").read_text()
    assert "lint failed" in report_text
    committed = run_git(["log", "--format=%s", f"{before_sha}..HEAD"], cfg.vault).splitlines()
    assert committed == ["aft: report widget", schedule.CHECKPOINT_MESSAGE]


def test_real_per_page_lint_skips_unwritten_broken_page(initialized, home, fake):
    """Real per-page lint only checks pages written by this run, ignoring unwritten broken pages."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.add("other", "Other")
    registry.save()

    # Create an unwritten page with an invalid fact line (missing date and ref)
    broken_page = cfg.vault / "projects" / "other" / "overview.md"
    broken_page.parent.mkdir(parents=True, exist_ok=True)
    broken_page.write_text(
        "---\ntype: Fact Page\naft:\n  project: "
        + registry.by_slug("other").id
        + "\n  kind: overview\n---\n\n## State\n\n- Broken line missing date and ref\n"
    )
    vault.commit(cfg.vault, ["projects/other/overview.md"], "commit broken page")

    # Confirm that lint_vault fails on the vault
    vault_findings = lint.lint_vault(cfg.vault)
    assert any("projects/other/overview.md" in str(f) for f in vault_findings)

    # Dream run for widget only writes widget pages
    write_digest(
        cfg,
        "widget",
        "2026-10-04-claude-sess1",
        facts=[
            {
                "statement": "Valid widget fact.",
                "kind": "state",
                "source": "observed",
                "date": "2026-10-04",
                "ref": "s1",
            }
        ],
        sources=[
            {"id": "s1", "resource": "aft://session/claude/sess1#e1", "quote": "Valid widget fact."}
        ],
    )
    fake.script = [reply("add", None, "add fact")]

    # schedule.run must succeed because per-page lint only lints widget pages!
    res = schedule.run(cfg, now=NOW)
    assert "widget" in res.ran
    assert "widget" not in res.failures
    rep = res.reports["widget"]
    assert rep.commit is not None
    assert (cfg.vault / "projects" / "widget" / "overview.md").exists()


def test_a_failing_project_leaves_the_next_project_alone(initialized, home, fake):
    """Two ready projects through the real run_project: the first fails partway and gives back
    every page it wrote, and the second still dreams and commits only its own files."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("alpha", "Alpha")
    registry.add("beta", "Beta")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "seed registry")

    write_digest(
        cfg,
        "alpha",
        "d-a1",
        facts=[
            {
                "statement": "Alpha first.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/c/a1#e1", "quote": "alpha first words"}],
    )
    write_digest(
        cfg,
        "alpha",
        "d-a2",
        facts=[
            {
                "statement": "Alpha second.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[{"id": "s2", "resource": "aft://session/c/a2#e1", "quote": "alpha second words"}],
    )
    write_digest(
        cfg,
        "beta",
        "d-b1",
        facts=[
            {
                "statement": "Beta only.",
                "kind": "state",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/c/b1#e1", "quote": "beta only words"}],
    )

    # alpha writes its first fact, then the endpoint refuses its second digest with a 400,
    # which aft neither retries nor cools down; beta's call is answered after the failure.
    fake.script = [
        reply("add", None, "alpha first"),
        Reply(status=400),
        reply("add", None, "beta only"),
    ]

    res = schedule.run(cfg, now=NOW)

    assert "400" in res.failures["alpha"]
    assert "alpha" not in res.ran
    assert "beta" in res.ran
    # alpha gave back what it wrote: no page, and its first digest is waiting again.
    assert not (cfg.vault / "projects" / "alpha" / "overview.md").exists()
    a1 = cfg.vault / "digests" / "alpha" / "2026" / "10" / "d-a1.md"
    a1_fm, _, _, _ = page.split_frontmatter(a1.read_text())
    assert not a1_fm["aft"].get("consumed")

    assert "Beta only." in (cfg.vault / "projects" / "beta" / "overview.md").read_text()
    committed = run_git(
        ["show", "--name-only", "--format=", res.reports["beta"].commit], cfg.vault
    ).split()
    assert "projects/beta/overview.md" in committed
    assert [
        name for name in committed if name.startswith(("projects/alpha", "digests/alpha"))
    ] == []
    assert run_git(["status", "--porcelain"], cfg.vault) == ""


def keep_out_of_the_idle_report(cfg: configmod.Config, tmp_path: Path, slug: str = "widget"):
    """A project the dream's idle report has nothing to say about, so a test counts only its
    own run's commits (DESIGN 7, F49). The session it records ended outside the quiet
    period, so the dream still runs, and inside the 90-day idle window, so the report
    names nothing."""
    repo = make_repo(tmp_path / slug)
    registry = Registry.load(cfg.registry_path)
    registered = registry.by_slug(slug)
    assert registered is not None
    registry.add_path(registered, path_key("testhost", repo))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register widget's path")
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="s-idle",
            cwd=repo,
            transcript_path=None,
            time=NOW - timedelta(days=1),
            detail=None,
        ),
    )


# Any directory can be a project (BUILD-PLAN F49; DESIGN 7, DESIGN 10)


def _unregistered_digest(
    cfg: configmod.Config, slug: str, filename: str, agent: str, session: str, at: str
) -> Path:
    """A digest of a session whose directory belongs to no registered project yet.

    It carries no ``aft.project``, because the registry has no id to name, and its evidence
    is stored with no project either (DESIGN 7, F49).
    """
    return write_digest(
        cfg,
        slug,
        filename,
        facts=[
            {
                "statement": "The tester prefers plain hyphens.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": f"aft://session/{agent}/{session}#e1",
                "quote": "plain hyphens",
            }
        ],
        with_project=False,
    )


def test_a_dream_registers_the_directory_a_session_ran_in(initialized, home, fake, tmp_path):
    """The dream registers a still-unregistered project from the directory its session-start
    record names, and writes its pages under that project (DESIGN 7, F49)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    assert queue.record_project(cfg, "claude", "s-ws1", None, "workspace", workspace)
    digest_path = _unregistered_digest(
        cfg, "workspace", f"{TODAY}-claude-s-ws1", "claude", "s-ws1", TODAY
    )
    fake.script = [reply("add", None, "add fact")]

    rep = apply.run_project(cfg, "workspace", [digest_path], now=NOW)

    registry = Registry.load(cfg.registry_path)
    project = registry.by_path_key(path_key("testhost", workspace))
    assert project is not None and project.slug == "workspace"
    assert rep is not None and rep.project == "workspace"
    assert (
        "The tester prefers plain hyphens."
        in (cfg.vault / "projects/workspace/overview.md").read_text()
    )
    fm, _, _, _ = page.split_frontmatter(digest_path.read_text())
    assert fm is not None and fm["aft"]["consumed"]
    assert run_git(["status", "--porcelain"], cfg.vault) == ""


def test_a_pathless_record_is_adopted_only_by_its_own_directory_in_the_dream(
    initialized, home, fake, tmp_path
):
    """The review case (DESIGN 7, F49): a session in /src/foo claimed ``foo`` before paths
    were kept and its digest waits under digests/foo; then a session records in
    /scratch/foo, and then one in /src/foo. The record is adopted by /src/foo alone, so
    the dream registers foo at /src/foo and files /src/foo's facts there, while
    /scratch/foo settles its own name and never sees them."""
    cfg = config_with_fake(home, initialized.vault, fake)
    src_foo = (tmp_path / "src" / "foo").resolve()
    src_foo.mkdir(parents=True)
    scratch_foo = (tmp_path / "scratch" / "foo").resolve()
    scratch_foo.mkdir(parents=True)
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="s0",
            cwd=src_foo,
            transcript_path=None,
            time=NOW - timedelta(hours=1),
            detail=None,
        ),
    )
    assert queue.record_project(cfg, "claude", "s0", None, "foo")
    old = _unregistered_digest(cfg, "foo", f"{TODAY}-claude-s0", "claude", "s0", TODAY)

    # A session in another directory of the same name records next: the name is
    # /src/foo's claim, so this one settles its own name and the record is left pathless.
    assert queue.record_project(
        cfg, "claude", "s1", None, "foo", scratch_foo, registry=Registry.load(cfg.registry_path)
    )
    assert queue.recorded_project(cfg, "claude", "s1").slug == "scratch-foo"
    assert queue.recorded_project(cfg, "claude", "s0").path is None

    # A session in the record's own directory adopts it, so the dream files the old
    # digest by /src/foo's path and makes one project of the directory.
    assert queue.record_project(
        cfg, "claude", "s2", None, "foo", src_foo, registry=Registry.load(cfg.registry_path)
    )
    assert queue.recorded_project(cfg, "claude", "s0").path == str(src_foo)

    registry = Registry.load(cfg.registry_path)
    groups = apply.digest_groups(cfg, registry, "foo", [old])
    assert groups == [[old]]
    assert apply._project_of_group(cfg, registry, "foo", groups[0]) == (None, src_foo)

    fake.script = [reply("add", None, "add fact")]
    rep = apply.run_project(cfg, "foo", groups[0], now=NOW)

    project = Registry.load(cfg.registry_path).by_path_key(path_key("testhost", src_foo))
    assert project is not None and project.slug == "foo"
    assert (
        "The tester prefers plain hyphens." in (cfg.vault / "projects/foo/overview.md").read_text()
    )
    fm, _, _, _ = page.split_frontmatter(old.read_text())
    assert fm is not None and fm["aft"]["consumed"]
    assert rep is not None and rep.project == "foo"


def test_a_dream_finds_a_registered_project_by_path(initialized, home, fake, tmp_path):
    """A later session in the same directory is dreamed into the same project by the path its
    record names, not by making a second project (DESIGN 7, F49)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    assert queue.record_project(cfg, "claude", "s-ws1", None, "workspace", workspace)
    first = _unregistered_digest(
        cfg, "workspace", f"{TODAY}-claude-s-ws1", "claude", "s-ws1", TODAY
    )
    fake.script = [reply("add", None, "first fact")]
    apply.run_project(cfg, "workspace", [first], now=NOW)

    assert queue.record_project(cfg, "claude", "s-ws2", None, "workspace", workspace)
    second = _unregistered_digest(
        cfg, "workspace", f"{TODAY}-claude-s-ws2", "claude", "s-ws2", TODAY
    )
    fake.script = [reply("add", None, "second fact")]
    rep = apply.run_project(cfg, "workspace", [second], now=NOW)

    assert rep is not None and rep.project == "workspace"
    registry = Registry.load(cfg.registry_path)
    assert len(registry.projects) == 1
    overview = (cfg.vault / "projects/workspace/overview.md").read_text()
    assert overview.count("The tester prefers plain hyphens.") == 2


def test_a_project_registered_before_git_init_keeps_its_memory_after(
    initialized, home, fake, tmp_path
):
    """The path decides (DESIGN 7, F49): a project the dream registered while its directory
    was plain keeps that directory as its own once the person runs `git init` in it, and the
    next session's digest is dreamed into the same project."""
    cfg = config_with_fake(home, initialized.vault, fake)
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    assert queue.record_project(cfg, "claude", "s-ws1", None, "workspace", workspace)
    first = _unregistered_digest(
        cfg, "workspace", f"{TODAY}-claude-s-ws1", "claude", "s-ws1", TODAY
    )
    fake.script = [reply("add", None, "first fact")]
    apply.run_project(cfg, "workspace", [first], now=NOW)
    registry = Registry.load(cfg.registry_path)
    assert len(registry.projects) == 1

    make_repo(workspace)
    resolution = keys.resolve(workspace, Registry.load(cfg.registry_path), cfg.host)
    assert (resolution.kind, resolution.project.slug) == (keys.PATH, "workspace")

    assert queue.record_project(cfg, "claude", "s-ws2", None, "workspace", workspace)
    second = _unregistered_digest(
        cfg, "workspace", f"{TODAY}-claude-s-ws2", "claude", "s-ws2", TODAY
    )
    fake.script = [reply("add", None, "second fact")]
    rep = apply.run_project(cfg, "workspace", [second], now=NOW)

    assert rep is not None and rep.project == "workspace"
    registry = Registry.load(cfg.registry_path)
    assert len(registry.projects) == 1
    overview = (cfg.vault / "projects/workspace/overview.md").read_text()
    assert overview.count("The tester prefers plain hyphens.") == 2


def test_the_dream_names_idle_projects_in_the_days_report(initialized, home, fake, tmp_path):
    """The dream's report names a project no session has touched in 90 days (DESIGN 7,
    F49), under its own commit, and says nothing about the project it dreamed."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register widget")
    keep_out_of_the_idle_report(cfg, tmp_path)

    idle_repo = (tmp_path / "idle-gizmo").resolve()
    idle_repo.mkdir()
    registry = Registry.load(cfg.registry_path)
    gizmo = registry.add("idle-gizmo", "Idle gizmo")
    registry.add_path(gizmo, path_key("testhost", idle_repo))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register idle-gizmo")
    queue.record_project(cfg, "claude", "s-idle-gizmo", gizmo.id, "idle-gizmo", idle_repo)
    queue.enqueue(
        cfg,
        SessionEvent(
            agent="claude",
            kind=SESSION_END,
            session_id="s-idle-gizmo",
            cwd=idle_repo,
            transcript_path=None,
            time=NOW - timedelta(days=100),
            detail=None,
        ),
    )

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- State. [observed] 2026-09-01 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/c/1#e1", "quote": "state"}],
    )
    write_digest(cfg, "widget", "d1", facts=[], sources=[])

    schedule.run(cfg, now=NOW)

    report_path = cfg.vault / "reports" / "2026" / "10" / f"{NOW.date().isoformat()}.md"
    text = report_path.read_text()
    assert "- idle-gizmo: no session since" in text
    assert "widget: no session" not in text and "widget: recorded paths" not in text
    assert run_git(["log", "--format=%s"], cfg.vault).splitlines()[0] == (
        "aft: report idle and gone"
    )


def test_a_dream_does_not_attach_a_digest_to_a_project_that_merely_has_its_name(
    initialized, home, fake, tmp_path
):
    """The registry's name decides nothing once a record names a path (DESIGN 7, F49): a
    digest whose group name is a registered project's slug is dreamed into a project of its
    own, registered at the path its record names, not into the project that holds the
    name."""
    cfg = config_with_fake(home, initialized.vault, fake)
    same_name = (tmp_path / "yard" / "widget").resolve()
    same_name.mkdir(parents=True)
    real = (tmp_path / "real").resolve()
    real.mkdir()
    registry = Registry.load(cfg.registry_path)
    registered = registry.add("widget", "Widget")
    registry.add_path(registered, path_key("testhost", real))
    registry.save()
    vault.commit(cfg.vault, ["registry.yaml"], "register widget")

    # The digest is filed under the registered project's name, as a digest written before
    # records carried paths would be, and the record names a path the registry has never
    # met. The path decides, not the name.
    digest_path = write_digest(
        cfg,
        "widget",
        f"{TODAY}-claude-s-y1",
        facts=[
            {
                "statement": "The tester prefers plain hyphens.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/claude/s-y1#e1", "quote": "hyphens"}],
        with_project=False,
    )
    assert queue.record_project(cfg, "claude", "s-y1", None, "widget", same_name)
    fake.script = [reply("add", None, "add fact")]

    rep = apply.run_project(cfg, "widget", [digest_path], now=NOW)

    assert rep is not None and rep.project == "yard-widget"
    registry = Registry.load(cfg.registry_path)
    new = registry.by_path_key(path_key("testhost", same_name))
    assert new is not None and new.slug == "yard-widget"
    assert (
        "The tester prefers plain hyphens."
        in (cfg.vault / "projects/yard-widget/overview.md").read_text()
    )
    assert not (cfg.vault / "projects/widget/overview.md").exists()


def test_a_dream_registers_a_directory_of_a_taken_name_under_its_parent(
    initialized, home, fake, tmp_path
):
    """Two directories of one name become two projects: the digest of the second files under
    the name its parent gives it, and the dream registers it there (DESIGN 7, F49)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    first = (tmp_path / "work" / "foo").resolve()
    first.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-f1", None, "foo", first)
    first_digest = _unregistered_digest(cfg, "foo", f"{TODAY}-claude-s-f1", "claude", "s-f1", TODAY)
    fake.script = [reply("add", None, "first foo")]
    apply.run_project(cfg, "foo", [first_digest], now=NOW)

    second = (tmp_path / "yard" / "foo").resolve()
    second.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-f2", None, "foo", second)
    second_digest = _unregistered_digest(
        cfg, "foo", f"{TODAY}-claude-s-f2", "claude", "s-f2", TODAY
    )
    fake.script = [reply("add", None, "second foo")]
    rep = apply.run_project(cfg, "foo", [second_digest], now=NOW)

    # The first dream registered foo; the second group's record still names foo, so the
    # digest's own filing settled its slug before the dream ran.
    record = queue.recorded_project(cfg, "claude", "s-f2")
    assert record is not None and record.slug == "yard-foo"
    registry = Registry.load(cfg.registry_path)
    project = registry.by_path_key(path_key("testhost", second))
    assert project is not None and project.slug == "yard-foo"
    assert (
        "The tester prefers plain hyphens."
        in (cfg.vault / "projects/yard-foo/overview.md").read_text()
    )
    assert rep is not None and rep.project == "yard-foo"


def test_the_dream_splits_one_slug_by_each_digests_own_record(initialized, home, fake, tmp_path):
    """The review case (DESIGN 7, F49): /b/foo's session files its digest under foo, a
    project is registered over foo, then /c/foo's session files its digest under foo too.
    The dream decides each digest's project from its own record, so the two never share
    one, and a digest whose session left no record at all runs by itself."""
    cfg = config_with_fake(home, initialized.vault, fake)
    bfoo = (tmp_path / "b" / "foo").resolve()
    bfoo.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-b1", None, "foo", bfoo)
    registry = Registry.load(cfg.registry_path)
    registered = registry.add("foo", "Foo")
    registry.add_path(registered, path_key("testhost", bfoo))
    registry.save()
    assert queue.record_project(cfg, "claude", "s-c1", registered.id, "foo")
    cfoo = (tmp_path / "c" / "foo").resolve()
    cfoo.mkdir(parents=True)
    assert queue.record_project(cfg, "claude", "s-c2", None, "foo", cfoo)

    named_digest = _unregistered_digest(cfg, "foo", f"{TODAY}-claude-s-c1", "claude", "s-c1", TODAY)
    path_digest = _unregistered_digest(cfg, "foo", f"{TODAY}-claude-s-c2", "claude", "s-c2", TODAY)
    orphan = _unregistered_digest(cfg, "foo", f"{TODAY}-claude-s-x9", "claude", "s-x9", TODAY)

    groups = apply.digest_groups(cfg, registry, "foo", [named_digest, path_digest, orphan])

    assert len(groups) == 3
    assert groups[0] == [named_digest]
    assert groups[1] == [path_digest]
    assert groups[2] == [orphan]


def test_dream_refuses_stated_fact_with_question_quote(initialized, home, fake):
    """BUILD-PLAN F52: a quote holding a question mark is refused under R2."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-question",
        facts=[
            {
                "statement": "Approved sending smaller subsections.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_q#e1",
                "quote": "Should we send smaller subsections...?",
            }
        ],
    )

    fake.script = [reply("add", None, "add decision")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(r.rule == "R2" and "the quote is a question" in r.reason for r in report.rejected)
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert (
        not overview.exists() or "Approved sending smaller subsections" not in overview.read_text()
    )


def test_dream_adds_approval_whose_proposal_ends_in_question(initialized, home, fake):
    """BUILD-PLAN F52: an approval whose proposal ends in '?' is added."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-approval-q",
        facts=[
            {
                "statement": "The store uses PostgreSQL.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "refs": ["s1", "s2"],
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_app#e1",
                "quote": "Yes, do that.",
                "role": "user",
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sess_app#e2",
                "quote": "Shall I switch the store to PostgreSQL?",
                "role": "assistant",
            },
        ],
    )

    fake.script = [reply("add", None, "store uses postgresql")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].statement == "The store uses PostgreSQL."
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "The store uses PostgreSQL." in overview


def test_dream_refuses_stated_fact_with_ungrounded_statement(initialized, home, fake):
    """BUILD-PLAN F52: a stated statement naming a name or number not in quote is refused."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-ungrounded",
        facts=[
            {
                "statement": "The database runs on PostgreSQL.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_db#e1",
                "quote": "The database runs on SQLite.",
            }
        ],
    )

    fake.script = [reply("add", None, "add decision")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(r.rule == "R2" and "PostgreSQL" in r.reason for r in report.rejected)
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview.exists() or "PostgreSQL" not in overview.read_text()


def test_dream_refuses_stated_fact_quoted_from_a_question(initialized, home, fake):
    """R2: a quote cropped out of an asking sentence is a question, even though the crop
    itself holds no question mark (M1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-question",
        facts=[
            {
                "statement": "Store digest at session end.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_q#e1",
                "quote": "store digest at session end",
                "text": "Should we store digest at session end?",
            }
        ],
    )

    fake.script = [reply("add", None, "store digest at session end")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(
        r.rule == "R2" and r.reason == "the quote is a question, never a settled item"
        for r in report.rejected
    )
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview.exists() or "store digest at session end" not in overview.read_text()


def test_dream_refuses_stated_fact_quoted_from_a_question_with_a_version_dot(
    initialized, home, fake
):
    """R2: a dot inside a version is not a sentence boundary, so the question mark after
    it is still seen and the fact is refused (M1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-question-version",
        facts=[
            {
                "statement": "Pin requests to 2.31 or later.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_qv#e1",
                "quote": "pin requests to",
                "text": "Should we pin requests to 2.31 or later?",
            }
        ],
    )

    fake.script = [reply("add", None, "pin requests to")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(
        r.rule == "R2" and r.reason == "the quote is a question, never a settled item"
        for r in report.rejected
    )
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview.exists() or "pin requests to" not in overview.read_text()


def test_dream_refuses_stated_fact_quoted_from_a_question_with_a_filename_dot(
    initialized, home, fake
):
    """R2: a dot inside a filename is not a sentence boundary, so the question mark after
    it is still seen and the fact is refused (M1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-question-filename",
        facts=[
            {
                "statement": "Keep the setting in config.toml.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_qf#e1",
                "quote": "keep the setting",
                "text": "Do we keep the setting in config.toml or drop it?",
            }
        ],
    )

    fake.script = [reply("add", None, "keep the setting")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(
        r.rule == "R2" and r.reason == "the quote is a question, never a settled item"
        for r in report.rejected
    )
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview.exists() or "config.toml" not in overview.read_text()


def test_dream_refuses_stated_fact_quoted_from_a_question_across_lines(initialized, home, fake):
    """R2: a question written across lines is still a question; the quote is judged by the
    sentence it was cut from, whatever lines the turn broke it across (M1)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-question-lines",
        facts=[
            {
                "statement": "Store digest at session end.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_ql#e1",
                "quote": "store digest at session end",
                "text": "Should we store digest\nat session end?",
            }
        ],
    )

    fake.script = [reply("add", None, "store digest at session end")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert any(
        r.rule == "R2" and r.reason == "the quote is a question, never a settled item"
        for r in report.rejected
    )
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert not overview.exists() or "store digest at session end" not in overview.read_text()


def test_dream_keeps_grounded_stated_fact(initialized, home, fake):
    """BUILD-PLAN F52: a stated fact with no question mark and grounded names passes."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-grounded",
        facts=[
            {
                "statement": "Capture runs at session end.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_ok#e1",
                "quote": "Capture runs at session end.",
            }
        ],
    )

    fake.script = [reply("add", None, "capture at session end")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].statement == "Capture runs at session end."
    overview = cfg.vault / "projects" / "widget" / "overview.md"
    assert "Capture runs at session end." in overview.read_text()


def test_dream_grounds_stated_fact_in_user_turn_text(initialized, home, fake):
    """BUILD-PLAN F52: the dream grounds a stated fact in turn text from evidence archive."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-turn-grounded",
        facts=[
            {
                "statement": "PostgreSQL stays at version 16.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_pg#e1",
                "quote": "Keep it at 16.",
                "text": "We're on PostgreSQL. Keep it at 16.",
                "role": "user",
            }
        ],
    )

    fake.script = [reply("add", None, "postgresql stays 16")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].statement == "PostgreSQL stays at version 16."
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "PostgreSQL stays at version 16." in overview


def test_dream_grounds_stated_fact_with_computed_relative_date(initialized, home, fake):
    """BUILD-PLAN F59: a stated fact with a computed date from a relative expression passes R2."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    write_digest(
        cfg,
        "widget",
        "d-relative-date",
        facts=[
            {
                "statement": "The backup is paused until 2026-10-06.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess_backup#e1",
                "quote": "pause it until tomorrow",
                "text": "Please pause it until tomorrow.",
                "role": "user",
            }
        ],
    )

    fake.script = [reply("add", None, "pause backup until tomorrow")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 1
    assert report.added[0].statement == "The backup is paused until 2026-10-06."
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "The backup is paused until 2026-10-06." in overview


def test_dream_supersedes_replaced_rule_fixture(initialized, home, fake):
    """BUILD-PLAN F52: a rule the user replaced supersedes the old rule."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body=(
            "## Decisions\n\n"
            "- Any reply to the notes prompt goes through the notes. [stated] 2026-09-30 [^s1]\n"
        ),
        sources=[{"id": "s1", "resource": "aft://session/claude/old#e1", "quote": "any reply"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-f51",
        facts=[
            {
                "statement": "Reply settle to go through notes; any other reply skips them.",
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/f51#e1",
                "quote": "Reply settle to go through notes; any other reply skips them.",
            }
        ],
    )

    fake.script = [reply("supersede", "L1", "rule replaced per F51")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.superseded) == 1
    prompt_sent = fake.requests[0]["messages"][0]["content"]
    assert (
        "A new fact that changes how something is done, which an existing line describes, "
        "supersedes that line rather than adding beside it." in " ".join(prompt_sent.split())
    )
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "~~Any reply to the notes prompt goes through the notes.~~" in overview
    assert "Reply settle to go through notes; any other reply skips them. [stated]" in overview


def test_dream_supersedes_made_false_line_fixture(initialized, home, fake):
    """BUILD-PLAN F52: a line made false by a later change is superseded."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    seed_overview(
        cfg,
        "widget",
        body="## State\n\n- Notes are settled with aft settle. [stated] 2026-09-30 [^s1]\n",
        sources=[{"id": "s1", "resource": "aft://session/claude/old#e1", "quote": "aft settle"}],
    )

    write_digest(
        cfg,
        "widget",
        "d-false",
        facts=[
            {
                "statement": "The aft settle command was removed.",
                "kind": "state",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/false#e1",
                "quote": "The aft settle command was removed.",
            }
        ],
    )

    fake.script = [reply("supersede", "L1", "aft settle removed")]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.superseded) == 1
    prompt_sent = fake.requests[0]["messages"][0]["content"]
    assert (
        "A new fact that changes how something is done, which an existing line describes, "
        "supersedes that line rather than adding beside it." in " ".join(prompt_sent.split())
    )
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert "~~Notes are settled with aft settle.~~" in overview
    assert "The aft settle command was removed. [stated]" in overview


def register(cfg: configmod.Config, slug: str, title: str | None = None) -> None:
    reg = Registry.load(cfg.registry_path)
    reg.add(slug, title or slug.capitalize())
    reg.save()


def test_f75_purge_then_dream_still_applies_waiting_digest(initialized, home, fake):
    """Purge then dream still applies a waiting digest because digest-time quote
    check is trusted (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    quote = "Capture runs at session end, not per turn."
    statement = "Capture runs at session end, not per turn."
    dpath = write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": statement,
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/claude/sess1#e1", "quote": quote}],
        store_evidence=True,
        with_check=True,
    )
    # Purge evidence from archive
    evidence.purge(cfg, NOW + timedelta(days=95))
    with evidence._reading(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0

    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.added) == 1
    assert rep.added[0].statement == statement
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert statement in overview


def test_f75_vault_with_empty_archive_clone_applies_waiting_digests(initialized, home, fake):
    """A vault with an empty archive (a clone) applies its waiting digests (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    quote = "Python 3.12 is the minimum supported version."
    statement = "Python 3.12 is the minimum supported version."
    dpath = write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": statement,
                "kind": "fact",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/claude/sess1#e1", "quote": quote}],
        store_evidence=False,
        with_check=True,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.added) == 1
    assert rep.added[0].statement == statement
    overview = (cfg.vault / "projects" / "widget" / "overview.md").read_text()
    assert statement in overview


def test_f75_edited_quote_with_stale_hash_rejected_once_evidence_gone(initialized, home, fake):
    """An edited quote with a stale hash is rejected once its evidence is gone (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    quote = "SQLite WAL mode is enabled by default."
    stale_hash = evidence.quote_check("sess1", "e1", "user", TODAY, "Old unedited quote")
    dpath = write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": "SQLite WAL mode is enabled.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": quote,
                "role": "user",
                "said": TODAY,
                "verified": TODAY,
                "check": stale_hash,
            }
        ],
        store_evidence=False,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.added) == 0
    assert len(rep.rejected) == 1
    assert rep.rejected[0].rule == "R1"
    assert rep.rejected[0].reason == "quote not verified"


def test_f75_pre_f75_digest_falls_back_to_archive(initialized, home, fake):
    """A pre-F75 digest without check hash falls back to archive: applies if in
    archive, rejects if gone (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    # Case A: pre-F75 digest with evidence in archive applies
    quote = "Tests use pytest with uv run."
    statement = "Tests use pytest with uv run."
    d1 = write_digest(
        cfg,
        "widget",
        "d-pre-present",
        facts=[
            {
                "statement": statement,
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[{"id": "s1", "resource": "aft://session/claude/sess1#e1", "quote": quote}],
        store_evidence=True,
        with_check=False,
    )
    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [d1], now=NOW)
    assert len(rep.added) == 1
    assert rep.added[0].statement == statement

    # Case B: pre-F75 digest without evidence in archive is rejected under R1
    d2 = write_digest(
        cfg,
        "widget",
        "d-pre-gone",
        facts=[
            {
                "statement": "Evidence is purged.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess2#e2",
                "quote": "Evidence is purged.",
            }
        ],
        store_evidence=False,
        with_check=False,
    )
    fake.script = [reply("add")]
    rep2 = apply.run_project(cfg, "widget", [d2], now=NOW)
    assert len(rep2.added) == 0
    assert len(rep2.rejected) == 1
    assert rep2.rejected[0].rule == "R1"
    assert rep2.rejected[0].reason == "quote not verified"


def test_f75_r3_counts_signal_older_than_retention_at_half_weight(initialized, home, fake):
    """R3 counts a signal older than retention at half weight using recorded date (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    old_date = (NOW - timedelta(days=95)).strftime("%Y-%m-%d")
    old_q1 = "SGLang works faster for serving."
    old_q2 = "Benchmarking showed lower latency."
    today_q1 = "SGLang latency is confirmed lower."
    today_q2 = "Serving throughput improved by 20%."

    # Seed overview with Candidate having 2 sources from old session sess0
    seed_overview(
        cfg,
        "widget",
        body=(
            "## Candidates\n\n"
            f"- SGLang serving is faster. [suggested] {old_date} [^s1] [^s2] "
            "(signals 2/3, sessions 1/2)\n"
        ),
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess0#e1",
                "quote": old_q1,
                "role": "user",
                "said": old_date,
                "verified": TODAY,
                "check": evidence.quote_check("sess0", "e1", "user", old_date, old_q1),
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sess0#e2",
                "quote": old_q2,
                "role": "user",
                "said": old_date,
                "verified": TODAY,
                "check": evidence.quote_check("sess0", "e2", "user", old_date, old_q2),
            },
        ],
    )
    # Old evidence is NOT in archive.
    # New session digest arrives with 2 signals from a second primary session sess1.
    dpath = write_digest(
        cfg,
        "widget",
        "d-new",
        facts=[
            {
                "statement": "SGLang serving is faster.",
                "kind": "fact",
                "source": "suggested",
                "date": TODAY,
                "refs": ["s3", "s4"],
            }
        ],
        sources=[
            {
                "id": "s3",
                "resource": "aft://session/claude/sess1#e3",
                "quote": today_q1,
                "role": "user",
                "said": TODAY,
                "verified": TODAY,
                "check": evidence.quote_check("sess1", "e3", "user", TODAY, today_q1),
            },
            {
                "id": "s4",
                "resource": "aft://session/claude/sess1#e4",
                "quote": today_q2,
                "role": "user",
                "said": TODAY,
                "verified": TODAY,
                "check": evidence.quote_check("sess1", "e4", "user", TODAY, today_q2),
            },
        ],
        store_evidence=True,
    )

    fake.script = [reply("confirm", "L1")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.promoted) == 1
    assert rep.promoted[0].statement == "SGLang serving is faster."


def test_f75_purged_user_quote_keeps_user_authority_from_role(initialized, home, fake):
    """A purged user quote keeps user authority from role (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    old_date = (NOW - timedelta(days=10)).strftime("%Y-%m-%d")
    seed_overview(
        cfg,
        "widget",
        body=(f"## Decisions\n\n- Use Postgres for storage. [stated] {old_date} [^s0]\n"),
        sources=[
            {
                "id": "s0",
                "resource": "aft://session/claude/sess0#e0",
                "quote": "Use Postgres for storage.",
                "role": "user",
                "said": old_date,
                "verified": old_date,
                "check": evidence.quote_check(
                    "sess0", "e0", "user", old_date, "Use Postgres for storage."
                ),
            }
        ],
    )

    new_quote = "Switch to SQLite for storage."
    new_stmt = "Switch to SQLite for storage."
    dpath = write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": new_stmt,
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": new_quote,
                "role": "user",
                "said": TODAY,
                "verified": TODAY,
            }
        ],
        store_evidence=False,
        with_check=True,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("supersede", "L1")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.superseded) == 1
    assert rep.superseded[0].new_statement == new_stmt


def test_f75_purged_assistant_quote_does_not_gain_authority(initialized, home, fake):
    """A purged assistant quote does not gain user authority (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    old_date = (NOW - timedelta(days=10)).strftime("%Y-%m-%d")
    seed_overview(
        cfg,
        "widget",
        body=(f"## Decisions\n\n- Use Postgres for storage. [stated] {old_date} [^s0]\n"),
        sources=[
            {
                "id": "s0",
                "resource": "aft://session/claude/sess0#e0",
                "quote": "Use Postgres for storage.",
                "role": "user",
                "said": old_date,
                "verified": old_date,
                "check": evidence.quote_check(
                    "sess0", "e0", "user", old_date, "Use Postgres for storage."
                ),
            }
        ],
    )

    new_quote = "Switch to SQLite for storage."
    new_stmt = "Switch to SQLite for storage."
    dpath = write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": new_stmt,
                "kind": "decision",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": new_quote,
                "role": "assistant",
                "said": TODAY,
                "verified": TODAY,
            }
        ],
        store_evidence=False,
        with_check=True,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("supersede", "L1")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.superseded) == 0
    assert len(rep.rejected) == 1
    assert rep.rejected[0].rule == "R2"


def test_f75_edited_role_or_said_falls_back_to_archive_and_rejected_when_gone(
    initialized, home, fake
):
    """An edited role or said falls back to archive and is rejected once entry is gone (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    # Case A: Edited role (hashed as assistant, claimed as user)
    quote1 = "The database is ready."
    valid_hash_role = evidence.quote_check("sess1", "e1", "assistant", TODAY, quote1)
    d1 = write_digest(
        cfg,
        "widget",
        "d-edited-role",
        facts=[
            {
                "statement": quote1,
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": quote1,
                "role": "user",  # Tampered: hashed as assistant
                "said": TODAY,
                "verified": TODAY,
                "check": valid_hash_role,
            }
        ],
        store_evidence=False,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("add")]
    rep1 = apply.run_project(cfg, "widget", [d1], now=NOW)
    assert len(rep1.added) == 0
    assert len(rep1.rejected) == 1
    assert rep1.rejected[0].rule == "R1"
    assert rep1.rejected[0].reason == "quote not verified"

    # Case B: Edited said (hashed with older date, claimed as TODAY)
    quote2 = "The server is running."
    older_said = (NOW - timedelta(days=5)).strftime("%Y-%m-%d")
    valid_hash_said = evidence.quote_check("sess2", "e2", "user", older_said, quote2)
    d2 = write_digest(
        cfg,
        "widget",
        "d-edited-said",
        facts=[
            {
                "statement": quote2,
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s2",
            }
        ],
        sources=[
            {
                "id": "s2",
                "resource": "aft://session/claude/sess2#e2",
                "quote": quote2,
                "role": "user",
                "said": TODAY,  # Tampered: hashed with older_said
                "verified": TODAY,
                "check": valid_hash_said,
            }
        ],
        store_evidence=False,
    )
    rep2 = apply.run_project(cfg, "widget", [d2], now=NOW)
    assert len(rep2.added) == 0
    assert len(rep2.rejected) == 1
    assert rep2.rejected[0].rule == "R1"
    assert rep2.rejected[0].reason == "quote not verified"


def test_f75_r4_compares_said_not_verified_with_digest_made_days_after_turn(
    initialized, home, fake
):
    """R4 compares said:, not verified:, with a digest made days after the turn (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    line_date = (NOW - timedelta(days=3)).strftime("%Y-%m-%d")
    turn_date = (NOW - timedelta(days=5)).strftime("%Y-%m-%d")
    digest_date = TODAY

    seed_overview(
        cfg,
        "widget",
        body=(f"## Decisions\n\n- Use Postgres for storage. [stated] {line_date} [^s0]\n"),
        sources=[
            {
                "id": "s0",
                "resource": "aft://session/claude/sess0#e0",
                "quote": "Use Postgres for storage.",
                "role": "user",
                "said": line_date,
                "verified": line_date,
                "check": evidence.quote_check(
                    "sess0", "e0", "user", line_date, "Use Postgres for storage."
                ),
            }
        ],
    )

    new_quote = "Switch to SQLite for storage."
    new_stmt = "Switch to SQLite for storage."
    dpath = write_digest(
        cfg,
        "widget",
        "d-stale-turn",
        facts=[
            {
                "statement": new_stmt,
                "kind": "decision",
                "source": "stated",
                "date": turn_date,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": new_quote,
                "role": "user",
                "said": turn_date,
                "verified": digest_date,
                "check": evidence.quote_check("sess1", "e1", "user", turn_date, new_quote),
            }
        ],
        store_evidence=False,
    )
    db_path = cfg.home / "evidence.sqlite"
    if db_path.exists():
        db_path.unlink()

    fake.script = [reply("supersede", "L1")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.superseded) == 0
    assert len(rep.conflicts) == 1


def test_f75_pre_f75_assistant_source_carried_onto_page_gets_assistant_role_and_purged_not_yours(
    initialized, home, fake
):
    """A pre-F75 assistant source carried onto a page gets role assistant from the archive,
    and after a purge does not count as the user's (F75)."""
    from aft.readers import Entry

    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    quote = "The migration was completed in background."
    statement = "The migration was completed in background."

    # Archive row has role assistant
    evidence.store(
        cfg,
        agent="claude",
        session_id="sess1",
        project_id="p_widget",
        entries=[
            Entry(
                id="e1",
                time=NOW,
                role="assistant",
                text=quote,
                is_error=False,
            )
        ],
    )

    # Pre-F75 digest: no role, no said, no check
    dpath = write_digest(
        cfg,
        "widget",
        "d-pre-f75",
        facts=[
            {
                "statement": statement,
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": quote,
            }
        ],
        store_evidence=False,
        with_check=False,
    )

    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    assert len(rep.added) == 1
    assert rep.added[0].statement == statement

    # The source carried onto the page gets role assistant from the archive
    overview_path = cfg.vault / "projects" / "widget" / "overview.md"
    ov_doc = pagewrite.PageDoc.read(overview_path)
    page_sources = list(ov_doc.sources.values())
    assert len(page_sources) == 1
    src = page_sources[0]
    assert src["role"] == "assistant"
    assert src["said"] == TODAY
    assert src["check"] == evidence.quote_check("sess1", "e1", "assistant", TODAY, quote)

    # Purge the archive
    evidence.purge(cfg, NOW + timedelta(days=95))
    with evidence._reading(cfg) as conn:
        assert conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0

    # After a purge, it does not count as the user's
    v, origin, ev_date, is_yours = apply._lookup_evidence(
        cfg,
        src["resource"],
        src["quote"],
        source_entry=src,
    )
    assert v is True
    assert is_yours is False
    assert apply._is_quote_yours(cfg, src["resource"], recorded_role=src["role"]) is False


def test_f75_source_with_purged_archive_and_no_recorded_role_writes_no_role_said_check(
    initialized, home, fake
):
    """If the archive row is gone and nothing is recorded, write no role, said or check (F75)."""
    cfg = config_with_fake(home, initialized.vault, fake)
    register(cfg, "widget")

    quote = "Background task ran."
    statement = "Background task ran."

    # A digest whose archive row is missing and has no check
    dpath = write_digest(
        cfg,
        "widget",
        "d-missing-row",
        facts=[
            {
                "statement": statement,
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": "s1",
            }
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": quote,
            }
        ],
        store_evidence=False,
        with_check=False,
    )

    fake.script = [reply("add")]
    rep = apply.run_project(cfg, "widget", [dpath], now=NOW)
    # Since evidence is not verified without archive row or check hash, it is rejected at R1
    assert len(rep.rejected) == 1
    assert rep.rejected[0].rule == "R1"


def test_digest_metadata_keyed_avoids_collision_on_same_statement(initialized, home, fake):
    """L11: two facts with the same statement do not collide in digest metadata."""
    cfg = config_with_fake(home, initialized.vault, fake)
    registry = Registry.load(cfg.registry_path)
    registry.add("widget", "Widget")
    registry.save()

    statement = "The service runs on port 8080."
    write_digest(
        cfg,
        "widget",
        "d-collision",
        facts=[
            {
                "statement": statement,
                "kind": "fact",
                "source": "stated",
                "date": TODAY,
                "ref": "s1",
                "entry": "e1",
                "scope": "project",
                "term": "long",
            },
            {
                "statement": statement,
                "kind": "fact",
                "source": "stated",
                "date": TODAY,
                "ref": "s2",
                "entry": "e2",
                "scope": "global",
                "term": "long",
            },
        ],
        sources=[
            {
                "id": "s1",
                "resource": "aft://session/claude/sess1#e1",
                "quote": statement,
            },
            {
                "id": "s2",
                "resource": "aft://session/claude/sess1#e2",
                "quote": statement,
            },
        ],
    )

    fake.script = [
        reply("add", None, "project fact"),
        reply("add", None, "global fact"),
    ]
    res = schedule.run(cfg, now=NOW)
    report = res.reports["widget"]
    assert len(report.added) == 2
    paths = {a.path for a in report.added}
    assert paths == {"projects/widget/overview.md", "global/profile.md"}


def test_the_dream_walks_the_digests_once_per_run(initialized, home, fake, monkeypatch):
    """The worker-role lookups ask the map one walk of the digests directory built
    (BUILD-PLAN T51): a run that judges several facts of a worker session answers as it
    always did, and walks the directory once for the run, not once per lookup."""
    from aft.dream import shortterm

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

    # The digest of the worker session sits in the tree, already consumed: the run's
    # digest cites its session, and the role lookup answers from the worker's page.
    write_digest(
        cfg,
        "widget",
        "2026-10-05-w-1",
        role="worker",
        facts=[],
        sources=[],
        consumed="dream-widget-20260929T000000Z",
        store_evidence=False,
    )
    write_digest(
        cfg,
        "widget",
        "d1",
        facts=[
            {
                "statement": f"Day {n} of the run is green.",
                "kind": "fact",
                "source": "observed",
                "date": TODAY,
                "ref": f"s{n}",
            }
            for n in (1, 2, 3)
        ],
        sources=[
            {"id": f"s{n}", "resource": f"aft://session/w/1#e{n}", "quote": f"day {n} is green"}
            for n in (1, 2, 3)
        ],
    )

    walks = []
    real_walk = shortterm.digest_roles

    def counting_walk(vault_path):
        walks.append(vault_path)
        return real_walk(vault_path)

    monkeypatch.setattr(shortterm, "digest_roles", counting_walk)

    lookups = []
    real_names = apply._worker_digest_names

    def counting_names(config, agent, sess_id, roles=None):
        lookups.append((agent, sess_id))
        return real_names(config, agent, sess_id, roles)

    monkeypatch.setattr(apply, "_worker_digest_names", counting_names)

    fake.script = [reply("add", None, "worker signal") for _ in range(3)]
    res = schedule.run(cfg, now=NOW)

    assert len(lookups) >= 3, "each fact's source asked the worker role"
    assert len(walks) == 1, "the digests directory was walked once for the run"
    report = res.reports["widget"]
    assert [c.statement for c in report.candidates] == [
        "Day 1 of the run is green.",
        "Day 2 of the run is green.",
        "Day 3 of the run is green.",
    ]
    assert res.ran == ["widget"]


def test_expiry_walks_the_digests_once_per_pass(initialized, home, fake):
    """The expiry of a project's short-term lines answers each line's newest source
    through the map one walk of the digests directory built (BUILD-PLAN T51): two lines
    to judge walk the directory once, and the lines expire as they always did."""
    from aft.dream import shortterm

    cfg = config_with_fake(home, initialized.vault, fake)
    write_digest(
        cfg,
        "widget",
        "2026-10-05-w-1",
        role="worker",
        facts=[],
        sources=[],
        consumed="dream-widget-20260929T000000Z",
        store_evidence=False,
    )
    recent = cfg.vault / "projects" / "widget" / "recent.md"
    recent.parent.mkdir(parents=True)
    recent.write_text(
        "---\ntype: Project\ntitle: t\ndescription: d\n"
        "sources:\n"
        "  - {id: s1, resource: 'aft://session/w/1#e1', quote: first}\n"
        "  - {id: s2, resource: 'aft://session/w/1#e2', quote: second}\n"
        "aft:\n  project: p_widget\n---\n\n"
        "## State\n\n"
        "- The rewrite is paused. [observed] 2026-08-01 [^s1]\n"
        "- The queue is on SQLite. [observed] 2026-08-01 [^s2]\n"
    )

    walks = []
    real_walk = shortterm.digest_roles

    def counting_walk(vault_path):
        walks.append(vault_path)
        return real_walk(vault_path)

    monkeypatch_walk = pytest.MonkeyPatch()
    monkeypatch_walk.setattr(shortterm, "digest_roles", counting_walk)
    try:
        result = apply.expire_short_term(cfg, "widget", now=NOW)
    finally:
        monkeypatch_walk.undo()

    assert result is not None
    report, written = result
    assert [line.statement for line in report.expired] == [
        "The rewrite is paused.",
        "The queue is on SQLite.",
    ]
    assert "projects/widget/recent.md" in written
    assert len(walks) == 1, "the digests directory was walked once for the pass"
