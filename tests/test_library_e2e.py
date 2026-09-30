"""End-to-end integration tests for library lifecycle (F19, DESIGN 11).

Tests the complete flow for a small collection:
1. Multi-page PDF generated dynamically in pure Python, markdown notes, AGENTS.md,
   and an index.json map holding one true claim and one false claim.
2. Survey, manifest with authority: authoritative, and import.
3. Describe with one true claim and one false claim.
4. Propose with facts from PDF, notes, and AGENTS.md.
5. Dream run turning waiting digests into curated memory.
6. Verification of:
   - Rejected and reported false map claim and false description.
   - PDF fact is [observed] and active.
   - Notes and AGENTS.md facts are candidates.
   - Nothing is [stated].
   - AGENTS.md is not in the vault.
   - Search for words on page 2 of the PDF returns that document and page.
   - Re-import with nothing changed modifies nothing.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from aft import cli, lint, page, search, vault
from aft.config import Config
from aft.dream import rules, schedule
from aft.dream.decide import Proposal
from aft.gitutil import git
from aft.library import describe, importing, propose, survey
from aft.library import manifest as manifest_mod
from aft.registry import Registry

pytestmark = pytest.mark.skipif(
    shutil.which("pdftotext") is None,
    reason="pdftotext is not installed",
)


def _generate_minimal_pdf(pages: list[str]) -> bytes:
    """Generate a minimal valid multi-page PDF 1.4 byte sequence without external deps.

    pdftotext extracts each page separated by form-feed (\x0c). Standard Type1 Helvetica
    is used so standard PDF viewers and pdftotext render/extract text faithfully.
    """
    lines: list[bytes] = [b"%PDF-1.4\n"]
    offsets: list[int] = []

    def add_obj(obj_str: str) -> None:
        offsets.append(sum(len(x) for x in lines))
        lines.append(obj_str.encode("latin1"))

    add_obj("1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    num_pages = len(pages)
    font_id = 3 + num_pages
    page_ids = [3 + i for i in range(num_pages)]
    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
    add_obj(f"2 0 obj\n<< /Type /Pages /Kids [{kids_str}] /Count {num_pages} >>\nendobj\n")
    content_ids = [font_id + 1 + i for i in range(num_pages)]

    for i, pid in enumerate(page_ids):
        cid = content_ids[i]
        add_obj(
            f"{pid} 0 obj\n"
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {cid} 0 R >>\nendobj\n"
        )

    add_obj(f"{font_id} 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    for i, cid in enumerate(content_ids):
        txt = pages[i]
        stream_content = f"BT /F1 12 Tf 72 700 Td ({txt}) Tj ET\n"
        stream_bytes = stream_content.encode("latin1")
        add_obj(
            f"{cid} 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n"
            f"{stream_content}endstream\nendobj\n"
        )

    startxref = sum(len(x) for x in lines)
    total_objs = len(offsets) + 1
    xref = [f"xref\n0 {total_objs}\n0000000000 65535 f \n"]
    for off in offsets:
        xref.append(f"{off:010d} 00000 n \n")
    xref.append(f"trailer\n<< /Size {total_objs} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n")
    lines.append("".join(xref).encode("latin1"))
    return b"".join(lines)


@pytest.fixture
def library_e2e_collection(tmp_path: Path, initialized: Config) -> tuple[Path, Path]:
    """Build a small collection in a temp directory and register its project.

    The collection holds:
    - manual.pdf: a real 2-page PDF
    - notes.md: markdown notes file
    - AGENTS.md: instruction file (role: exclude)
    - index.json: map holding one true claim and one false claim
    """
    col_dir = tmp_path / "widget_collection"
    col_dir.mkdir(parents=True)

    # 1. Real 2-page PDF
    pdf_bytes = _generate_minimal_pdf(
        [
            "Widget Architecture Overview and modular component layout.",
            "Telemetry sensor calibration required before flight operations.",
        ]
    )
    (col_dir / "manual.pdf").write_bytes(pdf_bytes)

    # 2. Markdown notes file
    (col_dir / "notes.md").write_text(
        "# Engineering Notes\n\n## Voltage\nAlways verify battery voltage before start.\n",
        encoding="utf-8",
    )

    # 3. Instruction file to exclude
    (col_dir / "AGENTS.md").write_text(
        "# Agent Instructions\n\nDo not commit secrets to the repository.\n",
        encoding="utf-8",
    )

    # 4. Map file with 1 true claim and 1 false claim
    (col_dir / "index.json").write_text(
        json.dumps(
            [
                {
                    "path": "notes.md",
                    "heading": "Voltage",
                    "quote": "Always verify battery voltage before start.",
                },
                {
                    "path": "notes.md",
                    "heading": "Voltage",
                    "quote": "This false claim was never in notes.",
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    # Register project in vault registry
    with vault.locked(initialized):
        reg = Registry.load(initialized.registry_path)
        reg.add("widget", "Widget Project")
        reg.save()
    vault.commit(initialized.vault, ["registry.yaml"], "aft: register widget")

    manifest_data = {
        "name": "widget-docs",
        "title": "Widget Documentation",
        "description": "Comprehensive widget system documentation and notes",
        "authority": "authoritative",
        "projects": ["widget"],
        "path": str(col_dir),
        "files": {
            "manual.pdf": "original",
            "notes.md": "notes",
            "AGENTS.md": "exclude",
            "index.json": "exclude",
        },
    }
    manifest_file = tmp_path / "manifest.yaml"
    manifest_file.write_text(yaml.safe_dump(manifest_data, sort_keys=False), encoding="utf-8")

    return col_dir, manifest_file


def test_library_end_to_end_full_flow(
    initialized: Config, library_e2e_collection: tuple[Path, Path], tmp_path: Path
) -> None:
    """Test the complete library lifecycle from survey to dreaming and search."""
    col_dir, manifest_file = library_e2e_collection

    # -------------------------------------------------------------------------
    # 1. Survey
    # -------------------------------------------------------------------------
    survey_rep = survey.survey(col_dir)
    assert len(survey_rep.files) == 4
    assert "AGENTS.md" in survey_rep.instruction_files
    file_map = {f.path: f for f in survey_rep.files}
    assert file_map["AGENTS.md"].is_instruction
    assert file_map["index.json"].is_tooling

    # -------------------------------------------------------------------------
    # 2. Manifest with authority: authoritative, and import
    # -------------------------------------------------------------------------
    manifest_obj = manifest_mod.validate(manifest_file)
    assert manifest_obj.authority == "authoritative"
    assert manifest_obj.name == "widget-docs"

    import_res = importing.import_collection(manifest_file, config=initialized)
    assert import_res.committed is True
    assert import_res.files_imported == 2
    assert import_res.files_excluded == 2
    assert import_res.authority == "authoritative"

    # Assert: the false map claim is rejected and reported
    assert import_res.map_summary is not None
    assert import_res.map_summary.total == 2
    assert len(import_res.map_summary.verified) == 1
    assert len(import_res.map_summary.rejected) == 1
    rejected_map_claim = import_res.map_summary.rejected[0]
    assert rejected_map_claim.claim.target_path == "notes.md"
    assert rejected_map_claim.claim.quote == "This false claim was never in notes."
    assert "quoted description does not appear" in rejected_map_claim.reason

    import_report = (initialized.vault / import_res.report_path).read_text(encoding="utf-8")
    assert "### Map evidence" in import_report
    assert "Claims checked: 2" in import_report
    assert "Claims verified: 1" in import_report
    assert "Claims rejected: 1" in import_report
    assert "quoted description does not appear in 'notes.md'" in import_report
    assert "`notes.md` from `index.json`" in import_report

    # Assert: AGENTS.md is not in the vault
    assert not (initialized.vault / "library" / "widget-docs" / "AGENTS.md").exists()
    assert not (initialized.vault / "references" / "widget-docs" / "agents.md").exists()
    assert not (initialized.vault / "AGENTS.md").exists()
    tracked_files = git(["ls-files"], cwd=initialized.vault).stdout.splitlines()
    assert not any("AGENTS.md" in f for f in tracked_files)

    # -------------------------------------------------------------------------
    # 3. Describe with one true claim and one false claim
    # -------------------------------------------------------------------------
    desc_data = [
        {
            "file": "manual.pdf",
            "what": {
                "claim": "Widget system architecture and modular component layout.",
                "quote": "Widget Architecture Overview and modular component layout.",
            },
            "version": {
                "claim": "Fabricated revision 99.0.",
                "quote": "This fabricated quote does not exist anywhere in manual.pdf.",
            },
        }
    ]
    desc_file = tmp_path / "descriptions.yaml"
    desc_file.write_text(yaml.safe_dump(desc_data, sort_keys=False), encoding="utf-8")

    desc_res = describe.describe_collection(manifest_file, desc_file, config=initialized)

    # Assert: the false description is rejected and reported
    assert len(desc_res.verified_claims) == 1
    assert desc_res.verified_claims[0][1].name == "what"

    rejected_claims_by_name = {c.name: (c, reason) for _, c, reason in desc_res.rejected_claims}
    assert "version" in rejected_claims_by_name
    version_reason = rejected_claims_by_name["version"][1]
    assert "quote for 'version' not found in extracted text" in version_reason

    assert desc_res.report_path is not None
    desc_report = (initialized.vault / desc_res.report_path).read_text(encoding="utf-8")
    assert "### Rejected descriptions" in desc_report
    assert "`manual.pdf` (version): quote for 'version' not found in extracted text" in desc_report

    # Verified claim lands under ## Description on the reference page; false claim omitted
    ref_manual = initialized.vault / "references" / "widget-docs" / "manual.md"
    assert ref_manual.is_file()
    ref_text = ref_manual.read_text(encoding="utf-8")
    assert "## Description" in ref_text
    assert "- What: Widget system architecture and modular component layout." in ref_text
    assert "Fabricated revision 99.0" not in ref_text

    # -------------------------------------------------------------------------
    # 4. Propose with a fact from PDF, notes, and AGENTS.md
    # -------------------------------------------------------------------------
    proposals = [
        {
            "statement": "Widget architecture is based on modular components.",
            "quote": "Widget Architecture Overview and modular component layout.",
            "file": "manual.pdf",
            "kind": "fact",
        },
        {
            "statement": "Always verify battery voltage before start.",
            "quote": "Always verify battery voltage before start.",
            "file": "notes.md",
            "kind": "lesson",
        },
        {
            "statement": "Do not commit secrets to the repository.",
            "quote": "Do not commit secrets to the repository.",
            "file": "AGENTS.md",
            "kind": "fact",
        },
    ]

    prop_res = propose.propose_facts(manifest_file, proposals, config=initialized)
    assert len(prop_res.verified_facts) == 3
    assert len(prop_res.digests_written) == 1

    vf_by_file = {vf.fact.file: vf.fact for vf in prop_res.verified_facts}
    assert vf_by_file["manual.pdf"].source == "observed"
    assert vf_by_file["notes.md"].source == "inferred"
    assert vf_by_file["AGENTS.md"].source == "inferred"

    digest_file = initialized.vault / prop_res.digests_written[0]
    digest_text = digest_file.read_text(encoding="utf-8")
    assert "[observed]" in digest_text
    assert "[inferred]" in digest_text
    assert "[stated]" not in digest_text

    # -------------------------------------------------------------------------
    # 5. A dream run
    # -------------------------------------------------------------------------
    def fake_decide(config, new_fact, quote, slug):
        return Proposal(
            new_fact=new_fact,
            lines=(),
            decision=rules.Decision(action="add"),
            target=None,
        )

    with patch("aft.dream.decide.decide", side_effect=fake_decide):
        dream_run = schedule.run(initialized)

    assert "widget" in dream_run.ran
    assert dream_run.failures == {}

    # -------------------------------------------------------------------------
    # 6. Assertions on Curated Memory
    # -------------------------------------------------------------------------
    overview_path = initialized.vault / "projects" / "widget" / "overview.md"
    assert overview_path.is_file()
    ov_text = overview_path.read_text(encoding="utf-8")
    parsed_ov = page.parse(ov_text, overview_path)

    # Assert: the PDF fact is [observed] and active
    pdf_fact_matches = [
        f
        for f in parsed_ov.facts
        if f.statement == "Widget architecture is based on modular components."
    ]
    assert len(pdf_fact_matches) == 1
    pdf_fact = pdf_fact_matches[0]
    assert pdf_fact.source == "observed"
    assert pdf_fact.kind == "fact"  # Under ## Facts (active memory)
    assert "- Widget architecture is based on modular components. [observed]" in ov_text

    # Assert: the notes and AGENTS.md facts are candidates
    notes_fact_matches = [
        f for f in parsed_ov.facts if f.statement == "Always verify battery voltage before start."
    ]
    assert len(notes_fact_matches) == 1
    notes_fact = notes_fact_matches[0]
    assert notes_fact.source == "inferred"
    assert notes_fact.kind is None  # Under ## Candidates (not yet active)

    agents_fact_matches = [
        f for f in parsed_ov.facts if f.statement == "Do not commit secrets to the repository."
    ]
    assert len(agents_fact_matches) == 1
    agents_fact = agents_fact_matches[0]
    assert agents_fact.source == "inferred"
    assert agents_fact.kind is None  # Under ## Candidates (not yet active)

    assert "## Candidates" in ov_text
    assert "Always verify battery voltage before start. [inferred]" in ov_text
    assert "Do not commit secrets to the repository. [inferred]" in ov_text

    # Assert: nothing is [stated]
    assert "[stated]" not in ov_text
    assert all(f.source != "stated" for f in parsed_ov.facts)
    assert "[stated]" not in digest_text

    # -------------------------------------------------------------------------
    # 7. Assert: aft search for words on page 2 of the PDF returns that document and page
    # -------------------------------------------------------------------------
    search_hits = search.search(initialized, "telemetry", project="widget")
    assert len(search_hits) >= 1
    pdf_hits = [h for h in search_hits if "manual.pdf" in h.path]
    assert len(pdf_hits) == 1
    hit = pdf_hits[0]
    assert hit.path == "library/widget-docs/manual.pdf"
    assert hit.heading.lower() == "page 2"

    # Also test via search CLI
    capsys_code = cli.main(["search", "telemetry", "--project", "widget"])
    assert capsys_code == 0

    # -------------------------------------------------------------------------
    # 8. Assert: a re-import with nothing changed modifies nothing
    # -------------------------------------------------------------------------
    manual_before = ref_manual.read_text(encoding="utf-8")
    ref_notes = initialized.vault / "references" / "widget-docs" / "notes.md"
    notes_before = ref_notes.read_text(encoding="utf-8")
    ref_col = initialized.vault / "references" / "widget-docs" / "collection.md"
    col_before = ref_col.read_text(encoding="utf-8")

    reimp_res = importing.import_collection(manifest_file, config=initialized)
    assert reimp_res.retired == ()
    assert reimp_res.descriptions_dropped == ()

    assert ref_manual.read_text(encoding="utf-8") == manual_before
    assert ref_notes.read_text(encoding="utf-8") == notes_before
    assert ref_col.read_text(encoding="utf-8") == col_before

    diff = git(
        ["diff", "HEAD~1..HEAD", "--", "references/widget-docs", "library/widget-docs"],
        cwd=initialized.vault,
    ).stdout.strip()
    assert diff == ""

    # Whole vault remains clean and lint-free
    assert lint.lint_vault(initialized.vault) == []
