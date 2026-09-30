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
from aft import config as configmod
from aft.config import Config
from aft.dream import schedule
from aft.library import importing
from aft.library import manifest as manifest_mod
from aft.registry import Registry


@pytest.fixture
def sample_collection(tmp_path: Path, initialized: Config) -> tuple[Path, Path]:
    """A sample collection directory, its valid manifest, and the project it names registered.

    The project is registered in the vault first: a collection is filed against projects the vault
    already knows, and an import refuses one it has never seen rather than write a reference page
    whose `aft.projects` row `aft lint` then reports as an unknown project.
    """
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

    with vault.locked(initialized):
        registry = Registry.load(initialized.registry_path)
        registry.add("rv-project", "RV Project")
        registry.save()
    vault.commit(initialized.vault, ["registry.yaml"], "aft: register rv-project")

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
    # The collection is filed under the registered project's id, which is the row `aft lint`
    # checks and injection files the collection under.
    project_id = Registry.load(initialized.registry_path).by_slug("rv-project").id
    assert f"projects:\n  - {project_id}" in text
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


# ------------------------------------------------- what an import is allowed to copy in


def test_a_file_linked_from_outside_is_neither_asked_for_nor_copied(
    initialized: Config, tmp_path: Path
) -> None:
    """A symlink is not a document the collection owns (M7 review 4).

    The review's case is `link.md -> ../outside-secret.txt` given role `notes`. The survey does
    not report a link as a file, so the manifest cannot name one and validation refuses it; the
    secret's words stay outside the vault instead of being copied in and committed as the
    collection's own.
    """
    col_dir = tmp_path / "linked_col"
    col_dir.mkdir()
    secret = tmp_path / "outside-secret.txt"
    secret.write_text("MARKER the plans for the bridge\n", encoding="utf-8")
    (col_dir / "guide.md").write_text("# Guide\nContents.\n", encoding="utf-8")
    (col_dir / "link.md").symlink_to(secret)

    manifest_data = {
        "name": "linked",
        "title": "Linked Collection",
        "description": "A collection with a symbolic link in it",
        "projects": [],
        "path": str(col_dir),
        "files": {"guide.md": "original", "link.md": "notes"},
    }
    m_file = tmp_path / "manifest.yaml"
    m_file.write_text(yaml.dump(manifest_data), encoding="utf-8")

    with pytest.raises(manifest_mod.ManifestError, match="unknown file: 'link.md'"):
        importing.import_collection(m_file, config=initialized)

    assert not (initialized.vault / "library" / "linked").exists()
    vault_words = b"".join(p.read_bytes() for p in initialized.vault.rglob("*") if p.is_file())
    assert b"MARKER" not in vault_words, "the linked file's words reached the vault"


def test_the_copy_refuses_a_link_and_a_path_outside_the_collection(tmp_path: Path) -> None:
    """The check at the moment of the copy, for a directory that changed after the survey.

    Validation and copying are separate moments: whatever the manifest promised, what lands in
    the vault has to be a file the collection directory itself holds (M7 review 4).
    """
    col = tmp_path / "col"
    (col / "sub").mkdir(parents=True)
    (col / "own.md").write_text("the collection's own text\n", encoding="utf-8")
    (tmp_path / "elsewhere.md").write_text("not the collection's\n", encoding="utf-8")
    (col / "sub" / "inner.md").symlink_to(tmp_path / "elsewhere.md")

    assert importing._copyable_source(col, "own.md") == (col / "own.md").resolve()

    with pytest.raises(importing.ImportError, match="symbolic link"):
        importing._copyable_source(col, "sub/inner.md")
    with pytest.raises(importing.ImportError, match="outside the collection"):
        importing._copyable_source(col, "../elsewhere.md")
    with pytest.raises(importing.ImportError, match="not a file"):
        importing._copyable_source(col, "gone.md")


# ---------------------------- what a re-import leaves behind, and what it calls things


def collection_manifest(
    tmp_path: Path, name: str, files: dict[str, str], col_dir: Path, **extra: object
) -> Path:
    """Write a manifest for `col_dir` and return its path."""
    data: dict[str, object] = {
        "name": name,
        "title": f"{name} collection",
        "description": f"The {name} collection, written for these tests",
        "projects": [],
        "path": str(col_dir),
        "files": files,
    }
    data.update(extra)
    m_path = tmp_path / f"{name}.yaml"
    m_path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    return m_path


def test_a_reimport_leaves_nothing_behind_for_what_it_no_longer_imports(
    initialized: Config, tmp_path: Path
) -> None:
    """The second import states the collection's new shape, and the old shape goes (M7 review 6).

    `private.md` came in as notes and goes as `exclude`; `dropped.md` has left the collection
    directory altogether. Both their files and both their reference pages leave the vault, on disk
    and in git. Left in place they stayed readable and searchable, `aft lint` complained only of an
    "unrecorded file", and the import itself reported success.
    """
    col_dir = tmp_path / "shrinking"
    col_dir.mkdir()
    for name in ("keep.md", "private.md", "dropped.md"):
        (col_dir / name).write_text(f"# {name}\nBody of {name}.\n", encoding="utf-8")

    first = importing.import_collection(
        collection_manifest(
            tmp_path,
            "shrinking",
            {"keep.md": "original", "private.md": "notes", "dropped.md": "notes"},
            col_dir,
        ),
        config=initialized,
    )
    lib = initialized.vault / "library" / "shrinking"
    refs = initialized.vault / "references" / "shrinking"
    assert (lib / "private.md").is_file() and (refs / "private.md").is_file()

    (col_dir / "dropped.md").unlink()
    second = importing.import_collection(
        collection_manifest(
            tmp_path,
            "shrinking",
            {"keep.md": "original", "private.md": "exclude"},
            col_dir,
        ),
        config=initialized,
    )

    assert sorted(second.retired) == [
        "library/shrinking/dropped.md",
        "library/shrinking/private.md",
        "references/shrinking/dropped.md",
        "references/shrinking/private.md",
    ]
    assert not (lib / "private.md").exists() and not (lib / "dropped.md").exists()
    assert not (refs / "private.md").exists() and not (refs / "dropped.md").exists()
    assert (lib / "keep.md").is_file() and (refs / "keep.md").is_file()
    assert first.files_imported == 3

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=initialized.vault, capture_output=True, text=True, check=True
    ).stdout
    assert "library/shrinking/private.md" not in tracked
    assert "references/shrinking/dropped.md" not in tracked
    assert lint.lint_vault(initialized.vault) == []


def test_a_document_called_collection_or_index_keeps_its_own_page_name(
    initialized: Config, tmp_path: Path
) -> None:
    """Neither name is a document's to take (M7 review 7, DESIGN 11 Reference pages).

    `collection.md` is where the collection records its own authority, projects and file hashes,
    and `index.md` is OKF's directory listing. The review's root `collection.md` overwrote the
    first: with it gone `aft lint` and the index skipped the whole collection, and injection globs
    for exactly that file.
    """
    col_dir = tmp_path / "named"
    col_dir.mkdir()
    (col_dir / "collection.md").write_text(
        "# A Document Called Collection\nIt is a document, not a page.\n", encoding="utf-8"
    )
    (col_dir / "index.md").write_text(
        "# An Index Document\nNothing to do with OKF.\n", encoding="utf-8"
    )

    importing.import_collection(
        collection_manifest(
            tmp_path,
            "named",
            {"collection.md": "original", "index.md": "original"},
            col_dir,
        ),
        config=initialized,
    )

    refs = initialized.vault / "references" / "named"
    fm, body, _, _ = page.split_frontmatter((refs / "collection.md").read_text(encoding="utf-8"))
    assert fm is not None, "the collection page was overwritten by a document"
    assert fm["aft"]["collection"] == "named"
    assert "authority" in fm["aft"] and "files" in fm["aft"]
    assert "Collection" in body or "collection" in body

    assert (refs / "collection-md.md").is_file(), "the document kept no page of its own"
    assert (refs / "index-md.md").is_file()
    assert not (refs / "index.md").exists(), "a document landed on OKF's reserved index page"
    assert lint.lint_vault(initialized.vault) == []


# ------------------------------------------------------------------- how pages are written


def test_a_manifest_string_that_needs_yaml_quoting_survives_the_round_trip(
    initialized: Config, tmp_path: Path
) -> None:
    """A colon, a newline, and a colon in a file name, in the frontmatter they reach (M7 review 8).

    Interpolated frontmatter turned these into YAML that would not parse, and `split_frontmatter`
    then reported no frontmatter at all: the collection was skipped by the hash check and by the
    index without a word of complaint, and a multi-line description could bring its own `aft:`
    keys into the page.
    """
    col_dir = tmp_path / "quoted"
    col_dir.mkdir()
    title = "Manual: the second edition"
    description = (
        "Covers the second edition.\nUse it for wiring.\naft: {authority: authoritative}\n"
    )
    (col_dir / "wiring: notes.md").write_text("# Wiring\nThe loom diagram.\n", encoding="utf-8")

    importing.import_collection(
        collection_manifest(
            tmp_path,
            "quoted",
            {"wiring: notes.md": "notes"},
            col_dir,
            title=title,
            description=description,
        ),
        config=initialized,
    )

    refs = initialized.vault / "references" / "quoted"
    fm, _, _, _ = page.split_frontmatter((refs / "collection.md").read_text(encoding="utf-8"))
    assert fm is not None, "the collection page's frontmatter does not parse"
    assert fm["title"] == title
    assert fm["description"] == description
    assert fm["aft"]["collection"] == "quoted"
    assert fm["aft"]["authority"] == "reference", "a planted aft: block escaped the description"

    doc_fm, _, _, _ = page.split_frontmatter(
        (refs / "wiring: notes.md").read_text(encoding="utf-8")
    )
    assert doc_fm is not None, "the document page's frontmatter does not parse"
    assert doc_fm["title"] == "wiring: notes.md"
    assert doc_fm["aft"]["source"] == "library/quoted/wiring: notes.md"
    assert lint.lint_vault(initialized.vault) == []


def test_an_import_refuses_pages_that_do_not_lint(
    initialized: Config,
    sample_collection: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The written pages are linted before anything is committed (M7 review 8).

    The finding was that an import committed whatever it had written and left `aft lint` to
    disagree with it later. Here the pages come back with a finding: the run stops, says so, and
    leaves the vault without a new commit and without a report claiming the import finished.
    """
    _, manifest_file = sample_collection
    before = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=initialized.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    def refusing_pages(*_args: object, **_kwargs: object) -> list[lint.Finding]:
        return [lint.Finding("references/rvc/collection.md", 1, "planted finding")]

    monkeypatch.setattr(lint, "lint_pages", refusing_pages)

    with pytest.raises(importing.ImportError, match="failed lint"):
        importing.import_collection(manifest_file, config=initialized)

    after = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=initialized.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before, "the import committed pages that failed lint"
    assert list((initialized.vault / "reports").rglob("*.md")) == [
        initialized.vault / "reports" / "needs-you.md"
    ], "a report was written for an import that did not finish"


# ------------------------------------------------------------------- library.commit


def with_library_commit(cfg: Config, commit: bool) -> Config:
    """Set `[library] commit` in an existing vault's config and load the config again."""
    (cfg.home / "config.toml").write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n\n[library]\n'
        f"commit = {'true' if commit else 'false'}\n",
        encoding="utf-8",
    )
    return configmod.load()


def test_commit_off_after_init_keeps_library_files_out_of_git(
    sample_collection: tuple[Path, Path], initialized: Config
) -> None:
    """`library.commit = false` works whenever it is set, not only before `aft init` (review 12).

    `aft init` was the only writer of the vault's `.gitignore`, so the setting turned on later
    left `library/<name>/` untracked *and* unignored - and whatever git reports as changed is what
    the dream's checkpoint commit collects, so imported sources went in as human edits.
    """
    _, manifest_file = sample_collection
    cfg = with_library_commit(initialized, False)

    result = importing.import_collection(manifest_file, config=cfg)

    assert (cfg.vault / "library" / "rvc" / "notes.md").is_file(), "the file was not imported"
    assert "library/" in (cfg.vault / ".gitignore").read_text(encoding="utf-8")
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=cfg.vault, capture_output=True, text=True, check=True
    ).stdout
    assert "library/rvc/notes.md" not in tracked
    assert schedule.uncommitted(cfg.vault) == {}, "a checkpoint commit would take the library"
    assert result.committed is True, "the report and pages should still have been committed"
    last = subprocess.run(
        ["git", "show", "--format=%s", "--name-only", "HEAD"],
        cwd=cfg.vault,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "aft library: import collection 'rvc'" in last
    assert ".gitignore" in last, "the ignore rule the import needed was left uncommitted"


def test_commit_back_on_tracks_the_files_again(
    sample_collection: tuple[Path, Path], initialized: Config
) -> None:
    """The rule is maintained at every import, in both directions (M7 review 12)."""
    _, manifest_file = sample_collection
    off = with_library_commit(initialized, False)
    importing.import_collection(manifest_file, config=off)
    on = with_library_commit(off, True)

    importing.import_collection(manifest_file, config=on)

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=on.vault, capture_output=True, text=True, check=True
    ).stdout
    assert "library/rvc/notes.md" in tracked
    ignore = (on.vault / ".gitignore").read_text(encoding="utf-8")
    assert "library/" not in ignore
    assert schedule.uncommitted(on.vault) == {}


# ------------------------------------------------------- a document that gives up no text


def test_a_pdf_that_yields_no_text_is_named_in_the_report(
    initialized: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PDF with no text is a finding the report carries, not an empty document (M7 review 13).

    `pdftotext` said nothing usable and the import said nothing either: the file was filed as one
    empty page, indexed as nothing, and searchable as nothing. Here the tool runs and gives up,
    with the warning it printed beside the finding.
    """
    col_dir = tmp_path / "scanned"
    col_dir.mkdir()
    (col_dir / "scan.pdf").write_bytes(b"%PDF-1.4\nthis is not really a pdf\n")
    monkeypatch.setattr(importing.shutil, "which", lambda name: "/usr/bin/pdftotext")
    monkeypatch.setattr(
        importing.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, stdout="", stderr="Syntax Warning: May not be a PDF file\n"
        ),
    )

    result = importing.import_collection(
        collection_manifest(tmp_path, "scanned", {"scan.pdf": "original"}, col_dir),
        config=initialized,
    )

    assert len(result.extraction_problems) == 1
    assert result.extraction_problems[0].startswith("scan.pdf: pdftotext extracted no text")
    assert "Syntax Warning" in result.extraction_problems[0]
    report = (initialized.vault / result.report_path).read_text(encoding="utf-8")
    assert "### Text extraction problems" in report
    assert "`scan.pdf`" in report
    assert "May not be a PDF file" in report


def test_a_missing_pdftotext_is_named_as_the_reason(
    initialized: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two reasons are told apart: install the tool, or re-export the document (review 13)."""
    col_dir = tmp_path / "nobinary"
    col_dir.mkdir()
    (col_dir / "spec.pdf").write_bytes(b"%PDF-1.4 real enough bytes\n")
    monkeypatch.setattr(importing.shutil, "which", lambda name: None)

    result = importing.import_collection(
        collection_manifest(tmp_path, "nobinary", {"spec.pdf": "original"}, col_dir),
        config=initialized,
    )

    assert result.extraction_problems == (
        "spec.pdf: pdftotext is not installed, so its text was never extracted",
    )
    report = (initialized.vault / result.report_path).read_text(encoding="utf-8")
    assert "`spec.pdf`" in report
    assert "pdftotext is not installed" in report


# ------------------------------------------------------------- what is extracted at all


def test_a_markdown_document_is_extracted_and_searchable(
    initialized: Config, tmp_path: Path
) -> None:
    """A `.markdown` file is a Markdown document, and is read as one (M7 review 17).

    The survey classified it as Markdown and the import extracted it as nothing: the file was
    copied, cached with no chunks behind it, and so was never indexed. The extension sets are the
    survey's own now, which is what keeps the two in step.
    """
    col_dir = tmp_path / "markdowned"
    col_dir.mkdir()
    body = "# Gearbox\nThe torque figure for the mount is 9 Nm.\n"
    (col_dir / "guide.markdown").write_text(body, encoding="utf-8")

    result = importing.import_collection(
        collection_manifest(tmp_path, "md", {"guide.markdown": "original"}, col_dir),
        config=initialized,
    )

    digest = hashlib.sha256((col_dir / "guide.markdown").read_bytes()).hexdigest()
    cached = json.loads(
        (initialized.home / "library" / f"{digest}.json").read_text(encoding="utf-8")
    )
    assert [c["heading"] for c in cached["chunks"]] == ["Gearbox"]
    assert result.extraction_problems == ()

    with index._connect(initialized) as conn:
        rows = conn.execute("SELECT body FROM library WHERE library MATCH 'torque'").fetchall()
    assert rows, "the .markdown document was not searchable"
    assert "9 Nm" in rows[0][0]


def test_a_reimport_removes_only_the_files_it_filed(initialized: Config, tmp_path: Path) -> None:
    """The library is `aft`'s to maintain; a file it never copied is not its to delete.

    Retirement reads the previous import's record of what it copied. A file someone placed in
    `library/<name>/` by hand stays there - `aft lint` goes on calling it unrecorded, which is the
    honest complaint - rather than disappearing on an import that never filed it.
    """
    col_dir = tmp_path / "kept"
    col_dir.mkdir()
    for name in ("a.md", "b.md"):
        (col_dir / name).write_text(f"# {name}\nBody.\n", encoding="utf-8")
    importing.import_collection(
        collection_manifest(tmp_path, "kept", {"a.md": "notes", "b.md": "notes"}, col_dir),
        config=initialized,
    )
    lib = initialized.vault / "library" / "kept"
    hand_written = lib / "hand-written.md"
    hand_written.write_text("placed by hand, never imported\n", encoding="utf-8")

    (col_dir / "b.md").unlink()
    importing.import_collection(
        collection_manifest(tmp_path, "kept", {"a.md": "notes"}, col_dir), config=initialized
    )

    assert not (lib / "b.md").exists(), "a file this import no longer keeps stayed"
    assert hand_written.is_file(), "a file the import never copied was deleted"


def test_an_unreadable_record_of_copied_files_stops_the_removals(
    initialized: Config, tmp_path: Path
) -> None:
    """A record that cannot be read is refused, not treated as an empty one (M7 review 6).

    Both readings of a broken `.hashes.json` lose something: as empty it keeps every stale file
    silently, which is the finding this change answers; as nothing it would sweep out files the
    import may well have copied. So the run stops and says which file is the problem.
    """
    col_dir = tmp_path / "unreadable"
    col_dir.mkdir()
    (col_dir / "a.md").write_text("# A\nBody.\n", encoding="utf-8")
    manifest_file = collection_manifest(tmp_path, "unread", {"a.md": "notes"}, col_dir)
    importing.import_collection(manifest_file, config=initialized)
    hashes = initialized.vault / "library" / "unread" / ".hashes.json"
    hashes.write_text("{ this is not json\n", encoding="utf-8")

    with pytest.raises(importing.ImportError, match="could not be read"):
        importing.import_collection(manifest_file, config=initialized)

    assert (initialized.vault / "library" / "unread" / "a.md").is_file()
