"""Tests for the afterthought library import skill installer (BUILD-PLAN M7.5, DESIGN 11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aft.install import claude as claude_install
from aft.install import pi as pi_install
from aft.install import skill as skill_install
from aft.install.skill import InstallError


def test_skill_source_exists_and_matches_text():
    source = skill_install.skill_source()
    assert source.is_file()
    assert skill_install.skill_text() == source.read_text(encoding="utf-8")
    assert "name: aft" in skill_install.skill_text()
    procedure = skill_install.file_text("library-import.md")
    assert "aft library survey" in procedure
    assert "aft library validate" in procedure
    assert "aft library import" in procedure


def test_install_creates_skill_in_empty_directory(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)

    change = skill_install.install(skills)

    assert change.changed is True
    assert change.backup is None
    assert change.target == target
    assert target.is_file()
    assert target.read_text(encoding="utf-8") == skill_install.skill_text()


def test_install_twice_is_idempotent(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)

    first = skill_install.install(skills)
    assert first.changed is True

    second = skill_install.install(skills)
    assert second.changed is False
    assert second.backup is None
    assert second.diff == ""
    assert target.read_text(encoding="utf-8") == skill_install.skill_text()
    assert list(target.parent.glob("*.bak")) == []


def test_install_backs_up_modified_content(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    target.parent.mkdir(parents=True)
    custom_text = "# Custom skill text\n"
    target.write_text(custom_text, encoding="utf-8")

    change = skill_install.install(skills)

    assert change.changed is True
    assert change.backup is not None
    assert change.backup.is_file()
    assert change.backup.read_text(encoding="utf-8") == custom_text
    assert target.read_text(encoding="utf-8") == skill_install.skill_text()


def test_install_dry_run_writes_nothing(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)

    change = skill_install.install(skills, dry_run=True)

    assert change.changed is True
    assert change.backup is None
    assert change.diff != ""
    assert not target.exists()
    assert not target.parent.exists()


def test_uninstall_removes_file_and_empty_container(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    skill_install.install(skills)
    assert target.is_file()

    change = skill_install.uninstall(skills)

    assert change.changed is True
    assert not target.exists()
    assert not target.parent.exists()
    assert skills.is_dir()
    assert set(change.removed) == {target, target.parent / "library-import.md", target.parent}


def test_uninstall_leaves_container_if_other_files_exist(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    skill_install.install(skills)
    other = target.parent / "other.txt"
    other.write_text("hello", encoding="utf-8")

    change = skill_install.uninstall(skills)

    assert change.changed is True
    assert not target.exists()
    assert target.parent.is_dir()
    assert other.is_file()
    assert change.removed == (target, target.parent / "library-import.md")


def test_uninstall_preserves_custom_content_in_backup(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    target.parent.mkdir(parents=True)
    custom_text = "# Custom notes\n"
    target.write_text(custom_text, encoding="utf-8")

    change = skill_install.uninstall(skills)

    assert change.changed is True
    assert change.backup is not None
    assert change.backup.read_text(encoding="utf-8") == custom_text
    assert not target.exists()


def test_uninstall_dry_run_removes_nothing(tmp_path):
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    skill_install.install(skills)

    change = skill_install.uninstall(skills, dry_run=True)

    assert change.changed is True
    assert target.is_file()
    assert target.parent.is_dir()
    assert set(change.removed) == {target, target.parent / "library-import.md", target.parent}


def test_refuses_when_path_is_not_directory(tmp_path):
    skills = tmp_path / "skills"
    skills.write_text("a file", encoding="utf-8")

    with pytest.raises(InstallError, match="not a directory"):
        skill_install.install(skills)


def test_claude_adapter_skills_wiring(tmp_path):
    claude_dir = tmp_path / ".claude"
    settings = claude_dir / "settings.json"
    skills = claude_install.skills_dir(settings)
    assert skills == claude_dir / "skills"

    target = claude_install.skill_target(skills)
    change = claude_install.install_skill(skills)
    assert change.changed is True
    assert target.is_file()

    uninst = claude_install.uninstall_skill(skills)
    assert uninst.changed is True
    assert not target.exists()


def test_pi_adapter_skills_wiring(tmp_path, monkeypatch):
    agent_dir = tmp_path / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    skills = pi_install.skills_dir()
    assert skills == agent_dir / "skills"

    target = pi_install.skill_target(skills)
    change = pi_install.install_skill(skills)
    assert change.changed is True
    assert target.is_file()

    uninst = pi_install.uninstall_skill(skills)
    assert uninst.changed is True
    assert not target.exists()


def test_install_with_backup_then_uninstall_keeps_the_persons_text(tmp_path):
    """Installing over custom text creates a backup, and uninstalling removes only the
    shipped skill, preserving the backup holding the person's text.
    """
    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    target.parent.mkdir(parents=True, exist_ok=True)
    custom_text = "# My Custom Import Procedure\n"
    target.write_text(custom_text, encoding="utf-8")

    # Install creates backup of custom text and writes shipped content
    change = skill_install.install(skills)
    assert change.changed is True
    assert change.backup is not None
    assert change.backup.is_file()
    assert change.backup.read_text(encoding="utf-8") == custom_text
    assert target.read_text(encoding="utf-8") == skill_install.skill_text()

    # Uninstall removes shipped SKILL.md, but preserves the backup with person's text
    un_change = skill_install.uninstall(skills)
    assert un_change.changed is True
    assert not target.exists()
    assert change.backup.is_file()
    assert change.backup.read_text(encoding="utf-8") == custom_text
    assert target.parent.is_dir()


def legacy_text() -> str:
    """The skill as afterthought last shipped it under its name before the aft- prefix."""
    fixture = Path(__file__).parent / "fixtures" / "legacy-library-import-SKILL.md"
    return fixture.read_text(encoding="utf-8")


def test_the_last_shipped_legacy_skills_are_known():
    import hashlib

    digest = hashlib.sha256(legacy_text().encode("utf-8")).hexdigest()
    assert digest in skill_install.LEGACY["library-import"]
    assert len(skill_install.LEGACY["aft-library-import"]) == 1


@pytest.mark.parametrize("old_name", ["library-import", "aft-library-import"])
def test_installing_retires_a_shipped_copy_under_an_old_name(tmp_path, old_name, monkeypatch):
    import hashlib

    text = legacy_text()
    monkeypatch.setitem(
        skill_install.LEGACY, old_name, frozenset({hashlib.sha256(text.encode()).hexdigest()})
    )
    old = tmp_path / old_name / "SKILL.md"
    old.parent.mkdir()
    old.write_text(text, encoding="utf-8")
    assert skill_install.retire_legacy(tmp_path, dry_run=True) == [
        f"would remove the old skill {old.parent}"
    ]
    assert old.exists(), "a dry run removed the old skill"
    assert skill_install.retire_legacy(tmp_path) == [f"removed the old skill {old.parent}"]
    assert not old.parent.exists()
    assert skill_install.retire_legacy(tmp_path) == []


def test_a_library_import_skill_afterthought_did_not_ship_is_kept(tmp_path):
    old = tmp_path / "library-import" / "SKILL.md"
    old.parent.mkdir()
    old.write_text(legacy_text() + "\nA line someone added.\n", encoding="utf-8")
    [line] = skill_install.retire_legacy(tmp_path)
    assert line.startswith("kept")
    assert old.exists()


def test_a_custom_procedure_file_is_backed_up_before_it_is_replaced(tmp_path):
    skills = tmp_path / "skills"
    procedure = skills / "aft" / "library-import.md"
    procedure.parent.mkdir(parents=True)
    procedure.write_text("# My own import notes\n", encoding="utf-8")
    change = skill_install.install(skills)
    assert [backup.name.split(".aft-")[0] for backup in change.backups] == ["library-import.md"]
    assert change.backups[0].read_text(encoding="utf-8") == "# My own import notes\n"
    assert procedure.read_text(encoding="utf-8") == skill_install.file_text("library-import.md")


def test_the_skill_names_every_aft_command():
    """An agent asked for any of afterthought's commands finds it in the skill."""
    import argparse

    from aft import cli

    text = skill_install.skill_text() + skill_install.file_text("library-import.md")

    def names(parser, prefix):
        """Each command as the alternatives it can be typed as: its name and its aliases."""
        found = []
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                spelled: dict[int, list[str]] = {}
                for name, sub in action.choices.items():
                    spelled.setdefault(id(sub), []).append(f"{prefix} {name}")
                for spellings in spelled.values():
                    found.append(spellings)
                    sub = action.choices[spellings[0].rsplit(" ", 1)[1]]
                    found += names(sub, spellings[0])
        return found

    missing = [
        spellings[0]
        for spellings in names(cli._parser(), "aft")
        if not any(f"`{name}" in text for name in spellings)
    ]
    assert missing == [], f"commands the aft skill does not name: {missing}"


def test_every_shipped_file_is_in_the_list_of_shipped_versions():
    """Changing a skill file means adding its sha256 to SHIPPED_SHA256, so an installed copy
    of it is later recognized as afterthought's and replaced without a backup."""
    import hashlib

    for name in skill_install.FILES:
        digest = hashlib.sha256(skill_install.file_text(name).encode("utf-8")).hexdigest()
        assert digest in skill_install.SHIPPED_SHA256[name], f"add {digest} for {name}"


def test_an_earlier_shipped_version_is_replaced_without_a_backup(tmp_path, monkeypatch):
    import hashlib

    skills = tmp_path / "skills"
    target = skill_install.target_path(skills)
    target.parent.mkdir(parents=True)
    earlier = "---\nname: aft\n---\nAn earlier release.\n"
    target.write_text(earlier, encoding="utf-8")
    digest = hashlib.sha256(earlier.encode("utf-8")).hexdigest()
    shipped = {**skill_install.SHIPPED_SHA256}
    shipped["SKILL.md"] = shipped["SKILL.md"] | {digest}
    monkeypatch.setattr(skill_install, "SHIPPED_SHA256", shipped)
    change = skill_install.install(skills)
    assert change.changed and change.backups == ()
    assert list(target.parent.glob("*.bak")) == []


def test_skill_text_contains_marker_guidance():
    text = skill_install.skill_text()
    assert 'A prompt starting with "aft off"' in text
    assert "is not a request for an aft command" in text
