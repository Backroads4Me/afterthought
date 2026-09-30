"""Tests for library manifest parser and validator (BUILD-PLAN M7.2 / M7.3)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aft import cli
from aft.library import manifest as manifest_mod


@pytest.fixture
def collection_tree(tmp_path: Path) -> Path:
    """Create a sample library collection directory with mixed files."""
    root = tmp_path / "rvc_docs"
    root.mkdir()

    # Source files (PDFs)
    source_dir = root / "source"
    source_dir.mkdir()
    (source_dir / "rvc_spec_2023.pdf").write_bytes(b"%PDF-1.4 test 2023")
    (source_dir / "rvc_spec_2016.pdf").write_bytes(b"%PDF-1.4 test 2016")

    # Derived file (clean markdown of spec)
    (root / "rvc_spec_clean.md").write_text("# RV-C Spec Clean\nExtracted content")

    # Errata file (JSON)
    (root / "spec_errata.json").write_text('{"PGN 12345": "correction note"}')

    # Working notes
    notes_dir = root / "notes"
    notes_dir.mkdir()
    (notes_dir / "aquahot.md").write_text("# AquaHot notes")

    # Instruction files and tooling
    (root / "AGENTS.md").write_text("# Agent instructions")
    (root / ".gitignore").write_text("*.tmp\n")
    tools_dir = root / "tools"
    tools_dir.mkdir()
    (tools_dir / "generate.py").write_text("# generator\n")

    return root


@pytest.fixture
def valid_manifest_data(collection_tree: Path) -> dict:
    """Valid manifest mapping every surveyed file to a role and relations."""
    return {
        "name": "rvc",
        "title": "RV-C Protocol Documentation",
        "description": "Standard specifications, errata and notes for RV-C CAN network.",
        "projects": ["afterthought", "librecoach"],
        "path": str(collection_tree),
        "files": {
            "source/rvc_spec_2023.pdf": {
                "role": "original",
                "relations": {
                    "supersedes": "source/rvc_spec_2016.pdf",
                },
            },
            "source/rvc_spec_2016.pdf": {
                "role": "original",
            },
            "rvc_spec_clean.md": {
                "role": "derived",
                "relations": {
                    "derived-from": "source/rvc_spec_2023.pdf",
                },
            },
            "spec_errata.json": {
                "role": "errata",
                "relations": {
                    "errata-for": "source/rvc_spec_2023.pdf",
                },
            },
            "notes/aquahot.md": {
                "role": "notes",
            },
            "AGENTS.md": {
                "role": "exclude",
            },
            ".gitignore": {
                "role": "exclude",
            },
            "tools/generate.py": {
                "role": "exclude",
            },
        },
    }


def write_manifest(path: Path, data: dict) -> Path:
    path.write_text(yaml.dump(data, sort_keys=False))
    return path


def test_valid_manifest(tmp_path: Path, valid_manifest_data: dict, capsys):
    """A complete and valid manifest passes validation with exit code 0."""
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)
    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "manifest 'rvc' is valid:" in captured.out
    assert "8 files accounted for" in captured.out


def test_string_role_shortcut(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Entries can be written as simple string roles when they have no relations."""
    valid_manifest_data["files"]["notes/aquahot.md"] = "notes"
    valid_manifest_data["files"][".gitignore"] = "exclude"
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 0


def test_unassigned_file_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Files in the collection but omitted from the manifest fail validation."""
    del valid_manifest_data["files"]["notes/aquahot.md"]
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "unassigned file: 'notes/aquahot.md'" in captured.err


def test_unknown_file_in_manifest_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Files in the manifest that don't exist in the collection fail validation."""
    valid_manifest_data["files"]["nonexistent.pdf"] = {"role": "original"}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "unknown file: 'nonexistent.pdf'" in captured.err


def test_unknown_role_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Invalid roles are rejected with available options listed."""
    valid_manifest_data["files"]["notes/aquahot.md"] = {"role": "doc"}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "unknown role 'doc'" in captured.err or "role must be one of" in captured.err


def test_instruction_file_not_excluded_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Instruction files (AGENTS.md) not marked exclude fail validation."""
    valid_manifest_data["files"]["AGENTS.md"] = {"role": "notes"}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "instruction file 'AGENTS.md' must have role 'exclude'" in captured.err


def test_tooling_allowed_non_exclude_role(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Tooling files (.gitignore, tools/) can be assigned non-exclude roles by author."""
    valid_manifest_data["files"][".gitignore"] = {"role": "notes"}
    valid_manifest_data["files"]["tools/generate.py"] = {"role": "original"}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 0


def test_derived_from_requires_derived_role(tmp_path: Path, valid_manifest_data: dict, capsys):
    """derived-from relation on non-derived role fails validation."""
    valid_manifest_data["files"]["source/rvc_spec_2016.pdf"]["relations"] = {
        "derived-from": "source/rvc_spec_2023.pdf"
    }
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "only allowed on 'derived'" in captured.err


def test_derived_role_requires_derived_from(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Role derived without derived-from relation fails validation."""
    valid_manifest_data["files"]["rvc_spec_clean.md"]["relations"] = {}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "requires 'derived-from' relation" in captured.err


def test_errata_for_requires_errata_role(tmp_path: Path, valid_manifest_data: dict, capsys):
    """errata-for relation on non-errata role fails validation."""
    valid_manifest_data["files"]["notes/aquahot.md"]["relations"] = {
        "errata-for": "source/rvc_spec_2023.pdf"
    }
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "only allowed on 'errata'" in captured.err


def test_duplicate_yaml_keys_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Duplicate YAML keys in manifest are refused."""
    m_path = tmp_path / "manifest_dup.yaml"
    text = (
        "name: rvc\n"
        "title: RV-C\n"
        "description: Docs\n"
        "title: Duplicate Title\n"
        "projects: []\n"
        "files: {}\n"
    )
    m_path.write_text(text, encoding="utf-8")

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "manifest is not valid YAML" in captured.err
    assert "duplicate key" in captured.err


def test_relation_to_nonexistent_target_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """Relations pointing to files outside the collection fail validation."""
    valid_manifest_data["files"]["rvc_spec_clean.md"]["relations"] = {
        "derived-from": "source/missing.pdf"
    }
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "target 'source/missing.pdf' does not exist in collection" in captured.err


def test_relation_to_self_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """A file declaring a relation to itself fails validation."""
    valid_manifest_data["files"]["rvc_spec_clean.md"]["relations"] = {
        "derived-from": "rvc_spec_clean.md"
    }
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "cannot point to itself" in captured.err


def test_relation_to_excluded_file_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """A relation pointing to an excluded file fails validation."""
    valid_manifest_data["files"]["rvc_spec_clean.md"]["relations"] = {"derived-from": "AGENTS.md"}
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "points to excluded file 'AGENTS.md'" in captured.err


def test_path_override(tmp_path: Path, valid_manifest_data: dict, collection_tree: Path, capsys):
    """The --path CLI option overrides the path specified in the manifest."""
    valid_manifest_data["path"] = "/nonexistent/path"
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    # Without override it fails
    assert cli.main(["library", "validate", str(m_path)]) == 2

    # With override pointing to actual collection tree it succeeds
    exit_code = cli.main(["library", "validate", str(m_path), "--path", str(collection_tree)])
    assert exit_code == 0


def test_malformed_yaml_rejected(tmp_path: Path, capsys):
    """Malformed YAML reports an error and returns code 2."""
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("name: [unclosed list\n")

    exit_code = cli.main(["library", "validate", str(bad_yaml)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "aft: manifest is not valid YAML:" in captured.err


def test_missing_required_fields_rejected(tmp_path: Path, capsys):
    """Manifest missing required fields fails validation."""
    incomplete = tmp_path / "incomplete.yaml"
    incomplete.write_text("name: ''\n")

    exit_code = cli.main(["library", "validate", str(incomplete)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "name must be a lowercase alphanumeric identifier" in captured.err
    assert "title must be a non-empty string" in captured.err
    assert "description must be a non-empty string" in captured.err


def test_manifest_authority_defaults_to_reference(tmp_path: Path, valid_manifest_data: dict):
    """A manifest without authority defaults to reference."""
    if "authority" in valid_manifest_data:
        del valid_manifest_data["authority"]
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    m = manifest_mod.validate(m_path)
    assert m.authority == "reference"


def test_manifest_authority_explicit_authoritative(tmp_path: Path, valid_manifest_data: dict):
    """A manifest with authority: authoritative validates successfully."""
    valid_manifest_data["authority"] = "authoritative"
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    m = manifest_mod.validate(m_path)
    assert m.authority == "authoritative"


def test_manifest_authority_explicit_reference(tmp_path: Path, valid_manifest_data: dict):
    """A manifest with authority: reference validates successfully."""
    valid_manifest_data["authority"] = "reference"
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    m = manifest_mod.validate(m_path)
    assert m.authority == "reference"


def test_manifest_invalid_authority_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """An invalid authority string fails validation."""
    valid_manifest_data["authority"] = "official"
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "authority must be one of" in captured.err


def test_manifest_non_string_authority_rejected(tmp_path: Path, valid_manifest_data: dict, capsys):
    """A non-string authority value fails validation."""
    valid_manifest_data["authority"] = 123
    m_path = write_manifest(tmp_path / "manifest.yaml", valid_manifest_data)

    exit_code = cli.main(["library", "validate", str(m_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "authority must be a string" in captured.err
