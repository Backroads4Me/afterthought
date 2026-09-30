"""Tests for `aft library survey` (milestone M7.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aft import cli
from aft.library import survey


def hash_tree(root: Path) -> dict[str, str]:
    """Compute a recursive map of relative path to sha256 for all files under root."""
    result: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            result[rel] = survey.file_sha256(p)
    return result


@pytest.fixture
def sample_collection(tmp_path: Path) -> Path:
    """Create a sample library collection directory with various file kinds."""
    root = tmp_path / "rvc_sample"
    root.mkdir()

    # Documents and derived candidates
    (root / "spec.pdf").write_bytes(b"%PDF-1.4 sample pdf content")
    (root / "spec.md").write_text("# RV-C Specification\nClean markdown version.")
    (root / "notes.txt").write_text("Working notes on CAN bus.")

    # Duplicates: two files with identical contents
    dup1 = root / "copy_a.txt"
    dup2 = root / "sub" / "copy_b.txt"
    dup2.parent.mkdir(parents=True)
    dup1.write_text("identical duplicate content")
    dup2.write_text("identical duplicate content")

    # Instruction file
    (root / "AGENTS.md").write_text("# Agent instructions\nDo not import this.")

    # Tooling
    (root / ".gitignore").write_text("*.tmp\n")
    (root / "regenerate.py").write_text("print('regenerating...')\n")
    (root / "manifest.json").write_text('{"chunks": []}\n')

    tools_dir = root / "tools"
    tools_dir.mkdir()
    (tools_dir / "helper.py").write_text("# tool helper\n")

    # Directories that must be skipped by survey traversal
    git_dir = root / ".git" / "objects" / "ab"
    git_dir.mkdir(parents=True)
    (git_dir / "123456").write_bytes(b"git internal object")

    venv_dir = root / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "module.py").write_text("# venv library code")

    cache_dir = root / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "cache.pyc").write_bytes(b"compiled bytecode")

    return root


def test_survey_is_read_only(sample_collection: Path):
    """Surveying a directory changes nothing: hash tree is identical before and after."""
    before_hashes = hash_tree(sample_collection)

    # Run Python survey function
    report = survey.survey(sample_collection)
    assert len(report.files) > 0

    mid_hashes = hash_tree(sample_collection)
    assert mid_hashes == before_hashes

    # Run CLI survey command
    exit_code = cli.main(["library", "survey", str(sample_collection)])
    assert exit_code == 0

    after_hashes = hash_tree(sample_collection)
    assert after_hashes == before_hashes


def test_survey_skips_vcs_and_cache_directories(sample_collection: Path):
    """VCS directories (.git) and caches (.venv, __pycache__) are not surveyed."""
    report = survey.survey(sample_collection)
    surveyed_paths = {f.path for f in report.files}

    assert not any(p == ".git" or p.startswith(".git/") for p in surveyed_paths)
    assert not any(p == ".venv" or p.startswith(".venv/") for p in surveyed_paths)
    assert not any(p == "__pycache__" or p.startswith("__pycache__/") for p in surveyed_paths)


def test_survey_classifies_file_types(sample_collection: Path):
    """File types are classified accurately by format."""
    report = survey.survey(sample_collection)
    types = {f.path: f.file_type for f in report.files}

    assert types["spec.pdf"] == "PDF"
    assert types["spec.md"] == "Markdown"
    assert types["notes.txt"] == "Text"
    assert types["manifest.json"] == "JSON"
    assert types["regenerate.py"] == "Code"


def test_survey_detects_duplicates(sample_collection: Path):
    """Files with identical contents are detected as duplicates."""
    report = survey.survey(sample_collection)

    expected_group = {"copy_a.txt", "sub/copy_b.txt"}
    matching_groups = [set(g) for g in report.duplicates if expected_group.issubset(set(g))]
    assert len(matching_groups) == 1


def test_survey_suggests_derived_pairs(sample_collection: Path):
    """Files sharing a stem with original/derived extensions are suggested as pairs."""
    report = survey.survey(sample_collection)

    pairs = {(p.original, p.derived) for p in report.derived_pairs}
    assert ("spec.pdf", "spec.md") in pairs


def test_survey_flags_instruction_and_tooling_files(sample_collection: Path):
    """Instruction files and tooling are flagged for exclusion."""
    report = survey.survey(sample_collection)

    assert "AGENTS.md" in report.instruction_files
    assert ".gitignore" in report.tooling_files
    assert "regenerate.py" in report.tooling_files
    assert "manifest.json" in report.tooling_files
    assert "tools/helper.py" in report.tooling_files

    by_path = {f.path: f for f in report.files}
    assert by_path["AGENTS.md"].is_instruction
    assert not by_path["AGENTS.md"].is_tooling
    assert by_path[".gitignore"].is_tooling
    assert not by_path[".gitignore"].is_instruction
    assert not by_path["spec.pdf"].is_instruction
    assert not by_path["spec.pdf"].is_tooling


def test_survey_format_includes_procedure(sample_collection: Path):
    """Formatted survey text contains file listings and the import procedure."""
    report = survey.survey(sample_collection)
    formatted = survey.format_survey(report)

    assert "Survey of" in formatted
    assert "Files:" in formatted
    assert "spec.pdf" in formatted
    assert "Duplicates:" in formatted
    assert "Suggested derived pairs:" in formatted
    assert "Flagged for exclusion:" in formatted
    assert "Import procedure:" in formatted
    assert "1. Write a manifest:" in formatted
    assert "2. Validate the manifest:" in formatted
    assert "3. Import the collection:" in formatted
    assert "4. Add verified descriptions:" in formatted
    assert "aft library describe" in formatted
    assert "5. Propose facts:" in formatted
    assert "6. Report and commit:" in formatted


def test_cli_survey_json_output(sample_collection: Path, capsys):
    """`aft library survey --json` prints valid JSON containing all report keys."""
    exit_code = cli.main(["library", "survey", str(sample_collection), "--json"])
    assert exit_code == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)

    assert "root" in data
    assert "files" in data
    assert "duplicates" in data
    assert "derived_pairs" in data
    assert "instruction_files" in data
    assert "tooling_files" in data
    assert any(f["path"] == "spec.pdf" for f in data["files"])


def test_survey_nonexistent_path_fails(tmp_path: Path, capsys):
    """Surveying a nonexistent path reports an error and returns code 2."""
    missing = tmp_path / "does_not_exist"
    exit_code = cli.main(["library", "survey", str(missing)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "aft: path does not exist:" in captured.err


def test_survey_file_path_fails(sample_collection: Path, capsys):
    """Surveying a file instead of a directory reports an error and returns code 2."""
    file_path = sample_collection / "spec.pdf"
    exit_code = cli.main(["library", "survey", str(file_path)])
    assert exit_code == 2

    captured = capsys.readouterr()
    assert "aft: path is not a directory:" in captured.err
