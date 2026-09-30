from __future__ import annotations

from pathlib import Path

import pytest

from aft import config as configmod
from aft.gitutil import normalize_remote
from aft.registry import Registry, path_key
from conftest import make_repo


def test_defaults_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setenv(configmod.HOME_ENV, str(tmp_path / "nowhere"))
    cfg = configmod.load()
    assert cfg.home == tmp_path / "nowhere"
    assert cfg.vault.name == "afterthought"
    assert cfg.settings.evidence.retention_days == 90
    assert cfg.settings.library.commit is True
    assert cfg.host == configmod.default_host()


def test_values_and_sections(home):
    (home / "config.toml").write_text(
        'vault = "/srv/mem"\nhost = "box"\n[library]\ncommit = false\n'
        '[capture]\nworker_paths = ["/w"]\n'
    )
    cfg = configmod.load()
    assert (str(cfg.vault), cfg.host) == ("/srv/mem", "box")
    assert cfg.settings.library.commit is False
    assert cfg.settings.capture.worker_paths == ("/w",)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("valut = '/x'\n", "unknown setting valut"),
        ("[dream]\ninterval = 5\n", "unknown setting dream.interval"),
        ("[dream]\ninterval_minutes = '5'\n", "dream.interval_minutes must be an integer"),
        ("[dream]\ninterval_minutes = 0\n", "dream.interval_minutes must be positive"),
        ("[dream]\ninterval_minutes = -5\n", "dream.interval_minutes must be positive"),
        ("[library]\ncommit = 1\n", "library.commit must be true or false"),
        ("[evidence]\nretention_days = true\n", "must be an integer"),
        ("[capture]\nworker_paths = '/w'\n", "must be a list of strings"),
        ("dream = 3\n", "[dream] must be a table"),
        ("vault = 'relative/path'\n", "vault must be an absolute path"),
        ("vault = \n", "config.toml"),
    ],
)
def test_invalid_config(home, text, message):
    (home / "config.toml").write_text(text)
    with pytest.raises(
        configmod.ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")
    ):
        configmod.load()


def test_default_config_text_loads(home):
    (home / "config.toml").write_text(configmod.DEFAULT_CONFIG_TEXT)
    assert configmod.load().settings == configmod.Settings()


def test_path_matching_and_roles(home, tmp_path):
    (home / "config.toml").write_text(
        "[capture]\n"
        f'exclude_paths = ["{tmp_path / "secret"}"]\n'
        f'worker_paths = ["{tmp_path / "workers"}", "{tmp_path / "opt" / "workers"}"]\n'
    )
    cfg = configmod.load()

    secret_dir = tmp_path / "secret" / "project"
    worker_dir = tmp_path / "workers" / "w1"
    second_worker = tmp_path / "opt" / "workers"
    primary_dir = tmp_path / "opt" / "other" / "normal"

    assert configmod.is_excluded(secret_dir, cfg) is True
    assert configmod.is_excluded(worker_dir, cfg) is False
    assert configmod.is_excluded(primary_dir, cfg) is False

    assert configmod.is_worker(worker_dir, cfg) is True
    assert configmod.is_worker(second_worker, cfg) is True
    assert configmod.is_worker(primary_dir, cfg) is False
    assert configmod.is_worker(secret_dir, cfg) is False


@pytest.mark.parametrize("named", ["slug", "id"])
def test_a_project_named_in_exclude_projects_is_excluded_wherever_it_ran(
    home, initialized, tmp_path, named
):
    """`capture.exclude_projects` answers by project, so a second clone is excluded too.

    The project is named by its slug or by its id, either of which is what a person reads out
    of `aft key list`. Only the first repository is registered and both carry the same origin
    remote, which is how a re-clone or a worktree of one project arrives at a directory nobody
    listed (DESIGN 12).
    """
    origin = "git@github.com:Example/Widget.git"
    registered = make_repo(tmp_path / "dev" / "widget", origin=origin)
    (registered / "src").mkdir()  # a session ran in a directory of the project
    clone = make_repo(tmp_path / "clones" / "widget", origin=origin)
    elsewhere = make_repo(tmp_path / "dev" / "gadget")

    registry = Registry.load(initialized.registry_path)
    project = registry.add("widget", "Widget")
    registry.add_path(project, path_key(initialized.host, registered))
    registry.add_remote(project, normalize_remote(origin))
    registry.save()
    name = project.slug if named == "slug" else project.id

    written = (home / "config.toml").read_text()
    (home / "config.toml").write_text(written + f'[capture]\nexclude_projects = ["{name}"]\n')
    cfg = configmod.load()

    assert configmod.is_excluded(registered / "src", cfg) is True
    assert configmod.is_excluded(clone, cfg) is True, "a second clone is the same project"
    assert configmod.is_excluded(elsewhere, cfg) is False
    # A directory git will not read resolves to no project, which is what a run with nothing
    # named does too: its own resolve fails the job rather than archiving the session.
    assert configmod.is_excluded(tmp_path / "gone", cfg) is False


def test_the_registry_is_not_read_when_no_project_is_named(home, monkeypatch):
    """Naming no project costs the hook nothing: the registry stays shut (DESIGN 8's budget)."""

    def never(*_args, **_kwargs):
        raise AssertionError("the registry was read for a config that names no project")

    monkeypatch.setattr("aft.keys.resolve", never)
    cfg = configmod.load()
    assert configmod.is_excluded(home / "dev" / "widget", cfg) is False


def test_a_tilde_in_a_path_list_names_the_home_directory(home):
    """A path written ``~/secret`` in config.toml is the home directory, not a directory
    called ``~``, so excluding one's own work needs no host spelled out (DESIGN 12).

    This is the one place in this file that names a directory outside the temporary home,
    because the expansion is the thing under test: the paths are compared as names and
    nothing here is created, read, or written.
    """
    (home / "config.toml").write_text('[capture]\nexclude_paths = ["~/secret"]\n')
    cfg = configmod.load()

    assert configmod.is_excluded(Path.home() / "secret" / "project", cfg) is True
    assert configmod.is_excluded(Path.home().parent / "secret", cfg) is False
