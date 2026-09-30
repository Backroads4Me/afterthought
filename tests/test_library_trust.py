"""What a copy of a document is worth, and what it is not (design 11, F12).

A collection arrives as files, an index, and a manifest from whoever assembled it. The manifest
is the least trustworthy thing in the package: it is an offer, and the same words can be offered
with different paperwork. So the vault remembers what an import actually did - the authority it
recorded, the roles it kept, the bytes it copied - and both `aft library propose` and the dream
read that memory instead of the paper they were handed.

A quote is then looked for in the text an index and a reference page carry, not in raw bytes:
words inside a comment or a script tag are in the file and are not something the document says.
And the copy is weighed against the hash the import recorded before any of its words count,
because a sentence written into the vault copy afterwards is not the document's either.

The tests import collections the way the command does. Where a test wants a collection imported
differently it imports it that way, since a manifest edited afterwards is the bypass at issue.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aft.config import Config
from aft.dream import apply, rules
from aft.library import importing, propose
from aft.library.survey import file_sha256
from aft.registry import Registry

GROUNDING = "Always connect ground before positive terminal.\n"
SPEC_LINE = "Authoritative RV-C specification 2023.\n"


@pytest.fixture
def vault(initialized: Config) -> Config:
    """An initialized vault with one project a collection may name."""
    cfg = initialized
    reg = Registry.load(cfg.vault / "registry.yaml")
    if not reg.by_slug("rv-project"):
        reg.add("rv-project", "RV Project")
    reg.save()
    return cfg


def import_collection(
    cfg: Config, source: Path, *, name: str, roles: dict, authority: str = "reference"
) -> Path:
    """Import `source` under `name` through the real import, and return its manifest file."""
    manifest_file = cfg.home.parent / f"{name}.yaml"
    manifest_file.write_text(
        yaml.safe_dump(
            {
                "name": name,
                "title": f"{name} collection",
                "description": "A collection imported for the trust tests.",
                "projects": ["rv-project"],
                "path": str(source),
                "authority": authority,
                "files": roles,
            }
        ),
        encoding="utf-8",
    )
    importing.import_collection(manifest_file, config=cfg)
    return manifest_file


def proposed(
    manifest_file: Path, cfg: Config, *, quote: str, file: str, **changes
) -> propose.ProposeResult:
    """One proposed fact through `aft library propose`, with the fields a test cares about."""
    fact = {"statement": "A claim the document is meant to carry.", "quote": quote, "file": file}
    fact.update(changes)
    return propose.propose_facts(manifest_file, [fact], config=cfg)


def test_a_manifest_cannot_rename_itself_authoritative(vault: Config, tmp_path: Path) -> None:
    """Authority is what an import recorded, not what the manifest handed over says (F12.2)."""
    cfg = vault
    source = tmp_path / "docs"
    source.mkdir()
    (source / "rvc_spec.txt").write_text(SPEC_LINE, encoding="utf-8")
    imported = import_collection(cfg, source, name="rvcref", roles={"rvc_spec.txt": "original"})

    handed = tmp_path / "handed.yaml"
    paperwork = yaml.safe_load(imported.read_text(encoding="utf-8"))
    paperwork["authority"] = "authoritative"
    handed.write_text(yaml.safe_dump(paperwork), encoding="utf-8")

    res = proposed(handed, cfg, quote="Authoritative RV-C specification 2023.", file="rvc_spec.txt")

    assert res.verified_facts == []
    assert "the manifest says this collection is 'authoritative'" in res.dropped_facts[0].reason

    # The same fact through the manifest the import read stands, and stays a reference's word.
    honest = proposed(
        imported, cfg, quote="Authoritative RV-C specification 2023.", file="rvc_spec.txt"
    )
    assert len(honest.verified_facts) == 1
    assert honest.verified_facts[0].fact.source == "inferred"


def test_a_manifest_cannot_relabel_a_notes_file_as_the_document(
    vault: Config, tmp_path: Path
) -> None:
    """The role an import recorded is the role propose works from (F12.2).

    Whether a file is a document or somebody's notes decides whether its fact is the world's word
    or an inference, and `original` is the role that earns [observed] in an authoritative
    collection. Rewriting the manifest is how that would be claimed without the import.
    """
    cfg = vault
    source = tmp_path / "notedoc"
    source.mkdir()
    (source / "notes.md").write_text(GROUNDING, encoding="utf-8")
    imported = import_collection(
        cfg, source, name="notedoc", roles={"notes.md": "notes"}, authority="authoritative"
    )

    paperwork = yaml.safe_load(imported.read_text(encoding="utf-8"))
    paperwork["files"] = {"notes.md": "original"}
    handed = tmp_path / "handed-original.yaml"
    handed.write_text(yaml.safe_dump(paperwork), encoding="utf-8")

    res = proposed(
        handed,
        cfg,
        statement="Ground goes before positive.",
        quote="Always connect ground before positive terminal.",
        file="notes.md",
    )
    assert res.verified_facts == []
    assert "the import record says 'notes'" in res.dropped_facts[0].reason

    honest = proposed(
        imported,
        cfg,
        statement="Ground goes before positive.",
        quote="Always connect ground before positive terminal.",
        file="notes.md",
    )
    assert len(honest.verified_facts) == 1
    assert honest.verified_facts[0].fact.source == "inferred"


def test_an_unregistered_project_writes_nothing_outside_the_vault(
    vault: Config, tmp_path: Path
) -> None:
    """A project a manifest targets must be a project the vault has (F12.3).

    A slug is turned into a directory, so an unregistered one is how a collection's paperwork
    writes beside the vault. Nothing is written before the names are checked, so the command
    refuses and the directory around the vault is as it was.
    """
    cfg = vault
    source = tmp_path / "notesonly"
    source.mkdir()
    (source / "notes.md").write_text(GROUNDING, encoding="utf-8")
    imported = import_collection(cfg, source, name="notesref", roles={"notes.md": "notes"})

    around_vault = sorted(p.name for p in cfg.home.parent.iterdir())

    with pytest.raises(propose.ProposeError, match="no such project in the vault"):
        proposed(
            imported,
            cfg,
            quote="Always connect ground before positive terminal.",
            file="notes.md",
            project="../../outside",
        )

    assert sorted(p.name for p in cfg.home.parent.iterdir()) == around_vault
    assert not (cfg.home.parent / "outside").exists()


def test_the_dream_reads_an_imported_quote_as_no_session_speaking(vault: Config) -> None:
    """An imported quote verifies a fact and stands for no session (F12.1).

    The origin is what R3 counts, so this is the line where a collection stops being able to
    promote a fact on its own: three files repeating a sentence remain zero sessions.
    """
    cfg = vault
    source = cfg.home.parent / "origin-check"
    source.mkdir()
    (source / "notes.md").write_text(GROUNDING, encoding="utf-8")
    import_collection(cfg, source, name="originref", roles={"notes.md": "notes"})

    verified, origin, _date, yours = apply._lookup_evidence(
        cfg,
        "aft://library/originref/notes.md",
        "Always connect ground before positive terminal.",
    )

    assert verified is True
    assert origin == rules.LIBRARY
    assert yours is False


def test_words_that_live_only_in_an_html_comment_verify_nothing(vault: Config) -> None:
    """A comment is in the file and is not something the document says (F12.10)."""
    cfg = vault
    source = cfg.home.parent / "htmldoc"
    source.mkdir()
    (source / "spec.html").write_text(
        "<html><body>\n"
        "<p>The lunar landing protocol runs at 250 kbps.</p>\n"
        "<!-- The controller was bought at a garage sale. -->\n"
        "<script>var telemetryRate = '250 kbps';</script>\n"
        "</body></html>\n",
        encoding="utf-8",
    )
    imported = import_collection(cfg, source, name="htmlspec", roles={"spec.html": "original"})

    prose = proposed(
        imported,
        cfg,
        quote="The lunar landing protocol runs at 250 kbps.",
        file="spec.html",
    )
    assert len(prose.verified_facts) == 1

    hidden = proposed(
        imported,
        cfg,
        statement="The controller was bought at a garage sale.",
        quote="The controller was bought at a garage sale.",
        file="spec.html",
    )
    assert hidden.verified_facts == []
    assert "not found in 'spec.html'" in hidden.dropped_facts[0].reason

    coded = proposed(
        imported,
        cfg,
        statement="Telemetry runs at 250 kbps.",
        quote="var telemetryRate = '250 kbps';",
        file="spec.html",
    )
    assert coded.verified_facts == []


def test_a_sentence_written_into_the_vault_copy_carries_no_fact(vault: Config) -> None:
    """The copy in the vault is worth its recorded hash, and a quote is looked for in it only
    once it still matches what the import copied (F12.11)."""
    cfg = vault
    source = cfg.home.parent / "specimen"
    source.mkdir()
    (source / "rvc_spec.txt").write_text(SPEC_LINE, encoding="utf-8")
    imported = import_collection(
        cfg, source, name="specimen", roles={"rvc_spec.txt": "original"}, authority="authoritative"
    )

    copy = cfg.vault / "library" / "specimen" / "rvc_spec.txt"
    copy.chmod(0o644)  # a retained copy is read-only until its owner changes it
    copy.write_text(SPEC_LINE + "The lunar landing protocol runs at 250 kbps.\n", encoding="utf-8")

    res = proposed(
        imported,
        cfg,
        statement="The lunar landing protocol runs at 250 kbps.",
        quote="The lunar landing protocol runs at 250 kbps.",
        file="rvc_spec.txt",
    )
    assert res.verified_facts == []
    assert "no longer matches the hash the import recorded" in res.dropped_facts[0].reason

    verified, origin, _date, _yours = apply._lookup_evidence(
        cfg,
        "aft://library/specimen/rvc_spec.txt",
        "The lunar landing protocol runs at 250 kbps.",
    )
    assert verified is False
    assert origin == rules.LIBRARY


def test_propose_and_the_dream_read_a_page_break_the_same_way(
    vault: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sentence a reader reads across a page break is one sentence to both commands (F12.14).

    The extractor stands in for `pdftotext -layout`: the page it reports is the page a reader
    would see, ending mid-sentence, which is the ordinary shape of a page break.
    """
    cfg = vault
    source = cfg.home.parent / "specpdf"
    source.mkdir()
    pdf = source / "spec.pdf"
    pdf.write_bytes(b"%PDF-1.4 not a real pdf, the extractor stands in\n")

    def pages(path: Path) -> list[tuple[str, str]]:
        assert path.suffix == ".pdf"  # both commands read the copy, never the source
        return [
            ("Page 1", "The lunar landing\nprotocol runs at 250 kbps."),
            ("Page 2", "It is verified by its checksum."),
        ]

    monkeypatch.setattr(importing, "extract_pdf_pages", pages)
    imported = import_collection(cfg, source, name="specpdf", roles={"spec.pdf": "original"})

    across = proposed(
        imported,
        cfg,
        statement="The lunar landing protocol runs at 250 kbps.",
        quote="The lunar landing protocol runs at 250 kbps.",
        file="spec.pdf",
    )
    assert len(across.verified_facts) == 1

    verified, _origin, _date, _yours = apply._lookup_evidence(
        cfg,
        "aft://library/specpdf/spec.pdf",
        "The lunar landing protocol runs at 250 kbps.",
    )
    assert verified is True

    # A sentence that never was one - the two pages are not adjacent prose - stays refused.
    invented = proposed(
        imported,
        cfg,
        statement="The protocol is verified by its checksum at the lunar landing.",
        quote="protocol runs at 250 kbps. It is verified by its checksum. The lunar landing",
        file="spec.pdf",
    )
    assert invented.verified_facts == []

    # The cache holds the same text the dream reads, so a deleted cache rebuilds it.
    cached = cfg.home / "library"
    hash_of_copy = file_sha256(cfg.vault / "library" / "specpdf" / "spec.pdf")
    assert (cached / f"{hash_of_copy}.json").is_file()
    (cached / f"{hash_of_copy}.json").unlink()
    rebuilt, _origin, _date, _yours = apply._lookup_evidence(
        cfg,
        "aft://library/specpdf/spec.pdf",
        "The lunar landing protocol runs at 250 kbps.",
    )
    assert rebuilt is True
    assert (cached / f"{hash_of_copy}.json").is_file()
