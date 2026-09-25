"""Settings: load Glosa's configuration from a secrets file (.env) and an
event configuration file (config.yaml), apply defaults, and validate.

Secrets (GEMINI_API_KEY, TYPESAFE_API_KEY, ADMIN_PASSWORD) come only from the
.env file (never from config.yaml, never committed). Everything else (event
metadata, rooms, budget, prices, relay/VAD/segmenter tuning) comes from
config.yaml, falling back to the defaults below when the key is absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# Ruling 35: a blank or weak /admin password must fail loudly at boot,
# rather than quietly accept the empty default some deploy tooling leaves
# behind.
ADMIN_PASSWORD_MIN_LEN = 8


class ConfigError(RuntimeError):
    """Raised when Settings.load() cannot build a valid configuration."""


class Branding(BaseModel):
    logo_url: str = ""
    primary: str = "#0B5FFF"
    accent: str = "#FF7A00"


class RoomCfg(BaseModel):
    """Static, config-time description of a room (see config.yaml `rooms`).

    Runtime-only fields (slug, mode, public_token) live on glosa.models.Room
    and are assigned when the room is created, not here.
    """

    id: str
    name: str
    source_type: Literal["file", "url", "youtube", "emitter"] = "file"
    source_url: str | None = None
    default_targets: list[str] = Field(default_factory=list)
    # Language spoken in the room's "free session" (the synthetic talk a room
    # with a source runs when there is no agenda). Targets: default_targets.
    language: str = "en"
    # Names this room goes by in an external agenda (e.g. Nerdearla's room
    # slug "gran-sala"). An agenda import maps them, the room's id and its
    # name to this room.
    agenda_names: list[str] = Field(default_factory=list)
    # The demo loop (config.demo-fake.yaml only: a real engine would spend API
    # money all day): a free session whose file ends plays it again instead
    # of ending (glosa/room.py). "file" sources only.
    loop: bool = False

    @model_validator(mode="after")
    def _loop_needs_a_file(self) -> "RoomCfg":
        if self.loop and self.source_type != "file":
            raise ValueError(f"room {self.id!r}: loop is only for source_type 'file'")
        return self


class Prices(BaseModel):
    lt_per_min: float = 0.0368
    transcribe_per_min: float = 0.009
    flash_lite_in_per_m: float = 0.30
    flash_lite_out_per_m: float = 2.50


class RelayCfg(BaseModel):
    standby_at: float = 510
    force_at: float = 570
    stall_timeout: float = 8.0


class VadCfg(BaseModel):
    pause_ms: int = 400
    min_speech_s: float = 1.5


class SegmenterCfg(BaseModel):
    comma_min_words: int = 5
    max_words: int = 14
    max_wait_s: float = 3.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        # Fix round 2, #2: pydantic's default ValidationError repr echoes
        # the rejected (or, for an unrelated field, the whole merged) input
        # back verbatim -- for Settings that can be a secret straight out
        # of .env. hide_input_in_errors replaces it with a redaction marker
        # everywhere pydantic renders one, including Settings.load()'s
        # ConfigError below.
        hide_input_in_errors=True,
    )

    # --- secrets, from .env only ---
    gemini_api_key: str
    typesafe_api_key: str | None = None
    admin_password: str

    # --- event configuration, from config.yaml ---
    event_name: str = "Glosa"
    timezone: str = "UTC"
    branding: Branding = Field(default_factory=Branding)
    audience_mode: Literal["all", "qr_only"] = "all"
    rooms: list[RoomCfg] = Field(default_factory=list)
    budget_usd: float = 10.0
    exports_public: bool = True
    prices: Prices = Field(default_factory=Prices)
    relay: RelayCfg = Field(default_factory=RelayCfg)
    vad: VadCfg = Field(default_factory=VadCfg)
    segmenter: SegmenterCfg = Field(default_factory=SegmenterCfg)
    default_export_shift_s: float = 2.4
    # Task 19: stop sending audio to the engine after this long with no
    # speech (glosa/audio/gate.py's SilenceGate); 0 disables the gate.
    silence_gate_s: float = 20.0
    # Engine an imported English talk gets when the agenda does not say
    # (Spanish talks get "glossary": verbatim transcription).
    default_engine_en: Literal["fast", "glossary"] = "fast"
    # "live": Gemini Live Translate. "fake": FakeEngine replaying a recorded
    # session (fake_fixture, by default samples/fixtures/lt_en.jsonl), so a
    # demo or a load test runs without spending API credit. "local" (Task
    # 16): Parakeet + TranslateGemma via MLX, 100% on-device, Apple silicon
    # only (glosa/engines/local.py, glosa/text/local_translator.py) -- no
    # cloud API, no API key spent, one shared model per process.
    engine_mode: Literal["live", "fake", "local"] = "live"
    fake_fixture: str | None = None
    db_path: str = "data/glosa.db"

    @field_validator("admin_password")
    @classmethod
    def _admin_password_must_be_strong(cls, value: str) -> str:
        # Ruling 35. glosa/web/auth.py also refuses to sign or verify a
        # session for a blank admin_password (defense in depth), but the
        # loud failure belongs here, at boot.
        if len(value) < ADMIN_PASSWORD_MIN_LEN:
            raise ValueError(
                f"ADMIN_PASSWORD must be at least {ADMIN_PASSWORD_MIN_LEN} characters; "
                "generate one with `openssl rand -base64 18`"
            )
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Only read explicit config.yaml values (init_settings) and the
        # explicit .env file (dotenv_settings). Deliberately drop
        # env_settings (the real process environment) and
        # file_secret_settings (Docker-style secret files) so loading is
        # deterministic and isolated from whatever happens to be exported in
        # the caller's shell.
        return (init_settings, dotenv_settings)

    @classmethod
    def load(cls, env_path: str = ".env", config_path: str = "config.yaml") -> "Settings":
        """Load settings from env_path (secrets) and config_path (event config).

        Raises ConfigError if the resulting configuration is invalid (e.g.
        GEMINI_API_KEY is missing from env_path).
        """
        yaml_path = Path(config_path)
        data: dict[str, Any] = {}
        if yaml_path.exists():
            try:
                loaded = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError as exc:
                location = ""
                mark = getattr(exc, "problem_mark", None)
                if mark is not None:
                    location = f" (line {mark.line + 1}, column {mark.column + 1})"
                raise ConfigError(f"{yaml_path}: invalid YAML{location}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"{yaml_path} must contain a YAML mapping at the top level")
            data = loaded

        try:
            return cls(_env_file=env_path, **data)  # type: ignore[call-arg]
        except ValidationError as exc:
            raise ConfigError(f"Invalid Glosa configuration: {exc}") from exc
