"""Forget and redigest tests: removing traces, byte-level erasure, and redigesting from evidence.

DESIGN 12 promises that forgotten content leaves everything afterthought controls.
Tests prove at the byte level that forgotten words are in none of the evidence archive,
the search index, their write-ahead logs, or the vault working tree.
"""

from __future__ import annotations

import json
import os
import sqlite3
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aft import (
    capture,
    cli,
    digest,
    evidence,
    forget,
    index,
    lint,
    model,
    pagewrite,
    queue,
    registry,
    search,
    work,
)
from aft import config as configmod
from aft.config import Config
from aft.events import SESSION_END, SessionEvent
from aft.forget import FORGOTTEN_MARKER
from aft.library import propose
from aft.readers import CLAUDE, PI, Entry
from conftest import make_repo, run_git
from fakes.model_server import FakeEndpoint

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)

FORGOTTEN_SESSION = "s-forget"
KEPT_SESSION = "s-keep"

FORGOTTEN_PHRASE = "whispering cedar ledger reveals ninety-seven secrets under bridge seven"
KEPT_PHRASE = "permanent bedrock truth that stands for all future sessions"


def serve(cfg: Config, *answers: object) -> tuple[FakeEndpoint, Config]:
    """Point the config at the fake endpoint returning these answers in order."""
    fake = FakeEndpoint(list(answers))
    fake.start()
    (cfg.home / "config.toml").write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n',
        encoding="utf-8",
    )
    return fake, configmod.load()


def create_curated_page(vault_root: Path, rel: str, content: str) -> Path:
    target = vault_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    run_git(["add", rel], vault_root)
    run_git(["commit", "-q", "-m", f"add {rel}"], vault_root)
    return target


def files_matching(directory: Path, prefix: str) -> list[Path]:
    return [p for p in directory.iterdir() if p.is_file() and p.name.startswith(prefix)]


def vault_working_tree_files(vault_root: Path) -> list[Path]:
    return [p for p in vault_root.rglob("*") if p.is_file() and ".git" not in p.parts]


def test_forget_session_removes_every_trace_and_byte_level_erasure(initialized: Config, capsys):
    """Forget session removes evidence, index rows, digests, and withdraws curated lines,
    leaving no byte-level trace in the archive, index, write-ahead logs, or vault working tree."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("widget", "Widget")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)

    entry_forget = Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)
    entry_keep = Entry("k1", NOW, "user", KEPT_PHRASE, False)
    evidence.store(initialized, "claude", FORGOTTEN_SESSION, prj.id, [entry_forget])
    evidence.store(initialized, "claude", KEPT_SESSION, prj.id, [entry_keep])

    digest_forget_rel = f"digests/widget/2026/10/2026-10-01-claude-{FORGOTTEN_SESSION}.md"
    digest_keep_rel = f"digests/widget/2026/10/2026-10-01-claude-{KEPT_SESSION}.md"

    create_curated_page(
        initialized.vault,
        digest_forget_rel,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Widget session 2026-10-01\n"
            f"description: A session with {FORGOTTEN_PHRASE}\n"
            "sources:\n"
            f"  - id: s1\n"
            f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
            f'    quote: "{FORGOTTEN_PHRASE}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            f"- Use forgotten approach. [stated] 2026-10-01 [^s1]\n"
        ),
    )

    create_curated_page(
        initialized.vault,
        digest_keep_rel,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Widget session 2026-10-01\n"
            f"description: A session with {KEPT_PHRASE}\n"
            "sources:\n"
            f"  - id: s2\n"
            f"    resource: aft://session/claude/{KEPT_SESSION}#k1\n"
            f'    quote: "{KEPT_PHRASE}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            f"- Use kept approach. [stated] 2026-10-01 [^s2]\n"
        ),
    )

    curated_rel = "projects/widget/overview.md"
    create_curated_page(
        initialized.vault,
        curated_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Widget\n"
            "sources:\n"
            f"  - id: sf\n"
            f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
            f'    title: "Session with {FORGOTTEN_PHRASE}"\n'
            f'    quote: "{FORGOTTEN_PHRASE}"\n'
            f"  - id: sk\n"
            f"    resource: aft://session/claude/{KEPT_SESSION}#k1\n"
            '    quote: "something else"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Solely on forgotten session. [stated] 2026-10-01 [^sf]\n"
            "- On both sessions. [stated] 2026-10-01 [^sf] [^sk]\n"
            "- Solely on kept session. [stated] 2026-10-01 [^sk]\n"
        ),
    )

    index.update(initialized)

    # Prove forgotten words are present before forget
    compressed_secret = zlib.compress(FORGOTTEN_PHRASE.encode("utf-8"))
    assert any(
        compressed_secret in p.read_bytes()
        for p in files_matching(initialized.home, "evidence.sqlite")
    )
    assert any(
        FORGOTTEN_PHRASE.encode("utf-8") in p.read_bytes()
        for p in files_matching(initialized.home, "index.sqlite")
    )
    assert (initialized.vault / digest_forget_rel).exists()

    code = cli.main(["forget", "session", FORGOTTEN_SESSION])
    assert code == 0
    out = capsys.readouterr().out
    assert "the vault's git history still holds older commits" in out
    assert f"forgot session {FORGOTTEN_SESSION}" in out

    # Check evidence rows
    assert evidence.entries(initialized, FORGOTTEN_SESSION) == []
    assert evidence.entries(initialized, KEPT_SESSION) == [entry_keep]

    # Check digest files in vault
    assert not (initialized.vault / digest_forget_rel).exists()
    assert (initialized.vault / digest_keep_rel).exists()

    # Check curated lines
    curated_doc = pagewrite.PageDoc.read(initialized.vault / curated_rel)
    facts_by_statement = {f.statement: f for f in curated_doc.page.facts}
    assert facts_by_statement["Solely on forgotten session."].state == "withdrawn"
    assert facts_by_statement["On both sessions."].state == "active"
    assert facts_by_statement["Solely on kept session."].state == "active"
    assert curated_doc.sources["sf"]["quote"] == "[forgotten]"
    assert curated_doc.sources["sf"].get("title") == "[forgotten]"
    assert curated_doc.sources["sk"]["quote"] == "something else"
    assert lint.lint_vault(initialized.vault) == []

    # Byte-level verification across archive, index, write-ahead logs, and vault working tree
    for p in files_matching(initialized.home, "evidence.sqlite"):
        assert compressed_secret not in p.read_bytes(), f"found secret in {p.name}"

    for p in files_matching(initialized.home, "index.sqlite"):
        assert FORGOTTEN_PHRASE.encode("utf-8") not in p.read_bytes(), f"found secret in {p.name}"

    for p in vault_working_tree_files(initialized.vault):
        assert FORGOTTEN_PHRASE.encode("utf-8") not in p.read_bytes(), (
            f"found secret in working tree {p}"
        )

    # Vault git status is clean and committed
    assert run_git(["status", "--porcelain"], initialized.vault) == ""
    last_commit = run_git(["log", "-1", "--format=%s"], initialized.vault)
    assert last_commit == f"aft: forget session {FORGOTTEN_SESSION}"


def test_forget_session_with_multipart_digests_and_stale_line(initialized: Config, capsys):
    """Forget removes all multipart digest files for the session and withdraws stale lines."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("alpha", "Alpha")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register alpha"], initialized.vault)

    sid = "multi-sess"
    entry1 = Entry("e1", NOW, "user", "multi part entry one", False)
    evidence.store(initialized, "claude", sid, prj.id, [entry1])

    part1 = f"digests/alpha/2026/10/2026-10-01-claude-{sid}.md"
    part2 = f"digests/alpha/2026/10/2026-10-01-claude-{sid}-2.md"

    create_curated_page(
        initialized.vault,
        part1,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Alpha part 1\n"
            "sources:\n"
            f"  - id: s1\n"
            f"    resource: aft://session/claude/{sid}#e1\n"
            '    quote: "part 1 quote"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Part 1 decision. [stated] 2026-10-01 [^s1]\n"
        ),
    )
    create_curated_page(
        initialized.vault,
        part2,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Alpha part 2\n"
            "sources:\n"
            f"  - id: s2\n"
            f"    resource: aft://session/claude/{sid}#e1\n"
            '    quote: "part 2 quote"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Part 2 decision. [stated] 2026-10-01 [^s2]\n"
        ),
    )

    page_rel = "projects/alpha/overview.md"
    create_curated_page(
        initialized.vault,
        page_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Alpha\n"
            "sources:\n"
            f"  - id: s1\n"
            f"    resource: aft://session/claude/{sid}#e1\n"
            '    quote: "part 1 quote"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## State\n\n"
            "- Stale fact based on multi-sess. [observed] 2026-10-01 [^s1]"
            " (stale 2026-10-01: anchor changed)\n"
        ),
    )

    index.update(initialized)

    code = cli.main(["forget", "session", sid])
    assert code == 0
    assert not (initialized.vault / part1).exists()
    assert not (initialized.vault / part2).exists()

    doc = pagewrite.PageDoc.read(initialized.vault / page_rel)
    assert doc.page.facts[0].state == "withdrawn"


def test_forget_session_unknown_leaves_state_intact(initialized: Config, capsys):
    """Forgetting an unrecorded session writes no row and prints the history notice."""
    code = cli.main(["forget", "session", "unknown-session"])
    assert code == 0
    out = capsys.readouterr().out
    assert "the vault's git history still holds older commits" in out
    assert run_git(["status", "--porcelain"], initialized.vault) == ""


def test_forget_session_with_open_note_does_not_abort_and_reports(initialized: Config, capsys):
    """When a line with an open note cannot be withdrawn, forget_session withdraws other lines,
    scrubs source quotes, and lists the un-withdrawable line in the report and CLI output."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("beta", "Beta")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register beta"], initialized.vault)

    entry = Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)
    evidence.store(initialized, "claude", FORGOTTEN_SESSION, prj.id, [entry])

    page_rel = "projects/beta/overview.md"
    create_curated_page(
        initialized.vault,
        page_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Beta\n"
            "sources:\n"
            f"  - id: sf\n"
            f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
            f'    title: "Beta notes on {FORGOTTEN_PHRASE}"\n'
            f'    quote: "{FORGOTTEN_PHRASE}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Line with open note. [stated] 2026-10-01 [^sf]\n"
            "> [!question] 2026-10-01\n"
            "> Should this line remain?\n"
            "- Line without note. [stated] 2026-10-01 [^sf]\n"
        ),
    )
    index.update(initialized)

    # First run: open note stops line 1 from being withdrawn, but line 2 is withdrawn
    code = cli.main(["forget", "session", FORGOTTEN_SESSION])
    assert code == 0
    out = capsys.readouterr().out
    assert "1 line(s) with open notes could not be withdrawn; close the note and rerun:" in out
    assert "Line with open note." in out
    assert page_rel in out
    assert "withdrew 1 curated line(s)" in out

    doc = pagewrite.PageDoc.read(initialized.vault / page_rel)
    facts = {f.statement: f for f in doc.page.facts}
    assert facts["Line with open note."].state == "needs-you"
    assert facts["Line without note."].state == "withdrawn"
    assert doc.sources["sf"]["quote"] == "[forgotten]"
    assert doc.sources["sf"]["title"] == "[forgotten]"
    assert lint.lint_vault(initialized.vault) == []

    # Close note and rerun forget: now the line is withdrawn
    cur_text = (initialized.vault / page_rel).read_text(encoding="utf-8")
    closed_lines = [line for line in cur_text.splitlines() if not line.startswith(">")]
    (initialized.vault / page_rel).write_text("\n".join(closed_lines) + "\n", encoding="utf-8")
    run_git(["add", page_rel], initialized.vault)
    run_git(["commit", "-q", "-m", "close note on line"], initialized.vault)

    code2 = cli.main(["forget", "session", FORGOTTEN_SESSION])
    assert code2 == 0
    out2 = capsys.readouterr().out
    assert "could not be withdrawn" not in out2
    assert "withdrew 1 curated line(s)" in out2

    doc2 = pagewrite.PageDoc.read(initialized.vault / page_rel)
    facts2 = {f.statement: f for f in doc2.page.facts}
    assert facts2["Line with open note."].state == "withdrawn"
    assert facts2["Line without note."].state == "withdrawn"
    assert lint.lint_vault(initialized.vault) == []


def test_forget_session_mixed_session_line_scrubs_source_quote_and_keeps_line_active(
    initialized: Config,
):
    """Lines citing both the forgotten session and another session stay active,
    while the forgotten session's source quote is scrubbed so footnotes still resolve."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("gamma", "Gamma")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register gamma"], initialized.vault)

    entry_f = Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)
    entry_k = Entry("k1", NOW, "user", KEPT_PHRASE, False)
    evidence.store(initialized, "claude", FORGOTTEN_SESSION, prj.id, [entry_f])
    evidence.store(initialized, "claude", KEPT_SESSION, prj.id, [entry_k])

    page_rel = "projects/gamma/overview.md"
    create_curated_page(
        initialized.vault,
        page_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Gamma\n"
            "sources:\n"
            f"  - id: sf\n"
            f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
            f'    title: "Title with {FORGOTTEN_PHRASE}"\n'
            f'    quote: "{FORGOTTEN_PHRASE}"\n'
            f"  - id: sk\n"
            f"    resource: aft://session/claude/{KEPT_SESSION}#k1\n"
            f'    quote: "{KEPT_PHRASE}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Mixed evidence decision. [stated] 2026-10-01 [^sf] [^sk]\n"
            "- Forgotten evidence decision. [stated] 2026-10-01 [^sf]\n"
            "- Kept evidence decision. [stated] 2026-10-01 [^sk]\n"
        ),
    )
    index.update(initialized)

    report = forget.forget_session(initialized, FORGOTTEN_SESSION)
    assert report.lines_withdrawn == ("Forgotten evidence decision.",)
    assert report.lines_unwithdrawn == ()

    doc = pagewrite.PageDoc.read(initialized.vault / page_rel)
    facts = {f.statement: f for f in doc.page.facts}
    assert facts["Mixed evidence decision."].state == "active"
    assert facts["Forgotten evidence decision."].state == "withdrawn"
    assert facts["Kept evidence decision."].state == "active"

    # Source sf is scrubbed with the fixed marker; sk is unchanged
    assert doc.sources["sf"]["quote"] == "[forgotten]"
    assert doc.sources["sf"]["title"] == "[forgotten]"
    assert doc.sources["sk"]["quote"] == KEPT_PHRASE

    # Both footnotes on the mixed line still resolve cleanly
    assert lint.lint_vault(initialized.vault) == []
    # Forgotten phrase is absent from the working tree
    assert FORGOTTEN_PHRASE.encode("utf-8") not in (initialized.vault / page_rel).read_bytes()


def test_forget_session_candidate_lines_scrubbed_or_removed(initialized: Config):
    """Candidate lines resting only on the forgotten session are removed,
    while candidate lines resting on mixed sessions remain with scrubbed source quotes."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("delta", "Delta")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register delta"], initialized.vault)

    entry_f = Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)
    entry_k = Entry("k1", NOW, "user", KEPT_PHRASE, False)
    evidence.store(initialized, "claude", FORGOTTEN_SESSION, prj.id, [entry_f])
    evidence.store(initialized, "claude", KEPT_SESSION, prj.id, [entry_k])

    page_rel = "projects/delta/overview.md"
    create_curated_page(
        initialized.vault,
        page_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Delta\n"
            "sources:\n"
            f"  - id: sf\n"
            f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
            f'    quote: "{FORGOTTEN_PHRASE}"\n'
            f"  - id: sk\n"
            f"    resource: aft://session/claude/{KEPT_SESSION}#k1\n"
            f'    quote: "{KEPT_PHRASE}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Candidates\n\n"
            "- Forgotten candidate. [inferred] 2026-10-01 [^sf] (signals 1/3, sessions 1/2)\n"
            "- Mixed candidate. [inferred] 2026-10-01 [^sf] [^sk] (signals 2/3, sessions 2/2)\n"
            "- Kept candidate. [inferred] 2026-10-01 [^sk] (signals 1/3, sessions 1/2)\n"
        ),
    )
    index.update(initialized)

    report = forget.forget_session(initialized, FORGOTTEN_SESSION)
    assert report.candidates_removed == ("Forgotten candidate.",)

    doc = pagewrite.PageDoc.read(initialized.vault / page_rel)
    statements = [f.statement for f in doc.page.facts]
    assert "Forgotten candidate." not in statements
    assert "Mixed candidate." in statements
    assert "Kept candidate." in statements
    assert doc.sources["sf"]["quote"] == "[forgotten]"
    assert lint.lint_vault(initialized.vault) == []
    assert FORGOTTEN_PHRASE.encode("utf-8") not in (initialized.vault / page_rel).read_bytes()


def test_forget_pattern_redacts_archive_and_byte_level_erasure(initialized: Config, capsys):
    """Forget pattern redacts matching text across the archive and updates the index,
    printing the redact_patterns line for the user to add."""
    secret_pat = "secret-token-4815162342"
    filler = "ordinary conversation text"
    entries = [
        Entry("e1", NOW, "user", f"connect with {secret_pat} now", False),
        Entry("e2", NOW, "assistant", filler, False),
    ]
    evidence.store(initialized, "claude", "s-pat", None, entries)
    index.update(initialized)

    # Prove present in index before pattern forget
    assert any(
        secret_pat.encode("utf-8") in p.read_bytes()
        for p in files_matching(initialized.home, "index.sqlite")
    )

    code = cli.main(["forget", "pattern", "secret-token-[0-9]+"])
    assert code == 0
    out = capsys.readouterr().out
    assert "redacted 1 entry in the evidence archive" in out
    assert 'redact_patterns = ["secret-token-[0-9]+"]' in out
    assert "the vault's git history still holds older commits" in out

    # Config file was not edited
    config_text = (initialized.home / "config.toml").read_text(encoding="utf-8")
    assert "secret-token-[0-9]+" not in config_text

    # Read back entries through evidence
    read_back = evidence.entries(initialized, "s-pat")
    assert read_back[0].text == "connect with [redacted] now"
    assert read_back[1].text == filler

    # Byte-level check on index and archive files
    for p in files_matching(initialized.home, "evidence.sqlite"):
        assert secret_pat.encode("utf-8") not in p.read_bytes(), f"found in {p.name}"

    for p in files_matching(initialized.home, "index.sqlite"):
        assert secret_pat.encode("utf-8") not in p.read_bytes(), f"found in {p.name}"


def test_forget_pattern_appends_to_existing_patterns(initialized: Config, capsys):
    """Existing redact_patterns in config are included in the printed line."""
    (initialized.home / "config.toml").write_text(
        f'vault = "{initialized.vault}"\nhost = "{initialized.host}"\n'
        '[capture]\nredact_patterns = ["prior-token-[0-9]+"]\n',
        encoding="utf-8",
    )
    code = cli.main(["forget", "pattern", "new-token-[0-9]+"])
    assert code == 0
    out = capsys.readouterr().out
    assert 'redact_patterns = ["prior-token-[0-9]+", "new-token-[0-9]+"]' in out


def test_forget_pattern_invalid_regex_fails(initialized: Config, capsys):
    """An uncompilable regex is reported and exits non-zero."""
    code = cli.main(["forget", "pattern", "["])
    assert code == 2
    err = capsys.readouterr().err
    assert "aft:" in err


def test_redigest_rebuilds_from_evidence_alone(initialized: Config, capsys):
    """Redigest reproduces a digest from evidence alone without a transcript."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("widget", "Widget")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)

    user_quote = "Store the digest at session end, not per turn."
    user_entry = Entry("e1", NOW, "user", user_quote, False)
    asst_entry = Entry(
        "e2",
        NOW,
        "assistant",
        "Done: the hook writes one row and returns without calling a model.",
        False,
    )
    evidence.store(initialized, "claude", "s-redigest", prj.id, [user_entry, asst_entry])

    answer = {
        "summary": "Rebuilt summary from archived evidence alone.",
        "handoff": {
            "in_flight": "redigest verified",
            "failed": [],
            "open": ["test next step"],
        },
        "facts": [
            {
                "statement": "Digest runs at session end.",
                "kind": "decision",
                "source": "stated",
                "quote": user_quote,
                "entry": "e1",
                "scope": "project",
            }
        ],
    }

    _, cfg = serve(initialized, answer)

    code = cli.main(["redigest", "s-redigest"])
    assert code == 0
    out = capsys.readouterr().out
    assert "rebuilt digest at digests/widget/2026/10/2026-10-01-claude-s-redigest.md" in out

    digest_path = cfg.vault / "digests/widget/2026/10/2026-10-01-claude-s-redigest.md"
    assert digest_path.exists()
    content = digest_path.read_text(encoding="utf-8")
    assert "Rebuilt summary from archived evidence alone." in content
    assert "Digest runs at session end." in content
    assert user_quote in content

    # Clean working tree and commit made
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    last_commit = run_git(["log", "-1", "--format=%s"], cfg.vault)
    assert last_commit == "aft: redigest s-redigest"


def test_redigest_replaces_existing_digest(initialized: Config, capsys):
    """Redigest replaces an existing digest file with the newly generated digest."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("widget", "Widget")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)

    user_quote = "Primary fact text for testing."
    entry = Entry("e1", NOW, "user", user_quote, False)
    evidence.store(initialized, "claude", "s-replace", prj.id, [entry])

    old_digest_rel = "digests/widget/2026/10/2026-10-01-claude-s-replace.md"
    create_curated_page(
        initialized.vault,
        old_digest_rel,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Old digest\n"
            "description: Old summary text\n"
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Old decision. [stated] 2026-10-01 [^s1]\n"
        ),
    )

    answer = {
        "summary": "New and improved summary.",
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "facts": [
            {
                "statement": "Primary fact verified.",
                "kind": "fact",
                "source": "stated",
                "quote": user_quote,
                "entry": "e1",
                "scope": "project",
            }
        ],
    }

    _, cfg = serve(initialized, answer)

    code = cli.main(["redigest", "s-replace"])
    assert code == 0

    content = (cfg.vault / old_digest_rel).read_text(encoding="utf-8")
    assert "New and improved summary." in content
    assert "Old summary text" not in content
    assert "Primary fact verified." in content


def test_redigest_no_evidence_fails(initialized: Config, capsys):
    """Redigest for a session with no archived evidence exits non-zero."""
    code = cli.main(["redigest", "never-seen"])
    assert code == 2
    err = capsys.readouterr().err
    assert "no archived evidence for session 'never-seen'" in err


def test_cli_forget_and_redigest_usage_errors(initialized: Config, capsys):
    """Subcommands with missing arguments exit 2 with help/error."""
    assert cli.main(["forget"]) == 2
    for args in (["forget", "session"], ["forget", "pattern"], ["redigest"]):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(args)
        assert excinfo.value.code == 2


def test_forget_and_redigest_with_session_and_session_2_prefix_collision(
    initialized: Config, capsys
):
    """Multipart digests ending in -2 do not collide with a separate session named session-2."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("widget", "Widget")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)

    sess1 = "session"
    sess2 = "session-2"

    entry1_1 = Entry("e1", NOW, "user", "entry one of session", False)
    entry1_2 = Entry("e2", NOW, "assistant", "entry two of session", False)
    entry2_1 = Entry("e3", NOW, "user", "entry one of session-2", False)

    evidence.store(initialized, "claude", sess1, prj.id, [entry1_1, entry1_2])
    evidence.store(initialized, "claude", sess2, prj.id, [entry2_1])

    # sess1 has multipart digests: part 1 and part 2 (-2)
    part1_sess1 = "digests/widget/2026/10/2026-10-01-claude-session.md"
    part2_sess1 = "digests/widget/2026/10/2026-10-01-claude-session-2.md"
    create_curated_page(
        initialized.vault,
        part1_sess1,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Widget session part 1\n"
            "sources:\n"
            "  - id: s1\n"
            f"    resource: aft://session/claude/{sess1}#e1\n"
            '    quote: "entry one of session"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Part 1 decision. [stated] 2026-10-01 [^s1]\n"
        ),
    )
    create_curated_page(
        initialized.vault,
        part2_sess1,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Widget session part 2\n"
            "sources:\n"
            "  - id: s2\n"
            f"    resource: aft://session/claude/{sess1}#e2\n"
            '    quote: "entry two of session"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Part 2 decision. [stated] 2026-10-01 [^s2]\n"
        ),
    )

    # sess2 has single digest on day 2
    digest_sess2 = "digests/widget/2026/10/2026-10-02-claude-session-2.md"
    create_curated_page(
        initialized.vault,
        digest_sess2,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Widget session-2\n"
            "sources:\n"
            "  - id: s3\n"
            f"    resource: aft://session/claude/{sess2}#e3\n"
            '    quote: "entry one of session-2"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Session-2 decision. [stated] 2026-10-02 [^s3]\n"
        ),
    )

    # Curated overview page citing both sessions and holding a candidate
    overview_rel = "projects/widget/overview.md"
    create_curated_page(
        initialized.vault,
        overview_rel,
        (
            "---\n"
            "type: Project\n"
            "title: Widget\n"
            "sources:\n"
            "  - id: sf1\n"
            f"    resource: aft://session/claude/{sess1}#e1\n"
            '    quote: "entry one of session"\n'
            "  - id: sf2\n"
            f"    resource: aft://session/claude/{sess2}#e3\n"
            '    quote: "entry one of session-2"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Decision from sess1. [stated] 2026-10-01 [^sf1]\n"
            "- Decision from sess2. [stated] 2026-10-02 [^sf2]\n\n"
            "## Candidates\n\n"
            "- Candidate resting only on sess1. [inferred] 2026-10-01 [^sf1]"
            " (signals 1/3, sessions 1/2)\n"
        ),
    )

    # Redigest for sess1 does not delete sess2 digest
    answer_sess1 = {
        "summary": "Rebuilt summary for session.",
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "facts": [
            {
                "statement": "Session decision rebuilt.",
                "kind": "decision",
                "source": "stated",
                "quote": "entry one of session",
                "entry": "e1",
                "scope": "project",
            }
        ],
    }
    _, cfg = serve(initialized, answer_sess1)
    code = cli.main(["redigest", sess1])
    assert code == 0
    # sess2 digest is preserved despite session prefix
    assert (cfg.vault / digest_sess2).exists()

    # Forgetting sess1 removes its digests and candidate but preserves sess2
    code = cli.main(["forget", "session", sess1])
    assert code == 0
    out = capsys.readouterr().out
    assert (
        f"forgot session {sess1}: removed evidence, 1 digest file(s), 0 queue job row(s), "
        "withdrew 1 curated line(s), removed 1 candidate line(s), scrubbed 0 report page(s)"
    ) in out

    assert not (initialized.vault / part1_sess1).exists()
    assert not (initialized.vault / part2_sess1).exists()
    assert (initialized.vault / digest_sess2).exists()

    # Evidence for sess2 remains intact
    assert evidence.entries(initialized, sess2) == [entry2_1]
    assert evidence.entries(initialized, sess1) == []

    # Curated lines
    doc = pagewrite.PageDoc.read(initialized.vault / overview_rel)
    facts_by_statement = {f.statement: f for f in doc.page.facts}
    assert facts_by_statement["Decision from sess1."].state == "withdrawn"
    assert facts_by_statement["Decision from sess2."].state == "active"
    assert "Candidate resting only on sess1." not in facts_by_statement

    # Redigest for sess2 rebuilds its digest
    answer_sess2 = {
        "summary": "Rebuilt summary for session-2.",
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "facts": [
            {
                "statement": "Session-2 decision rebuilt.",
                "kind": "decision",
                "source": "stated",
                "quote": "entry one of session-2",
                "entry": "e3",
                "scope": "project",
            }
        ],
    }
    _, cfg = serve(initialized, answer_sess2)
    code = cli.main(["redigest", sess2])
    assert code == 0
    new_digest_sess2 = "digests/widget/2026/10/2026-10-01-claude-session-2.md"
    assert (cfg.vault / new_digest_sess2).exists()

    # Forgetting sess2 removes sess2 digest and withdraws remaining line
    code = cli.main(["forget", "session", sess2])
    assert code == 0
    assert not (cfg.vault / new_digest_sess2).exists()
    doc = pagewrite.PageDoc.read(initialized.vault / overview_rel)
    facts_by_statement = {f.statement: f for f in doc.page.facts}
    assert facts_by_statement["Decision from sess2."].state == "withdrawn"


def test_redigest_prunes_empty_date_directories(initialized: Config, capsys):
    """Redigest replaces an old digest in an older date directory and
    removes empty parent directories."""
    reg = registry.Registry.load(initialized.registry_path)
    prj = reg.add("widget", "Widget")
    reg.save()
    run_git(["add", "registry.yaml"], initialized.vault)
    run_git(["commit", "-q", "-m", "register widget"], initialized.vault)

    sess = "s-migrate"
    user_quote = "Migrate date directory quote."
    entry = Entry("e1", NOW, "user", user_quote, False)
    evidence.store(initialized, "claude", sess, prj.id, [entry])

    old_rel = "digests/widget/2026/08/2026-08-15-claude-s-migrate.md"
    create_curated_page(
        initialized.vault,
        old_rel,
        (
            "---\n"
            "type: Session Digest\n"
            "title: Old august digest\n"
            "sources:\n"
            "  - id: s1\n"
            f"    resource: aft://session/claude/{sess}#e1\n"
            f'    quote: "{user_quote}"\n'
            "aft:\n"
            f"  project: {prj.id}\n"
            "---\n\n"
            "## Decisions\n\n"
            "- Old august decision. [stated] 2026-08-15 [^s1]\n"
        ),
    )

    old_dir = initialized.vault / "digests/widget/2026/08"
    assert old_dir.is_dir()

    answer = {
        "summary": "New october digest summary.",
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "facts": [
            {
                "statement": "New october decision.",
                "kind": "decision",
                "source": "stated",
                "quote": user_quote,
                "entry": "e1",
                "scope": "project",
            }
        ],
    }
    _, cfg = serve(initialized, answer)

    code = cli.main(["redigest", sess])
    assert code == 0

    new_rel = "digests/widget/2026/10/2026-10-01-claude-s-migrate.md"
    assert (cfg.vault / new_rel).exists()
    assert not (cfg.vault / old_rel).exists()
    # The empty August date directory is pruned
    assert not old_dir.exists()


# ------------------------------------------- the queue, the work that follows, and the reports

DAILY_REPORT = f"""---
type: Dream Report
title: "2026-10-01"
---

# widget 12:00:00Z

## Added and Confirmed

### Added

- The capture hook files one row per session end. [stated] (2026-10-01)
  Quote: "{FORGOTTEN_PHRASE}"

## Superseded

- Old: The hook called a model on every turn.
  New: The hook files one row and returns.
  Quote: "{KEPT_PHRASE}"
"""

NEEDS_YOU = f"""---
type: Needs You
title: Needs you
description: Every open conflict and question, regenerated on every dream run.
---

# Needs you

## Conflicts

- [projects/widget/overview.md:12](../projects/widget/overview.md#L12): The hook files one
  row. (reports/2026/10/2026-10-01.md#conflicts)
  > [!conflict] 2026-10-01
  > For: "{FORGOTTEN_PHRASE}"
  > Against: "{KEPT_PHRASE}"
"""


def with_budget(cfg: Config, tokens: int) -> Config:
    """The same config.toml with the day's token budget set, re-read from disk (DESIGN 6)."""
    path = cfg.home / configmod.CONFIG_NAME
    text = path.read_text(encoding="utf-8")
    kept = text[: text.index("[work]")] if "[work]" in text else text
    path.write_text(f"{kept}[work]\ndaily_token_budget = {tokens}\n", encoding="utf-8")
    return configmod.load()


def queue_job_rows(cfg: Config, session_id: str) -> list[tuple]:
    """Every job row the queue holds for one session, at any status, with its error text."""
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME)
    try:
        return conn.execute(
            "SELECT status, error FROM jobs WHERE session_id = ? ORDER BY created_time, id",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def queue_session_rows(cfg: Config, session_id: str) -> list[tuple]:
    """The queue's session rows for one session, with their watermark and digest debt."""
    conn = sqlite3.connect(cfg.home / queue.QUEUE_NAME)
    try:
        return conn.execute(
            "SELECT agent, watermark, digest_due FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def hook_event(session_id: str, cwd: Path) -> SessionEvent:
    """The capture event a session's hook files for it."""
    return SessionEvent(
        agent=CLAUDE,
        kind=SESSION_END,
        session_id=session_id,
        cwd=cwd,
        transcript_path=Path("/agent-transcripts") / f"{session_id}.jsonl",
        time=NOW,
        detail=None,
    )


def agent_record(entry_id: str, role: str, text: str, model: str | None = None) -> str:
    """One conversation record of a Claude Code transcript, as the file holds it."""
    message: dict = {"content": [{"type": "text", "text": text}]}
    if model:
        message["model"] = model
    return json.dumps(
        {"type": role, "uuid": entry_id, "timestamp": NOW.isoformat(), "message": message}
    )


def digest_answer(quote: str, entry: str = "e1") -> dict:
    """A digest answer of the shape the schema describes, citing the transcript's own words."""
    return {
        "summary": "Settled how the hook files its work.",
        "handoff": {"in_flight": "", "failed": [], "open": []},
        "facts": [
            {
                "statement": "The capture hook files one row at session end.",
                "kind": "decision",
                "source": "stated",
                "quote": quote,
                "entry": entry,
                "scope": "project",
            }
        ],
    }


def digest_pages(cfg: Config) -> list[str]:
    """The digest pages standing in the vault."""
    root = cfg.vault / "digests"
    if not root.is_dir():
        return []
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.md") if p.is_file())


def search_hits(cfg: Config, phrase: str) -> list[str]:
    """The pages the index answers to ``phrase`` with, memory pages and reports alike."""
    return sorted(
        {hit.path for hit in search.search(cfg, phrase, all_projects=True, reports=True, limit=20)}
    )


def report_page(cfg: Config, rel: str, text: str) -> Path:
    """A report page committed to the vault, as a finished dream run left it."""
    target = cfg.vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    run_git(["add", rel], cfg.vault)
    run_git(["commit", "-q", "-m", f"add {rel}"], cfg.vault)
    return target


def plant_forgotten_words(initialized: Config) -> None:
    """Archive the forgotten phrase and leave it quoted in today's report and in needs-you."""
    evidence.store(
        initialized,
        CLAUDE,
        FORGOTTEN_SESSION,
        None,
        [Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)],
    )
    report_page(initialized, "reports/2026/10/2026-10-01.md", DAILY_REPORT)
    report_page(initialized, "reports/needs-you.md", NEEDS_YOU)
    index.update(initialized)


def test_forget_session_clears_its_queue_rows_watermark_and_debt(
    initialized: Config, tmp_path: Path
):
    """A forgetting clears the queue with everything else: the job rows and the model text
    they carry, the watermark, and the digest debt, which is what files a stored chunk as
    new work (DESIGN 12)."""
    cfg = initialized
    cwd = tmp_path / "dev" / "widget"
    queue.enqueue(cfg, hook_event(FORGOTTEN_SESSION, cwd))
    claimed = queue.next_job(cfg)
    assert claimed is not None and claimed.session_id == FORGOTTEN_SESSION
    queue.fail(cfg, claimed, f"the model answered 500 about {FORGOTTEN_PHRASE}")

    # The session's hook reports it again, and the archive stores the chunk a later run owes
    # it a digest for.
    queue.enqueue(cfg, hook_event(FORGOTTEN_SESSION, cwd))
    queue.owe_digest(cfg, queue.pending_jobs(cfg)[0])
    queue.enqueue(cfg, hook_event(KEPT_SESSION, cwd))
    assert queue_job_rows(cfg, FORGOTTEN_SESSION) == [
        (queue.FAILED, f"the model answered 500 about {FORGOTTEN_PHRASE}"),
        (queue.DONE, None),
    ]
    assert queue_session_rows(cfg, FORGOTTEN_SESSION) == [(CLAUDE, None, 1)]
    assert queue.redeem_digest_debts(cfg) == 1, "the debt the queue holds files no work"

    report = forget.forget_session(cfg, FORGOTTEN_SESSION)

    assert report.jobs_removed == 3, "the row the debt filed and the two before it were left"
    assert queue_job_rows(cfg, FORGOTTEN_SESSION) == []
    assert queue_session_rows(cfg, FORGOTTEN_SESSION) == []
    assert queue.redeem_digest_debts(cfg) == 0, "a forgotten session still owes a digest"
    assert queue.session_state(cfg, CLAUDE, FORGOTTEN_SESSION).forgotten is True
    assert [job.session_id for job in queue.pending_jobs(cfg)] == [KEPT_SESSION]


def test_a_later_hook_for_a_forgotten_session_files_no_work(initialized: Config, tmp_path: Path):
    """A hook for a session that was forgotten files nothing: the transcript is still on disk,
    and a row for it would put its words back in the archive and the index (DESIGN 12)."""
    cfg = initialized
    cwd = tmp_path / "dev" / "widget"
    queue.enqueue(cfg, hook_event(FORGOTTEN_SESSION, cwd))
    forget.forget_session(cfg, FORGOTTEN_SESSION)

    queue.enqueue(cfg, hook_event(FORGOTTEN_SESSION, cwd))
    queue.enqueue(cfg, hook_event(KEPT_SESSION, cwd))

    assert queue_job_rows(cfg, FORGOTTEN_SESSION) == []
    assert [job.session_id for job in queue.pending_jobs(cfg)] == [KEPT_SESSION]


def test_the_work_that_follows_a_forgetting_finds_nothing_to_digest(
    initialized: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`aft work` after a forgetting asks nothing of the forgotten session: no sweep, no model
    call, no page, no archive row, no index row (DESIGN 12).

    The transcript is left where the sweep finds it, because that is the state a person is
    actually in: forgetting what afterthought recorded does not remove the agent's own file.
    """
    cfg = initialized
    registered = registry.Registry.load(cfg.registry_path)
    project = registered.add("widget", "Widget")
    cwd = make_repo(tmp_path / "dev" / "widget")
    registered.add_path(project, registry.path_key(cfg.host, cwd))
    registered.save()
    run_git(["add", "registry.yaml"], cfg.vault)
    run_git(["commit", "-q", "-m", "register widget"], cfg.vault)

    sessions = cfg.home / "claude-projects" / "-dev-widget"
    sessions.mkdir(parents=True)
    transcript = sessions / f"{FORGOTTEN_SESSION}.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps({"cwd": str(cwd)}),
                agent_record("e1", "user", FORGOTTEN_PHRASE),
                agent_record("e2", "assistant", "Noted.", model="fake-model"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    quiet = datetime.now(UTC) - timedelta(minutes=45)
    os.utime(transcript, (quiet.timestamp(), quiet.timestamp()))
    monkeypatch.setattr(
        capture, "SESSION_DIRS", {CLAUDE: sessions.parent, PI: cfg.home / "no-pi-sessions"}
    )
    queue.watching_since(cfg, quiet - timedelta(hours=6))

    fake, cfg = serve(cfg, digest_answer(FORGOTTEN_PHRASE))
    try:
        # The day's budget is spent before the queue is reached, so the row the sweep filed
        # waits for a later run with the session's words still unread (DESIGN 13).
        first = work.run(with_budget(cfg, 0))
        assert (first.swept, first.digested) == (1, 0), "the run spent no sweep on the session"
        assert queue_job_rows(cfg, FORGOTTEN_SESSION) != []

        forget.forget_session(cfg, FORGOTTEN_SESSION)
        second = work.run(with_budget(cfg, 100_000))
    finally:
        fake.stop()

    assert (second.swept, second.digested, second.empty) == (0, 0, 0), (
        "the work a forgetting should have cleared was digested"
    )
    assert len(fake.requests) == 0, "the forgotten session was asked of the model"
    assert queue_job_rows(cfg, FORGOTTEN_SESSION) == []
    assert evidence.entries(cfg, FORGOTTEN_SESSION) == [], "the forgotten words were archived"
    assert digest_pages(cfg) == [], "a page was written for a forgotten session"
    assert search_hits(cfg, FORGOTTEN_PHRASE) == [], "the forgotten words were indexed"


def test_forget_session_scrubs_the_session_words_from_reports_and_needs_you(
    initialized: Config,
):
    """The quotes a dream report wrote and the callout text needs-you repeats carry a
    forgotten session's words, so a forgetting scrubs them there as it does everywhere else
    (DESIGN 12)."""
    cfg = initialized
    plant_forgotten_words(cfg)
    daily = cfg.vault / "reports" / "2026" / "10" / "2026-10-01.md"
    needs = cfg.vault / "reports" / "needs-you.md"
    assert FORGOTTEN_PHRASE in daily.read_text(encoding="utf-8")
    assert FORGOTTEN_PHRASE in needs.read_text(encoding="utf-8")

    report = forget.forget_session(cfg, FORGOTTEN_SESSION)

    assert report.reports_scrubbed == (
        "reports/2026/10/2026-10-01.md",
        "reports/needs-you.md",
    )
    for path in (daily, needs):
        text = path.read_text(encoding="utf-8")
        assert FORGOTTEN_PHRASE not in text, path
        assert FORGOTTEN_MARKER in text, path
        # The rest of the page is the person's own reading of the day and stays put.
        assert KEPT_PHRASE in text, path
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    committed = run_git(["show", "--name-only", "--format=", "HEAD"], cfg.vault)
    assert "reports/2026/10/2026-10-01.md" in committed
    assert "reports/needs-you.md" in committed


def test_forget_session_leaves_the_reports_collection_nothing_to_find(initialized: Config):
    """Search answers to a forgotten phrase in no collection, reports included: a report is a
    collection of its own, and its rows say nothing of the session they were written from."""
    cfg = initialized
    plant_forgotten_words(cfg)
    assert search_hits(cfg, "whispering cedar ledger") == [
        "aft://session/claude/s-forget",
        "reports/2026/10/2026-10-01.md",
        "reports/needs-you.md",
    ]

    forget.forget_session(cfg, FORGOTTEN_SESSION)

    assert search_hits(cfg, "whispering cedar ledger") == []
    assert search_hits(cfg, "permanent bedrock truth") == [
        "reports/2026/10/2026-10-01.md",
        "reports/needs-you.md",
    ]


# ------------------------------------------- a pattern's copies, and a rebuild of a digest

SECRET = "secret-token-4815162342"
SECRET_PATTERN = "secret-token-[0-9]+"
SECRET_WORD = "4815162342"
PATTERN_SENTENCE = f"rotate the vault key {SECRET} before friday"

PATTERN_ARCHIVE = f"""---
type: Archive
title: widget archive
description: Notes and lines resolved out of widget.
---

## Superseded

An older note said to rotate the vault key {SECRET} on sight.
"""

PATTERN_REPORT = f"""---
type: Dream Report
title: "2026-10-01"
---

# widget 12:00:00Z

## Added and Confirmed

### Added

- Rotate the vault key before friday. [stated] (2026-10-01)
  Quote: "{PATTERN_SENTENCE}"
"""


def pattern_digest(project_id: str) -> str:
    """A digest page holding the secret in its description and its source quote."""
    return (
        "---\n"
        "type: Session Digest\n"
        "title: Widget session 2026-10-01\n"
        f"description: How to rotate the widget key {SECRET}.\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/claude/s-pat#e1\n"
        f'    quote: "{PATTERN_SENTENCE}"\n'
        "aft:\n"
        f"  project: {project_id}\n"
        "---\n\n"
        "## Decisions\n\n"
        "- Rotate the vault key before friday. [stated] 2026-10-01 [^s1]\n"
    )


def pattern_page(project_id: str) -> str:
    """A memory page holding the secret in a source quote, a statement and a note."""
    return (
        "---\n"
        "type: Project\n"
        "title: Widget\n"
        "description: The widget project.\n"
        "sources:\n"
        "  - id: s1\n"
        "    resource: aft://session/claude/s-pat#e1\n"
        '    title: "Key rotation session"\n'
        f'    quote: "{PATTERN_SENTENCE}"\n'
        "aft:\n"
        f"  project: {project_id}\n"
        "---\n\n"
        "## Decisions\n\n"
        f"- Rotate the vault key {SECRET} before friday. [stated] 2026-10-01 [^s1]\n"
        "\n"
        "> [!conflict] 2026-10-01\n"
        f"> Rotate {SECRET} only after the release window closes.\n"
    )


def widget_project(cfg: Config) -> str:
    """A registered widget project, committed as a finished run would leave it."""
    registered = registry.Registry.load(cfg.registry_path)
    project = registered.add("widget", "Widget")
    registered.save()
    run_git(["add", "registry.yaml"], cfg.vault)
    run_git(["commit", "-q", "-m", "register widget"], cfg.vault)
    return project.id


def pattern_hits(cfg: Config, words: str) -> set[str]:
    """Every page the index answers to ``words`` with, the hidden collections included."""
    return {
        hit.path
        for hit in search.search(
            cfg, words, all_projects=True, archive=True, reports=True, limit=50
        )
    }


def point_at(cfg: Config, fake: FakeEndpoint, *, patterns: tuple[str, ...] = ()) -> Config:
    """A config.toml naming this endpoint, these redaction patterns, and this vault."""
    capture_block = (
        f"[capture]\nredact_patterns = {json.dumps(list(patterns))}\n" if patterns else ""
    )
    (cfg.home / configmod.CONFIG_NAME).write_text(
        f'vault = "{cfg.vault}"\nhost = "{cfg.host}"\n'
        f'[model]\nendpoint = "{fake.endpoint}"\nname = "fake-model"\n'
        f"{capture_block}",
        encoding="utf-8",
    )
    return configmod.load()


def test_forget_pattern_reaches_every_copy_the_vault_holds(initialized: Config, capsys):
    """The vault holds the archive's words in digest quotes and descriptions, in page sources,
    statements and notes, in superseded lines and in reports, and a pattern redaction reaches
    every one of them: `aft search` then answers to those words nowhere (DESIGN 12)."""
    cfg = initialized
    project_id = widget_project(cfg)
    evidence.store(
        cfg, CLAUDE, "s-pat", project_id, [Entry("e1", NOW, "user", PATTERN_SENTENCE, False)]
    )
    create_curated_page(
        cfg.vault,
        "digests/widget/2026/10/2026-10-01-claude-s-pat.md",
        pattern_digest(project_id),
    )
    create_curated_page(cfg.vault, "projects/widget/overview.md", pattern_page(project_id))
    create_curated_page(cfg.vault, "archive/projects/widget/overview.md", PATTERN_ARCHIVE)
    report_page(cfg, "reports/2026/10/2026-10-01.md", PATTERN_REPORT)
    index.update(cfg)

    # The searchable copies. The archive page holds prose rather than superseded fact lines, so
    # it indexes no row of its own and is checked on disk instead, below.

    planted = {
        "aft://session/claude/s-pat",
        "digests/widget/2026/10/2026-10-01-claude-s-pat.md",
        "projects/widget/overview.md",
        "reports/2026/10/2026-10-01.md",
    }

    assert pattern_hits(cfg, SECRET_WORD) == planted, "the planted copies are not searchable"

    code = cli.main(["forget", "pattern", SECRET_PATTERN])
    assert code == 0
    out = capsys.readouterr().out

    assert pattern_hits(cfg, SECRET_WORD) == set(), "the redacted words are still searchable"
    for path in cfg.vault.rglob("*.md"):
        if ".git" in path.parts:
            continue
        assert SECRET not in path.read_text(encoding="utf-8"), path
    for stored in evidence.entries(cfg, "s-pat"):
        assert SECRET not in stored.text
    assert lint.lint_vault(cfg.vault) == []
    assert run_git(["status", "--porcelain"], cfg.vault) == ""
    assert "aft: forget pattern" in run_git(["log", "-1", "--format=%s"], cfg.vault)
    assert "vault page(s)" in out, "the command counted the archive only"


def test_forget_pattern_leaves_a_page_it_would_break_alone_and_says_so(initialized: Config, capsys):
    """A pattern that would swallow a fact line's citation is refused on that page and named,
    rather than leaving the person a page `aft lint` refuses."""
    cfg = initialized
    project_id = widget_project(cfg)
    create_curated_page(cfg.vault, "projects/widget/overview.md", pattern_page(project_id))
    untouched = (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8")

    code = cli.main(["forget", "pattern", r"\[\^\w+\]|\[stated\]"])
    assert code == 0
    out = capsys.readouterr().out

    assert "1 page(s) would not read as itself redacted" in out
    assert "projects/widget/overview.md" in out
    assert (cfg.vault / "projects/widget/overview.md").read_text(encoding="utf-8") == untouched
    assert lint.lint_vault(cfg.vault) == []


def test_a_redigest_sends_none_of_what_a_later_pattern_marks_secret(initialized: Config):
    """A pattern added after the archive was written says that text is not to leave this host,
    so a rebuild reads the archive through today's patterns and none of the marked text reaches
    the model, while the rebuild is still charged to the day's budget (DESIGN 12, DESIGN 13)."""
    cfg = initialized
    project_id = widget_project(cfg)
    evidence.store(
        cfg, CLAUDE, "s-pat", project_id, [Entry("e1", NOW, "user", PATTERN_SENTENCE, False)]
    )

    fake = FakeEndpoint([digest_answer("rotate the vault key")])
    fake.start()
    try:
        cfg = point_at(cfg, fake, patterns=(SECRET_PATTERN,))
        result = digest.redigest(cfg, "s-pat")
        sent = json.dumps(fake.requests)
        charged = model.tokens_used_today(cfg)
    finally:
        fake.stop()

    assert SECRET not in sent, "the rebuild sent the marked text to the model"
    assert "[redacted]" in sent, "the model was shown the archive's text unredacted"
    page_text = (cfg.vault / result.path).read_text(encoding="utf-8")
    assert SECRET not in page_text
    assert charged > 0, "a rebuild spent nothing of the day's token budget"


def test_a_redigest_after_the_day_s_budget_is_spent_asks_nothing_of_the_model(
    initialized: Config,
):
    """Rebuilding a digest is digest work, so a day with no token budget left refuses it and the
    endpoint hears nothing (DESIGN 13)."""
    cfg = initialized
    project_id = widget_project(cfg)
    evidence.store(
        cfg, CLAUDE, "s-pat", project_id, [Entry("e1", NOW, "user", PATTERN_SENTENCE, False)]
    )

    fake = FakeEndpoint([digest_answer("rotate the vault key")])
    fake.start()
    try:
        cfg = with_budget(point_at(cfg, fake), 0)
        with pytest.raises(digest.DigestError):
            digest.redigest(cfg, "s-pat")
        asked = len(fake.requests)
    finally:
        fake.stop()

    assert asked == 0, "a rebuild spent past the day's token budget"
    assert digest_pages(cfg) == []


# --------------------------------------- what a redaction writes down, and what a report quotes


IMPORT_RESOURCE = "aft://library/claude-memory/widget/AGENTS.md"
IMPORT_QUOTE = f"rotate the key {SECRET} before friday"
IMPORT_QUOTE_REDACTED = "rotate the key [redacted] before friday"

REJECTED_STATEMENT = "Rotate the vault key by hand before every release."
KEPT_STATEMENT = "The hook writes one row per session end."


def session_digest(project_id: str) -> str:
    """A digest page for the forgotten session: one kept fact and one the rules refused.

    Neither statement is in the archive. A digest's sentences are the model's own, and the
    refused one reaches no memory page, so this page is the only place it is written down.
    """
    return (
        "---\n"
        "type: Session Digest\n"
        "title: Widget session 2026-10-01\n"
        "description: A session about the widget hook.\n"
        "sources:\n"
        "  - id: s1\n"
        f"    resource: aft://session/claude/{FORGOTTEN_SESSION}#e1\n"
        f'    quote: "{FORGOTTEN_PHRASE}"\n'
        "aft:\n"
        f"  project: {project_id}\n"
        "  facts:\n"
        f"    - statement: {KEPT_STATEMENT}\n"
        "      kind: decision\n"
        "      source: stated\n"
        "      date: '2026-10-01'\n"
        "      ref: s1\n"
        "  dropped:\n"
        f"    - statement: {REJECTED_STATEMENT}\n"
        "      kind: fact\n"
        "      source: inferred\n"
        "      entry: e1\n"
        "      reason: the quote is not found in entry e1\n"
        "---\n\n"
        "## Decisions\n\n"
        f"- {KEPT_STATEMENT} [stated] 2026-10-01 [^s1]\n"
    )


def digest_quoting_report() -> str:
    """A dream report that quotes the digest's kept line and names the fact it refused."""
    return f"""---
type: Dream Report
title: "2026-10-01"
---

# widget 12:00:00Z

## Added and Confirmed

### Added

- {KEPT_STATEMENT} [stated] (2026-10-01)
  Quote: "{FORGOTTEN_PHRASE}"

## Rejected

- Proposed add on "{REJECTED_STATEMENT}": refused by R1 (the quote is not found in entry e1)
"""


def test_forget_session_scrubs_the_digest_sentences_a_report_quoted(initialized: Config):
    """A dream report quotes the session's digest, including the facts the rules refused and
    the statements that never reached a memory page, so a forgetting reads those off the digest
    page before deleting it and scrubs them out of the report (DESIGN 12)."""
    cfg = initialized
    digest_rel = f"digests/widget/2026/10/2026-10-01-claude-{FORGOTTEN_SESSION}.md"
    project_id = widget_project(cfg)
    evidence.store(
        cfg,
        CLAUDE,
        FORGOTTEN_SESSION,
        project_id,
        [Entry("e1", NOW, "user", FORGOTTEN_PHRASE, False)],
    )
    create_curated_page(
        cfg.vault,
        digest_rel,
        session_digest(project_id),
    )
    report = report_page(cfg, "reports/2026/10/2026-10-01.md", digest_quoting_report())
    assert REJECTED_STATEMENT in report.read_text(encoding="utf-8")

    result = forget.forget_session(cfg, FORGOTTEN_SESSION)

    text = report.read_text(encoding="utf-8")
    assert REJECTED_STATEMENT not in text, "a refused digest fact is still quoted in the report"
    assert KEPT_STATEMENT not in text, "a digest statement that never reached a page remains"
    assert FORGOTTEN_PHRASE not in text
    assert text.count(FORGOTTEN_MARKER) == 3, text
    # The report's own reading of the refusal is the person's, and stays.
    assert "refused by R1 (the quote is not found in entry e1)" in text
    assert not (cfg.vault / digest_rel).exists()
    assert "reports/2026/10/2026-10-01.md" in result.reports_scrubbed
    assert run_git(["status", "--porcelain"], cfg.vault) == ""


def test_forget_pattern_commits_the_redaction_without_naming_the_pattern(initialized: Config):
    """A redaction pattern is often the secret it removes, so the commit that carries the
    redacted pages says what happened and names nothing: a forgetting cannot rewrite git
    history, and writes none of the pattern into it either (DESIGN 12)."""
    cfg = initialized
    project_id = widget_project(cfg)
    create_curated_page(cfg.vault, "projects/widget/overview.md", pattern_page(project_id))

    assert cli.main(["forget", "pattern", SECRET_PATTERN]) == 0

    history = run_git(["log", "--format=%B"], cfg.vault)
    assert run_git(["log", "-1", "--format=%s"], cfg.vault) == "aft: forget pattern"
    assert SECRET not in history, "the commit message carries the secret"
    assert SECRET_PATTERN not in history, "the commit message carries the pattern"


def test_forget_pattern_redacts_the_imported_quote_record_that_still_verifies(
    initialized: Config, capsys
):
    """An imported quote lives in the library record as well as on the page quoting it, so the
    redaction moves both: the secret leaves the record, and the redacted quote a page now shows
    still verifies against it (DESIGN 12)."""
    cfg = initialized
    propose.record_verified_quote(
        cfg.home, IMPORT_RESOURCE, IMPORT_QUOTE, "sha256:0f9e", "2026-10-01T00:00:00Z"
    )
    record = cfg.home / "library" / propose.VERIFIED_QUOTES_FILE
    assert propose.is_verified_import_quote(cfg.home, IMPORT_RESOURCE, IMPORT_QUOTE)

    assert cli.main(["forget", "pattern", SECRET_PATTERN]) == 0
    out = capsys.readouterr().out

    assert SECRET not in record.read_text(encoding="utf-8"), "the record still holds the secret"
    assert "imported quote(s)" in out
    assert propose.is_verified_import_quote(cfg.home, IMPORT_RESOURCE, IMPORT_QUOTE_REDACTED), (
        "the redacted quote no longer verifies against the redacted record"
    )
    assert not propose.is_verified_import_quote(cfg.home, IMPORT_RESOURCE, IMPORT_QUOTE), (
        "the unredacted words are still accepted as a verified quote"
    )
