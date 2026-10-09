"""Tests for proposing facts from notes and instruction files (M7.6, DESIGN 11)."""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from aft import cli, evidence, lint, page
from aft.config import Config
from aft.dream import apply, rules
from aft.dream.decide import Proposal
from aft.library import importing, propose
from aft.registry import Registry

SAMPLE_ROLES = {
    "rvc_spec.txt": "original",
    "notes.md": "notes",
    "AGENTS.md": "exclude",
    "tools/build.sh": "exclude",
}


@pytest.fixture
def library_env(tmp_path: Path, initialized: Config) -> tuple[Config, Path, Path]:
    """Set up an isolated vault, config, and sample collection directory with manifest."""
    cfg = initialized
    vault_dir = cfg.vault

    # Register project
    reg = Registry.load(vault_dir / "registry.yaml")
    if not reg.by_slug("rv-project"):
        reg.add("rv-project", "RV Project")
    if not reg.by_slug("widget"):
        reg.add("widget", "Widget Project")
    reg.save()

    # Create collection
    col_dir = tmp_path / "sample_rvc"
    col_dir.mkdir(parents=True)

    # 1. Notes file (role: notes)
    notes_file = col_dir / "notes.md"
    notes_text = (
        "# Inverter Notes\n\n"
        "Working notes on Victron inverter.\n"
        "Always connect ground before positive terminal.\n"
    )
    notes_file.write_text(notes_text, encoding="utf-8")

    # 2. Excluded instruction file (role: exclude)
    agents_file = col_dir / "AGENTS.md"
    agents_file.write_text(
        "# Agent Instructions\n\n"
        "All CAN messages must use 250 kbps baud rate.\n"
        "Never send raw hex without a DGN description.\n",
        encoding="utf-8",
    )

    # 3. Original file (role: original)
    spec_file = col_dir / "rvc_spec.txt"
    spec_file.write_text("Authoritative RV-C specification 2023.\n", encoding="utf-8")

    # 4. Tooling file (role: exclude)
    tools_dir = col_dir / "tools"
    tools_dir.mkdir()
    tool_file = tools_dir / "build.sh"
    tool_file.write_text("#!/bin/sh\necho building\n", encoding="utf-8")

    manifest_data = {
        "name": "rvc",
        "title": "RV-C Standard",
        "description": "RV-C specifications and notes",
        "projects": ["rv-project"],
        "path": str(col_dir),
        "files": {
            "notes.md": "notes",
            "AGENTS.md": "exclude",
            "rvc_spec.txt": "original",
            "tools/build.sh": "exclude",
        },
    }
    manifest_file = tmp_path / "rvc.yaml"
    manifest_file.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")

    # The collection the tests propose against was imported, not hand-copied: the import is
    # what leaves the record (references/rvc/) that propose and the dream verify against.
    importing.import_collection(manifest_file, config=cfg)

    return cfg, manifest_file, col_dir


def import_collection_as(
    cfg: Config,
    col_dir: Path,
    *,
    name: str,
    roles: dict,
    authority: str = "reference",
    projects: tuple[str, ...] = ("rv-project",),
) -> Path:
    """Import the sample collection this way and return the manifest the import read.

    Authority and roles are set by an import and recorded in the vault, so a test that wants a
    collection imported differently imports it that way instead of editing a manifest afterwards:
    propose and the dream trust the record, not the manifest (DESIGN 11).
    """
    manifest_data = {
        "name": name,
        "title": f"{name} collection",
        "description": f"RV-C specifications and notes, imported as {authority}.",
        "projects": list(projects),
        "path": str(col_dir),
        "authority": authority,
        "files": roles,
    }
    manifest_file = col_dir.parent / f"{name}.yaml"
    manifest_file.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")
    importing.import_collection(manifest_file, config=cfg)
    return manifest_file


def test_propose_facts_from_notes(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Connect ground before positive terminal on Victron inverter.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "kind": "lesson",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert res.total_proposed == 1
    assert len(res.verified_facts) == 1
    assert len(res.dropped_facts) == 0
    assert len(res.digests_written) == 1
    assert res.committed is True

    digest_rel = res.digests_written[0]
    assert digest_rel.startswith("digests/rv-project/")
    digest_path = cfg.vault / digest_rel
    assert digest_path.is_file()

    text = digest_path.read_text(encoding="utf-8")
    fm, body, _, _ = page.split_frontmatter(text)
    assert fm["type"] == "Session Digest"
    assert "rvc" in fm["description"]
    assert len(fm["sources"]) == 1

    source = fm["sources"][0]
    assert source["resource"] == "aft://library/rvc/notes.md"
    assert source["quote"] == "Always connect ground before positive terminal."
    assert "verified" in source
    assert source["check"] == evidence.quote_check("rvc", "notes.md", source["quote"])

    assert "## Lessons" in body
    assert lint.lint_vault(cfg.vault) == []


def test_propose_notes_before_import_refused(library_env):
    cfg, manifest_file, _ = library_env

    # Remove the imported copy in the vault
    lib_notes = cfg.vault / "library" / "rvc" / "notes.md"
    if lib_notes.is_file():
        lib_notes.unlink()

    proposals = [
        {
            "statement": "Connect ground before positive terminal.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "collection has not been imported yet" in res.dropped_facts[0].reason


def test_propose_facts_from_excluded_instruction_file(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "All CAN messages must use 250 kbps baud rate.",
            "quote": "All CAN messages must use 250 kbps baud rate.",
            "file": "AGENTS.md",
            "kind": "procedure",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert res.total_proposed == 1
    assert len(res.verified_facts) == 1
    assert len(res.dropped_facts) == 0
    assert len(res.digests_written) == 1

    digest_path = cfg.vault / res.digests_written[0]
    text = digest_path.read_text(encoding="utf-8")
    fm, body, _, _ = page.split_frontmatter(text)

    source = fm["sources"][0]
    assert source["resource"] == "aft://library/rvc/AGENTS.md"

    # Untamperable record is written to home/library/verified_quotes.json
    assert propose.is_verified_import_quote(
        cfg.home,
        "aft://library/rvc/AGENTS.md",
        "All CAN messages must use 250 kbps baud rate.",
    )

    assert "## Procedures" in body
    assert lint.lint_vault(cfg.vault) == []


def test_propose_stated_fact_refused(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Stated fact from notes.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "stated",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "stated facts cannot be proposed" in res.dropped_facts[0].reason


def test_propose_suggested_fact_accepted(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Musing about Victron ground.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "suggested",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert len(res.dropped_facts) == 0


def test_propose_global_scope_refused(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Fact claiming global scope.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "scope": "global",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "cannot have scope 'global'" in res.dropped_facts[0].reason


def test_propose_project_not_in_manifest_refused(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Fact for another project.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "project": "widget",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "is not in collection manifest projects" in res.dropped_facts[0].reason


def test_propose_disallowed_roles_rejected(library_env):
    cfg, manifest_file, _ = library_env

    tooling = [
        {
            "statement": "Build script echoes building.",
            "quote": "echo building",
            "file": "tools/build.sh",  # role: exclude, and no instruction file
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, tooling, config=cfg)
    assert res.total_proposed == 1
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert len(res.digests_written) == 0
    assert "excluded as tooling/data" in res.dropped_facts[0].reason

    # A document the import retained is another matter: its copy and its hash stand.
    document = [
        {
            "statement": "RV-C spec is the standard.",
            "quote": "Authoritative RV-C specification 2023.",
            "file": "rvc_spec.txt",  # role: original, imported
            "source": "inferred",
        }
    ]

    res2 = propose.propose_facts(manifest_file, document, config=cfg)
    assert len(res2.verified_facts) == 1
    assert len(res2.dropped_facts) == 0


def test_propose_quote_not_found_dropped(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Fabricated fact statement.",
            "quote": "This quote is nowhere in notes.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "quote 'This quote is nowhere in notes.' not found" in res.dropped_facts[0].reason


def test_propose_file_not_in_manifest(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Some fact from mystery file.",
            "quote": "mystery quote",
            "file": "unmanifested.txt",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "not in collection manifest" in res.dropped_facts[0].reason


def test_propose_path_containment_rejected(library_env):
    cfg, manifest_file, _ = library_env

    # 1. Absolute path
    res = propose.propose_facts(
        manifest_file,
        [
            {
                "statement": "Secret fact.",
                "quote": "root",
                "file": "/etc/passwd",
                "source": "inferred",
            }
        ],
        config=cfg,
    )
    assert len(res.dropped_facts) == 1
    assert "is absolute" in res.dropped_facts[0].reason

    # 2. Directory traversal
    res = propose.propose_facts(
        manifest_file,
        [
            {
                "statement": "Traversal fact.",
                "quote": "foo",
                "file": "../../outside.txt",
                "source": "inferred",
            }
        ],
        config=cfg,
    )
    assert len(res.dropped_facts) == 1
    assert "steps outside collection root" in res.dropped_facts[0].reason


def test_propose_statement_and_tag_validation(library_env):
    cfg, manifest_file, _ = library_env

    bad_proposals = [
        # Newline in statement
        {
            "statement": "Line 1\nLine 2",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        },
        # Struck marker in statement
        {
            "statement": "~~Struck statement~~",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        },
        # Invalid source tag
        {
            "statement": "Fact with bad source.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "unsupported",
        },
        # Invalid kind
        {
            "statement": "Fact with bad kind.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "kind": "nonsense",
            "source": "inferred",
        },
    ]

    res = propose.propose_facts(manifest_file, bad_proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 4
    assert "one line" in res.dropped_facts[0].reason
    assert "cannot contain ~~" in res.dropped_facts[1].reason
    assert "unsupported source tag" in res.dropped_facts[2].reason
    assert "unknown fact kind" in res.dropped_facts[3].reason


def test_propose_duplicate_yaml_keys_rejected(library_env, tmp_path):
    _, manifest_file, _ = library_env

    dup_file = tmp_path / "dup_facts.yaml"
    dup_file.write_text(
        "- statement: Statement one\n"
        "  statement: Statement duplicate\n"
        "  quote: Always connect ground before positive terminal.\n"
        "  file: notes.md\n"
        "  source: inferred\n",
        encoding="utf-8",
    )

    with pytest.raises(propose.ProposeError) as exc_info:
        propose.propose_facts(manifest_file, dup_file)
    assert "duplicate key" in str(exc_info.value)


def test_propose_multiple_projects(library_env, tmp_path):
    cfg, manifest_file, col_dir = library_env

    # A collection aims its facts at the projects it was imported for, so the run that
    # writes two digests imports the collection for both of them.
    m_file = import_collection_as(
        cfg,
        col_dir,
        name="rvcmulti",
        roles=SAMPLE_ROLES,
        projects=("rv-project", "widget"),
    )

    proposals = [
        {
            "statement": "Victron inverter requires ground connection.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(m_file, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 2

    paths = res.digests_written
    assert any(p.startswith("digests/rv-project/") for p in paths)
    assert any(p.startswith("digests/widget/") for p in paths)

    assert lint.lint_vault(cfg.vault) == []


def test_propose_dry_run(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Always connect ground first.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg, dry_run=True)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 0
    assert res.committed is False


def test_propose_commit_setting_respected(library_env):
    """library.commit=false gates only library/<name>; digests and reports are always committed."""
    cfg, manifest_file, _ = library_env
    new_settings = dataclasses.replace(
        cfg.settings, library=dataclasses.replace(cfg.settings.library, commit=False)
    )
    cfg = dataclasses.replace(cfg, settings=new_settings)

    proposals = [
        {
            "statement": "Always connect ground first.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 1
    assert res.committed is True


def test_propose_file_disambiguation(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Always connect ground first.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "inferred",
        }
    ]

    res1 = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res1.digests_written) == 1
    digest1 = res1.digests_written[0]

    # Run again on same day
    res2 = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res2.digests_written) == 1
    digest2 = res2.digests_written[0]

    assert digest1 != digest2
    assert digest2.endswith("-2.md")
    assert (cfg.vault / digest1).is_file()
    assert (cfg.vault / digest2).is_file()


def test_propose_facts_input_formats(library_env):
    cfg, manifest_file, _ = library_env

    # 1. Mapping with "facts" list
    stream = io.StringIO(
        yaml.safe_dump(
            {
                "facts": [
                    {
                        "statement": "Always connect ground first.",
                        "quote": "Always connect ground before positive terminal.",
                        "file": "notes.md",
                        "source": "inferred",
                    }
                ]
            }
        )
    )
    res = propose.propose_facts(manifest_file, stream, config=cfg)
    assert len(res.verified_facts) == 1

    # 2. Empty stream
    with pytest.raises(propose.ProposeError):
        propose.propose_facts(manifest_file, io.StringIO(""), config=cfg)


def test_forged_verified_at_import_flag_ignored(library_env):
    """Frontmatter flags in a digest cannot forge verification without the propose record."""
    cfg, _, _ = library_env

    v, origin, _, yours = apply._lookup_evidence(
        cfg,
        "aft://library/rvc/nothing/at/all.md",
        "anything I like",
        source_entry={"verified_at_import": True, "original_retained": False},
    )
    assert v is False
    assert yours is False


def test_stated_library_fact_refused_by_r2_in_dream(library_env):
    """Library quotes are never yours, so a [stated] fact citing library is refused by R2."""
    cfg, _, _ = library_env

    # Hand-craft a digest with a [stated] library fact
    digest_dir = cfg.vault / "digests" / "rv-project" / "2026" / "09"
    digest_dir.mkdir(parents=True, exist_ok=True)
    digest_file = digest_dir / "2026-09-30-library-rvc.md"

    content = (
        "---\n"
        "type: Session Digest\n"
        "title: Forged Stated Digest\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://library/rvc/notes.md\n"
        "    quote: Always connect ground before positive terminal.\n"
        "aft:\n"
        "  project: p_rv-project\n"
        "  facts:\n"
        "    - statement: Always connect ground before positive terminal.\n"
        "      kind: fact\n"
        "      source: stated\n"
        "      scope: project\n"
        "---\n\n"
        "## Facts\n\n"
        "- Always connect ground before positive terminal. [stated] 2026-09-30 [^s1]\n"
    )
    digest_file.write_text(content, encoding="utf-8")

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_file])

    # Refused by R2!
    assert len(rep.rejected) == 1
    assert rep.rejected[0].rule == "R2"


def test_excluded_file_fact_verifies_on_page_and_later_runs(library_env):
    """Excluded file quotes remain verifiable once written to a page on later dream runs."""
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "All CAN messages must use 250 kbps baud rate.",
            "quote": "All CAN messages must use 250 kbps baud rate.",
            "file": "AGENTS.md",
            "kind": "fact",
            "source": "inferred",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    digest_path = cfg.vault / res.digests_written[0]

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])
    assert rep.rejected == []

    assert rep.rejected == []

    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    assert overview.is_file()

    # Read overview page source
    ov_fm, _, _, _ = page.split_frontmatter(overview.read_text(encoding="utf-8"))
    ov_sources = ov_fm.get("sources") or []
    assert len(ov_sources) == 1
    s_entry = ov_sources[0]
    assert s_entry["resource"] == "aft://library/rvc/AGENTS.md"

    # Later dream run lookup on the page source footnote
    v, origin, _, yours = apply._lookup_evidence(
        cfg,
        s_entry["resource"],
        s_entry["quote"],
    )
    assert v is True
    assert yours is False


def test_end_to_end_dream_consumes_candidate_digest(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            # Notes fact (original_retained: True)
            "statement": "Connect ground before positive terminal.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "kind": "lesson",
            "source": "inferred",
        },
        {
            # Excluded instruction fact (original_retained: False)
            "statement": "All CAN messages must use 250 kbps baud rate.",
            "quote": "All CAN messages must use 250 kbps baud rate.",
            "file": "AGENTS.md",
            "kind": "fact",
            "source": "inferred",
        },
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.digests_written) == 1
    digest_path = cfg.vault / res.digests_written[0]

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    # Assert no fact was rejected by R1, R2, or any other rule
    assert rep.rejected == []

    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    assert overview.is_file()
    ov_text = overview.read_text(encoding="utf-8")

    # Inferred facts enter Candidates section under R3 gate
    assert "Connect ground before positive terminal." in ov_text
    assert "All CAN messages must use 250 kbps baud rate." in ov_text
    assert "## Candidates" in ov_text

    # Assert digest was marked consumed
    d_text = digest_path.read_text(encoding="utf-8")
    d_fm, _, _, _ = page.split_frontmatter(d_text)
    assert d_fm["aft"]["consumed"].startswith("dream-rv-project-")


def test_propose_cli(library_env, tmp_path, capsys):
    _, manifest_file, _ = library_env

    facts_file = tmp_path / "facts.yaml"
    facts_file.write_text(
        yaml.safe_dump(
            [
                {
                    "statement": "Always connect ground before positive terminal.",
                    "quote": "Always connect ground before positive terminal.",
                    "file": "notes.md",
                    "source": "inferred",
                }
            ]
        ),
        encoding="utf-8",
    )

    # 1. Propose CLI
    code = cli.main(["library", "propose", str(manifest_file), str(facts_file)])
    assert code == 0
    captured = capsys.readouterr()
    assert "verified 1 fact(s)" in captured.out
    assert "digests/rv-project/" in captured.out

    # 2. Propose-facts alias
    code = cli.main(["library", "propose-facts", str(manifest_file), str(facts_file), "--dry-run"])
    assert code == 0
    captured = capsys.readouterr()
    assert "dry-run: 1 fact(s) verified" in captured.out

    # 3. Invalid facts file
    code = cli.main(["library", "propose", str(manifest_file), str(tmp_path / "nonexistent.yaml")])
    assert code == 2


def test_authoritative_document_fact_enters_observed_and_active_in_dream(library_env, tmp_path):
    """An authoritative document fact enters as [observed] and goes active in dream (F5)."""
    cfg, _, col_dir = library_env

    auth_manifest = import_collection_as(
        cfg, col_dir, name="rvcauth", authority="authoritative", roles=SAMPLE_ROLES
    )

    proposals = [
        {
            "statement": "RV-C specification 2023 is the governing protocol standard.",
            "quote": "Authoritative RV-C specification 2023.",
            "file": "rvc_spec.txt",
            "kind": "fact",
        }
    ]

    res = propose.propose_facts(auth_manifest, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    vf = res.verified_facts[0]
    assert vf.fact.source == "observed"

    digest_rel = res.digests_written[0]
    digest_path = cfg.vault / digest_rel
    d_text = digest_path.read_text(encoding="utf-8")
    assert "[observed]" in d_text

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    assert rep.rejected == []
    assert len(rep.added) == 1

    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    assert overview.is_file()
    ov_text = overview.read_text(encoding="utf-8")
    parsed_ov = page.parse(ov_text, overview)

    facts = [
        f
        for f in parsed_ov.facts
        if f.statement == "RV-C specification 2023 is the governing protocol standard."
    ]
    assert len(facts) == 1
    assert facts[0].kind == "fact"
    assert facts[0].source == "observed"


def test_reference_document_fact_stays_candidate_in_dream(library_env, tmp_path):
    """The same document fact from a reference collection stays a candidate (F5)."""
    cfg, _, col_dir = library_env

    ref_manifest = import_collection_as(cfg, col_dir, name="rvcref", roles=SAMPLE_ROLES)

    proposals = [
        {
            "statement": "RV-C specification 2023 is the governing protocol standard.",
            "quote": "Authoritative RV-C specification 2023.",
            "file": "rvc_spec.txt",
            "kind": "fact",
        }
    ]

    res = propose.propose_facts(ref_manifest, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    vf = res.verified_facts[0]
    assert vf.fact.source == "inferred"

    digest_rel = res.digests_written[0]
    digest_path = cfg.vault / digest_rel
    d_text = digest_path.read_text(encoding="utf-8")
    assert "[inferred]" in d_text

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    assert rep.rejected == []

    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    assert overview.is_file()
    ov_text = overview.read_text(encoding="utf-8")
    parsed_ov = page.parse(ov_text, overview)

    candidate_facts = [
        f
        for f in parsed_ov.facts
        if f.statement == "RV-C specification 2023 is the governing protocol standard."
    ]
    assert len(candidate_facts) == 1
    assert candidate_facts[0].kind is None
    assert candidate_facts[0].source == "inferred"


def test_authoritative_notes_fact_stays_candidate_in_dream(library_env, tmp_path):
    """A notes-file fact in an authoritative collection stays a candidate (F5)."""
    cfg, _, col_dir = library_env

    auth_manifest = import_collection_as(
        cfg, col_dir, name="rvcauth", authority="authoritative", roles=SAMPLE_ROLES
    )

    proposals = [
        {
            "statement": "Always connect ground before positive terminal.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "kind": "lesson",
        }
    ]

    res = propose.propose_facts(auth_manifest, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert res.verified_facts[0].fact.source == "inferred"

    digest_rel = res.digests_written[0]
    digest_path = cfg.vault / digest_rel

    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    assert rep.rejected == []

    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    ov_text = overview.read_text(encoding="utf-8")
    parsed_ov = page.parse(ov_text, overview)
    candidate_facts = [
        f
        for f in parsed_ov.facts
        if f.statement == "Always connect ground before positive terminal."
    ]
    assert len(candidate_facts) == 1
    assert candidate_facts[0].kind is None


def test_authoritative_fact_contradicting_stated_decision_becomes_needs_you(library_env, tmp_path):
    """An authoritative fact contradicting a stated decision becomes needs you (F5)."""
    cfg, _, col_dir = library_env

    overview_path = cfg.vault / "projects" / "rv-project" / "overview.md"
    overview_path.parent.mkdir(parents=True, exist_ok=True)
    reg = Registry.load(cfg.vault / "registry.yaml")
    proj = reg.by_slug("rv-project")
    proj_id = proj.id if proj else "p_rv-project"
    standing_content = (
        f"---\n"
        f"type: Project\n"
        f"title: rv-project\n"
        f"sources:\n"
        f"  - id: s0\n"
        f"    resource: aft://session/user/seed#turn1\n"
        f"    quote: We decided to use 500 kbps.\n"
        f"aft:\n"
        f"  project: {proj_id}\n"
        f"---\n\n"
        f"# rv-project\n\n"
        f"## Decisions\n\n"
        f"- We decided on CAN baud rate 500 kbps. [stated] 2026-09-01 [^s0]\n"
    )
    overview_path.write_text(standing_content, encoding="utf-8")

    # The spec that contradicts the standing decision is its own collection, imported as
    # authoritative: authority comes from the import, not from a manifest edited afterwards.
    spec_dir = tmp_path / "baud-spec"
    spec_dir.mkdir()
    (spec_dir / "rvc_spec.txt").write_text(
        "All CAN communication must use 250 kbps baud rate.\n", encoding="utf-8"
    )
    auth_manifest = import_collection_as(
        cfg,
        spec_dir,
        name="rvcbaud",
        authority="authoritative",
        roles={"rvc_spec.txt": "original"},
    )

    proposals = [
        {
            "statement": "CAN communication must use 250 kbps baud rate.",
            "quote": "All CAN communication must use 250 kbps baud rate.",
            "file": "rvc_spec.txt",
            "kind": "fact",
        }
    ]

    res = propose.propose_facts(auth_manifest, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert res.verified_facts[0].fact.source == "observed"

    digest_rel = res.digests_written[0]
    digest_path = cfg.vault / digest_rel

    from aft.dream import decide

    def fake_decide(config, new_fact, quote, slug):
        standing_page = page.parse(overview_path.read_text(encoding="utf-8"), overview_path)
        dec_line = [f for f in standing_page.facts if f.kind == "decision"][0]
        target_line = decide.Line(
            label="L1",
            path="projects/rv-project/overview.md",
            project=proj_id,
            fact=dec_line,
            sources={
                "s0": {
                    "resource": "aft://session/user/seed#turn1",
                    "quote": "We decided to use 500 kbps.",
                }
            },
        )
        return Proposal(
            new_fact=new_fact,
            lines=(target_line,),
            decision=rules.Decision(
                action="conflict", line="L1", reason="specification requires 250 kbps"
            ),
            target=target_line,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    assert len(rep.questions) == 1
    q = rep.questions[0]
    assert "CAN communication must use 250 kbps baud rate" in q.new_statement
    assert "We decided on CAN baud rate 500 kbps." in q.target_statement

    ov_text = overview_path.read_text(encoding="utf-8")
    assert "[!question]" in ov_text


def test_stated_source_always_refused_on_propose(library_env, tmp_path):
    """[stated] is always refused on propose from library files (F5)."""
    cfg, manifest_file, col_dir = library_env

    auth_data = {
        "name": "rvc-auth",
        "title": "RV-C Standard Auth",
        "description": "RV-C specifications and notes",
        "authority": "authoritative",
        "projects": ["rv-project"],
        "path": str(col_dir),
        "files": {
            "rvc_spec.txt": "original",
            "notes.md": "notes",
            "AGENTS.md": "exclude",
            "tools/build.sh": "exclude",
        },
    }
    auth_file = tmp_path / "auth.yaml"
    auth_file.write_text(yaml.safe_dump(auth_data), encoding="utf-8")

    lib_dir = cfg.vault / "library" / "rvc-auth"
    lib_dir.mkdir(parents=True, exist_ok=True)
    (lib_dir / "rvc_spec.txt").write_text(
        "Authoritative RV-C specification 2023.\n", encoding="utf-8"
    )
    (lib_dir / "notes.md").write_text(
        "Always connect ground before positive terminal.\n", encoding="utf-8"
    )

    # 1. Propose stated from document file in authoritative collection
    proposals_doc = [
        {
            "statement": "RV-C spec is authoritative.",
            "quote": "Authoritative RV-C specification 2023.",
            "file": "rvc_spec.txt",
            "source": "stated",
        }
    ]
    res_doc = propose.propose_facts(auth_file, proposals_doc, config=cfg)
    assert len(res_doc.verified_facts) == 0
    assert len(res_doc.dropped_facts) == 1
    assert "stated facts cannot be proposed from library files" in res_doc.dropped_facts[0].reason

    # 2. Propose stated from notes file in authoritative collection
    proposals_notes = [
        {
            "statement": "Connect ground before positive.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "source": "stated",
        }
    ]
    res_notes = propose.propose_facts(auth_file, proposals_notes, config=cfg)
    assert len(res_notes.verified_facts) == 0
    assert len(res_notes.dropped_facts) == 1
    assert "stated facts cannot be proposed from library files" in res_notes.dropped_facts[0].reason

    # 3. Propose stated from reference collection
    res_ref = propose.propose_facts(manifest_file, proposals_notes, config=cfg)
    assert len(res_ref.verified_facts) == 0
    assert len(res_ref.dropped_facts) == 1
    assert "stated facts cannot be proposed from library files" in res_ref.dropped_facts[0].reason


def test_propose_derived_and_errata_document_roles(library_env, tmp_path):
    """Document facts from derived and errata roles are supported and tagged [observed] (F5)."""
    cfg, _, col_dir = library_env

    (col_dir / "clean_spec.md").write_text(
        "# Clean Spec\nNormalized RV-C 2023.\n", encoding="utf-8"
    )
    (col_dir / "errata.md").write_text(
        "# Errata\nCorrection: DGN 456 uses big-endian polarity.\n", encoding="utf-8"
    )

    manifest_data = {
        "name": "rvc-docs",
        "title": "RV-C Docs",
        "description": "Full docs",
        "authority": "authoritative",
        "projects": ["rv-project"],
        "path": str(col_dir),
        "files": {
            "rvc_spec.txt": "original",
            "clean_spec.md": {
                "role": "derived",
                "relations": {"derived-from": "rvc_spec.txt"},
            },
            "errata.md": {
                "role": "errata",
                "relations": {"errata-for": "rvc_spec.txt"},
            },
            "notes.md": "notes",
            "AGENTS.md": "exclude",
            "tools/build.sh": "exclude",
        },
    }
    m_path = tmp_path / "docs.yaml"
    m_path.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")

    importing.import_collection(m_path, config=cfg)

    proposals = [
        {
            "statement": "RV-C 2023 is normalized in clean spec.",
            "quote": "Normalized RV-C 2023.",
            "file": "clean_spec.md",
        },
        {
            "statement": "DGN 456 uses big-endian polarity.",
            "quote": "Correction: DGN 456 uses big-endian polarity.",
            "file": "errata.md",
        },
    ]

    res = propose.propose_facts(m_path, proposals, config=cfg)
    assert len(res.verified_facts) == 2
    assert res.verified_facts[0].fact.source == "observed"
    assert res.verified_facts[1].fact.source == "observed"
