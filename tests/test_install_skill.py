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
    assert "name: aft-library-import" in skill_install.skill_text()
    assert "aft library survey" in skill_install.skill_text()
    assert "aft library validate" in skill_install.skill_text()
    assert "aft library import" in skill_install.skill_text()


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
    assert set(change.removed) == {target, target.parent}


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
    assert change.removed == (target,)


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
    assert set(change.removed) == {target, target.parent}


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


def test_the_last_shipped_legacy_skill_is_known():
    import hashlib

    digest = hashlib.sha256(legacy_text().encode("utf-8")).hexdigest()
    assert digest in skill_install.LEGACY_SHA256


def test_installing_retires_a_shipped_copy_under_the_old_name(tmp_path):
    old = tmp_path / "library-import" / "SKILL.md"
    old.parent.mkdir()
    old.write_text(legacy_text(), encoding="utf-8")
    assert skill_install.retire_legacy(tmp_path, dry_run=True).startswith("would remove")
    assert old.exists(), "a dry run removed the old skill"
    assert skill_install.retire_legacy(tmp_path).startswith("removed the old skill")
    assert not old.parent.exists()
    assert skill_install.retire_legacy(tmp_path) is None


def test_a_library_import_skill_afterthought_did_not_ship_is_kept(tmp_path):
    old = tmp_path / "library-import" / "SKILL.md"
    old.parent.mkdir()
    old.write_text(legacy_text() + "\nA line someone added.\n", encoding="utf-8")
    line = skill_install.retire_legacy(tmp_path)
    assert line is not None and line.startswith("kept")
    assert old.exists()
