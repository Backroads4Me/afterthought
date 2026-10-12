"""Tests for memory vault creation, configuration, and Obsidian settings (DESIGN 3)."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from aft import gitutil, vault
from aft.gitutil import clear_stale_index_lock, git
from conftest import make_repo, run_git


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


def test_clear_stale_index_lock_removes_old_lock_and_leaves_fresh(tmp_path):
    """clear_stale_index_lock clears locks older than max_age_seconds and keeps fresh ones."""
    repo = make_repo(tmp_path / "repo")
    lock_file = repo / ".git" / "index.lock"

    # A fresh lock file must not be removed
    lock_file.write_text("locked", encoding="utf-8")
    assert lock_file.is_file()
    assert clear_stale_index_lock(repo, max_age_seconds=30.0) is False
    assert lock_file.is_file()

    # A stale lock file past max_age_seconds must be removed
    past_time = time.time() - 60.0
    os.utime(lock_file, (past_time, past_time))
    assert clear_stale_index_lock(repo, max_age_seconds=30.0) is True
    assert not lock_file.exists()

    # Worktree support: verify cleanup in a linked worktree
    wt_path = tmp_path / "worktree"
    run_git(["worktree", "add", str(wt_path), "-b", "wt-branch"], repo)
    wt_git_file = wt_path / ".git"
    assert wt_git_file.is_file()

    wt_raw = wt_git_file.read_text(encoding="utf-8").strip().removeprefix("gitdir:").strip()
    wt_git_dir = Path(wt_raw)
    wt_lock = wt_git_dir / "index.lock"

    # Fresh in worktree
    wt_lock.write_text("locked", encoding="utf-8")
    assert clear_stale_index_lock(wt_path, max_age_seconds=30.0) is False
    assert wt_lock.is_file()

    # Stale in worktree
    os.utime(wt_lock, (past_time, past_time))
    assert clear_stale_index_lock(wt_path, max_age_seconds=30.0) is True
    assert not wt_lock.exists()


def test_vault_commit_proceeds_with_stale_index_lock(initialized):
    """vault.commit removes a stale index.lock and successfully commits."""
    vault_path = initialized.vault
    note_path = vault_path / "note.md"
    note_path.write_text("new content\n", encoding="utf-8")

    # Leave a stale index.lock behind as if from an aborted git process
    lock_file = vault_path / ".git" / "index.lock"
    lock_file.write_text("aborted lock", encoding="utf-8")
    past_time = time.time() - 60.0
    os.utime(lock_file, (past_time, past_time))

    committed = vault.commit(vault_path, ["note.md"], "test: commit with stale lock")
    assert committed is True
    assert not lock_file.exists()
    assert "test: commit with stale lock" in run_git(["log", "-1", "--format=%s"], vault_path)


def test_vault_commit_retries_and_recovers_from_index_lock_error(initialized, monkeypatch):
    """vault.commit retries once if git add or commit encounters an index.lock error."""
    vault_path = initialized.vault
    note_path = vault_path / "retry_note.md"
    note_path.write_text("retry content\n", encoding="utf-8")

    first_add = True
    orig_git = gitutil.git

    def fake_git(args, cwd, check=True, timeout=gitutil.TIMEOUT_SECONDS):
        nonlocal first_add
        if args[:2] == ["add", "-A"] and first_add:
            first_add = False
            lock = cwd / ".git" / "index.lock"
            lock.write_text("stale", encoding="utf-8")
            past = time.time() - 60.0
            os.utime(lock, (past, past))
            raise gitutil.GitError(
                f"git add failed in {cwd}: fatal: Unable to create '{lock}': File exists."
            )
        return orig_git(args, cwd, check=check, timeout=timeout)

    monkeypatch.setattr(vault, "git", fake_git)
    committed = vault.commit(vault_path, ["retry_note.md"], "test: retry on lock")
    assert committed is True
    assert first_add is False


def test_vault_commit_only_commits_named_paths(initialized):
    """vault.commit commits only the named paths; a hand-staged path stays staged."""
    vault_path = initialized.vault
    (vault_path / "note.md").write_text("note content\n", encoding="utf-8")
    (vault_path / "other.md").write_text("other content\n", encoding="utf-8")
    # Stage other.md by hand, as an Obsidian plugin or a manual git add would.
    run_git(["add", "--", "other.md"], vault_path)

    committed = vault.commit(vault_path, ["note.md"], "test: only named paths")
    assert committed is True
    names = run_git(["show", "--name-only", "--format="], vault_path).splitlines()
    assert names == ["note.md"]
    status = run_git(["status", "--porcelain"], vault_path).splitlines()
    assert "A  other.md" in status


def test_vault_commit_commits_removal_within_paths(initialized):
    """vault.commit commits the removal of a named path."""
    vault_path = initialized.vault
    note = vault_path / "note.md"
    note.write_text("note content\n", encoding="utf-8")
    assert vault.commit(vault_path, ["note.md"], "test: create note") is True
    note.unlink()

    committed = vault.commit(vault_path, ["note.md"], "test: remove note")
    assert committed is True
    names = run_git(["show", "--name-only", "--format="], vault_path).splitlines()
    assert names == ["note.md"]
    assert run_git(["status", "--porcelain"], vault_path).splitlines() == []


def test_vault_commit_noop_when_nothing_staged_within_paths(initialized):
    """vault.commit makes no commit when nothing within paths is staged."""
    vault_path = initialized.vault
    (vault_path / "other.md").write_text("other content\n", encoding="utf-8")
    # Stage a path outside paths, as an outside change would be.
    run_git(["add", "--", "other.md"], vault_path)
    head_before = run_git(["rev-parse", "HEAD"], vault_path)

    committed = vault.commit(vault_path, ["index.md"], "test: should not commit")
    assert committed is False
    assert run_git(["rev-parse", "HEAD"], vault_path) == head_before
    status = run_git(["status", "--porcelain"], vault_path).splitlines()
    assert "A  other.md" in status


def test_git_accepts_custom_timeout(initialized, monkeypatch):
    """git() respects custom timeout values and raises GitError on timeout."""
    # Custom float timeout succeeds on fast command
    res = git(["status"], cwd=initialized.vault, timeout=15.0)
    assert res.returncode == 0

    # timeout=None succeeds
    res_none = git(["status"], cwd=initialized.vault, timeout=None)
    assert res_none.returncode == 0

    # Very small timeout triggers GitError
    with pytest.raises(gitutil.GitError) as exc_info:
        git(["status"], cwd=initialized.vault, timeout=0.000001)
    assert "timed out" in str(exc_info.value).lower()

    # Verify timeout argument is passed directly to subprocess.run
    recorded_timeouts = []
    real_run = subprocess.run

    def intercept_run(*args, **kwargs):
        recorded_timeouts.append(kwargs.get("timeout"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", intercept_run)
    git(["status"], cwd=initialized.vault, timeout=45.0)
    assert recorded_timeouts[-1] == 45.0


def test_init_creates_global_profile(cfg):
    """vault.init creates global/profile.md with the profile template."""
    created = vault.init(cfg)
    assert "global/profile.md" in created

    profile_path = cfg.vault / "global" / "profile.md"
    assert profile_path.is_file()
    content = profile_path.read_text(encoding="utf-8")
    assert "type: Profile" in content
    assert "## Facts" in content
