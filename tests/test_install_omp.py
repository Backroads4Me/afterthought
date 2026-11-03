"""The omp hook module installer.

Every test here points the installer at a directory under ``tmp_path``, either directly or by
naming a temporary agent directory in the environment omp itself reads. Nothing in this file
may read or write ``~/.omp``, ``~/.pi`` or ``~/.claude``, so the command-line tests redirect
``HOME`` and clear every variable omp resolves its agent directory from before they look at
anything else.

The property that sets this installer apart from pi's is the one directory it must never
remove: ``hooks/pre`` is omp's own, shared with every other hook module, so an uninstall that
emptied it would be taking away a container afterthought did not create.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from aft.cli import main
from aft.install import omp as omp_install
from aft.install.omp import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (REPO_ROOT / omp_install.SOURCE).read_text(encoding="utf-8")

# Every variable omp reads to place its agent directory, cleared before any test resolves one.
AGENT_DIR_VARS = ("OMP_PROFILE", "PI_PROFILE", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR")


@pytest.fixture
def hooks(tmp_path) -> Path:
    """A temporary stand-in for omp's hooks directory, with the empty ``pre`` omp scans."""
    directory = tmp_path / "hooks" / "pre"
    directory.mkdir(parents=True)
    return directory.parent


def install(hooks: Path) -> Path:
    """Install, and return the file the installer is supposed to have written."""
    omp_install.install(hooks)
    return omp_install.target_path(hooks)


def names(directory: Path) -> set[str]:
    """The names inside ``directory``; a directory that was never created holds nothing."""
    return set() if not directory.exists() else {entry.name for entry in directory.iterdir()}


def backups(hooks: Path) -> set[str]:
    return {name for name in names(omp_install.target_path(hooks).parent) if ".bak" in name}


def test_install_copies_the_shipped_hook_module(hooks):
    target = install(hooks)

    assert target.read_text(encoding="utf-8") == SHIPPED
    assert target == hooks / "pre" / "afterthought.ts"


def test_install_creates_the_pre_directory_when_omp_has_none_yet(tmp_path):
    hooks = tmp_path / "hooks"

    target = install(hooks)

    assert target.read_text(encoding="utf-8") == SHIPPED


def test_install_twice_writes_nothing_the_second_time(hooks):
    target = install(hooks)

    change = omp_install.install(hooks)

    assert change.changed is False
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert backups(hooks) == set()


def test_install_backs_up_the_text_it_replaces(hooks):
    target = omp_install.target_path(hooks)
    target.write_text("// hand written by somebody else\n", encoding="utf-8")

    change = omp_install.install(hooks)

    assert change.changed is True
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert change.backup is not None and change.backup.read_text(encoding="utf-8") == (
        "// hand written by somebody else\n"
    )


def test_install_leaves_no_name_omp_would_load_as_a_second_hook(hooks):
    """The rescue copies and the temporary file of a write are named out of omp's own scan,
    which takes every ``*.ts`` and ``*.js`` in ``hooks/pre`` as a hook module of its own."""
    target = omp_install.target_path(hooks)
    target.write_text("// hand written by somebody else\n", encoding="utf-8")

    omp_install.install(hooks)

    loadable = {name for name in names(target.parent) if name.endswith((".ts", ".js"))}
    assert loadable == {"afterthought.ts"}


def test_install_refuses_a_directory_where_the_hook_module_belongs(hooks):
    target = omp_install.target_path(hooks)
    target.mkdir()

    with pytest.raises(InstallError):
        omp_install.install(hooks)

    assert target.is_dir() and names(target) == set()


def test_install_refuses_a_file_where_the_pre_directory_belongs(tmp_path):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre").write_text("// somebody's file\n", encoding="utf-8")

    with pytest.raises(InstallError):
        omp_install.install(hooks)

    assert (hooks / "pre").read_text(encoding="utf-8") == "// somebody's file\n"


def test_install_refuses_text_it_cannot_read(hooks):
    target = omp_install.target_path(hooks)
    target.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(InstallError):
        omp_install.install(hooks)

    assert target.read_bytes() == b"\xff\xfe not utf-8"


def test_install_leaves_a_link_of_somebody_own_where_our_file_goes(hooks, tmp_path):
    target = omp_install.target_path(hooks)
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    target.symlink_to(dotfiles / "not-on-this-machine.ts")

    with pytest.raises(InstallError):
        omp_install.install(hooks)

    assert stat.S_ISLNK(target.lstat().st_mode), f"{target} is still the person's own link"


def test_dry_run_reports_the_new_file_and_writes_nothing(hooks):
    target = omp_install.target_path(hooks)

    change = omp_install.install(hooks, dry_run=True)

    assert change.changed is True
    assert not target.exists()
    assert change.diff
    assert names(target.parent) == set()


def test_uninstall_removes_the_file_and_keeps_the_directory_omp_owns(hooks):
    target = install(hooks)

    change = omp_install.uninstall(hooks)

    assert change.changed is True
    assert change.removed == (target,)
    assert not target.exists()
    assert target.parent.is_dir(), "hooks/pre is omp's own directory, never afterthought's"
    assert hooks.is_dir()


def test_uninstall_leaves_a_sibling_hook_module_alone(hooks):
    install(hooks)
    sibling = hooks / "pre" / "somebody-else.ts"
    sibling.write_text("// theirs\n", encoding="utf-8")

    omp_install.uninstall(hooks)

    assert names(hooks / "pre") == {"somebody-else.ts"}
    assert sibling.read_text(encoding="utf-8") == "// theirs\n"


def test_uninstall_keeps_hand_edited_text_as_a_backup(hooks):
    target = omp_install.target_path(hooks)
    target.write_text("// somebody's own hook\n", encoding="utf-8")

    change = omp_install.uninstall(hooks)

    assert change.backup is not None
    assert change.backup.read_text(encoding="utf-8") == "// somebody's own hook\n"
    assert not target.exists()


def test_uninstall_takes_the_copies_of_earlier_installs(hooks):
    target = omp_install.target_path(hooks)
    target.write_text("// hand written\n", encoding="utf-8")
    omp_install.install(hooks)
    assert backups(hooks)

    change = omp_install.uninstall(hooks)

    assert change.backup is None, "the shipped text needs no copy to survive it"
    assert set(change.removed) >= {target}
    assert names(hooks / "pre") == set()
    assert (hooks / "pre").is_dir()


def test_uninstall_removes_nothing_twice(hooks):
    install(hooks)
    omp_install.uninstall(hooks)

    change = omp_install.uninstall(hooks)

    assert change.changed is False
    assert change.removed == ()


def test_uninstall_dry_run_reports_without_removing(hooks):
    target = install(hooks)

    change = omp_install.uninstall(hooks, dry_run=True)

    assert change.changed is True
    assert change.removed == (target,)
    assert target.read_text(encoding="utf-8") == SHIPPED


def agent_env(monkeypatch, tmp_path, *, use_agent_dir: bool = False, profile: str | None = None):
    """Point omp's agent directory at ``tmp_path`` and return the hooks directory.

    Redirecting ``HOME`` as well means a mistake in the resolver lands in the temporary
    directory this test can see, rather than in the real ``~/.omp/agent``.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in AGENT_DIR_VARS:
        monkeypatch.delenv(name, raising=False)
    if use_agent_dir:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    if profile is not None:
        monkeypatch.setenv("OMP_PROFILE", profile)
    return omp_install.hooks_dir()


def test_default_agent_directory_is_under_home(monkeypatch, tmp_path):
    hooks = agent_env(monkeypatch, tmp_path)

    assert hooks == tmp_path / "home" / ".omp" / "agent" / "hooks"


def test_agent_directory_variable_moves_the_default_profile(monkeypatch, tmp_path):
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert hooks == tmp_path / "agent" / "hooks"


def test_a_named_profile_has_its_own_agent_directory(monkeypatch, tmp_path):
    """A profile never sees the default profile's agent configuration, so a named profile
    ignores ``PI_CODING_AGENT_DIR`` (omp's config-usage.md, "Profiles")."""
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True, profile="work")

    assert hooks == tmp_path / "home" / ".omp" / "profiles" / "work" / "agent" / "hooks"


@pytest.mark.parametrize("profile", ["", "   ", "default"])
def test_an_empty_or_default_profile_is_the_default_profile(monkeypatch, tmp_path, profile):
    hooks = agent_env(monkeypatch, tmp_path, profile=profile)

    assert hooks == tmp_path / "home" / ".omp" / "agent" / "hooks"


def test_omp_profile_wins_over_pi_profile_even_when_empty(monkeypatch, tmp_path):
    """``OMP_PROFILE`` wins over ``PI_PROFILE`` even when it is set to an empty value
    (omp's environment-variables.md)."""
    agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PI_PROFILE", "work")
    monkeypatch.setenv("OMP_PROFILE", "")

    assert omp_install.hooks_dir() == tmp_path / "home" / ".omp" / "agent" / "hooks"


def test_pi_profile_is_read_when_omp_profile_is_absent(monkeypatch, tmp_path):
    agent_env(monkeypatch, tmp_path)
    monkeypatch.setenv("PI_PROFILE", "work")

    assert omp_install.hooks_dir() == (
        tmp_path / "home" / ".omp" / "profiles" / "work" / "agent" / "hooks"
    )


def test_cli_installs_into_the_hooks_directory_omp_names(monkeypatch, tmp_path, capsys):
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True)
    target = omp_install.target_path(hooks)

    assert main(["install", "omp"]) == 0

    assert hooks.is_relative_to(tmp_path)
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert f"installed {target}" in capsys.readouterr().out

    assert main(["install", "omp"]) == 0

    assert f"{target} is already up to date" in capsys.readouterr().out

    assert main(["install", "omp", "--uninstall"]) == 0

    assert f"removed {target}" in capsys.readouterr().out
    assert not target.exists()
    assert target.parent.is_dir()


def test_cli_installs_no_skill_of_its_own(monkeypatch, tmp_path):
    """omp reads the ``aft`` skill through ``skills.customDirectories``, so this command
    leaves no second copy to drift from the one ``aft install pi`` keeps."""
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert main(["install", "omp"]) == 0

    assert names(hooks.parent) == {"hooks"}


def test_cli_dry_run_prints_an_applicable_diff_and_writes_nothing(monkeypatch, tmp_path, capsys):
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True)
    target = omp_install.target_path(hooks)

    assert main(["install", "omp", "--dry-run"]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert not target.exists()
    assert f"would write {target}" in printed
    diff_lines = [line for line in printed if line != f"would write {target}"]
    assert diff_lines
    assert all(line.startswith(("---", "+++", "@@", "+", "-")) for line in diff_lines)
    assert f"+{SHIPPED.splitlines()[-1]}" in diff_lines


def test_cli_uninstall_of_nothing_says_so(monkeypatch, tmp_path, capsys):
    hooks = agent_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert main(["install", "omp", "--uninstall"]) == 0

    assert f"no afterthought hook module in {hooks}" in capsys.readouterr().out
