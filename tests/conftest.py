"""Shared pytest fixtures for the Glosa test suite."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    """A minimal, valid .env file with all three secrets."""
    path = tmp_path / ".env"
    path.write_text(
        "GEMINI_API_KEY=test-gemini-key\n"
        "TYPESAFE_API_KEY=test-typesafe-key\n"
        "ADMIN_PASSWORD=test-admin-password\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def config_yaml(tmp_path: Path) -> Path:
    """A minimal config.yaml overriding just event_name and one room."""
    path = tmp_path / "config.yaml"
    path.write_text(
        "event_name: Nerdearla Vibeathon\n"
        "timezone: America/Argentina/Buenos_Aires\n"
        "rooms:\n"
        "  - id: main\n"
        "    name: Main Stage\n"
        "    source_type: youtube\n"
        "    source_url: https://www.youtube.com/watch?v=example\n"
        "    default_targets: [es]\n",
        encoding="utf-8",
    )
    return path
