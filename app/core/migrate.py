import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]


def alembic_config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return cfg


def current_revision() -> str | None:
    engine = create_engine(get_settings().database_url, poolclass=NullPool)
    try:
        with engine.connect() as conn:
            return MigrationContext.configure(conn).get_current_revision()
    finally:
        engine.dispose()


def upgrade_head() -> None:
    """Upgrade to head. Logs "schema upgraded" only when the revision changed; the
    Ansible role uses that to report `changed`."""
    before = current_revision()
    log.info("running migrations", extra={"revision": before})
    command.upgrade(alembic_config(), "head")
    after = current_revision()
    if after != before:
        log.info("schema upgraded", extra={"from_revision": before, "to_revision": after})
    log.info("migrations done", extra={"revision": after})
