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

import os
import stat
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


def added_lines(diff: str) -> list[str]:
    """The lines a unified diff says the file will hold: its additions, headers aside."""
    return [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]


def tree(root: Path) -> dict[str, str]:
    """What the filesystem under ``root`` looks like, links un-followed: each path keyed to
    its link target, its kind if it is a directory, or its text."""
    state: dict[str, str] = {}
    for entry in sorted(root.rglob("*")):
        where = str(entry.relative_to(root))
        if entry.is_symlink():
            state[where] = f"link -> {entry.readlink()}"
        elif entry.is_dir():
            state[where] = "directory"
        else:
            state[where] = entry.read_text(encoding="utf-8")
    return state


def stand_a_symlink(kind: str, extensions: Path, dotfiles: Path) -> Path:
    """Stand somebody's own symlink where one of afterthought's two paths goes, and return
    the path that was linked.

    ``container`` links afterthought's directory to a directory of theirs holding an
    extension; ``file`` links the entry point to a file of theirs; ``dangling`` links the entry
    point to a file that is not on this machine, the one layout whose text no backup can reach.
    """
    dotfiles.mkdir(parents=True)
    container = extensions / pi_install.EXTENSION_DIR
    if kind == "container":
        (dotfiles / pi_install.EXTENSION_FILE).write_text("// theirs\n", encoding="utf-8")
        container.symlink_to(dotfiles)
        return container
    container.mkdir()
    target = container / pi_install.EXTENSION_FILE
    if kind == "file":
        (dotfiles / pi_install.EXTENSION_FILE).write_text("// theirs\n", encoding="utf-8")
        target.symlink_to(dotfiles / pi_install.EXTENSION_FILE)
    else:
        target.symlink_to(dotfiles / "not-on-this-machine.ts")
    return target


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
    assert added_lines(change.diff) == SHIPPED.splitlines()
    assert names(extensions) == set()


def test_dry_run_reports_a_change_no_line_diff_can_show(extensions):
    """Text that differs only by its final newline changes no line, so the diff is empty:
    the fields still say a change is coming and what it will keep beforehand."""
    target = pi_install.target_path(extensions)
    target.parent.mkdir(parents=True)
    target.write_text(SHIPPED.rstrip("\n"), encoding="utf-8")

    change = pi_install.install(extensions, dry_run=True)

    assert change.changed is True
    assert change.diff == ""
    assert change.backup is not None and not change.backup.exists()
    assert target.read_text(encoding="utf-8") == SHIPPED.rstrip("\n")
    assert names(target.parent) == {pi_install.EXTENSION_FILE}


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
    assert change.removed == (extensions / pi_install.EXTENSION_DIR,)
    assert names(extensions) == set()


def test_uninstall_reports_every_path_it_took_away(extensions):
    target = install(extensions)

    change = pi_install.uninstall(extensions)

    assert change.changed is True
    assert change.removed == (target, target.parent)
    assert not target.exists()
    assert not target.parent.exists()
    assert extensions.is_dir(), "pi's own extensions directory is never removed"


def test_uninstall_reports_the_extension_it_took_that_had_no_text_to_diff(extensions):
    """A 0-byte extension leaves no diff behind, so the diff cannot be what the report is
    read from: the file went, and the directory of ours stayed because it holds a note."""
    target = pi_install.target_path(extensions)
    target.parent.mkdir(parents=True)
    target.write_text("", encoding="utf-8")
    note = target.parent / "notes.md"
    note.write_text("# theirs\n", encoding="utf-8")

    change = pi_install.uninstall(extensions)

    assert change.changed is True
    assert change.diff == "", "there is no text change to point at"
    assert change.removed == (target,)
    assert not target.exists()
    assert note.read_text(encoding="utf-8") == "# theirs\n"
    assert target.parent.is_dir()


def test_uninstall_removes_nothing_twice(extensions):
    install(extensions)
    pi_install.uninstall(extensions)

    change = pi_install.uninstall(extensions)

    assert change.changed is False
    assert change.removed == ()
    assert backups(extensions) == set()


def test_uninstall_refuses_a_file_where_our_directory_belongs(extensions):
    container = extensions / pi_install.EXTENSION_DIR
    container.write_text("// not a directory\n", encoding="utf-8")

    with pytest.raises(InstallError):
        pi_install.uninstall(extensions)

    assert container.read_text(encoding="utf-8") == "// not a directory\n"


def test_uninstall_leaves_a_directory_of_nothing_of_ours(extensions):
    """No extension file and a directory holding only somebody else's text: there is
    nothing of afterthought's left to take away, so nothing goes and nothing is said."""
    note = extensions / pi_install.EXTENSION_DIR / "notes.md"
    note.parent.mkdir(parents=True)
    note.write_text("# theirs\n", encoding="utf-8")

    change = pi_install.uninstall(extensions)

    assert change.changed is False
    assert note.read_text(encoding="utf-8") == "# theirs\n"
    assert names(extensions) == {pi_install.EXTENSION_DIR}


@pytest.mark.parametrize("kind", ["container", "file", "dangling"])
def test_a_symlink_of_somebody_elses_is_refused_with_nothing_written(kind, extensions, tmp_path):
    """Afterthought creates and removes its own two paths, so it refuses to follow a link
    standing at either one: the link is the person's, and a write through it would leave them
    with a file where their link was and no copy of the link to put back."""
    linked = stand_a_symlink(kind, extensions, tmp_path / "dotfiles")
    before = tree(tmp_path)

    for command in (pi_install.install, pi_install.uninstall):
        for dry_run in (True, False):
            with pytest.raises(InstallError):
                command(extensions, dry_run=dry_run)

    assert tree(tmp_path) == before
    assert stat.S_ISLNK(linked.lstat().st_mode), f"{linked} is still the person's own link"


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
    target = pi_install.target_path(extensions)

    assert main(["install", "pi"]) == 0

    assert extensions.is_relative_to(tmp_path)
    assert target.read_text(encoding="utf-8") == SHIPPED
    assert f"installed {target}" in capsys.readouterr().out

    assert main(["install", "pi"]) == 0

    assert capsys.readouterr().out == f"{target} is already up to date\n"

    assert main(["install", "pi", "--uninstall"]) == 0
    assert names(extensions) == set()


def test_cli_default_agent_directory_is_under_home(monkeypatch, tmp_path):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=False)

    assert main(["install", "pi"]) == 0

    assert extensions == tmp_path / "home" / ".pi" / "agent" / "extensions"
    assert pi_install.target_path(extensions).is_file()


def test_cli_dry_run_prints_an_applicable_diff_and_writes_nothing(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    target = pi_install.target_path(extensions)

    assert main(["install", "pi", "--dry-run"]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert names(extensions) == set()
    assert printed[-1] == f"would write {target}"
    assert added_lines("\n".join(printed)) == SHIPPED.splitlines()
    # The patch is every line above that last one, and each of them carries one of unified
    # diff's own prefixes: no blank line sits inside it, so a tool can read it as a change.
    patch = printed[:-1]
    assert patch
    assert all(line.startswith(("---", "+++", "@@", "+", "-")) for line in patch)
    assert patch[-1] == f"+{SHIPPED.splitlines()[-1]}"


def test_cli_dry_run_reports_a_change_only_in_line_endings(monkeypatch, tmp_path, capsys):
    """A target that differs from the shipped text by its final newline changes no line, so
    no diff can show it - and the preview still has to say what the run will do, because a
    silent preview promises nothing and then rewrites the file."""
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.mkdir(parents=True)
    target = pi_install.target_path(extensions)
    target.parent.mkdir()
    target.write_text(SHIPPED.rstrip("\n"), encoding="utf-8")
    before = tree(tmp_path)

    assert main(["install", "pi", "--dry-run"]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert printed[-1] == f"would write {target}"
    assert printed[0].startswith(f"would back up the previous {target.name} to ")
    assert tree(tmp_path) == before, "a dry run writes nothing, not even the backup it names"

    assert main(["install", "pi"]) == 0

    assert target.read_text(encoding="utf-8") == SHIPPED
    assert len(backups(extensions)) == 1, "the text it replaced was kept before the rewrite"


def test_cli_uninstall_dry_run_promises_the_copy_the_run_keeps(monkeypatch, tmp_path, capsys):
    """Hand-edited text is copied before it goes, and that copy is why the directory stays:
    the preview that omits it promises a cleaner removal than the command performs."""
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.mkdir(parents=True)
    target = pi_install.target_path(extensions)
    target.parent.mkdir()
    target.write_text("// an edit worth keeping\n", encoding="utf-8")

    assert main(["install", "pi", "--dry-run", "--uninstall"]) == 0
    previewed = capsys.readouterr().out.splitlines()

    assert target.is_file() and names(target.parent) == {pi_install.EXTENSION_FILE}
    assert previewed[-1] == f"would remove {target}"
    promised = f"would back up the previous {target.name} to "
    named = [line for line in previewed if line.startswith(promised)]
    assert len(named) == 1, previewed
    copy = Path(named[0].removeprefix(promised))
    assert copy.parent == target.parent and not copy.exists(), "a preview names, it does not write"

    assert main(["install", "pi", "--uninstall"]) == 0

    run = capsys.readouterr().out.splitlines()
    assert run[-1] == f"removed {target}"
    assert run[0].startswith(f"backed up the previous {target.name} to ")
    assert len(backups(extensions)) == 1
    kept = target.parent / next(iter(backups(extensions)))
    assert kept.read_text(encoding="utf-8") == "// an edit worth keeping\n"
    assert target.parent.is_dir(), "the copy left behind is why the directory stands"


def test_cli_uninstall_dry_run_says_what_the_real_run_would_say(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)

    assert main(["install", "pi", "--dry-run", "--uninstall"]) == 0
    dry = capsys.readouterr().out

    assert main(["install", "pi", "--uninstall"]) == 0

    assert dry == capsys.readouterr().out == f"no afterthought extension in {extensions}\n"
    assert pi_install.EXTENSION_FILE not in dry, "an uninstall cannot talk about text to write"


def test_cli_uninstall_names_the_empty_directory_it_takes_away(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    container = extensions / pi_install.EXTENSION_DIR
    container.mkdir(parents=True)

    assert main(["install", "pi", "--dry-run", "--uninstall"]) == 0

    assert capsys.readouterr().out == f"would remove {container}\n"
    assert container.is_dir(), "a dry run removes nothing"

    assert main(["install", "pi", "--uninstall"]) == 0

    assert capsys.readouterr().out == f"removed {container}\n"
    assert not container.exists()


def test_cli_uninstall_names_every_path_the_run_takes_away(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.mkdir(parents=True)
    target = pi_install.target_path(extensions)
    target.parent.mkdir()
    target.write_text(SHIPPED, encoding="utf-8")

    assert main(["install", "pi", "--dry-run", "--uninstall"]) == 0

    assert target.is_file() and target.parent.is_dir(), "a dry run removes nothing"
    assert capsys.readouterr().out.splitlines()[-1] == f"would remove {target}, {target.parent}"

    assert main(["install", "pi", "--uninstall"]) == 0

    assert capsys.readouterr().out == f"removed {target}, {target.parent}\n"
    assert not target.exists()
    assert not target.parent.exists()
    assert extensions.is_dir()


def test_cli_reports_what_it_refused_and_fails(monkeypatch, tmp_path, capsys):
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.parent.mkdir(parents=True)
    extensions.write_text("in the way\n", encoding="utf-8")

    assert main(["install", "pi"]) == 2

    assert f"{extensions} is not a directory" in capsys.readouterr().err


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory anyway")
def test_cli_reports_a_write_the_os_refuses(monkeypatch, tmp_path, capsys):
    """The OS can still refuse - afterthought's directory of somebody's own is read-only -
    and that answer is one aft line and exit 2, not a traceback out of the installer."""
    extensions = cli_env(monkeypatch, tmp_path, use_agent_dir=True)
    extensions.mkdir(parents=True)
    container = extensions / pi_install.EXTENSION_DIR
    container.mkdir()
    container.chmod(0o500)

    code = main(["install", "pi"])
    container.chmod(0o700)

    assert code == 2
    assert f"cannot install into {extensions}" in capsys.readouterr().err
    assert tree(container) == {}, "nothing was written into the directory that refused"


def test_cli_help_lists_the_agent_it_installs(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["install", "--help"])

    assert exited.value.code == 0
    assert "pi" in capsys.readouterr().out
