"""Forget and redigest tests: removing traces, byte-level erasure, and redigesting from evidence.

DESIGN 12 promises that forgotten content leaves everything afterthought controls.
Tests prove at the byte level that forgotten words are in none of the evidence archive,
the search index, their write-ahead logs, or the vault working tree.
"""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aft import cli, evidence, index, pagewrite, registry
from aft import config as configmod
from aft.config import Config
from aft.readers import Entry
from conftest import run_git
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
            '    quote: "something"\n'
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


def test_forget_pattern_redacts_archive_and_byte_level_erasure(initialized: Config, capsys):
    """Forget pattern redacts matching text across the archive and updates the index,
    printing the redact_patterns line for Ted to add."""
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
