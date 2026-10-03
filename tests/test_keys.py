from __future__ import annotations

from pathlib import Path

import pytest

from aft import keys
from aft.gitutil import normalize_remote
from aft.registry import Registry, RegistryError, path_key, slugify, unique_slug
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
    assert keys.resolve(tmp_path / "foobar", registry, HOST).kind == keys.UNREGISTERED
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


def test_a_plain_directory_is_a_project_the_registry_has_not_met(tmp_path):
    """Any directory can be a project (DESIGN 7, F49): a directory with no git boundary of
    its own resolves as unregistered, named after itself, carrying the directory so the
    session-start record and the digest can file it."""
    plain = tmp_path / "fieldwork"
    plain.mkdir()
    result = keys.resolve(plain, registry_with(tmp_path), HOST)
    assert (result.kind, result.slug, result.root) == (
        keys.UNREGISTERED,
        "fieldwork",
        plain.resolve(),
    )


def test_a_plain_directory_inside_a_registered_project_belongs_to_it(tmp_path):
    outer = make_repo(tmp_path / "outer")
    (outer / "notes").mkdir()
    registry = registry_with(tmp_path, outer={"paths": [outer]})
    assert keys.resolve(outer / "notes", registry, HOST).slug == "outer"


def test_a_directory_of_a_taken_name_files_under_its_parent(tmp_path):
    """A slug another project holds is not taken a second time (DESIGN 7, F49): the second
    ``foo`` resolves to its own project under the name its parent gives it."""
    first = tmp_path / "work" / "foo"
    first.mkdir(parents=True)
    second = tmp_path / "other" / "foo"
    second.mkdir(parents=True)
    registry = registry_with(tmp_path, foo={"paths": [first]})
    result = keys.resolve(second, registry, HOST)
    assert (result.kind, result.slug) == (keys.UNREGISTERED, "other-foo")
    assert result.root == second.resolve()


def test_the_home_directory_itself_is_a_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    result = keys.resolve(home, registry_with(tmp_path), HOST)
    assert (result.kind, result.slug, result.root) == (
        keys.UNREGISTERED,
        "home",
        home.resolve(),
    )


def test_a_directory_under_the_temporary_directory_is_no_project(tmp_path, monkeypatch):
    temp = tmp_path / "system-temp"
    temp.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(temp))
    repo = make_repo(temp / "scratch")
    registry = registry_with(tmp_path)
    assert keys.resolve(repo, registry, HOST).kind == keys.NONE
    (temp / "plain").mkdir()
    assert keys.resolve(temp / "plain", registry, HOST).kind == keys.NONE


def test_the_downloads_directory_is_no_project(tmp_path, monkeypatch):
    home = tmp_path / "home"
    downloads = home / "Downloads"
    (downloads / "unpacked").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    registry = registry_with(tmp_path)
    assert keys.resolve(downloads, registry, HOST).kind == keys.NONE
    assert keys.resolve(downloads / "unpacked", registry, HOST).kind == keys.NONE


def test_a_path_capture_excludes_is_no_project(tmp_path):
    excluded = tmp_path / "classified"
    excluded.mkdir()
    registry = registry_with(tmp_path)
    assert keys.resolve(excluded, registry, HOST, never=[str(excluded)]).kind == keys.NONE


def test_unique_slug_prefixes_parent_names_until_the_name_is_free(tmp_path):
    first = tmp_path / "work" / "foo"
    first.mkdir(parents=True)
    registry = Registry(tmp_path / "registry.yaml")
    registry.add("foo")
    second = tmp_path / "other" / "foo"
    second.mkdir(parents=True)
    assert unique_slug(second, registry) == "other-foo"
    registry.add("other-foo")
    third = tmp_path / "yard" / "other" / "foo"
    third.mkdir(parents=True)
    assert unique_slug(third, registry) == "yard-other-foo"


def test_unique_slug_counts_a_path_by_its_own_name_first(tmp_path):
    plain = tmp_path / "notes"
    plain.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    assert unique_slug(plain, registry) == "notes"


def test_unique_slug_falls_back_to_a_number_past_the_root(tmp_path):
    """Every name the path's chain can offer is taken, the walk reaches the filesystem root,
    which has no name to prefix, and a number keeps the project's own name (DESIGN 7, F49)."""
    registry = Registry(tmp_path / "registry.yaml")
    plain = (tmp_path / "foo").resolve()
    plain.mkdir()
    candidate = "foo"
    for parent in plain.parents:
        registry.add(candidate)
        name = slugify(parent.name)
        if not name:
            break
        candidate = f"{name}-{candidate}"
    assert unique_slug(plain, registry) == "foo-2"


def test_add_for_path_registers_the_directory_under_a_free_name(tmp_path):
    work = (tmp_path / "work" / "widget").resolve()
    work.mkdir(parents=True)
    registry = Registry(tmp_path / "registry.yaml")
    project = registry.add_for_path(HOST, work)
    assert project.slug == "widget"
    assert path_key(HOST, work) in project.paths
    assert registry.by_path_key(path_key(HOST, work)) is project


def test_add_for_path_keeps_the_name_the_digests_file_it_under(tmp_path):
    """The dream proposes the name the session-start record already claims (DESIGN 7, F49):
    the home directory registers as ``home``, not under its own directory's name."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    registry = Registry(tmp_path / "registry.yaml")
    project = registry.add_for_path(HOST, home, base="home")
    assert project.slug == "home"
    assert path_key(HOST, home) in project.paths


def test_add_for_path_steps_aside_when_the_name_is_taken(tmp_path):
    first = (tmp_path / "work" / "foo").resolve()
    first.mkdir(parents=True)
    second = (tmp_path / "yard" / "foo").resolve()
    second.mkdir(parents=True)
    registry = Registry(tmp_path / "registry.yaml")
    assert registry.add_for_path(HOST, first).slug == "foo"
    assert registry.add_for_path(HOST, second).slug == "yard-foo"


def test_set_archived_dates_a_project_and_bringing_it_back_clears_it(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add("widget")
    registry.save()
    assert registry.set_archived("widget", "2026-10-04").archived == "2026-10-04"
    registry.save()
    reloaded = Registry.load(tmp_path / "registry.yaml")
    widget = reloaded.by_slug("widget")
    assert widget is not None and widget.archived == "2026-10-04"
    reloaded.set_archived("widget", "")
    reloaded.save()
    cleared = Registry.load(tmp_path / "registry.yaml").by_slug("widget")
    assert cleared is not None and cleared.archived == ""


def test_set_archived_wants_a_date(tmp_path):
    registry = Registry(tmp_path / "registry.yaml")
    registry.add("widget")
    with pytest.raises(RegistryError):
        registry.set_archived("widget", "soon")
