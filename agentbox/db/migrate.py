"""Alembic entry points that include migrations shipped by installed extensions."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config

from alembic import command

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    from agentbox.extensions import registry

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    locations = [str(ROOT / "alembic" / "versions"), *registry().migration_dirs()]
    cfg.set_main_option("version_locations", " ".join(locations))
    return cfg


def upgrade(revision: str = "heads") -> None:
    command.upgrade(alembic_config(), revision)


def downgrade(revision: str = "base") -> None:
    command.downgrade(alembic_config(), revision)
