import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SCHED_", extra="ignore")

    database_url: str
    runtime_dir: Path = Path("/run/scheduler")
    repo_cache_dir: Path = Path("/var/cache/scheduler/repos")
    worker_id: str = Field(default_factory=socket.gethostname)
    poll_interval_s: float = 2.0
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"  # noqa: S104 - luistert binnen de container
    api_port: int = 8000

    # Fase 1: credentials worden als bestand gelezen uit
    # <dev_secrets_dir>/<openbao_path>/<openbao_key>. Vervalt met OpenBao in fase 3.
    dev_secrets_dir: Path | None = None

    ansible_host_key_checking: bool = True

    scheduler_lock_retry_s: float = 10.0
    # Maximale tijd tussen twee reconciles van schedules -> jobs (NOTIFY is sneller).
    scheduler_sync_interval_s: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()  # database_url komt uit de omgeving
