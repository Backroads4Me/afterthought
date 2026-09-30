"""Tests for proposing facts from notes and instruction files (M7.6, DESIGN 11)."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from aft import cli, lint, page
from aft.config import Config
from aft.dream import apply, rules
from aft.dream.decide import Proposal
from aft.library import propose
from aft.registry import Registry


@pytest.fixture
def library_env(tmp_path: Path, initialized: Config) -> tuple[Config, Path, Path]:
    """Set up an isolated vault, config, and sample collection directory with manifest."""
    cfg = initialized
    vault_dir = cfg.vault

    # Register project
    reg = Registry(vault_dir / "registry.yaml")
    reg.add("rv-project", "RV Project")
    reg.add("widget", "Widget Project")

    # Create collection
    col_dir = tmp_path / "sample_rvc"
    col_dir.mkdir(parents=True)

    # 1. Notes file (role: notes)
    notes_file = col_dir / "notes.md"
    notes_file.write_text(
        "# Inverter Notes\n\n"
        "Working notes on Victron inverter.\n"
        "Always connect ground before positive terminal.\n",
        encoding="utf-8",
    )

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

    return cfg, manifest_file, col_dir


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
    assert source["verified_at_import"] is True
    assert source["original_retained"] is True

    assert "## Lessons" in body
    assert lint.lint_vault(cfg.vault) == []


def test_propose_facts_from_excluded_instruction_file(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "All CAN messages must use 250 kbps baud rate.",
            "quote": "All CAN messages must use 250 kbps baud rate.",
            "file": "AGENTS.md",
            "kind": "procedure",
            "source": "stated",
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
    assert source["verified_at_import"] is True
    assert source["original_retained"] is False

    assert "## Procedures" in body
    assert lint.lint_vault(cfg.vault) == []


def test_propose_facts_disallowed_roles_rejected(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "RV-C spec is authoritative.",
            "quote": "Authoritative RV-C specification 2023.",
            "file": "rvc_spec.txt",  # role: original
        },
        {
            "statement": "Build script echoes building.",
            "quote": "echo building",
            "file": "tools/build.sh",  # role: exclude (tooling)
        },
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert res.total_proposed == 2
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 2
    assert len(res.digests_written) == 0

    assert "has role 'original'" in res.dropped_facts[0].reason
    assert "excluded as tooling/data" in res.dropped_facts[1].reason


def test_propose_facts_quote_not_found_dropped(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Fabricated fact statement.",
            "quote": "This quote is nowhere in notes.",
            "file": "notes.md",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "quote 'This quote is nowhere in notes.' not found" in res.dropped_facts[0].reason


def test_propose_facts_file_not_in_manifest(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Some fact from mystery file.",
            "quote": "mystery quote",
            "file": "unmanifested.txt",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 1
    assert "not in collection manifest" in res.dropped_facts[0].reason


def test_propose_facts_path_containment_rejected(library_env):
    cfg, manifest_file, _ = library_env

    # 1. Absolute path
    res = propose.propose_facts(
        manifest_file,
        [{"statement": "Secret fact.", "quote": "root", "file": "/etc/passwd"}],
        config=cfg,
    )
    assert len(res.dropped_facts) == 1
    assert "is absolute" in res.dropped_facts[0].reason

    # 2. Directory traversal
    res = propose.propose_facts(
        manifest_file,
        [{"statement": "Traversal fact.", "quote": "foo", "file": "../../outside.txt"}],
        config=cfg,
    )
    assert len(res.dropped_facts) == 1
    assert "steps outside collection root" in res.dropped_facts[0].reason


def test_propose_facts_statement_and_tag_validation(library_env):
    cfg, manifest_file, _ = library_env

    bad_proposals = [
        # Newline in statement
        {
            "statement": "Line 1\nLine 2",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
        },
        # Struck marker in statement
        {
            "statement": "~~Struck statement~~",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
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
        },
        # Invalid scope
        {
            "statement": "Fact with bad scope.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
            "scope": "everywhere",
        },
    ]

    res = propose.propose_facts(manifest_file, bad_proposals, config=cfg)
    assert len(res.verified_facts) == 0
    assert len(res.dropped_facts) == 5
    assert "one line" in res.dropped_facts[0].reason
    assert "cannot contain ~~" in res.dropped_facts[1].reason
    assert "unknown source tag" in res.dropped_facts[2].reason
    assert "unknown fact kind" in res.dropped_facts[3].reason
    assert "unknown fact scope" in res.dropped_facts[4].reason


def test_propose_facts_duplicate_yaml_keys_rejected(library_env, tmp_path):
    _, manifest_file, _ = library_env

    # Duplicate key in proposal YAML mapping
    dup_file = tmp_path / "dup_facts.yaml"
    dup_file.write_text(
        "- statement: Statement one\n"
        "  statement: Statement duplicate\n"
        "  quote: Always connect ground before positive terminal.\n"
        "  file: notes.md\n",
        encoding="utf-8",
    )

    with pytest.raises(propose.ProposeError) as exc_info:
        propose.propose_facts(manifest_file, dup_file)
    assert "duplicate key" in str(exc_info.value)


def test_propose_facts_multiple_projects(library_env, tmp_path):
    cfg, manifest_file, col_dir = library_env

    # Update manifest to target both rv-project and widget
    manifest_data = {
        "name": "rvc",
        "title": "RV-C Standard",
        "description": "RV-C specifications and notes",
        "projects": ["rv-project", "widget"],
        "path": str(col_dir),
        "files": {
            "notes.md": "notes",
            "AGENTS.md": "exclude",
            "rvc_spec.txt": "original",
            "tools/build.sh": "exclude",
        },
    }
    m_file = tmp_path / "multi.yaml"
    m_file.write_text(yaml.safe_dump(manifest_data), encoding="utf-8")

    proposals = [
        {
            "statement": "Victron inverter requires ground connection.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
        }
    ]

    res = propose.propose_facts(m_file, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 2

    paths = res.digests_written
    assert any(p.startswith("digests/rv-project/") for p in paths)
    assert any(p.startswith("digests/widget/") for p in paths)

    assert lint.lint_vault(cfg.vault) == []


def test_propose_facts_dry_run(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Always connect ground first.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg, dry_run=True)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 0
    assert res.committed is False


def test_propose_facts_commit_setting_respected(library_env):
    import dataclasses

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
        }
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.verified_facts) == 1
    assert len(res.digests_written) == 1
    assert res.committed is False


def test_propose_facts_file_disambiguation(library_env):
    cfg, manifest_file, _ = library_env

    proposals = [
        {
            "statement": "Always connect ground first.",
            "quote": "Always connect ground before positive terminal.",
            "file": "notes.md",
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


def test_end_to_end_dream_consumes_candidate_digest(library_env, tmp_path):
    cfg, manifest_file, _ = library_env

    # Simulate library import: notes.md copied to vault/library/rvc/notes.md
    lib_notes = cfg.vault / "library" / "rvc" / "notes.md"
    lib_notes.parent.mkdir(parents=True, exist_ok=True)
    lib_notes.write_text(
        "Working notes on Victron inverter.\nAlways connect ground before positive terminal.\n",
        encoding="utf-8",
    )

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
            "source": "stated",
        },
    ]

    res = propose.propose_facts(manifest_file, proposals, config=cfg)
    assert len(res.digests_written) == 1
    digest_path = cfg.vault / res.digests_written[0]

    # Mock decide so it returns an add proposal for each new fact
    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        rep = apply.run_project(cfg, "rv-project", [digest_path])

    # Assert neither fact was rejected by R1 (quotes verified!)
    rejected_reasons = [r.reason for r in rep.rejected]
    assert "quote not verified" not in rejected_reasons

    # Check that overview was created
    overview = cfg.vault / "projects" / "rv-project" / "overview.md"
    assert overview.is_file()
    ov_text = overview.read_text(encoding="utf-8")

    # The stated fact is added under ## Facts
    assert "All CAN messages must use 250 kbps baud rate." in ov_text
    # The inferred fact enters Candidates section under R3 gate
    assert "Connect ground before positive terminal." in ov_text
    assert "## Candidates" in ov_text

    # Assert digest was marked consumed
    d_text = digest_path.read_text(encoding="utf-8")
    d_fm, _, _, _ = page.split_frontmatter(d_text)
    assert d_fm["aft"]["consumed"].startswith("dream-rv-project-")
