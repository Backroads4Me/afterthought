"""The seed script writes a page that lints clean, and its quotes are verbatim (M1.5)."""

from __future__ import annotations

import importlib.util
import sys
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


def run(seed, vault: Path) -> int:
    return seed.main([str(vault)])


def page_of(cfg, seed) -> pagemod.Page:
    text = (cfg.vault / seed.REL_PATH).read_text()
    return pagemod.parse(text, Path(seed.REL_PATH))


def source_ids(page: pagemod.Page) -> dict[str, dict]:
    sources = (page.frontmatter or {}).get("sources") or []
    return {str(s["id"]): s for s in sources}


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
        assert heading in sections, source["resource"]
        assert source["quote"] in sections[heading]


def test_facts_are_observed_and_each_has_a_source(registered, seed):
    run(seed, registered.vault)
    page = page_of(registered, seed)
    assert page.errors == []
    assert 10 <= len(page.facts) <= 20
    assert {fact.source for fact in page.facts} == {"observed"}
    assert all(fact.refs for fact in page.facts)
    assert len(source_ids(page)) == len(page.facts)


def test_reruns_write_the_same_page_and_commit_nothing(registered, seed):
    assert run(seed, registered.vault) == 0
    first = run_git(["log", "--format=%s"], registered.vault)
    text = (registered.vault / seed.REL_PATH).read_text()
    assert run(seed, registered.vault) == 0
    assert (registered.vault / seed.REL_PATH).read_text() == text
    assert run_git(["log", "--format=%s"], registered.vault) == first
    assert run_git(["status", "--porcelain"], registered.vault) == ""


def test_an_unregistered_project_writes_nothing(initialized, seed, capsys):
    assert run(seed, initialized.vault) == 2
    assert not (initialized.vault / seed.REL_PATH).exists()
    assert "aft project add" in capsys.readouterr().err


def test_a_missing_vault_is_refused(seed, tmp_path, capsys):
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
