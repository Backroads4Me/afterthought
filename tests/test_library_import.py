"""Tests for library import command, extraction, reference pages and index (BUILD-PLAN M7.3)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from aft import cli, index, lint, page, vault
from aft.config import Config
from aft.library import importing
from aft.library import manifest as manifest_mod
from aft.registry import Registry


@pytest.fixture
def sample_collection(tmp_path: Path) -> tuple[Path, Path]:
    """Create a sample collection directory and valid manifest file."""
    col_dir = tmp_path / "sample_rvc"
    col_dir.mkdir(parents=True)

    # 1. Original file
    spec_txt = col_dir / "rvc_spec.txt"
    spec_txt.write_text(
        "# RV-C Specification 2023\n\n## Section 1\nIntroduction to CAN network.\n\n"
        "## Section 2\nParameters and DGN definitions.\n",
        encoding="utf-8",
    )

    # 2. Derived file (cleans / derives from rvc_spec.txt)
    clean_md = col_dir / "rvc_clean.md"
    clean_md.write_text(
        "# Clean RV-C\n\n## Section 1 Cleaned\nRefined CAN network notes.\n\n"
        "## Section 2 Cleaned\nRefined DGN tables.\n",
        encoding="utf-8",
    )

    # 3. Notes file
    notes_md = col_dir / "notes.md"
    notes_md.write_text(
        "# Inverter Notes\n\n## Inverter Details\nWorking notes on Victron inverter.\n",
        encoding="utf-8",
    )

    # 4. JSON file (split by top-level key)
    errata_json = col_dir / "errata.json"
    errata_json.write_text(
        json.dumps(
            {
                "DGN_123": {"issue": "typo in length", "fix": "set to 8"},
                "DGN_456": {"issue": "inverted polarity", "fix": "active high"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # 5. Excluded instruction file and tooling
    (col_dir / "AGENTS.md").write_text("Instruction file to exclude\n", encoding="utf-8")
    (col_dir / "tools").mkdir()
    (col_dir / "tools" / "build.sh").write_text("#!/bin/sh\necho building\n", encoding="utf-8")

    manifest_data = {
        "name": "rvc",
        "title": "RV-C Standard",
        "description": "RV-C specifications and errata",
        "projects": ["rv-project"],
        "path": str(col_dir),
        "files": {
            "rvc_spec.txt": "original",
            "rvc_clean.md": {
                "role": "derived",
                "relations": {"derived-from": "rvc_spec.txt"},
            },
            "notes.md": "notes",
            "errata.json": {
                "role": "errata",
                "relations": {"errata-for": "rvc_spec.txt"},
            },
            "AGENTS.md": "exclude",
            "tools/build.sh": "exclude",
        },
    }

    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text(yaml.dump(manifest_data, sort_keys=False), encoding="utf-8")

    return col_dir, manifest_file


def test_import_copies_files_and_sets_readonly(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    result = importing.import_collection(manifest_file, config=initialized)

    assert result.collection == "rvc"
    assert result.files_imported == 4
    assert result.files_excluded == 2

    lib_dir = initialized.vault / "library" / "rvc"
    assert (lib_dir / "rvc_spec.txt").is_file()
    assert (lib_dir / "rvc_clean.md").is_file()
    assert (lib_dir / "notes.md").is_file()
    assert (lib_dir / "errata.json").is_file()

    # Excluded files are not copied into the library
    assert not (lib_dir / "AGENTS.md").exists()
    assert not (lib_dir / "tools" / "build.sh").exists()

    # Imported files are read-only (0o444)
    spec_mode = stat.S_IMODE((lib_dir / "rvc_spec.txt").stat().st_mode)
    assert spec_mode & 0o222 == 0  # no write bits set


def test_import_extracts_and_caches_text(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    # Check cached extracted text in home/library/<hash>.json
    cache_dir = initialized.home / "library"
    assert cache_dir.is_dir()

    errata_hash = hashlib.sha256((col_dir / "errata.json").read_bytes()).hexdigest()
    cached_errata = cache_dir / f"{errata_hash}.json"
    assert cached_errata.is_file()

    data = json.loads(cached_errata.read_text(encoding="utf-8"))
    headings = {c["heading"] for c in data["chunks"]}
    assert "DGN_123" in headings
    assert "DGN_456" in headings


def test_import_generates_reference_pages(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    col_md = initialized.vault / "references" / "rvc" / "collection.md"
    assert col_md.is_file()

    text = col_md.read_text(encoding="utf-8")
    assert "type: Reference" in text
    assert "collection: rvc" in text
    assert "projects:" in text
    assert "rv-project" in text
    assert "## Documents" in text
    assert "## Contents Map" in text
    assert "rvc_spec.txt" in text
    assert "rvc_clean.md" in text

    # Per-document reference pages exist
    assert (initialized.vault / "references" / "rvc" / "rvc_clean.md").is_file()
    assert (initialized.vault / "references" / "rvc" / "notes.md").is_file()


def test_import_indexes_in_library_collection(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    importing.import_collection(manifest_file, config=initialized)

    with index._connect(initialized) as conn:
        # Search library table for "Refined CAN"
        cur = conn.execute("SELECT path, heading, body FROM library WHERE library MATCH 'CAN'")
        hits = cur.fetchall()
        assert len(hits) >= 1

        # Check derived text replaced original in index (DESIGN 11)
        cur = conn.execute("SELECT path FROM library WHERE path = 'library/rvc/rvc_spec.txt'")
        assert cur.fetchall() == []  # rvc_spec.txt had derived counterpart rvc_clean.md

        cur = conn.execute("SELECT path FROM library WHERE path = 'library/rvc/rvc_clean.md'")
        assert len(cur.fetchall()) > 0

        # Check JSON keys in library
        cur = conn.execute("SELECT heading FROM library WHERE library MATCH 'polarity'")
        assert any(row[0] == "DGN_456" for row in cur.fetchall())


def test_import_records_hashes_and_lint_verifies(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    with vault.locked(initialized):
        reg = Registry.load(initialized.registry_path)
        reg.add("rv-project", "RV Project")
        reg.save()
    importing.import_collection(manifest_file, config=initialized)

    # 1. Clean vault passes lint
    assert lint.lint_vault(initialized.vault) == []

    # 2. Modifying a library file reports hash mismatch
    lib_file = initialized.vault / "library" / "rvc" / "notes.md"
    os.chmod(lib_file, 0o644)
    lib_file.write_text("tampered content\n", encoding="utf-8")
    os.chmod(lib_file, 0o444)

    findings = lint.lint_vault(initialized.vault)
    assert any("hash mismatch" in str(f) for f in findings)

    # 3. Deleting a library file reports missing
    os.chmod(lib_file, 0o644)
    lib_file.unlink()

    findings = lint.lint_vault(initialized.vault)
    assert any("library file missing" in str(f) for f in findings)


def test_import_writes_report_and_commits(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    col_dir, manifest_file = sample_collection
    now = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)
    result = importing.import_collection(manifest_file, config=initialized, now=now)

    report_path = initialized.vault / result.report_path
    assert report_path.is_file()
    content = report_path.read_text(encoding="utf-8")
    assert "Library import: rvc" in content
    assert "rvc_spec.txt" in content
    assert "AGENTS.md" in content

    # Git log has the commit
    res = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"],
        cwd=initialized.vault,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "aft library: import collection 'rvc'" in res.stdout


def test_cli_library_import(
    initialized: Config, sample_collection: tuple[Path, Path], capsys
) -> None:
    col_dir, manifest_file = sample_collection
    code = cli.main(["library", "import", str(manifest_file)])
    assert code == 0

    out = capsys.readouterr().out
    assert "imported collection 'rvc'" in out
    assert "4 files imported" in out
    assert "2 excluded" in out


def test_import_fails_on_invalid_manifest(initialized: Config, tmp_path: Path) -> None:
    bad_manifest = tmp_path / "bad_manifest.yaml"
    bad_manifest.write_text("name: 123_invalid\n", encoding="utf-8")

    with pytest.raises(manifest_mod.ManifestError):
        importing.import_collection(bad_manifest, config=initialized)

    # Vault library directory remains untouched
    assert not (initialized.vault / "library").exists()


def test_import_verifies_exclude_map_files_with_planted_false_claim(
    initialized: Config, tmp_path: Path
) -> None:
    """import calls verify_map_files for exclude-role map files and writes failures (B1)."""
    col_dir = tmp_path / "planted_col"
    col_dir.mkdir()

    # 1. Authentic document
    (col_dir / "guide.md").write_text(
        "# User Guide\n\n## Getting Started\nThis is the authentic guide content.\n",
        encoding="utf-8",
    )

    # 2. Excluded index.json with 1 valid claim and 2 false claims
    (col_dir / "index.json").write_text(
        json.dumps(
            [
                {
                    "path": "guide.md",
                    "heading": "Getting Started",
                    "quote": "authentic guide content",
                },
                {
                    "path": "guide.md",
                    "heading": "Getting Started",
                    "quote": "This false statement was never in guide",
                },
                {
                    "path": "missing_doc.pdf",
                    "heading": "Chapter 1",
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    # 3. Excluded README.md with 1 valid claim and 1 path traversal false claim
    (col_dir / "README.md").write_text(
        "# Table of Contents\n\n"
        "| File | Link |\n"
        "|---|---|\n"
        "| Guide | [Guide](guide.md#Getting Started) |\n"
        "| Escaping file | [Secret](../outside_secret.txt) |\n",
        encoding="utf-8",
    )

    manifest_data = {
        "name": "planted",
        "title": "Planted False Claims Test",
        "description": "Verification of map evidence during import",
        "projects": [],
        "path": str(col_dir),
        "files": {
            "guide.md": "original",
            "index.json": "exclude",
            "README.md": "exclude",
        },
    }

    m_file = tmp_path / "manifest.yaml"
    m_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

    result = importing.import_collection(m_file, config=initialized)

    # Verify ImportResult carries map_summary
    assert result.map_summary is not None
    assert result.map_summary.total == 5
    assert len(result.map_summary.verified) == 2
    assert len(result.map_summary.rejected) == 3

    # Verify report contains Map evidence section and rejected claims
    report_file = initialized.vault / result.report_path
    assert report_file.is_file()
    report_text = report_file.read_text(encoding="utf-8")

    assert "### Map evidence" in report_text
    assert "Claims checked: 5" in report_text
    assert "Claims verified: 2" in report_text
    assert "Claims rejected: 3" in report_text
    assert "#### Rejected claims" in report_text

    # Verify specific reasons for planted false claims
    assert "quoted description does not appear in 'guide.md'" in report_text
    assert "missing_doc.pdf" in report_text
    assert "does not exist in collection" in report_text
    assert "outside_secret.txt" in report_text
    assert "escapes collection root" in report_text


def test_import_manifest_without_authority_defaults_to_reference(
    initialized: Config, sample_collection: tuple[Path, Path]
) -> None:
    """A manifest without authority imports as reference (BUILD-PLAN F5)."""
    col_dir, manifest_file = sample_collection
    now = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
    result = importing.import_collection(manifest_file, config=initialized, now=now)

    assert result.authority == "reference"

    # 1. references/<name>/collection.md records authority: reference in frontmatter under aft:
    col_md = initialized.vault / "references" / "rvc" / "collection.md"
    assert col_md.is_file()
    text = col_md.read_text(encoding="utf-8")
    fm, _, _, _ = page.split_frontmatter(text)
    assert fm["aft"]["authority"] == "reference"

    # 2. Import report records Authority: reference
    rep_file = initialized.vault / result.report_path
    assert rep_file.is_file()
    assert "- Authority: reference" in rep_file.read_text(encoding="utf-8")

    # 3. Commit message records authority
    res = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"],
        cwd=initialized.vault,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "aft library: import collection 'rvc' (reference)" in res.stdout


def test_import_authoritative_manifest(initialized: Config, tmp_path: Path) -> None:
    """An authoritative manifest records authority across pages, report, and commit."""
    col_dir = tmp_path / "auth_col"
    col_dir.mkdir()
    (col_dir / "standard.md").write_text(
        "# Standard\nOfficial protocol standard.\n", encoding="utf-8"
    )

    manifest_data = {
        "name": "standard-spec",
        "title": "Protocol Standard",
        "description": "Authoritative protocol specification",
        "authority": "authoritative",
        "projects": [],
        "path": str(col_dir),
        "files": {
            "standard.md": "original",
        },
    }
    m_file = tmp_path / "manifest.yaml"
    m_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

    now = datetime(2026, 9, 29, 15, 30, tzinfo=UTC)
    result = importing.import_collection(m_file, config=initialized, now=now)
    assert result.authority == "authoritative"

    # 1. references/<name>/collection.md records authority in frontmatter
    col_md = initialized.vault / "references" / "standard-spec" / "collection.md"
    assert col_md.is_file()
    fm, _, _, _ = page.split_frontmatter(col_md.read_text(encoding="utf-8"))
    assert fm["aft"]["authority"] == "authoritative"

    # 2. Report records Authority: authoritative
    rep_file = initialized.vault / result.report_path
    assert "- Authority: authoritative" in rep_file.read_text(encoding="utf-8")

    # 3. Commit message names authority
    res = subprocess.run(
        ["git", "log", "-n", "1", "--oneline"],
        cwd=initialized.vault,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "aft library: import collection 'standard-spec' (authoritative)" in res.stdout
