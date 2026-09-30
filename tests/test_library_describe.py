"""Tests for aft library describe command (BUILD-PLAN F14, DESIGN 11 step 4)."""

from __future__ import annotations

import io
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from aft import cli, lint, page, vault
from aft.config import Config
from aft.library import describe, importing
from aft.library.describe import DescribeError
from aft.registry import Registry


@pytest.fixture
def sample_collection(tmp_path: Path, initialized: Config) -> tuple[Path, Path]:
    """A sample collection directory, its manifest, and the project it names registered.

    The project is registered in the vault first: a collection is filed against projects the vault
    already knows, and an import refuses one it has never seen rather than write a reference page
    whose `aft.projects` row `aft lint` then reports as an unknown project.
    """
    col_dir = tmp_path / "sample_rvc"
    col_dir.mkdir(parents=True)

    spec_txt = col_dir / "rvc_spec.txt"
    spec_txt.write_text(
        "# RV-C Specification 2023\n\n"
        "The RV-C protocol specification for recreational vehicles.\n"
        "Edition 2023.1 dated November 2023.\n\n"
        "## Section 1\nIntroduction to CAN network.\n\n"
        "## Section 2\nReference for implementers of RV-C nodes.\n",
        encoding="utf-8",
    )

    clean_md = col_dir / "rvc_clean.md"
    clean_md.write_text(
        "# Clean RV-C\n\nCleaned CAN network notes.\n"
        "Revision B from December 2023.\n"
        "Guidance for device integration.\n",
        encoding="utf-8",
    )

    (col_dir / "AGENTS.md").write_text("Instruction file to exclude\n", encoding="utf-8")

    manifest_data = {
        "name": "rvc",
        "title": "RV-C Standard",
        "description": "RV-C specifications and notes",
        "projects": ["rv-project"],
        "path": str(col_dir),
        "files": {
            "rvc_spec.txt": "original",
            "rvc_clean.md": {
                "role": "derived",
                "relations": {"derived-from": "rvc_spec.txt"},
            },
            "AGENTS.md": "exclude",
        },
    }

    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text(yaml.dump(manifest_data, sort_keys=False), encoding="utf-8")

    with vault.locked(initialized):
        registry = Registry.load(initialized.registry_path)
        registry.add("rv-project", "RV Project")
        registry.save()
    vault.commit(initialized.vault, ["registry.yaml"], "aft: register rv-project")

    return col_dir, manifest_file


def test_verified_description_lands_on_page_with_quote(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Verified description lands on the reference page under ## Description with sources."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C protocol specification defining CAN network communications.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when_to_use": {
                "claim": "Use when implementing CAN message parsing for RV-C nodes.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)

    assert result.collection == "rvc"
    assert len(result.verified_claims) == 3
    assert len(result.rejected_claims) == 0
    assert len(result.pages_modified) == 1
    assert result.committed is True

    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    assert ref_page.is_file()
    page_text = ref_page.read_text(encoding="utf-8")

    # Check ## Description section
    assert "## Description" in page_text
    what_line = "- What: The RV-C protocol specification defining CAN network communications. [^s1]"
    assert what_line in page_text
    assert "- Version: Edition 2023.1 [^s2]" in page_text
    use_line = "- When to use: Use when implementing CAN message parsing for RV-C nodes. [^s3]"
    assert use_line in page_text

    # Check sources in frontmatter
    fm, _, _, _ = page.split_frontmatter(page_text)
    assert fm is not None
    sources = {s["id"]: s for s in fm.get("sources", [])}
    assert "s1" in sources
    assert sources["s1"]["resource"] == "aft://library/rvc/rvc_spec.txt"
    assert sources["s1"]["quote"] == "RV-C protocol specification for recreational vehicles"
    assert "s2" in sources
    assert sources["s2"]["resource"] == "aft://library/rvc/rvc_spec.txt"
    assert sources["s2"]["quote"] == "Edition 2023.1 dated November 2023"
    assert "s3" in sources
    assert sources["s3"]["resource"] == "aft://library/rvc/rvc_spec.txt"
    assert sources["s3"]["quote"] == "Reference for implementers of RV-C nodes"

    # Check lint passes with 0 findings
    findings = lint.lint_file(initialized.vault, ref_page, None)
    assert findings == []

    # Check git log
    log = subprocess.check_output(
        ["git", "log", "-1", "--format=%s"],
        cwd=initialized.vault,
        text=True,
    ).strip()
    assert log == "aft library: describe documents in 'rvc'"


def test_claim_whose_quote_is_not_in_document_is_refused_and_reported(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """A claim whose quote is not in the document is refused and listed in the import report."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Fabricated edition 99.9.",
                "quote": "This quote definitely does not exist in the file anywhere",
            },
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)

    assert len(result.verified_claims) == 2
    assert len(result.rejected_claims) == 1
    rejected_file, rejected_claim, rejected_reason = result.rejected_claims[0]
    assert rejected_file == "rvc_spec.txt"
    assert rejected_claim.name == "version"
    assert "quote for 'version' not found in extracted text" in rejected_reason

    # The verified claims are written, but the refused claim is omitted
    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    page_text = ref_page.read_text(encoding="utf-8")
    assert "- What: The RV-C specification." in page_text
    assert "- When to use: For implementers." in page_text
    assert "Fabricated edition" not in page_text

    # The report contains the rejection
    assert result.report_path is not None
    report_file = initialized.vault / result.report_path
    report_text = report_file.read_text(encoding="utf-8")
    assert "### Rejected descriptions" in report_text
    assert " (version): quote for 'version' not found in extracted text" in report_text


def test_description_for_file_outside_collection_is_refused(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """A description for a file outside the collection or manifest is refused."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "../outside.txt",
            "what": {"claim": "Outside file", "quote": "Outside"},
            "version": {"claim": "1.0", "quote": "1.0"},
            "when_to_use": {"claim": "Never", "quote": "Never"},
        },
        {
            "file": "nonexistent.txt",
            "what": {"claim": "Ghost file", "quote": "Ghost"},
            "version": {"claim": "1.0", "quote": "1.0"},
            "when_to_use": {"claim": "Never", "quote": "Never"},
        },
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)

    assert len(result.verified_claims) == 0
    assert len(result.rejected_claims) == 6
    reasons = [r[2] for r in result.rejected_claims]
    assert any("steps outside collection root" in r for r in reasons)
    assert any("not in collection manifest" in r for r in reasons)
    assert result.pages_modified == []


def test_tampered_document_hash_mismatch_is_refused(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """A document whose hash doesn't match the recorded hash in collection.md is refused."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    # Tamper with the vault copy of the file
    vault_file = initialized.vault / "library" / "rvc" / "rvc_spec.txt"
    os.chmod(vault_file, 0o644)
    vault_file.write_text("TAMPERED CONTENT\n", encoding="utf-8")
    os.chmod(vault_file, 0o444)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {"claim": "Spec", "quote": "TAMPERED"},
            "version": {"claim": "2023", "quote": "TAMPERED"},
            "when_to_use": {"claim": "Use", "quote": "TAMPERED"},
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)

    assert len(result.verified_claims) == 0
    assert len(result.rejected_claims) == 3
    assert all("hash mismatch" in r[2] for r in result.rejected_claims)
    assert result.pages_modified == []


def test_rerun_with_same_file_changes_nothing(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """A rerun with the same descriptions file changes nothing (idempotent, no commit)."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C protocol specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    # First run
    res1 = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(res1.pages_modified) == 1
    assert res1.committed is True

    head_commit_before = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=initialized.vault, text=True
    ).strip()

    # Second run (rerun with same file)
    res2 = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(res2.pages_modified) == 0
    assert res2.committed is False
    assert res2.report_path is None

    head_commit_after = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=initialized.vault, text=True
    ).strip()

    assert head_commit_after == head_commit_before


def test_dry_run_verifies_without_modifying_vault(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """--dry-run verifies quotes and hashes without writing to the vault or committing."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C protocol specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(
        manifest_file, desc_file, config=initialized, dry_run=True
    )

    assert len(result.verified_claims) == 3
    assert len(result.rejected_claims) == 0
    assert result.pages_modified == []
    assert result.committed is False

    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    page_text = ref_page.read_text(encoding="utf-8")
    assert "## Description" not in page_text


def test_cli_describe_command(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """aft library describe CLI command executes successfully and exits 0."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C protocol specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    ret = cli.main(["library", "describe", str(manifest_file), str(desc_file)])
    assert ret == 0

    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    assert "## Description" in ref_page.read_text(encoding="utf-8")


def test_unimported_collection_refused(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """An unimported collection is refused before describing."""
    col_dir, manifest_file = sample_collection
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text("- file: rvc_spec.txt\n  what:\n    claim: A\n    quote: A\n")

    with pytest.raises(DescribeError, match="has not been imported yet"):
        describe.describe_collection(manifest_file, desc_file, config=initialized)


def test_excluded_file_refused(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """A description for an excluded file is refused."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "AGENTS.md",
            "what": {"claim": "Instruction file", "quote": "Instruction"},
            "version": {"claim": "1.0", "quote": "1.0"},
            "when_to_use": {"claim": "Never", "quote": "Never"},
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(result.verified_claims) == 0
    assert len(result.rejected_claims) == 3
    assert any("excluded from collection" in r[2] for r in result.rejected_claims)


def test_descriptions_stdin(
    initialized: Config, sample_collection: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Descriptions can be loaded from stdin via '-'."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    yaml_text = """
- file: rvc_spec.txt
  what:
    claim: The RV-C protocol specification.
    quote: RV-C protocol specification for recreational vehicles
  version:
    claim: Edition 2023.1
    quote: Edition 2023.1 dated November 2023
  when_to_use:
    claim: For implementers.
    quote: Reference for implementers of RV-C nodes
"""
    monkeypatch.setattr("sys.stdin", io.StringIO(yaml_text))
    ret = cli.main(["library", "describe", str(manifest_file), "-"])
    assert ret == 0

    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    assert "## Description" in ref_page.read_text(encoding="utf-8")


def test_multiple_documents_and_hyphenated_keys(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Multiple documents and when-to-use with hyphen are supported."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C protocol specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when-to-use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        },
        {
            "file": "rvc_clean.md",
            "what": {
                "claim": "Cleaned notes.",
                "quote": "Cleaned CAN network notes",
            },
            "version": {
                "claim": "Revision B",
                "quote": "Revision B from December 2023",
            },
            "when_to_use": {
                "claim": "For integration.",
                "quote": "Guidance for device integration",
            },
        },
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(result.verified_claims) == 6
    assert len(result.rejected_claims) == 0
    assert len(result.pages_modified) == 2

    # Check both reference pages
    p1 = (initialized.vault / "references" / "rvc" / "rvc_spec.md").read_text(encoding="utf-8")
    assert "- When to use: For implementers." in p1

    p2 = (initialized.vault / "references" / "rvc" / "rvc_clean.md").read_text(encoding="utf-8")
    assert "- What: Cleaned notes." in p2
    assert "- Version: Revision B" in p2
    assert "- When to use: For integration." in p2


def test_flat_string_claims_format(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Descriptions with flat claim and claim_quote keys are supported."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": "The RV-C protocol specification.",
            "what_quote": "RV-C protocol specification for recreational vehicles",
            "version": "Edition 2023.1",
            "version_quote": "Edition 2023.1 dated November 2023",
            "when_to_use": "For implementers.",
            "when_to_use_quote": "Reference for implementers of RV-C nodes",
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(result.verified_claims) == 3
    assert len(result.rejected_claims) == 0
    assert len(result.pages_modified) == 1


def test_duplicate_mapping_key_in_descriptions_rejected(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Duplicate keys in descriptions YAML are rejected via UniqueKeyLoader."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    dup_yaml = """
- file: rvc_spec.txt
  what:
    claim: A
    quote: A
  what:
    claim: B
    quote: B
"""
    desc_file = tmp_path / "dup.yaml"
    desc_file.write_text(dup_yaml, encoding="utf-8")

    with pytest.raises(DescribeError, match="found duplicate key"):
        describe.describe_collection(manifest_file, desc_file, config=initialized)


def test_missing_required_claim_reported_as_failure(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """If what, version, or when_to_use is omitted, it is reported as a failure."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            # version omitted
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "missing.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(result.verified_claims) == 2
    assert len(result.rejected_claims) == 1
    rejected = result.rejected_claims[0]
    assert rejected[1].name == "version"
    assert "missing required claim 'version'" in rejected[2]


def test_source_id_numbering_avoids_collisions(
    initialized: Config, sample_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Source ID numbering increments past any pre-existing sources on the page."""
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    # Pre-populate page with sources s1 and s2
    ref_page = initialized.vault / "references" / "rvc" / "rvc_spec.md"
    orig_text = ref_page.read_text(encoding="utf-8")
    fm, body, _, _ = page.split_frontmatter(orig_text)
    assert fm is not None
    fm["sources"] = [
        {"id": "s1", "resource": "aft://other", "quote": "other quote 1"},
        {"id": "s2", "resource": "aft://other", "quote": "other quote 2"},
    ]
    new_text = f"---\n{yaml.dump(fm, sort_keys=False)}---\n\n{body}"
    ref_page.write_text(new_text, encoding="utf-8")

    desc_data = [
        {
            "file": "rvc_spec.txt",
            "what": {
                "claim": "The RV-C specification.",
                "quote": "RV-C protocol specification for recreational vehicles",
            },
            "version": {
                "claim": "Edition 2023.1",
                "quote": "Edition 2023.1 dated November 2023",
            },
            "when_to_use": {
                "claim": "For implementers.",
                "quote": "Reference for implementers of RV-C nodes",
            },
        }
    ]
    desc_file = tmp_path / "desc.yaml"
    desc_file.write_text(yaml.dump(desc_data, sort_keys=False), encoding="utf-8")

    result = describe.describe_collection(manifest_file, desc_file, config=initialized)
    assert len(result.verified_claims) == 3

    updated_text = ref_page.read_text(encoding="utf-8")
    up_fm, up_body, _, _ = page.split_frontmatter(updated_text)
    assert up_fm is not None
    sources = {s["id"]: s for s in up_fm.get("sources", [])}
    # Pre-existing sources preserved
    assert "s1" in sources
    assert "s2" in sources
    # New sources assigned s3, s4, s5
    assert "s3" in sources
    assert "s4" in sources
    assert "s5" in sources
    assert "[^s3]" in up_body
    assert "[^s4]" in up_body
    assert "[^s5]" in up_body
