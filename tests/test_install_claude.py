"""The Claude Code installer (BUILD-PLAN M1.3).

Every test here points the installer at a file under ``tmp_path``. Nothing in this file may
read or write the real ``~/.claude/settings.json``, so the command-line tests redirect
``HOME`` and then check that the path the command resolved is inside the temporary home
before they run it.

The properties BUILD-PLAN M1.3 is really about are read back out of the resulting file as
data: what the file runs, in what order, and whether bytes moved.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from datetime import datetime
from pathlib import Path

import pytest

from aft.adapters.claude import EVENTS as CLAUDE_EVENTS
from aft.cli import main
from aft.install import claude as claude_install
from aft.install.claude import InstallError

FIXTURES = Path(__file__).parent / "fixtures" / "hooks"


def settings_in(tmp_path) -> Path:
    return tmp_path / ".claude" / "settings.json"


def write_settings(settings: Path, document: object) -> str:
    """Put a document in the file the way Claude Code files read, and return that text."""
    text = json.dumps(document, indent=2) + "\n"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(text, encoding="utf-8")
    return text


def read_settings(settings: Path) -> dict:
    return json.loads(settings.read_text(encoding="utf-8"))


def command_order(settings_or_document) -> list[tuple[str, str]]:
    """Every (event, command) the file runs, in the file's own order."""
    document = (
        settings_or_document
        if isinstance(settings_or_document, dict)
        else read_settings(settings_or_document)
    )
    hooks = document.get("hooks", {})
    return [
        (event, hook["command"])
        for event, groups in hooks.items()
        for group in groups
        for hook in group["hooks"]
    ]


def aft_commands() -> set[str]:
    return {f"aft hook claude {event}" for event in claude_install.HOOK_EVENTS}


HOOK_COMMAND = claude_install.hook_command("SessionStart")


def foreign_commands(runs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The runs a file holds that afterthought did not add, in order."""
    ours = aft_commands()
    return [(event, command) for event, command in runs if command not in ours]


PEOPLE = {
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "model": "claude-opus",
    "hooks": {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "rtk hook claude"}]}
        ],
        "SessionStart": [
            {
                "matcher": "^(startup|resume|clear|compact|fork)$",
                "hooks": [
                    {
                        "type": "command",
                        "command": "bash ./hooks/herdr-agent-state.sh session",
                        "timeout": 10,
                    }
                ],
            },
            {"matcher": "startup", "hooks": [{"type": "command", "command": "echo second"}]},
        ],
        "Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}],
    },
    "statusLine": {"type": "command", "command": "echo status"},
}


def test_install_writes_three_entries_into_an_empty_settings_file(tmp_path):
    settings = settings_in(tmp_path)
    write_settings(settings, {})

    assert claude_install.install(settings).changed is True

    hooks = read_settings(settings)["hooks"]
    assert list(hooks) == ["SessionStart", "PreCompact", "SessionEnd"]
    for event in claude_install.HOOK_EVENTS:
        assert hooks[event] == [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"aft hook claude {event}",
                        "timeout": 5,
                    }
                ]
            }
        ]


def test_install_creates_a_missing_file_and_backs_up_nothing(tmp_path):
    settings = settings_in(tmp_path)

    change = claude_install.install(settings)

    assert change.changed is True and change.backup is None
    assert set(read_settings(settings)["hooks"]) == set(claude_install.HOOK_EVENTS)
    assert list(settings.parent.glob("*.bak")) == []


def test_install_treats_an_empty_file_as_no_settings_yet(tmp_path):
    settings = settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text("", encoding="utf-8")

    change = claude_install.install(settings)

    assert change.changed is True and change.backup is None
    assert set(read_settings(settings)["hooks"]) == set(claude_install.HOOK_EVENTS)


def test_install_appends_its_own_entries_after_everyone_elses(tmp_path):
    settings = settings_in(tmp_path)
    theirs = command_order(PEOPLE)
    text = write_settings(settings, PEOPLE)

    claude_install.install(settings)

    after = read_settings(settings)
    assert command_order(after) == [
        ("PreToolUse", "rtk hook claude"),
        ("SessionStart", "bash ./hooks/herdr-agent-state.sh session"),
        ("SessionStart", "echo second"),
        ("SessionStart", "aft hook claude SessionStart"),
        ("Stop", "echo done"),
        ("PreCompact", "aft hook claude PreCompact"),
        ("SessionEnd", "aft hook claude SessionEnd"),
    ]
    assert foreign_commands(command_order(after)) == theirs
    assert after["hooks"]["PreToolUse"] == PEOPLE["hooks"]["PreToolUse"]
    assert after["hooks"]["Stop"] == PEOPLE["hooks"]["Stop"]
    assert after["hooks"]["SessionStart"][:2] == PEOPLE["hooks"]["SessionStart"]
    assert list(after) == list(PEOPLE)
    assert after["$schema"] == PEOPLE["$schema"]
    assert after["model"] == PEOPLE["model"]
    assert after["statusLine"] == PEOPLE["statusLine"]
    # The other people's own text is still their text, in the same relative order.
    written = settings.read_text(encoding="utf-8")
    assert (
        written.index("rtk hook claude")
        < written.index("herdr-agent-state")
        < written.index("echo second")
    )
    assert text.count("rtk hook claude") == written.count("rtk hook claude") == 1


def test_installing_twice_changes_nothing(tmp_path):
    settings = settings_in(tmp_path)
    write_settings(settings, PEOPLE)
    claude_install.install(settings)
    text = settings.read_text(encoding="utf-8")
    backups = sorted(settings.parent.glob("*.bak"))

    second = claude_install.install(settings)

    assert second.changed is False and second.backup is None and second.preview == ""
    assert settings.read_text(encoding="utf-8") == text
    assert sorted(settings.parent.glob("*.bak")) == backups
    entries = dict.fromkeys(claude_install.HOOK_EVENTS, 0)
    for seen_event, command in command_order(settings):
        if command in aft_commands():
            entries[seen_event] += 1
    assert entries == dict.fromkeys(claude_install.HOOK_EVENTS, 1)


def test_reinstall_adds_only_the_entry_that_is_missing(tmp_path):
    settings = settings_in(tmp_path)
    write_settings(settings, {})
    claude_install.install(settings)

    # The user removes one entry and puts one of their own ahead of another of ours.
    document = read_settings(settings)
    del document["hooks"]["PreCompact"]
    document["hooks"]["SessionEnd"].insert(
        0, {"hooks": [{"type": "command", "command": "echo mine"}]}
    )
    settings.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")

    assert claude_install.install(settings).changed is True

    after = read_settings(settings)
    assert after["hooks"]["PreCompact"] == [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "aft hook claude PreCompact",
                    "timeout": 5,
                }
            ]
        }
    ]
    assert after["hooks"]["SessionEnd"][0]["hooks"][0]["command"] == "echo mine"
    assert after["hooks"]["SessionStart"] == document["hooks"]["SessionStart"]


def test_a_write_is_preceded_by_a_backup_holding_the_previous_text(tmp_path):
    settings = settings_in(tmp_path)
    text = write_settings(settings, PEOPLE)

    change = claude_install.install(settings)

    assert change.backup is not None
    assert change.backup.parent == settings.parent
    assert change.backup.name.startswith("settings.json.aft-")
    assert change.backup.name.endswith(".bak")
    assert change.backup.read_text(encoding="utf-8") == text
    assert read_settings(change.backup) == PEOPLE


@pytest.mark.parametrize("mode", [0o600, 0o640])
def test_the_backup_carries_the_mode_of_the_settings_file_it_copies(tmp_path, mode):
    settings = settings_in(tmp_path)
    write_settings(settings, PEOPLE)
    os.chmod(settings, mode)

    change = claude_install.install(settings)

    assert change.backup is not None
    assert stat.S_IMODE(change.backup.stat().st_mode) == mode
    assert stat.S_IMODE(settings.stat().st_mode) == mode


def test_a_settings_file_afterthought_creates_is_private_to_its_owner(tmp_path):
    """A file aft invents keeps the private mode its temporary file was made under."""
    plain = settings_in(tmp_path)
    plain.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "dotfiles" / "settings.json"
    linked = tmp_path / ".claude" / "linked.json"
    linked.symlink_to(target)

    claude_install.install(plain)
    claude_install.install(linked)

    assert stat.S_IMODE(plain.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


class ClockAt:
    """``datetime`` held at one instant, so two writes share one backup stamp."""

    @staticmethod
    def now(tz):
        return datetime(2026, 1, 2, 3, 4, 5, tzinfo=tz)


def test_two_writes_in_one_second_leave_two_backups(tmp_path, monkeypatch):
    settings = settings_in(tmp_path)
    before = write_settings(settings, PEOPLE)
    monkeypatch.setattr(claude_install, "datetime", ClockAt)

    claude_install.install(settings)
    installed = settings.read_text(encoding="utf-8")
    claude_install.uninstall(settings)

    backups = sorted(settings.parent.glob("*.bak"))
    assert len(backups) == 2
    assert {p.read_text(encoding="utf-8") for p in backups} == {before, installed}


def test_uninstall_leaves_every_hook_of_the_users_own(tmp_path):
    """What install added uninstall takes back, and nothing else moves."""
    settings = settings_in(tmp_path)
    theirs = command_order(PEOPLE)
    text = write_settings(settings, PEOPLE)

    claude_install.install(settings)
    assert claude_install.uninstall(settings).changed is True

    after = read_settings(settings)
    assert command_order(after) == theirs
    assert after["hooks"]["PreToolUse"] == PEOPLE["hooks"]["PreToolUse"]
    assert after["hooks"]["SessionStart"] == PEOPLE["hooks"]["SessionStart"]
    assert after["hooks"]["Stop"] == PEOPLE["hooks"]["Stop"]
    # The event keys aft emptied are gone; the user's own keys keep their order.
    assert list(after["hooks"]) == ["PreToolUse", "SessionStart", "Stop"]
    assert settings.read_text(encoding="utf-8") == text


def test_install_then_uninstall_returns_the_file_to_its_earlier_bytes(tmp_path):
    """An install a later uninstall follows leaves no container of aft's behind."""
    settings = settings_in(tmp_path)
    document = {
        "model": "claude-opus",
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]},
    }
    text = write_settings(settings, document)

    claude_install.install(settings)
    assert set(read_settings(settings)["hooks"]) == {"Stop", *claude_install.HOOK_EVENTS}

    claude_install.uninstall(settings)

    assert settings.read_text(encoding="utf-8") == text


def test_uninstall_drops_only_the_event_keys_this_operation_emptied(tmp_path):
    """An array aft filled and then emptied goes; one holding their hook stays."""
    settings = settings_in(tmp_path)
    theirs = {"hooks": [{"type": "command", "command": "echo theirs"}]}
    write_settings(settings, {"hooks": {"SessionStart": [theirs]}})

    claude_install.install(settings)
    claude_install.uninstall(settings)

    assert read_settings(settings) == {"hooks": {"SessionStart": [theirs]}}


@pytest.mark.parametrize(
    "array",
    [
        [],
        [{"hooks": [{"type": "command", "command": "echo theirs"}]}],
    ],
)
def test_uninstall_leaves_an_event_array_it_did_not_empty(tmp_path, array):
    """An array this operation emptied nothing out of keeps its key, empty or populated."""
    settings = settings_in(tmp_path)
    text = write_settings(settings, {"hooks": {"PreCompact": array}})

    change = claude_install.uninstall(settings)

    assert change.changed is False and change.preview == ""
    assert settings.read_text(encoding="utf-8") == text


def test_uninstall_leaves_a_hooks_object_the_file_already_had(tmp_path):
    """The ``hooks`` object the file already had keeps its key; only aft's event keys leave."""
    settings = settings_in(tmp_path)
    text = write_settings(settings, {"hooks": {}})

    claude_install.install(settings)
    claude_install.uninstall(settings)

    assert read_settings(settings) == {"hooks": {}}
    assert settings.read_text(encoding="utf-8") == text


def test_uninstall_leaves_a_matcher_group_that_held_only_our_hook(tmp_path):
    settings = settings_in(tmp_path)
    write_settings(
        settings,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "aft hook claude SessionStart",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        },
    )

    claude_install.uninstall(settings)

    assert read_settings(settings) == {
        "hooks": {"SessionStart": [{"matcher": "startup", "hooks": []}]}
    }


@pytest.mark.parametrize(
    "element",
    [
        {"type": "command", "command": HOOK_COMMAND, "timeout": 30},
        {"matcher": "clear", "hooks": [{"type": "command", "command": HOOK_COMMAND}]},
    ],
)
def test_install_adds_no_copy_of_a_command_an_event_list_already_runs(tmp_path, element):
    """An event list already running aft's command gets no second copy of it."""
    settings = settings_in(tmp_path)
    write_settings(settings, {"hooks": {"SessionStart": [element]}})

    assert claude_install.install(settings).changed is True

    after = read_settings(settings)
    assert after["hooks"]["SessionStart"] == [element]
    assert set(after["hooks"]) == set(claude_install.HOOK_EVENTS)


def test_uninstall_leaves_a_hand_written_element_of_the_event_list(tmp_path):
    """A command line of ours is not what makes an element ours: that one never installed."""
    settings = settings_in(tmp_path)
    hand_written = {"type": "command", "command": HOOK_COMMAND, "timeout": 30}
    text = write_settings(settings, {"hooks": {"SessionStart": [hand_written], "PreCompact": []}})

    change = claude_install.uninstall(settings)

    assert change.changed is False and change.preview == ""
    assert settings.read_text(encoding="utf-8") == text


def test_everything_aft_did_not_add_survives_install_and_uninstall_in_order(tmp_path):
    """Foreign entries at every level, and a hand-written one of ours, keep their places."""
    settings = settings_in(tmp_path)
    ours = {"type": "command", "command": HOOK_COMMAND, "timeout": 5}
    elements = [
        {"type": "command", "command": "echo direct"},
        {"type": "command", "command": HOOK_COMMAND, "timeout": 30},
        {"matcher": "startup", "hooks": [{"type": "command", "command": "echo nested"}]},
        {"matcher": "clear", "hooks": [dict(ours)]},
    ]
    write_settings(settings, {"hooks": {"SessionStart": elements}})

    claude_install.install(settings)

    assert read_settings(settings)["hooks"]["SessionStart"] == elements

    claude_install.uninstall(settings)

    assert read_settings(settings) == {
        "hooks": {
            "SessionStart": [
                {"type": "command", "command": "echo direct"},
                {"type": "command", "command": HOOK_COMMAND, "timeout": 30},
                {
                    "matcher": "startup",
                    "hooks": [{"type": "command", "command": "echo nested"}],
                },
                {"matcher": "clear", "hooks": []},
            ],
            # PreCompact and SessionEnd held nothing but aft's own entry, so both keys left.
        }
    }


def test_uninstall_leaves_elements_of_an_event_list_it_cannot_read(tmp_path):
    """Nothing but a command hook of ours can mean "remove this", not even a JSON null."""
    settings = settings_in(tmp_path)
    write_settings(
        settings,
        {
            "hooks": {
                "SessionStart": [
                    None,
                    "hand written",
                    {"matcher": "startup"},
                    {"hooks": [{"type": "command", "command": HOOK_COMMAND}]},
                ]
            }
        },
    )

    assert claude_install.uninstall(settings).changed is True

    assert read_settings(settings) == {
        "hooks": {"SessionStart": [None, "hand written", {"matcher": "startup"}]}
    }


def test_uninstall_keeps_a_group_it_did_not_add(tmp_path):
    """A user who moved our command into their own group loses the command, not the group."""
    settings = settings_in(tmp_path)
    write_settings(
        settings,
        {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {"type": "command", "command": "echo theirs"},
                            {
                                "type": "command",
                                "command": "aft hook claude SessionStart",
                                "timeout": 5,
                            },
                        ],
                    }
                ]
            }
        },
    )

    claude_install.uninstall(settings)

    assert read_settings(settings) == {
        "hooks": {
            "SessionStart": [
                {"matcher": "startup", "hooks": [{"type": "command", "command": "echo theirs"}]}
            ]
        }
    }


def test_uninstall_of_a_file_without_our_entries_changes_nothing(tmp_path):
    settings = settings_in(tmp_path)
    text = write_settings(settings, PEOPLE)

    change = claude_install.uninstall(settings)

    assert change.changed is False and change.backup is None and change.preview == ""
    assert settings.read_text(encoding="utf-8") == text


def test_the_installed_commands_are_the_hook_command_that_exists(tmp_path, monkeypatch, capsys):
    """The entries install the command line M1.1 merged, for an event its adapter maps."""
    settings = settings_in(tmp_path)
    claude_install.install(settings)

    for event, command in command_order(settings):
        assert shlex.split(command) == ["aft", "hook", "claude", event]
        assert event in CLAUDE_EVENTS

    monkeypatch.setenv("AFT_HOME", str(tmp_path / "aft-home"))
    payload = (FIXTURES / "claude-session-start.json").open(encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", payload)
    assert main(["hook", "claude", "SessionStart"]) == 0
    assert capsys.readouterr().out == ""


SECRET = "sk-ant-api03-NotForSomeonesTerminal"

KEEPER = {
    "model": "claude-opus",
    "env": {"ANTHROPIC_API_KEY": SECRET},
    "hooks": {},
    "statusLine": {"type": "command", "command": "echo status"},
}


def test_the_add_preview_names_only_aft_s_own_entries(tmp_path, monkeypatch, capsys):
    """The preview says what aft will write, never what the user left beside it."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    text = write_settings(settings, KEEPER)

    assert main(["install", "claude", "--dry-run"]) == 0

    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert "ANTHROPIC_API_KEY" not in printed
    assert "echo status" not in printed
    assert "claude-opus" not in printed
    lines = printed.splitlines()
    head = lines[0]
    hook_entries = lines[1:4]
    assert head.startswith(f"3 entries to add to {settings}")
    assert "5 second timeout" in head
    assert hook_entries == [
        f"  {event}: aft hook claude {event}" for event in claude_install.HOOK_EVENTS
    ]
    assert any("would write skill" in line for line in lines)
    assert settings.read_text(encoding="utf-8") == text
    assert list(settings.parent.glob("settings.json.aft-*")) == []


def test_the_removal_preview_names_only_aft_s_own_entries(tmp_path, monkeypatch, capsys):
    """The removal preview lists the entries aft is taking out, and nothing it is leaving."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    write_settings(settings, KEEPER)
    claude_install.install(settings)
    text = settings.read_text(encoding="utf-8")
    backups = sorted(settings.parent.glob("*.bak"))

    assert main(["install", "claude", "--uninstall", "--dry-run"]) == 0

    printed = capsys.readouterr().out
    assert SECRET not in printed
    assert "ANTHROPIC_API_KEY" not in printed
    assert "echo status" not in printed
    assert "claude-opus" not in printed
    head, *entries = printed.splitlines()
    assert head.startswith(f"3 entries to remove from {settings}")
    assert entries == [
        f"  {event}: aft hook claude {event}" for event in claude_install.HOOK_EVENTS
    ]
    assert settings.read_text(encoding="utf-8") == text
    assert sorted(settings.parent.glob("*.bak")) == backups


def test_dry_run_of_an_installed_file_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    claude_install.install(settings)
    claude_install.install_skill(claude_install.skills_dir(settings))
    text = settings.read_text(encoding="utf-8")

    assert main(["install", "claude", "--dry-run"]) == 0

    assert "no changes" in capsys.readouterr().out
    assert settings.read_text(encoding="utf-8") == text


def test_the_command_installs_and_uninstalls_the_real_default_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    theirs = command_order(PEOPLE)
    write_settings(settings, PEOPLE)

    assert main(["install", "claude"]) == 0
    assert "backed up" in capsys.readouterr().out
    assert foreign_commands(command_order(settings)) == theirs
    assert main(["install", "claude"]) == 0
    assert "already" in capsys.readouterr().out
    assert main(["install", "claude", "--uninstall"]) == 0

    runs = command_order(settings)
    assert foreign_commands(runs) == theirs
    assert aft_commands().isdisjoint({command for _, command in runs})


BROKEN = [
    '{"hooks": {"SessionStart": [',
    "not json at all",
    "[1, 2, 3]",
    '{"hooks": []}',
    '{"hooks": {"SessionStart": {}}}',
]


@pytest.mark.parametrize("text", BROKEN)
def test_a_settings_file_afterthought_cannot_read_aborts_without_writing(tmp_path, text):
    settings = settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(text, encoding="utf-8")

    for act in (claude_install.install, claude_install.uninstall):
        with pytest.raises(InstallError) as refused:
            act(settings)
        assert str(settings) in str(refused.value)
        assert settings.read_text(encoding="utf-8") == text
        assert list(settings.parent.glob("*.bak")) == []
        assert list(settings.parent.glob("settings.json.aft-*")) == []


UNENCODABLE = '{"note": "\\ud800"}\n'


def test_settings_text_that_will_not_encode_is_refused_without_writing(tmp_path):
    """JSON that survives reading but cannot be written back is still a refusal."""
    settings = settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(UNENCODABLE, encoding="utf-8")

    with pytest.raises(InstallError) as refused:
        claude_install.install(settings)

    assert str(settings) in str(refused.value)
    assert settings.read_text(encoding="utf-8") == UNENCODABLE
    assert list(settings.parent.glob("*.bak")) == []
    assert list(settings.parent.glob("settings.json.aft-*")) == []


def test_the_command_reports_settings_it_cannot_write_back(tmp_path, monkeypatch, capsys):
    """The unencodable file is refused like any other, not as a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(UNENCODABLE, encoding="utf-8")

    assert main(["install", "claude"]) == 2

    refusal = capsys.readouterr().err
    assert refusal.startswith("aft: ")
    assert "Traceback" not in refusal
    assert settings.read_text(encoding="utf-8") == UNENCODABLE
    assert list(settings.parent.iterdir()) == [settings]


def test_the_command_reports_an_unreadable_settings_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"hooks": ', encoding="utf-8")

    assert main(["install", "claude"]) == 2

    assert "not valid JSON" in capsys.readouterr().err
    assert settings.read_text(encoding="utf-8") == '{"hooks": '
    assert list(settings.parent.glob("*.bak")) == []


def test_the_command_reports_a_settings_path_it_cannot_open(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    settings.mkdir(parents=True)

    assert main(["install", "claude"]) == 2

    assert capsys.readouterr().err.startswith("aft: cannot read")
    assert settings.is_dir() and list(settings.iterdir()) == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root can search a directory at any mode")
def test_the_command_reports_a_settings_directory_it_cannot_search(tmp_path, monkeypatch, capsys):
    """A settings directory the user cannot search is refused, never raised as a traceback."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    text = '{"model": "claude-opus"}\n'
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(text, encoding="utf-8")
    os.chmod(settings.parent, 0o400)  # readable, but no name inside can be looked up
    try:
        assert main(["install", "claude"]) == 2
        refusal = capsys.readouterr().err
    finally:
        os.chmod(settings.parent, 0o700)

    assert refusal.startswith("aft: cannot read")
    assert "Traceback" not in refusal
    assert settings.read_text(encoding="utf-8") == text
    assert list(settings.parent.iterdir()) == [settings]


def test_the_command_reports_a_settings_file_it_cannot_write(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    settings.parent.write_text("not a directory", encoding="utf-8")

    assert main(["install", "claude"]) == 2

    err = capsys.readouterr().err
    assert err.startswith("aft: cannot write")
    assert "it was not changed" in err
    assert list(tmp_path.rglob("settings.json.aft-*")) == []
    assert str(settings) in err
    assert settings.parent.read_text(encoding="utf-8") == "not a directory"


def test_a_symbolic_link_at_the_settings_path_survives_the_install(tmp_path):
    """A settings file the user keeps in their dotfiles stays the file Claude Code reads."""
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir(parents=True)
    text = json.dumps(PEOPLE, indent=2) + "\n"
    real.write_text(text, encoding="utf-8")
    settings = settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(real)

    change = claude_install.install(settings)

    assert settings.is_symlink()
    assert foreign_commands(command_order(real)) == command_order(PEOPLE)
    assert aft_commands() <= {command for _, command in command_order(real)}
    assert change.backup is not None
    assert change.backup.parent == real.parent.resolve()
    assert json.loads(change.backup.read_text(encoding="utf-8")) == PEOPLE


def test_install_through_a_dangling_link_writes_its_target(tmp_path):
    """A link to a settings file not written yet gets a target, not a plain file."""
    real = tmp_path / "dotfiles" / "settings.json"
    settings = settings_in(tmp_path)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.symlink_to(real)

    change = claude_install.install(settings)

    assert change.changed is True and change.backup is None
    assert settings.is_symlink()
    assert set(read_settings(real)["hooks"]) == set(claude_install.HOOK_EVENTS)


def test_main_install_claude_on_symlinked_skill_returns_2_and_leaves_settings_unchanged(
    tmp_path, monkeypatch, capsys
):
    """A symlinked SKILL.md is refused before hooks are written, leaving settings.json unchanged."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    assert settings == settings_in(tmp_path)
    initial_text = '{\n  "model": "claude-opus"\n}\n'
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(initial_text, encoding="utf-8")

    skills = claude_install.skills_dir(settings)
    skill_container = skills / "library-import"
    skill_container.mkdir(parents=True, exist_ok=True)
    symlink_target = tmp_path / "somewhere_else.md"
    symlink_target.write_text("some external text", encoding="utf-8")
    skill_file = skill_container / "SKILL.md"
    skill_file.symlink_to(symlink_target)

    ret = main(["install", "claude"])
    assert ret == 2
    err = capsys.readouterr().err
    assert "aft:" in err
    assert "not a file; nothing was written" in err
    assert settings.read_text(encoding="utf-8") == initial_text


def test_claude_dry_run_prints_the_bak_the_run_would_create(tmp_path, monkeypatch, capsys):
    """When a custom skill file exists, --dry-run prints the .bak the run would create."""
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = claude_install.settings_path()
    skills = claude_install.skills_dir(settings)
    target = claude_install.skill_target(skills)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("Custom user skill text\n", encoding="utf-8")

    assert main(["install", "claude", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert ".bak" in out
    assert "would back up SKILL.md to" in out
