"""`aft import claude-memory`: memory another tool wrote, arriving as claims to be judged.

The fixture holds a fake `HOME` in a temporary directory, because reading the real
`~/.claude` would make a test depend on whatever this machine's agent happened to remember.
Two projects have memory: one registered, so it imports, and one not, so it is reported and
left alone.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from aft import claude_memory, lint
from aft import config as configmod
from aft.cli import main
from aft.config import Config
from aft.dream.apply import _lookup_evidence
from aft.dream.schedule import waiting_digests
from aft.registry import Registry, path_key
from conftest import make_repo

HOST = "testhost"

# Written the way Claude Code writes them: frontmatter it uses for its own index, then prose
# with the emphasis and list markers an agent typed rather than the ones a fact line wants.
STORAGE = """\
---
name: storage-facts
description: How widget storage works.
metadata:
  node_type: memory
  type: project
---

## Storage

- Storage is SQLite, with a write-ahead log.
- The daemon listens on 8123.
  - A nested note about the port.
* **Why:** the log keeps writes durable across a crash.
1. The archive lives under /var/lib/widget.
"""

# A heading is a section's name rather than a claim, and struck text is what the line grammar
# has no room for: both belong in the digest's dropped record, not among its facts.
ODD = """\
---
name: odd-lines
---

## Notes

- ~~The widget used to be square.~~
- The widget is round.
"""

UNREGISTERED = """\
---
name: unregistered
---

## Notes

- The unibrow ships in one size.
"""

WHEN = time.time() - 86_400 * 16


def projects_file(root: Path, project_path: Path, name: str, text: str) -> Path:
    """Put one memory file under a directory laid out like `~/.claude/projects`."""
    path = root / claude_memory.encoding_of(project_path) / "memory" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (WHEN, WHEN))
    return path


def memory_file(home: Path, project_path: Path, name: str, text: str) -> Path:
    """Put one memory file where Claude Code would have put it under that HOME."""
    return projects_file(home / ".claude/projects", project_path, name, text)


@pytest.fixture
def memory(initialized: Config, tmp_path: Path, monkeypatch) -> Config:
    """A registered project with memory, an unregistered one with memory, and no real HOME."""
    registry = Registry.load(initialized.registry_path)
    widget = registry.add("widget", "Widget")
    # A registered path that exists and is a repository, because capture's exclusion asks the
    # resolver which project a directory belongs to, and a directory git will not read is not
    # a project any setting can name.
    registry.add_path(widget, path_key(HOST, make_repo(tmp_path / "widget")))
    registry.save()

    home = tmp_path / "fakehome"
    memory_file(home, tmp_path / "widget", "MEMORY.md", STORAGE)
    memory_file(home, tmp_path / "widget", "odd.md", ODD)
    memory_file(home, tmp_path / "unibrow", "notes.md", UNREGISTERED)
    monkeypatch.setenv("HOME", str(home))
    return initialized


def digests(config: Config) -> list[Path]:
    return sorted((config.vault / "digests").rglob("*.md"))


def listing(config: Config) -> list[str]:
    return sorted(
        path.relative_to(config.vault).as_posix()
        for path in config.vault.rglob("*")
        if ".git" not in path.parts
    )


def test_one_digest_per_memory_file_lands_under_its_project(memory: Config):
    assert claude_memory.run(memory) == 0
    written = digests(memory)

    assert len(written) == 2
    slugs = {path.relative_to(memory.vault).parts[1] for path in written}
    assert slugs == {"widget"}
    assert all("import" in path.name for path in written)


def test_each_line_becomes_one_inferred_fact_quoted_from_the_file(memory: Config):
    claude_memory.run(memory)
    text = next(path for path in digests(memory) if "MEMORY" in path.name).read_text()

    assert "- Storage is SQLite, with a write-ahead log. [inferred] " in text
    assert "- A nested note about the port. [inferred] " in text
    # A list marker is how markdown marks a list item, not part of the claim, so the fact
    # line carries the sentence; the source quote carries the line as the file holds it.
    assert 'quote: "- The daemon listens on 8123."' in text
    assert "[stated]" not in text
    assert text.count("[inferred]") == 5


def test_a_heading_and_struck_text_are_dropped_with_a_reason_rather_than_imported(
    memory: Config,
):
    claude_memory.run(memory)
    text = next(path for path in digests(memory) if "odd" in path.name).read_text()

    assert "- The widget is round. [inferred] " in text
    assert "~~The widget used to be square.~~" in text
    assert "- ~~The widget used to be square.~~ [inferred]" not in text
    assert "a heading names a section" in text
    assert "struck" in text


def test_an_unregistered_directory_is_reported_and_left_alone(memory: Config, capsys):
    assert claude_memory.run(memory) == 0

    out = capsys.readouterr().out
    assert "unibrow" in out
    assert "aft project add" in out
    assert not (memory.vault / "digests/unibrow").exists()


def test_memory_for_no_registered_project_is_reported_as_nothing_to_import(
    memory: Config, tmp_path: Path, monkeypatch, capsys
):
    home = tmp_path / "quiet"
    memory_file(home, tmp_path / "unibrow", "notes.md", UNREGISTERED)
    monkeypatch.setenv("HOME", str(home))

    assert claude_memory.run(memory) == 1
    assert "no Claude Code memory" in capsys.readouterr().err


def test_the_project_filter_imports_one_project_and_no_other(memory: Config):
    assert claude_memory.run(memory, project="widget") == 0
    assert len(digests(memory)) == 2

    registry = Registry.load(memory.registry_path)
    assert registry.by_slug("unibrow") is None


def test_a_second_run_writes_nothing_and_leaves_the_vault_as_it_found_it(memory: Config, capsys):
    claude_memory.run(memory)
    before = listing(memory)
    capsys.readouterr()

    assert claude_memory.run(memory) == 0

    assert listing(memory) == before
    assert "nothing changed" in capsys.readouterr().out


def test_a_changed_file_becomes_a_new_digest_rather_than_editing_the_first(memory: Config):
    claude_memory.run(memory)
    first = digests(memory)
    path = memory_file(
        Path(os.environ["HOME"]),
        memory_path_project(memory),
        "MEMORY.md",
        STORAGE + "\n- The port moved to 8124.\n",
    )
    os.utime(path, (WHEN + 86_400, WHEN + 86_400))

    assert claude_memory.run(memory) == 0
    after = digests(memory)

    assert len(after) == 3
    assert set(first) <= set(after)


def memory_path_project(config: Config) -> Path:
    registry = Registry.load(config.registry_path)
    key = registry.by_slug("widget").paths[0]
    return Path(key.split(":", 1)[1])


def test_an_imported_quote_verifies_the_way_any_quoted_fact_does(memory: Config):
    """The whole point of the record: R1 still has something to look the quote up against."""
    claude_memory.run(memory)
    candidate = claude_memory.plans(memory)[0][0]
    quote = next(q for statement, q in candidate.lines if "SQLite" in statement)

    verified, _, _, yours = _lookup_evidence(memory, candidate.resource, quote)

    assert verified
    # Imported text is nobody's own words: it must not gain the standing a person's words have.
    assert not yours


def test_a_quote_the_file_never_held_does_not_verify(memory: Config):
    claude_memory.run(memory)
    candidate = claude_memory.plans(memory)[0][0]

    assert not _lookup_evidence(memory, candidate.resource, "the daemon listens on 9999.")[0]


def test_the_import_leaves_the_evidence_archive_untouched(memory: Config):
    """An archive row would fake a conversation and lend imported text a speaker it lacks."""
    claude_memory.run(memory)

    assert not (memory.home / "evidence.sqlite").exists()


def test_a_lost_record_is_rebuilt_by_a_rerun_so_memory_keeps_its_proof(
    memory: Config,
):
    """The record lives in the home directory, which is not under git like the vault is."""
    claude_memory.run(memory)
    candidate = claude_memory.plans(memory)[0][0]
    quote = next(q for statement, q in candidate.lines if "SQLite" in statement)
    (memory.home / "library/verified_quotes.json").unlink()
    assert not _lookup_evidence(memory, candidate.resource, quote)[0]

    assert claude_memory.run(memory) == 0

    assert _lookup_evidence(memory, candidate.resource, quote)[0]


def test_a_dry_run_says_what_it_would_do_and_writes_nothing(memory: Config, capsys):
    before = listing(memory)

    assert claude_memory.run(memory, dry_run=True) == 0
    out = capsys.readouterr().out

    assert listing(memory) == before
    assert "digests/widget" in out
    assert not (memory.home / "library/verified_quotes.json").exists()


def test_the_command_takes_its_memory_from_the_home_it_is_given(
    memory: Config, tmp_path: Path, monkeypatch
):
    """`--from` means a test or a mounted copy can be imported without reading any HOME."""
    source = tmp_path / "elsewhere"
    projects_file(source, tmp_path / "widget", "ports.md", "- The daemon listens on 8123.\n")
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))

    assert main(["import", "claude-memory", "--from", str(source)]) == 0

    assert len(digests(memory)) == 1


def test_the_command_exits_two_when_the_memory_it_was_pointed_at_is_not_there(
    memory: Config, tmp_path: Path
):
    assert main(["import", "claude-memory", "--from", str(tmp_path / "nope")]) == 2


def test_the_command_refuses_a_project_that_is_not_registered(memory: Config, tmp_path: Path):
    source = tmp_path / "src"
    projects_file(source, tmp_path / "widget", "ports.md", "- The daemon listens on 8123.\n")

    assert main(["import", "claude-memory", "--from", str(source), "--project", "unibrow"]) == 2


def test_the_digest_says_what_it_was_imported_from(memory: Config):
    claude_memory.run(memory)
    text = next(path for path in digests(memory) if "odd" in path.name).read_text()

    assert "aft://library/claude-memory/widget/odd.md" in text
    assert "type: Session Digest" in text


def test_the_imported_digests_pass_lint(memory: Config):
    """A page `aft lint` would flag is a page the dream refuses to read."""
    claude_memory.run(memory)

    assert lint.lint_vault(memory.vault) == []


def test_the_dream_finds_the_imported_digests_waiting(memory: Config):
    """Writing a digest only matters if the run that judges it picks it up."""
    claude_memory.run(memory)

    waiting = waiting_digests(memory.vault)

    assert len(waiting["widget"]) == 2


def with_capture(
    config: Config,
    exclude: tuple[str, ...] = (),
    patterns: tuple[str, ...] = (),
    projects: tuple[str, ...] = (),
    builtin_redaction: bool = True,
):
    """The config again with capture settings written into it, the way an operator sets them."""
    lines = [
        f'vault = "{config.vault}"',
        'host = "testhost"',
        "[capture]",
        f"builtin_redaction = {'true' if builtin_redaction else 'false'}",
    ]
    settings = (
        ("exclude_paths", exclude),
        ("exclude_projects", projects),
        ("redact_patterns", patterns),
    )
    for name, values in settings:
        listed = ", ".join(f'"{value}"' for value in values)
        lines.append(f"{name} = [{listed}]")
    (config.home / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return configmod.load()


# A token the built-in patterns know, a password-style assignment, and a secret only the
# configured pattern knows: an import that copied a memory line verbatim would carry all three
# into the vault, into the digest's own frontmatter, and into the quote record. With shape-only
# redaction, the password assignment survives while the token and custom pattern are redacted.
SECRET = """\
---
name: deploy-notes
---

## Deploy

- The staging key is sk-3f3f3f3f3f3f3f3f.
- vault_password: "hunter2hunter2"
- The oddball secret is aardvark-42.
"""


def test_a_secret_in_a_memory_file_reaches_neither_the_vault_nor_the_record(
    memory: Config,
):
    """Capture redacts before archiving for a reason; an import reading the same text has it too."""
    config = with_capture(memory, patterns=("aardvark-42",))
    memory_file(Path(os.environ["HOME"]), memory_path_project(config), "secrets.md", SECRET)

    assert claude_memory.run(config) == 0

    text = next(path for path in digests(config) if "secrets" in path.name).read_text()
    record = json.dumps(json.loads((config.home / "library/verified_quotes.json").read_text()))
    assert "sk-3f3f3f3f3f3f3f3f" not in text
    assert "hunter2hunter2" in text
    assert "aardvark-42" not in text
    assert "[redacted]" in text
    assert "sk-3f3f3f3f3f3f3f3f" not in record
    assert "aardvark-42" not in record

    # The quote verifies because what was recorded is the redacted line the digest holds.
    candidate = next(c for c in claude_memory.plans(config)[0] if c.path.name == "secrets.md")
    quote = next(q for _, q in candidate.lines if "staging key" in q)
    assert _lookup_evidence(config, candidate.resource, quote)[0]


def test_claude_memory_import_with_builtin_redaction_false(memory: Config):
    """When capture.builtin_redaction is false, built-in shapes survive and custom patterns
    still redact."""
    config = with_capture(memory, patterns=("aardvark-42",), builtin_redaction=False)
    memory_file(Path(os.environ["HOME"]), memory_path_project(config), "secrets.md", SECRET)

    assert claude_memory.run(config) == 0

    text = next(path for path in digests(config) if "secrets" in path.name).read_text()
    assert "sk-3f3f3f3f3f3f3f3f" in text
    assert "hunter2hunter2" in text
    assert "aardvark-42" not in text
    assert "[redacted]" in text


def test_a_project_under_capture_exclude_paths_is_never_read(memory: Config):
    """A project whose sessions capture must not read has memory capture must not import."""
    config = with_capture(memory, exclude=(str(memory_path_project(memory)),))

    assert claude_memory.run(config) == 1
    assert digests(config) == []


def test_a_project_named_in_capture_exclude_projects_is_never_read(memory: Config):
    """Naming the project rather than a directory also covers its other clones and worktrees."""
    config = with_capture(memory, projects=("widget",))

    assert claude_memory.run(config) == 1
    assert digests(config) == []


# Characters a reader never sees: a zero-width space in one line, and a Unicode tag block
# spelling "Ignore" in another. Copied verbatim, either would ride from a memory file an
# agent wrote into a digest, its quote record, and the memory the dream builds from them;
# sanitized, the tag block is gone whole, since what it hides is not text a reader held.
INVISIBLE = """\
---
name: invisible-lines
---

## Notes

- The daemon listens\u200b on 8123.
- The tag block says\U000e0049\U000e0067\U000e006e\U000e006f\U000e0072\U000e0065 nothing.
"""


def test_an_imported_memory_with_invisible_and_tag_characters_is_sanitized(memory: Config):
    memory_file(Path(os.environ["HOME"]), memory_path_project(memory), "invisible.md", INVISIBLE)

    assert claude_memory.run(memory) == 0

    text = next(path for path in digests(memory) if "invisible" in path.name).read_text()
    assert "- The daemon listens on 8123. [inferred] " in text
    assert "- The tag block says nothing. [inferred] " in text
    assert "\u200b" not in text
    assert "\U000e0049" not in text
