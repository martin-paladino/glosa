"""Settings.load: read .env + config.yaml, apply defaults, validate secrets."""

from __future__ import annotations

from pathlib import Path

import pytest

from glosa.config import ConfigError, Settings


def test_load_applies_defaults_and_overrides(env_file: Path, config_yaml: Path) -> None:
    settings = Settings.load(env_path=str(env_file), config_path=str(config_yaml))

    # secrets, loaded from .env
    assert settings.gemini_api_key == "test-gemini-key"
    assert settings.typesafe_api_key == "test-typesafe-key"
    assert settings.admin_password == "test-admin-password"

    # overridden by config.yaml
    assert settings.event_name == "Nerdearla Vibeathon"
    assert settings.timezone == "America/Argentina/Buenos_Aires"
    assert len(settings.rooms) == 1
    assert settings.rooms[0].id == "main"
    assert settings.rooms[0].name == "Main Stage"
    assert settings.rooms[0].source_type == "youtube"
    assert settings.rooms[0].default_targets == ["es"]

    # defaults from the brief, untouched by config.yaml
    assert settings.audience_mode == "all"
    assert settings.budget_usd == 10.0
    assert settings.exports_public is True
    assert settings.default_export_shift_s == 2.4
    assert settings.branding.primary
    assert settings.prices.lt_per_min == 0.0368
    assert settings.prices.transcribe_per_min == 0.009
    assert settings.prices.flash_lite_in_per_m == 0.30
    assert settings.prices.flash_lite_out_per_m == 2.50
    assert settings.relay.standby_at == 510
    assert settings.relay.force_at == 570
    assert settings.relay.stall_timeout == 8.0
    assert settings.vad.pause_ms == 400
    assert settings.vad.min_speech_s == 1.5
    assert settings.segmenter.comma_min_words == 5
    assert settings.segmenter.max_words == 14
    assert settings.segmenter.max_wait_s == 3.0


def test_load_without_config_yaml_uses_all_defaults(env_file: Path, tmp_path: Path) -> None:
    missing_yaml = tmp_path / "does-not-exist.yaml"

    settings = Settings.load(env_path=str(env_file), config_path=str(missing_yaml))

    assert settings.event_name == "Glosa"
    assert settings.rooms == []
    assert settings.budget_usd == 10.0


def test_load_rejects_missing_gemini_api_key(tmp_path: Path, config_yaml: Path) -> None:
    env_without_gemini = tmp_path / ".env"
    env_without_gemini.write_text(
        "TYPESAFE_API_KEY=test-typesafe-key\nADMIN_PASSWORD=test-admin-password\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="gemini_api_key"):
        Settings.load(env_path=str(env_without_gemini), config_path=str(config_yaml))


def test_load_rejects_missing_env_file_entirely(tmp_path: Path, config_yaml: Path) -> None:
    missing_env = tmp_path / "does-not-exist.env"

    with pytest.raises(ConfigError):
        Settings.load(env_path=str(missing_env), config_path=str(config_yaml))


def test_typesafe_api_key_is_optional(tmp_path: Path) -> None:
    env_without_typesafe = tmp_path / ".env"
    env_without_typesafe.write_text(
        "GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=test-admin-password\n",
        encoding="utf-8",
    )

    settings = Settings.load(env_path=str(env_without_typesafe), config_path=str(tmp_path / "missing.yaml"))

    assert settings.typesafe_api_key is None
