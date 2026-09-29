"""Tests for imported maps verification (BUILD-PLAN M7.4, DESIGN 11)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aft.library.map_evidence import (
    Claim,
    check_claim,
    parse_claims_from_json,
    parse_claims_from_markdown,
    verify_map_files,
)


@pytest.fixture
def collection_tree(tmp_path: Path) -> Path:
    root = tmp_path / "collection"
    root.mkdir()

    # Markdown document
    (root / "spec.md").write_text(
        "# Specification\n\n"
        "## General Overview\n"
        "This is the authoritative protocol specification for devices.\n\n"
        "## Signal Levels\n"
        "Nominal voltage is 12V DC.\n",
        encoding="utf-8",
    )

    # JSON document
    (root / "defs.json").write_text(
        json.dumps(
            {
                "DGN_100": {"name": "Status", "rate": "100ms", "desc": "Parameters"},
                "config": {"timeout": 5000},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Index map in JSON
    (root / "index.json").write_text(
        json.dumps(
            {
                "documents": [
                    {
                        "path": "spec.md",
                        "headings": [
                            {
                                "title": "General Overview",
                                "quote": "authoritative protocol specification",
                            },
                            {"title": "Signal Levels", "quote": "Nominal voltage is 12V DC."},
                        ],
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Index map in Markdown
    (root / "README.md").write_text(
        "# Collection Index\n\n"
        "| Document | Link |\n"
        "|---|---|\n"
        "| Specification details | [Spec](spec.md#General Overview) |\n"
        "| Parameters | [Defs](defs.json) |\n"
        "| Nonexistent file | [Missing](missing.pdf) |\n",
        encoding="utf-8",
    )

    return root


def test_claim_passes_when_path_heading_and_quote_match(collection_tree: Path) -> None:
    claim = Claim(
        target_path="spec.md",
        heading="General Overview",
        quote="authoritative protocol specification",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is True
    assert res.reason == ""


def test_claim_fails_when_path_missing(collection_tree: Path) -> None:
    claim = Claim(
        target_path="nonexistent.md",
        heading="Overview",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "does not exist in collection" in res.reason


def test_claim_fails_when_heading_missing(collection_tree: Path) -> None:
    claim = Claim(
        target_path="spec.md",
        heading="Fabricated Section",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "heading 'Fabricated Section' not found" in res.reason


def test_claim_passes_with_json_key(collection_tree: Path) -> None:
    claim = Claim(
        target_path="defs.json",
        key="DGN_100",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is True

    # Nested key check
    claim_nested = Claim(
        target_path="defs.json",
        key="config.timeout",
    )
    res_nested = check_claim(claim_nested, collection_tree)
    assert res_nested.passed is True


def test_claim_fails_when_json_key_missing(collection_tree: Path) -> None:
    claim = Claim(
        target_path="defs.json",
        key="NONEXISTENT_KEY",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "key 'NONEXISTENT_KEY' not found" in res.reason


def test_claim_fails_when_quote_missing(collection_tree: Path) -> None:
    claim = Claim(
        target_path="spec.md",
        quote="this quoted text does not exist anywhere in spec.md",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "quoted description does not appear" in res.reason


def test_parse_claims_from_json(collection_tree: Path) -> None:
    claims = parse_claims_from_json(collection_tree / "index.json")
    assert len(claims) == 2
    assert claims[0].target_path == "spec.md"
    assert claims[0].heading == "General Overview"
    assert claims[0].quote == "authoritative protocol specification"


def test_parse_claims_from_markdown(collection_tree: Path) -> None:
    claims = parse_claims_from_markdown(collection_tree / "README.md")
    assert len(claims) == 3
    targets = [c.target_path for c in claims]
    assert "spec.md" in targets
    assert "defs.json" in targets
    assert "missing.pdf" in targets


def test_verify_map_files(collection_tree: Path) -> None:
    summary = verify_map_files(
        [collection_tree / "index.json", collection_tree / "README.md"],
        collection_tree,
    )
    assert summary.total == 5
    # 2 from index.json pass, 2 from README pass, 1 from README fails (missing.pdf)
    assert len(summary.verified) == 4
    assert len(summary.rejected) == 1
    assert summary.rejected[0].claim.target_path == "missing.pdf"
    assert "does not exist" in summary.rejected[0].reason


def test_cli_library_check_map_all_pass(
    home: Path, collection_tree: Path, tmp_path: Path, capsys
) -> None:
    import yaml

    from aft import cli

    m_data = {
        "name": "col",
        "title": "Col",
        "description": "Desc",
        "projects": [],
        "path": str(collection_tree),
        "files": {
            "spec.md": "original",
            "defs.json": "original",
            "index.json": "exclude",
            "README.md": "exclude",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")

    code = cli.main(["library", "check-map", str(m_path), str(collection_tree / "index.json")])
    assert code == 0
    out = capsys.readouterr().out
    assert "✓ [index.json] spec.md#General Overview" in out
    assert "2 verified, 0 rejected" in out


def test_cli_library_check_map_with_rejected(
    home: Path, collection_tree: Path, tmp_path: Path, capsys
) -> None:
    import yaml

    from aft import cli

    m_data = {
        "name": "col",
        "title": "Col",
        "description": "Desc",
        "projects": [],
        "path": str(collection_tree),
        "files": {
            "spec.md": "original",
            "defs.json": "original",
            "index.json": "exclude",
            "README.md": "exclude",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")

    code = cli.main(["library", "check-map", str(m_path), str(collection_tree / "README.md")])
    assert code == 1
    out = capsys.readouterr().out
    assert "✗ [README.md] missing.pdf" in out
    assert "1 rejected" in out


def test_write_import_report_includes_map_evidence_section(
    initialized, collection_tree: Path, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    import yaml

    from aft.library import importing, map_evidence
    from aft.library import manifest as manifest_mod

    m_data = {
        "name": "testcol",
        "title": "Test Col",
        "description": "Collection with index map evidence",
        "projects": [],
        "path": str(collection_tree),
        "files": {
            "spec.md": "original",
            "defs.json": "original",
            "index.json": "exclude",
            "README.md": "exclude",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")
    manifest = manifest_mod.validate(m_path)

    summary = map_evidence.verify_map_files(
        [collection_tree / "index.json", collection_tree / "README.md"],
        collection_tree,
    )
    now = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
    report_rel = importing.write_import_report(
        initialized.vault,
        manifest,
        ["spec.md", "defs.json"],
        ["index.json", "README.md"],
        now,
        map_summary=summary,
    )

    report_text = (initialized.vault / report_rel).read_text(encoding="utf-8")
    assert "### Map evidence" in report_text
    assert "Claims checked: 5" in report_text
    assert "Claims verified: 4" in report_text
    assert "Claims rejected: 1" in report_text
    assert "missing.pdf" in report_text
    assert "does not exist in collection" in report_text


def test_unique_reference_slugs_when_stems_match(initialized, tmp_path: Path) -> None:
    """Documents with the same stem (guide.pdf, guide.txt) get unique slugs."""
    import yaml

    from aft.library import importing

    col_dir = tmp_path / "col"
    col_dir.mkdir()
    (col_dir / "guide.pdf").write_bytes(b"%PDF-1.4 dummy")
    (col_dir / "guide.txt").write_text("Text guide content\n", encoding="utf-8")

    m_data = {
        "name": "col",
        "title": "Colliding Stems",
        "description": "Test collision handling",
        "projects": [],
        "path": str(col_dir),
        "files": {
            "guide.pdf": "original",
            "guide.txt": "original",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")

    importing.import_collection(m_path, config=initialized)
    ref_dir = initialized.vault / "references" / "col"
    # Neither page should overwrite the other
    assert (ref_dir / "guide-pdf.md").is_file()
    assert (ref_dir / "guide-txt.md").is_file()

    col_md = (ref_dir / "collection.md").read_text(encoding="utf-8")
    assert "[[guide-pdf|guide.pdf]]" in col_md
    assert "[[guide-txt|guide.txt]]" in col_md


def test_library_indexing_repeated_headings(initialized, tmp_path: Path) -> None:
    """Markdown file with repeated headings creates distinct row_keys with occurrence counts."""
    import sqlite3

    import yaml

    from aft import index
    from aft.library import importing

    col_dir = tmp_path / "col"
    col_dir.mkdir()
    (col_dir / "repeated.md").write_text(
        "# Document\n\n## Notes\nFirst notes section.\n\n## Notes\nSecond notes section.\n",
        encoding="utf-8",
    )

    m_data = {
        "name": "repcol",
        "title": "Repeated Headings",
        "description": "Test repeated headings indexing",
        "projects": [],
        "path": str(col_dir),
        "files": {
            "repeated.md": "original",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")

    importing.import_collection(m_path, config=initialized)

    with index._connect(initialized) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT row_key, heading, body FROM library WHERE path = ? ORDER BY rowid",
            ("library/repcol/repeated.md",),
        )
        rows = cur.fetchall()
        assert len(rows) == 2
        # First occurrence has no number, second has 2
        assert rows[0]["row_key"] == "library/repcol/repeated.md#Notes"
        assert rows[1]["row_key"] == "library/repcol/repeated.md#Notes 2"
        assert "First notes" in rows[0]["body"]
        assert "Second notes" in rows[1]["body"]


def test_claim_fails_when_path_is_absolute(collection_tree: Path) -> None:
    """A claim specifying an absolute path fails containment check (B2)."""
    claim = Claim(
        target_path="/etc/passwd",
        heading="root",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "is absolute" in res.reason


def test_claim_fails_when_path_escapes_collection_root(collection_tree: Path) -> None:
    """A claim specifying a parent path traversal escapes collection root (B2)."""
    claim = Claim(
        target_path="../outside.txt",
        heading="Overview",
    )
    res = check_claim(claim, collection_tree)
    assert res.passed is False
    assert "escapes collection root" in res.reason


def test_claim_verifies_against_extracted_html(tmp_path: Path) -> None:
    """Quotes and headings in HTML are checked against extracted text chunks (B3)."""
    col = tmp_path / "html_col"
    col.mkdir()
    html_file = col / "page.html"
    html_file.write_text(
        "<html><head><title>System Manual</title></head>"
        "<body><h1>Configuration</h1><p>Baud rate is fixed at 250 kbps.</p></body></html>",
        encoding="utf-8",
    )

    # 1. Heading inside HTML heading tag
    claim_h = Claim(
        target_path="page.html",
        heading="Configuration",
    )
    res_h = check_claim(claim_h, col)
    assert res_h.passed is True

    # 2. Quote stripped of HTML tags
    claim_q = Claim(
        target_path="page.html",
        quote="Baud rate is fixed at 250 kbps.",
    )
    res_q = check_claim(claim_q, col)
    assert res_q.passed is True


def test_claim_verifies_against_extracted_chunks_in_memory(tmp_path: Path) -> None:
    """Quotes and headings are checked against extracted_chunks if provided (B3)."""
    col = tmp_path / "pdf_col"
    col.mkdir()
    pdf_file = col / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 binary dummy content")

    extracted = {"doc.pdf": [("page 1", "The quick brown fox jumps over the lazy dog.")]}
    claim = Claim(
        target_path="doc.pdf",
        quote="quick brown fox",
    )
    res = check_claim(claim, col, extracted_chunks=extracted)
    assert res.passed is True


def test_unreadable_map_file_is_reported_not_silent(collection_tree: Path) -> None:
    """An unreadable or malformed map file is reported in rejected claims, not silent."""
    bad_json = collection_tree / "bad_index.json"
    bad_json.write_text("invalid json {{{", encoding="utf-8")

    summary = verify_map_files([bad_json, "nonexistent.json"], collection_tree)
    assert summary.total == 2
    assert len(summary.verified) == 0
    assert len(summary.rejected) == 2
    reasons = [r.reason for r in summary.rejected]
    assert any("cannot read map file" in r for r in reasons)
    assert any("does not exist" in r for r in reasons)


def test_table_row_with_no_link_is_reported_not_silent(tmp_path: Path) -> None:
    """A markdown table row without a markdown link is reported, not silent."""
    col = tmp_path / "table_col"
    col.mkdir()
    (col / "doc.txt").write_text("Valid document\n", encoding="utf-8")
    readme = col / "README.md"
    readme.write_text(
        "# Index\n\n"
        "| File | Description |\n"
        "|---|---|\n"
        "| [doc.txt](doc.txt) | Valid document |\n"
        "| unlinked.txt | Missing link in row |\n",
        encoding="utf-8",
    )
    claims = parse_claims_from_markdown(readme)
    assert len(claims) == 2
    assert claims[0].target_path == "doc.txt"
    assert claims[1].target_path == "<no link>"
    assert claims[1].line == 6

    summary = verify_map_files([readme], col)
    assert summary.total == 2
    assert len(summary.verified) == 1
    assert len(summary.rejected) == 1
    assert summary.rejected[0].claim.target_path == "<no link>"
    assert "table row has no markdown link" in summary.rejected[0].reason


def test_cli_library_check_map_default_exclude_files(
    home: Path, collection_tree: Path, tmp_path: Path, capsys
) -> None:
    """cli check-map without map_files defaults to exclude-role map files with no AttributeError."""
    import yaml

    from aft import cli

    m_data = {
        "name": "col",
        "title": "Col",
        "description": "Desc",
        "projects": [],
        "path": str(collection_tree),
        "files": {
            "spec.md": "original",
            "defs.json": "original",
            "index.json": "exclude",
            "README.md": "exclude",
        },
    }
    m_path = tmp_path / "manifest.yaml"
    m_path.write_text(yaml.dump(m_data), encoding="utf-8")

    code = cli.main(["library", "check-map", str(m_path)])
    assert code == 1  # 1 because README.md has missing.pdf
    out = capsys.readouterr().out
    assert "✓ [index.json] spec.md#General Overview" in out
    assert "✗ [README.md] missing.pdf" in out
