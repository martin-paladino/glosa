"""Settings.load: read .env + config.yaml, apply defaults, validate secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

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


def test_room_language_engine_mode_and_storage_defaults(env_file: Path, config_yaml: Path) -> None:
    settings = Settings.load(env_path=str(env_file), config_path=str(config_yaml))

    assert settings.rooms[0].language == "en"  # the room's free-session language
    assert settings.engine_mode == "live"
    assert settings.db_path == "data/glosa.db"
    assert settings.fake_fixture is None


def test_engine_mode_fake_and_room_language_from_yaml(env_file: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "engine_mode: fake\n"
        "rooms:\n"
        "  - id: r2\n"
        "    name: Sala 2\n"
        "    source_url: samples/es_clip.opus\n"
        "    language: es\n"
        "    default_targets: [en]\n",
        encoding="utf-8",
    )

    settings = Settings.load(env_path=str(env_file), config_path=str(config))

    assert settings.engine_mode == "fake"
    assert settings.rooms[0].language == "es"


def test_room_agenda_names_and_the_default_english_engine(env_file: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "default_engine_en: glossary\n"
        "rooms:\n"
        "  - id: main\n"
        "    name: Main Stage\n"
        "    agenda_names: [gran-sala, Gran sala]\n"
        "  - id: side\n"
        "    name: Side\n",
        encoding="utf-8",
    )

    settings = Settings.load(env_path=str(env_file), config_path=str(config))

    assert settings.rooms[0].agenda_names == ["gran-sala", "Gran sala"]
    assert settings.rooms[1].agenda_names == []
    assert settings.default_engine_en == "glossary"
    assert Settings.load(env_path=str(env_file), config_path=str(tmp_path / "none.yaml")).default_engine_en == "fast"


def test_engine_mode_local_from_yaml(env_file: Path, tmp_path: Path) -> None:  # Task 16
    config = tmp_path / "config.yaml"
    config.write_text("engine_mode: local\n", encoding="utf-8")

    settings = Settings.load(env_path=str(env_file), config_path=str(config))

    assert settings.engine_mode == "local"


def test_engine_mode_rejects_unknown_values(env_file: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("engine_mode: turbo\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        Settings.load(env_path=str(env_file), config_path=str(config))


def test_a_room_can_loop_its_file_only_in_the_fake_demo(env_file: Path) -> None:  # demo loop
    """``loop: true`` (a file source only) is set in config.demo-fake.yaml,
    never in config.demo.yaml (that one spends real API money)."""
    root = Path(__file__).resolve().parents[1]
    fake = Settings.load(env_path=str(env_file), config_path=str(root / "config.demo-fake.yaml"))
    real = Settings.load(env_path=str(env_file), config_path=str(root / "config.demo.yaml"))

    assert {r.id: r.loop for r in fake.rooms} == {"demo-en": True, "demo-es": True, "station-demo": False}
    assert not any(r.loop for r in real.rooms)


def test_loop_is_only_for_file_sources(env_file: Path, tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "rooms:\n  - {id: r1, name: R1, source_type: url, source_url: 'https://x.test/live', loop: true}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="loop"):
        Settings.load(env_path=str(env_file), config_path=str(config))


# ---- readable errors for a broken config.yaml -------------------------------


def test_malformed_yaml_raises_config_error_with_path_and_location(
    env_file: Path, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    # Bad indentation: not valid YAML, raises yaml.YAMLError from safe_load.
    config.write_text("rooms:\n  - id: main\n   name: bad indent\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_path=str(env_file), config_path=str(config))

    message = str(excinfo.value)
    assert str(config) in message
    # yaml's 0-indexed problem_mark says line 2 (0-based) -> reported as
    # line 3 for operators (1-indexed, like an editor).
    assert "line 3" in message
    assert "column" in message
    # The raw YAMLError must not leak out; it's wrapped.
    assert not isinstance(excinfo.value, yaml.YAMLError)


def test_malformed_yaml_config_error_chains_the_original_yaml_error(
    env_file: Path, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("rooms:\n  - id: main\n   name: bad indent\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_path=str(env_file), config_path=str(config))

    assert isinstance(excinfo.value.__cause__, yaml.YAMLError)


def test_yaml_that_is_not_a_mapping_raises_a_readable_config_error(
    env_file: Path, tmp_path: Path
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_path=str(env_file), config_path=str(config))

    message = str(excinfo.value)
    assert str(config) in message
    assert "mapping" in message


# ---- Ruling 35: a weak ADMIN_PASSWORD must not boot ------------------------


def test_load_rejects_a_blank_admin_password(tmp_path: Path, config_yaml: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="ADMIN_PASSWORD must be at least 8 characters"):
        Settings.load(env_path=str(env), config_path=str(config_yaml))


def test_load_rejects_a_short_admin_password(tmp_path: Path, config_yaml: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=1234567\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="ADMIN_PASSWORD must be at least 8 characters"):
        Settings.load(env_path=str(env), config_path=str(config_yaml))


def test_load_accepts_an_eight_character_admin_password(tmp_path: Path, config_yaml: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=12345678\n", encoding="utf-8")

    settings = Settings.load(env_path=str(env), config_path=str(config_yaml))

    assert settings.admin_password == "12345678"


def test_weak_admin_password_error_mentions_how_to_generate_one(tmp_path: Path, config_yaml: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=short\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="openssl rand -base64 18"):
        Settings.load(env_path=str(env), config_path=str(config_yaml))


# ---- fix round 2, #2: boot errors must never echo a secret back -----------


def test_a_rejected_admin_password_never_appears_in_the_error(tmp_path: Path, config_yaml: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=test-gemini-key\nADMIN_PASSWORD=hunter2\n", encoding="utf-8")

    with pytest.raises(ConfigError) as excinfo:
        Settings.load(env_path=str(env), config_path=str(config_yaml))

    assert "hunter2" not in str(excinfo.value)


def test_a_valid_admin_password_never_appears_in_an_unrelated_error(tmp_path: Path) -> None:
    # A missing GEMINI_API_KEY used to make pydantic dump the *whole* input
    # dict, including a perfectly valid ADMIN_PASSWORD, into the error. No
    # config.yaml here (a real one, like the `config_yaml` fixture, pads the
    # merged input dict with room data long enough that pydantic's own
    # error-message truncation happens to cut the password out too --
    # which would make this test pass by accident, leak or not).
    env = tmp_path / ".env"
    env.write_text("ADMIN_PASSWORD=a-valid-long-password\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="gemini_api_key") as excinfo:
        Settings.load(env_path=str(env), config_path=str(tmp_path / "does-not-exist.yaml"))

    assert "a-valid-long-password" not in str(excinfo.value)
