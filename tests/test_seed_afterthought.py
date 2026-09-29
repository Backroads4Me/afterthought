"""The seed writes a page that lints clean with verbatim quotes (M1.5), and an overwrite
refuses any page a person curated afterwards (F1)."""

from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from aft import lint, vault
from aft import page as pagemod
from aft.registry import Registry
from conftest import run_git

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN = REPO_ROOT / "planning" / "DESIGN.md"
SCRIPT = REPO_ROOT / "scripts" / "seed_afterthought.py"


@pytest.fixture(scope="module")
def seed():
    spec = importlib.util.spec_from_file_location("seed_afterthought", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def registered(initialized):
    """A vault with the afterthought project registered, as 6.1 does before seeding."""
    registry = Registry.load(initialized.registry_path)
    registry.add("afterthought")
    registry.save()
    vault.commit(initialized.vault, ["registry.yaml"], "aft: register afterthought")
    return initialized


def run(seed, vault: Path, *flags: str) -> int:
    return seed.main([*flags, str(vault)])


def page_of(cfg, seed) -> pagemod.Page:
    text = (cfg.vault / seed.REL_PATH).read_text()
    return pagemod.parse(text, Path(seed.REL_PATH))


def source_ids(page: pagemod.Page) -> dict[str, dict]:
    sources = (page.frontmatter or {}).get("sources") or []
    return {str(s["id"]): s for s in sources}


def seeded_line(seed, index: int) -> str:
    """The exact line a fresh run writes for one fact, source id and today's date included."""
    fact = seed.FACTS[index]
    return f"- {fact.statement} [observed] {date.today().isoformat()} [^s{index + 1}]"


def refuses(registered, seed, capsys, edit, reason: str) -> None:
    """Seed a page, make one curated edit to it, and check that ``--overwrite`` refuses that edit.

    The refused run has to leave the curated text, the working tree and the history untouched:
    once a person edits the page, their text is the memory and the seed's is only a draft of
    it, so naming the edit is all the run may do (BUILD-PLAN F1).
    """
    assert run(seed, registered.vault) == 0
    path = registered.vault / seed.REL_PATH
    text = path.read_text()
    curated = edit(text)
    assert curated != text, "the edit has to change the page the seed wrote"
    path.write_text(curated)
    vault.commit(registered.vault, [seed.REL_PATH], "aft: curate the seeded page")
    history = run_git(["log", "--format=%s"], registered.vault)
    assert run(seed, registered.vault, "--overwrite") == 2
    err = capsys.readouterr().err
    assert reason in err
    assert str(path) in err
    assert path.read_text() == curated
    assert run_git(["status", "--porcelain"], registered.vault) == ""
    assert run_git(["log", "--format=%s"], registered.vault) == history


def test_seeded_page_lints_clean(registered, seed):
    assert run(seed, registered.vault) == 0
    assert lint.lint_vault(registered.vault) == []


def test_every_quote_appears_in_the_design(registered, seed):
    run(seed, registered.vault)
    design = DESIGN.read_text()
    for source in source_ids(page_of(registered, seed)).values():
        assert source["quote"] in design


def test_every_quote_sits_under_its_cited_heading(registered, seed):
    run(seed, registered.vault)
    sections = seed.sections(DESIGN.read_text())
    for source in source_ids(page_of(registered, seed)).values():
        resource, _, heading = source["resource"].partition("#")
        assert resource == seed.DESIGN_RESOURCE
        bodies = sections[heading]
        assert any(source["quote"] in body for body in bodies), source["resource"]


def test_facts_are_observed_and_each_has_a_source(registered, seed):
    run(seed, registered.vault)
    page = page_of(registered, seed)
    assert page.errors == []
    assert 10 <= len(page.facts) <= 20
    assert {fact.source for fact in page.facts} == {"observed"}
    assert all(fact.refs for fact in page.facts)
    assert len(source_ids(page)) == len(page.facts)


def test_a_page_with_no_earlier_dates_seeds_every_fact_today(registered, seed):
    assert run(seed, registered.vault) == 0
    assert {fact.date for fact in page_of(registered, seed).facts} == {date.today().isoformat()}


def test_a_rerun_refuses_to_replace_the_page(registered, seed, capsys):
    assert run(seed, registered.vault) == 0
    first = run_git(["log", "--format=%s"], registered.vault)
    text = (registered.vault / seed.REL_PATH).read_text()
    assert run(seed, registered.vault) == 2
    assert (registered.vault / seed.REL_PATH).read_text() == text
    assert run_git(["log", "--format=%s"], registered.vault) == first
    assert run_git(["status", "--porcelain"], registered.vault) == ""
    assert "--overwrite" in capsys.readouterr().err


def test_overwrite_keeps_the_dates_of_unchanged_facts(registered, seed):
    assert run(seed, registered.vault) == 0
    path = registered.vault / seed.REL_PATH
    past = (date.today() - timedelta(days=30)).isoformat()
    aged = re.sub(r"\[observed\] \d{4}-\d{2}-\d{2}", f"[observed] {past}", path.read_text())
    assert aged != path.read_text()
    path.write_text(aged)
    vault.commit(registered.vault, [seed.REL_PATH], "aft: age the seeded facts")
    first = run_git(["log", "--format=%s"], registered.vault)
    assert run(seed, registered.vault, "--overwrite") == 0
    assert path.read_text() == aged
    assert run_git(["log", "--format=%s"], registered.vault) == first


def test_overwrite_refuses_a_reworded_fact(registered, seed, capsys):
    def reword(text: str) -> str:
        return text.replace(
            f"- {seed.FACTS[0].statement} [observed]",
            f"- {seed.FACTS[0].statement} (as Ted reworded it) [observed]",
            1,
        )

    refuses(registered, seed, capsys, reword, "wording the seed never wrote")


def test_overwrite_refuses_a_hand_written_fact(registered, seed, capsys):
    def written(text: str) -> str:
        return text + "- Renew the TLS certificate before winter [observed] 2026-09-01 [^s1]\n"

    refuses(registered, seed, capsys, written, "wording the seed never wrote")


def test_overwrite_refuses_a_fact_moved_to_another_section(registered, seed, capsys):
    def moved(text: str) -> str:
        line = seeded_line(seed, 0)
        return text.replace(f"{line}\n", "", 1) + f"{line}\n"

    refuses(registered, seed, capsys, moved, "files the seed's fact under 'Facts'")


def test_overwrite_refuses_a_seeded_fact_the_page_drops(registered, seed, capsys):
    def dropped(text: str) -> str:
        return text.replace(f"{seeded_line(seed, 0)}\n", "", 1)

    refuses(registered, seed, capsys, dropped, "drops the fact the seed cites as [^s1]")


def test_overwrite_refuses_a_seeded_fact_written_twice(registered, seed, capsys):
    def twice(text: str) -> str:
        return text + text.rstrip("\n").split("\n")[-1] + "\n"

    refuses(registered, seed, capsys, twice, "repeats the fact the seed holds at line")


def test_overwrite_refuses_a_different_source_tag(registered, seed, capsys):
    def stated(text: str) -> str:
        line = seeded_line(seed, 0)
        return text.replace(line, line.replace(" [observed] ", " [stated] "), 1)

    refuses(registered, seed, capsys, stated, "is tagged [stated]")


def test_overwrite_refuses_a_candidate_on_the_page(registered, seed, capsys):
    def candidate(text: str) -> str:
        line = (
            "- A candidate the seed never proposed [observed] 2026-09-01 [^s1]"
            " (signals 1/3, sessions 1/2)"
        )
        return text + f"\n## Candidates\n\n{line}\n"

    refuses(registered, seed, capsys, candidate, "holds a candidate")


def test_overwrite_refuses_a_stale_mark_on_a_seeded_fact(registered, seed, capsys):
    def staled(text: str) -> str:
        return text.replace(" [^s1]\n", " [^s1] (stale 2026-09-01: the design moved)\n", 1)

    refuses(registered, seed, capsys, staled, "is marked stale")


def test_overwrite_refuses_a_superseded_seeded_fact(registered, seed, capsys):
    def superseded(text: str) -> str:
        marked = (
            f"- ~~{seed.FACTS[0].statement}~~ [observed] {date.today().isoformat()} [^s1]"
            " superseded 2026-09-02 by [^s2]"
        )
        return text.replace(seeded_line(seed, 0), marked, 1)

    refuses(registered, seed, capsys, superseded, "is marked superseded")


def test_overwrite_refuses_a_conflict_note_on_a_seeded_fact(registered, seed, capsys):
    def disputed(text: str) -> str:
        note = "> [!conflict] 2026-09-03\n> it disagrees with the design\n"
        return text.replace(" [^s1]\n", f" [^s1]\n{note}", 1)

    refuses(registered, seed, capsys, disputed, "is marked conflict")


def test_overwrite_refuses_an_extra_frontmatter_key(registered, seed, capsys):
    def owned(text: str) -> str:
        return text.replace("type: Project\n", "type: Project\nowner: ted\n", 1)

    refuses(registered, seed, capsys, owned, "adds the frontmatter key 'owner'")


def test_overwrite_refuses_an_extra_tag(registered, seed, capsys):
    def tagged(text: str) -> str:
        return text.replace("- agents\n", "- agents\n- urgent\n", 1)

    refuses(registered, seed, capsys, tagged, "adds the tag 'urgent'")


def test_overwrite_refuses_an_anchor_under_aft(registered, seed, capsys):
    def anchored(text: str) -> str:
        anchor = (
            "aft:\n  anchors:\n    s1:\n      path: src/aft/cli.py\n"
            "      all_of: [aft inject]\n      commit: 6810f79\n  project: "
        )
        return text.replace("aft:\n  project: ", anchor, 1)

    refuses(registered, seed, capsys, anchored, "adds aft.anchors")


def test_overwrite_refuses_a_source_the_page_rewrites(registered, seed, capsys):
    def rewritten(text: str) -> str:
        quote = f"  quote: {seed.FACTS[0].quote}"
        return text.replace(quote, f"{quote} (tightened by Ted)", 1)

    refuses(registered, seed, capsys, rewritten, "rewrites source s1's quote")


def test_overwrite_refuses_a_date_the_seed_cannot_read(registered, seed, capsys):
    def mistyped(text: str) -> str:
        return text.replace(f" [observed] {date.today().isoformat()}", " [observed] 2026-13-45", 1)

    refuses(registered, seed, capsys, mistyped, "invalid date 2026-13-45")


def test_overwrite_refuses_a_page_whose_frontmatter_it_cannot_read(registered, seed, capsys):
    assert run(seed, registered.vault) == 0
    path = registered.vault / seed.REL_PATH
    text = path.read_text()
    broken = text.replace("type: Project\n", "type: Project\n  bad: [unclosed\n", 1)
    assert broken != text
    path.write_text(broken)
    vault.commit(registered.vault, [seed.REL_PATH], "aft: break the frontmatter")
    assert run(seed, registered.vault, "--overwrite") == 2
    assert str(path) in capsys.readouterr().err
    assert path.read_text() == broken
    assert run_git(["status", "--porcelain"], registered.vault) == ""


def test_overwrite_refuses_a_page_whose_source_lost_its_quote(registered, seed, capsys):
    assert run(seed, registered.vault) == 0
    path = registered.vault / seed.REL_PATH
    text = path.read_text()
    broken = re.sub(r"^  quote:.*\n", "", text, count=1, flags=re.MULTILINE)
    assert broken != text
    path.write_text(broken)
    vault.commit(registered.vault, [seed.REL_PATH], "aft: drop a source quote")
    assert run(seed, registered.vault, "--overwrite") == 2
    assert "s1" in capsys.readouterr().err
    assert path.read_text() == broken


def test_overwrite_refuses_a_page_whose_source_quote_is_blank(registered, seed, capsys):
    assert run(seed, registered.vault) == 0
    path = registered.vault / seed.REL_PATH
    text = path.read_text()
    for value in ("quote:", 'quote: ""'):
        broken = re.sub(r"^  quote: .*\n", f"  {value}\n", text, count=1, flags=re.MULTILINE)
        assert broken != text
        path.write_text(broken)
        assert run(seed, registered.vault, "--overwrite") == 2
        assert "s1" in capsys.readouterr().err
    assert path.read_text() == broken


def test_overwrite_refuses_a_line_the_parser_rejects(registered, seed, capsys):
    def untagged(text: str) -> str:
        return text.replace(" [^s1]\n", "\n", 1)

    refuses(registered, seed, capsys, untagged, "fact line must read")


def test_an_unregistered_project_writes_nothing(initialized, seed, capsys):
    assert run(seed, initialized.vault) == 2
    assert not (initialized.vault / seed.REL_PATH).exists()
    assert "aft project add" in capsys.readouterr().err


def test_a_page_that_does_not_lint_is_never_written(registered, seed, monkeypatch, capsys):
    monkeypatch.setattr(seed, "render", lambda *args: "not a page\n")
    assert run(seed, registered.vault) == 2
    assert not (registered.vault / seed.REL_PATH).exists()
    assert run_git(["status", "--porcelain"], registered.vault) == ""
    assert "does not lint" in capsys.readouterr().err


def test_a_missing_vault_is_refused(seed, home, tmp_path, capsys):
    assert run(seed, tmp_path / "no-vault") == 2
    assert "aft init" in capsys.readouterr().err


def test_a_quote_the_design_no_longer_has_fails_the_seed(seed):
    stale = seed.SeedFact("Facts", "a statement", "Kinds", "words the design never said")
    with pytest.raises(seed.SeedError):
        seed.check_quotes((stale,), DESIGN.read_text())


def test_a_quote_copied_from_another_section_fails_the_seed(seed):
    moved = seed.SeedFact(
        "Facts",
        "a statement",
        "Kinds",
        "Reports describe memory; they are never memory.",
    )
    with pytest.raises(seed.SeedError):
        seed.check_quotes((moved,), DESIGN.read_text())


def test_a_quote_may_span_several_lines(seed):
    design = "# Kinds\n\nfirst line\nsecond line\n"
    fact = seed.SeedFact("Facts", "a statement", "Kinds", "first line\nsecond line")
    seed.check_quotes((fact,), design)


def test_a_quote_of_a_repeated_non_adjacent_line_fails_the_seed(seed):
    design = "# Kinds\n\nfirst line\nother line\nfirst line\n"
    repeated = seed.SeedFact("Facts", "a statement", "Kinds", "first line\nfirst line")
    with pytest.raises(seed.SeedError):
        seed.check_quotes((repeated,), design)


def test_a_heading_inside_a_code_block_is_not_part_of_the_design(seed):
    design = "# Page format\n\nreal\n\n```markdown\n## Decisions\n\n- fake\n```\n\nstill real\n"
    seed.check_quotes((seed.SeedFact("Facts", "a statement", "Page format", "still real"),), design)
    fenced = seed.SeedFact("Facts", "a statement", "Decisions", "fake")
    with pytest.raises(seed.SeedError):
        seed.check_quotes((fenced,), design)


def test_a_quote_under_one_of_two_headings_of_the_same_name_is_accepted(seed):
    design = "# Kinds\n\nthe real quote\n\n# Kinds\n\nother wording\n"
    quoted = seed.SeedFact("Facts", "a statement", "Kinds", "the real quote")
    seed.check_quotes((quoted,), design)
    absent = seed.SeedFact("Facts", "a statement", "Kinds", "a line neither section has")
    with pytest.raises(seed.SeedError):
        seed.check_quotes((absent,), design)


def test_a_fact_landing_outside_the_page_kinds_is_refused(seed):
    misplaced = seed.SeedFact("Decision", "a statement", "Kinds", "shared line")
    with pytest.raises(seed.SeedError):
        seed.check_quotes((misplaced,), "# Kinds\n\nshared line\n")
