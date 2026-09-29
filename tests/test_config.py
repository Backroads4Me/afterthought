from __future__ import annotations

import pytest

from aft import config as configmod


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
