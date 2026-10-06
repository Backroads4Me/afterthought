"""Tests for memory vault creation, configuration, and Obsidian settings (DESIGN 3)."""

from __future__ import annotations

import json

from aft import vault
from aft.gitutil import git
from conftest import run_git


def test_obsidian_graph_constant_structure():
    """OBSIDIAN_GRAPH defines forces, search parameters, and kind color groups."""
    assert vault.OBSIDIAN_GRAPH["collapse-filter"] is False
    assert vault.OBSIDIAN_GRAPH["search"] == ""
    assert vault.OBSIDIAN_GRAPH["local-search"] == ""
    assert vault.OBSIDIAN_GRAPH["local-backend"] == "current"
    assert vault.OBSIDIAN_GRAPH["showTags"] is True
    assert vault.OBSIDIAN_GRAPH["showAttachments"] is False
    assert vault.OBSIDIAN_GRAPH["hideUnresolved"] is False
    assert vault.OBSIDIAN_GRAPH["showOrphans"] is True
    assert vault.OBSIDIAN_GRAPH["showFilter"] is True
    assert vault.OBSIDIAN_GRAPH["linkStrength"] == 0.73
    assert vault.OBSIDIAN_GRAPH["linkDistance"] == 200
    assert vault.OBSIDIAN_GRAPH["repelStrength"] == 12.5
    assert vault.OBSIDIAN_GRAPH["centerStrength"] == 0.52

    groups = vault.OBSIDIAN_GRAPH["colorGroups"]
    assert len(groups) == 8
    assert groups[0] == {
        "query": 'tag:"#preference" or line:"[stated]"',
        "color": {"a": 1, "rgb": 16753920},
    }
    assert groups[1] == {
        "query": 'tag:"#decision" or section:"Decisions"',
        "color": {"a": 1, "rgb": 65330},
    }
    assert groups[2] == {
        "query": 'tag:"#state" or section:"State"',
        "color": {"a": 1, "rgb": 3394815},
    }
    assert groups[3] == {
        "query": 'tag:"#lesson" or section:"Lessons"',
        "color": {"a": 1, "rgb": 16724787},
    }
    assert groups[4] == {
        "query": 'tag:"#procedure" or section:"Procedures"',
        "color": {"a": 1, "rgb": 10040319},
    }
    assert groups[5] == {
        "query": 'tag:"#fact" or section:"Facts"',
        "color": {"a": 1, "rgb": 4474111},
    }
    assert groups[6] == {
        "query": 'path:"projects/"',
        "color": {"a": 1, "rgb": 16777215},
    }
    assert groups[7] == {
        "query": 'path:"digests/"',
        "color": {"a": 1, "rgb": 8947848},
    }


def test_gitignore_text_unignores_obsidian_graph():
    """gitignore_text always un-ignores .obsidian/graph.json."""
    for commit_library in (True, False):
        text = vault.gitignore_text(commit_library)
        assert "!.obsidian/graph.json\n" in text


def test_initial_files_includes_graph_json(cfg):
    """initial_files returns .obsidian/graph.json formatted as indented JSON."""
    files = vault.initial_files(cfg)
    assert ".obsidian/graph.json" in files
    content = files[".obsidian/graph.json"]
    assert content.endswith("\n")
    parsed = json.loads(content)
    assert parsed == vault.OBSIDIAN_GRAPH


def test_init_creates_obsidian_graph_json(cfg):
    """aft init creates .obsidian/graph.json with color groups and physics forces."""
    created = vault.init(cfg)
    assert ".obsidian/graph.json" in created

    graph_path = cfg.vault / ".obsidian" / "graph.json"
    assert graph_path.is_file()

    data = json.loads(graph_path.read_text(encoding="utf-8"))
    assert data["collapse-filter"] is False
    assert data["search"] == ""
    assert data["local-search"] == ""
    assert data["local-backend"] == "current"
    assert data["showTags"] is True
    assert data["showAttachments"] is False
    assert data["hideUnresolved"] is False
    assert data["showOrphans"] is True
    assert data["showFilter"] is True
    assert data["linkStrength"] == 0.73
    assert data["linkDistance"] == 200
    assert data["repelStrength"] == 12.5
    assert data["centerStrength"] == 0.52

    expected_color_groups = [
        {"query": 'tag:"#preference" or line:"[stated]"', "color": {"a": 1, "rgb": 16753920}},
        {"query": 'tag:"#decision" or section:"Decisions"', "color": {"a": 1, "rgb": 65330}},
        {"query": 'tag:"#state" or section:"State"', "color": {"a": 1, "rgb": 3394815}},
        {"query": 'tag:"#lesson" or section:"Lessons"', "color": {"a": 1, "rgb": 16724787}},
        {"query": 'tag:"#procedure" or section:"Procedures"', "color": {"a": 1, "rgb": 10040319}},
        {"query": 'tag:"#fact" or section:"Facts"', "color": {"a": 1, "rgb": 4474111}},
        {"query": 'path:"projects/"', "color": {"a": 1, "rgb": 16777215}},
        {"query": 'path:"digests/"', "color": {"a": 1, "rgb": 8947848}},
    ]
    assert data["colorGroups"] == expected_color_groups


def test_obsidian_graph_is_committed_and_not_ignored_by_git(cfg):
    """The graph configuration is tracked by git and un-ignored in .gitignore."""
    vault.init(cfg)

    # .gitignore un-ignores graph.json explicitly.
    gitignore = (cfg.vault / ".gitignore").read_text(encoding="utf-8")
    assert "!.obsidian/graph.json" in gitignore

    # git check-ignore returns returncode 1 (not ignored) for graph.json.
    check_ignore = git(["check-ignore", "-q", ".obsidian/graph.json"], cwd=cfg.vault, check=False)
    assert check_ignore.returncode != 0

    # The file is committed in the initial commit.
    committed_files = run_git(["ls-tree", "-r", "--name-only", "HEAD"], cwd=cfg.vault).splitlines()
    assert ".obsidian/graph.json" in committed_files

    # Working tree is clean after init.
    status = run_git(["status", "--porcelain"], cwd=cfg.vault)
    assert status == ""


def test_init_preserves_customized_graph_json(cfg):
    """A subsequent vault.init does not overwrite an existing graph.json."""
    vault.init(cfg)
    graph_path = cfg.vault / ".obsidian" / "graph.json"
    custom_content = '{\n  "custom": true\n}\n'
    graph_path.write_text(custom_content, encoding="utf-8")

    created = vault.init(cfg)
    assert ".obsidian/graph.json" not in created
    assert graph_path.read_text(encoding="utf-8") == custom_content
