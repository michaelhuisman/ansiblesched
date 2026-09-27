import logging
from pathlib import Path

from alembic import command
from alembic.config import Config

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def upgrade_head() -> None:
    log.info("running migrations")
    command.upgrade(alembic_config(), "head")
    log.info("migrations done")
