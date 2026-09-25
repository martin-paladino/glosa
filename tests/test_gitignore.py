"""B-Minor #3: .gitignore was missing config.yaml and .env.* (only .env),
while .dockerignore already excludes both. A real event's config.yaml can
carry private source URLs with credentials (Ruling 29), and an
.env.local/.env.prod is exactly the kind of file `git add -A` sweeps in by
accident. Checked with the real `git check-ignore`, not just a substring
match on the file, so this actually reflects what `git add -A` would do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _is_ignored(relpath: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", relpath], cwd=ROOT, capture_output=True, check=False
    )
    return result.returncode == 0


def test_a_real_event_config_yaml_is_gitignored() -> None:
    assert _is_ignored("config.yaml")


def test_env_variant_files_are_gitignored() -> None:
    assert _is_ignored(".env.local")
    assert _is_ignored(".env.prod")


def test_the_env_example_template_is_still_tracked() -> None:
    # .env.example carries no secrets and must stay committed -- a
    # too-broad `.env*` pattern would silently untrack it.
    assert not _is_ignored(".env.example")
