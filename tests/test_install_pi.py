"""The pi extension installer (BUILD-PLAN M1.4).

Every test here points the installer at a directory under ``tmp_path``, either directly or by
naming a temporary agent directory in ``PI_CODING_AGENT_DIR``, the variable pi itself reads to
move its agent directory. Nothing in this file may read or write ``~/.pi/agent``, so the
command-line tests redirect the environment first and then check that the path the command
resolved is inside the temporary directory before they look at anything else.

The properties BUILD-PLAN M1.4 and the installer's own rules are about are read back out of the
filesystem: which bytes landed where, which directories appeared, and which files survived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aft.cli import main
from aft.install import pi as pi_install
from aft.install.pi import InstallError

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = (REPO_ROOT / pi_install.SOURCE).read_text(encoding="utf-8")


@pytest.fixture
def extensions(tmp_path) -> Path:
    """A temporary stand-in for pi's extensions directory, empty to begin with."""
    directory = tmp_path / "extensions"
    directory.mkdir()
    return directory


def install(extensions: Path) -> Path:
    """Install, and return the file the installer is supposed to have written."""
    pi_install.install(extensions)
    return pi_install.target_path(extensions)


def names(directory: Path) -> set[str]:
    """The names inside ``directory``; a directory that was never created holds nothing."""
    return set() if not directory.exists() else {entry.name for entry in directory.iterdir()}


def backups(extensions: Path) -> set[str]:
    return {name for name in names(pi_install.target_path(extensions).parent) if ".bak" in name}


def test_install_copies_the_shipped_extension(extensions):
    target = install(extensions)

    assert target.read_text(encoding="utf-8") == SHIPPED
    assert names(extensions) == {pi_install.EXTENSION_DIR}


def test_install_twice_writes_nothing_the_second_time(extensions):
    target = install(extensions)

    change = pi_install.install(extensions)

    assert change.changed is False
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert backups(extensions) == set()


def test_install_backs_up_the_text_it_replaces(extensions):
    target = pi_install.target_path(extensions)
    target.parent.mkdir(parents=True)
    target.write_text("// hand written by somebody else\n", encoding="utf-8")

    change = pi_install.install(extensions)

    assert change.changed is True
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert change.backup is not None and change.backup.read_text(encoding="utf-8") == (
        "// hand written by somebody else\n"
    )


def test_install_refuses_a_directory_where_the_extension_belongs(extensions):
    target = pi_install.target_path(extensions)
    target.mkdir(parents=True)
    (target / "somebody-elses-entry.ts").write_text("// theirs\n", encoding="utf-8")

    with pytest.raises(InstallError):
        pi_install.install(extensions)

    assert names(target) == {"somebody-elses-entry.ts"}


def test_install_refuses_a_file_where_a_directory_belongs(tmp_path):
    extensions = tmp_path / "extensions"
    extensions.write_text("not a directory\n", encoding="utf-8")

    with pytest.raises(InstallError):
        pi_install.install(extensions)


def test_install_refuses_text_it_cannot_read(extensions):
    target = pi_install.target_path(extensions)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"\xff\xfe not utf-8 at all \xfa")

    with pytest.raises(InstallError):
        pi_install.install(extensions)

    assert target.read_bytes() == b"\xff\xfe not utf-8 at all \xfa"


def test_dry_run_reports_the_new_file_and_writes_nothing(extensions):
    change = pi_install.install(extensions, dry_run=True)

    assert change.changed is True
    assert "+export default function" in change.diff
    assert names(extensions) == set()


def test_uninstall_removes_the_file_and_the_directory_this_installer_created(extensions):
    install(extensions)

    change = pi_install.uninstall(extensions)

    assert change.changed is True
    assert extensions.exists(), "pi's own extensions directory is never removed"
    assert names(extensions) == set(), "no trace of afterthought is left behind"


def test_uninstall_leaves_what_it_did_not_install(extensions):
    sibling = extensions / "rtk.ts"
    sibling.write_text("// somebody else's extension\n", encoding="utf-8")
    neighbour = extensions / "herdr" / "index.ts"
    neighbour.parent.mkdir(parents=True)
    neighbour.write_text("// another extension of theirs\n", encoding="utf-8")
    install(extensions)

    pi_install.uninstall(extensions)

    assert sibling.read_text(encoding="utf-8") == "// somebody else's extension\n"
    assert neighbour.read_text(encoding="utf-8") == "// another extension of theirs\n"
    assert names(neighbour.parent) == {"index.ts"}


def test_uninstall_keeps_a_directory_that_holds_something_of_ours_no_longer(extensions):
    target = install(extensions)
    note = target.parent / "notes.md"
    note.write_text("# kept on purpose\n", encoding="utf-8")

    pi_install.uninstall(extensions)

    assert not target.exists()
    assert note.read_text(encoding="utf-8") == "# kept on purpose\n"


def test_uninstall_keeps_hand_edited_text_as_a_backup(extensions):
    target = install(extensions)
    target.write_text("// an edit worth keeping\n", encoding="utf-8")

    change = pi_install.uninstall(extensions)

    assert not target.exists()
    assert change.backup is not None and change.backup.read_text(encoding="utf-8") == (
        "// an edit worth keeping\n"
    )
    assert backups(extensions) == {change.backup.name}


def test_uninstall_of_a_directory_we_left_empty(extensions):
    pi_install.target_path(extensions).parent.mkdir(parents=True)

    change = pi_install.uninstall(extensions)

    assert change.changed is True
    assert names(extensions) == set()


def test_uninstall_removes_nothing_twice(extensions):
    install(extensions)
    pi_install.uninstall(extensions)

    change = pi_install.uninstall(extensions)

    assert change.changed is False
    assert backups(extensions) == set()


def test_uninstall_refuses_a_file_where_our_directory_belongs(extensions):
    container = extensions / pi_install.EXTENSION_DIR
    container.write_text("// not a directory\n", encoding="utf-8")

    with pytest.raises(InstallError):
        pi_install.uninstall(extensions)

    assert container.read_text(encoding="utf-8") == "// not a directory\n"


def cli_env(monkeypatch, tmp_path, *, use_agent_dir: bool) -> Path:
    """Point both spellings of pi's agent directory at ``tmp_path`` and return extensions.

    Redirecting ``HOME`` as well means a mistake in the resolver lands in the temporary
    directory this test can see, rather than in the real ``~/.pi/agent``.
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if use_agent_dir:
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "agent"))
    else:
        monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    return pi_install.extensions_dir()


def test_cli_installs_into_the_agent_directory_pi_names(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert main(["install", "pi"]) == 0
    assert extensions.is_relative_to(tmp_path)
    assert pi_install.target_path(extensions).read_text(encoding="utf-8") == SHIPPED
    assert "installed" in capsys.readouterr().out

    assert main(["install", "pi", "--uninstall"]) == 0
    assert names(extensions) == set()


def test_cli_default_agent_directory_is_under_home(monkeypatch, tmp_path):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=False)

    assert main(["install", "pi"]) == 0

    assert extensions == tmp_path / "home" / ".pi" / "agent" / "extensions"
    assert pi_install.target_path(extensions).is_file()


def test_cli_dry_run_prints_the_diff_and_writes_nothing(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert main(["install", "pi", "--dry-run"]) == 0

    assert names(extensions) == set()
    assert f"{pi_install.target_path(extensions)} (before)" in capsys.readouterr().out


def test_cli_reports_what_it_refused_and_fails(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.parent.mkdir(parents=True)
    extensions.write_text("in the way\n", encoding="utf-8")

    assert main(["install", "pi"]) == 2

    assert f"{extensions} is not a directory" in capsys.readouterr().err


def test_cli_help_lists_the_agent_it_installs(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["install", "--help"])

    assert exited.value.code == 0
    assert "pi" in capsys.readouterr().out
