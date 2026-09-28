from __future__ import annotations

from pathlib import Path

import pytest

from aft import keys
from aft.gitutil import normalize_remote
from aft.registry import Registry, path_key
from conftest import make_repo, run_git

HOST = "testhost"


def registry_with(tmp_path: Path, **projects: dict) -> Registry:
    registry = Registry(tmp_path / "registry.yaml")
    for slug, spec in projects.items():
        project = registry.add(slug)
        for path in spec.get("paths", []):
            registry.add_path(project, path_key(spec.get("host", HOST), Path(path).resolve()))
        for remote in spec.get("remotes", []):
            registry.add_remote(project, remote)
    return registry


def test_registered_path_resolves(tmp_path):
    repo = make_repo(tmp_path / "proj")
    (repo / "src" / "deep").mkdir(parents=True)
    registry = registry_with(tmp_path, proj={"paths": [repo]})
    result = keys.resolve(repo / "src" / "deep", registry, HOST)
    assert (result.kind, result.slug, result.root) == (keys.PATH, "proj", repo.resolve())


def test_repo_without_remote_resolves_by_path(tmp_path):
    repo = make_repo(tmp_path / "solo")
    registry = registry_with(tmp_path, solo={"paths": [repo]})
    assert keys.resolve(repo, registry, HOST).kind == keys.PATH


def test_nested_registered_repo_wins_over_outer(tmp_path):
    outer = make_repo(tmp_path / "outer")
    inner = make_repo(outer / "inner")
    registry = registry_with(tmp_path, outer={"paths": [outer]}, inner={"paths": [inner]})
    assert keys.resolve(inner, registry, HOST).slug == "inner"
    assert keys.resolve(outer, registry, HOST).slug == "outer"


def test_unregistered_nested_repo_is_not_claimed_by_outer(tmp_path):
    outer = make_repo(tmp_path / "outer")
    inner = make_repo(outer / "Inner_Repo")
    registry = registry_with(tmp_path, outer={"paths": [outer]})
    result = keys.resolve(inner, registry, HOST)
    assert (result.kind, result.slug, result.root) == (
        keys.UNREGISTERED,
        "inner-repo",
        inner.resolve(),
    )


def test_plain_directory_inside_outer_repo_belongs_to_it(tmp_path):
    outer = make_repo(tmp_path / "outer")
    (outer / "notes").mkdir()
    registry = registry_with(tmp_path, outer={"paths": [outer]})
    assert keys.resolve(outer / "notes", registry, HOST).slug == "outer"


def test_linked_worktree_resolves_to_main_checkout(tmp_path):
    repo = make_repo(tmp_path / "main")
    run_git(["worktree", "add", "-q", str(tmp_path / "wt"), "-b", "feature"], repo)
    (tmp_path / "wt" / "sub").mkdir()
    registry = registry_with(tmp_path, main={"paths": [repo]})
    result = keys.resolve(tmp_path / "wt" / "sub", registry, HOST)
    assert (result.kind, result.slug) == (keys.PATH, "main")
    assert result.cwd == repo.resolve() / "sub"


def test_symlinked_path_resolves(tmp_path):
    repo = make_repo(tmp_path / "real")
    link = tmp_path / "link"
    link.symlink_to(repo)
    registry = registry_with(tmp_path, real={"paths": [repo]})
    assert keys.resolve(link, registry, HOST).slug == "real"


def test_submodule_resolves_to_itself(tmp_path):
    lib = make_repo(tmp_path / "libsrc")
    parent = make_repo(tmp_path / "parent")
    run_git(["submodule", "add", "-q", str(lib), "vendor/lib"], parent)
    sub = parent / "vendor" / "lib"
    registry = registry_with(tmp_path, parent={"paths": [parent]})
    result = keys.resolve(sub, registry, HOST)
    assert (result.kind, result.slug, result.root) == (keys.UNREGISTERED, "lib", sub.resolve())

    registry = registry_with(tmp_path, parent={"paths": [parent]}, lib={"paths": [sub]})
    assert keys.resolve(sub, registry, HOST).slug == "lib"


def test_reclone_is_found_by_remote_and_alias_recorded(tmp_path):
    origin = "git@github.com:Example/Widget.git"
    first = make_repo(tmp_path / "first", origin=origin)
    moved = make_repo(tmp_path / "moved", origin="https://github.com/example/widget")
    registry = registry_with(
        tmp_path, widget={"paths": [first], "remotes": [normalize_remote(origin)]}
    )
    recorded = []
    result = keys.resolve(moved, registry, HOST, on_alias=lambda p, k: recorded.append((p.slug, k)))
    assert (result.kind, result.slug, result.root) == (keys.REMOTE, "widget", moved.resolve())
    assert recorded == [("widget", f"{HOST}:{moved.resolve()}")]


def test_other_hosts_paths_do_not_match(tmp_path):
    repo = make_repo(tmp_path / "proj")
    registry = registry_with(tmp_path, proj={"paths": [repo], "host": "elsewhere"})
    assert keys.resolve(repo, registry, HOST).kind == keys.UNREGISTERED


def test_prefix_matches_whole_path_components(tmp_path):
    (tmp_path / "foo").mkdir()
    (tmp_path / "foobar").mkdir()
    registry = registry_with(tmp_path, foo={"paths": [tmp_path / "foo"]})
    assert keys.resolve(tmp_path / "foobar", registry, HOST).kind == keys.NONE
    assert keys.resolve(tmp_path / "foo", registry, HOST).kind == keys.PATH


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/Owner/Repo.git", "github.com/owner/repo"),
        ("https://user@github.com/owner/repo/", "github.com/owner/repo"),
        ("git@github.com:Owner/Repo.git", "github.com/owner/repo"),
        ("ssh://git@github.com:22/owner/repo.git", "github.com/owner/repo"),
        ("gitea.lan:tools/aft", "gitea.lan/tools/aft"),
    ],
)
def test_normalize_remote(url, expected):
    assert normalize_remote(url) == expected


def test_normalize_local_remote(tmp_path):
    target = tmp_path / "bare.git"
    target.mkdir()
    assert normalize_remote(str(target)) == f"file:{tmp_path.resolve() / 'bare'}"
    assert normalize_remote(f"file://{target}") == f"file:{tmp_path.resolve() / 'bare'}"
