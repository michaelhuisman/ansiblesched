import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAMPLIGHTER_", extra="ignore")

    database_url: str
    runtime_dir: Path = Path("/run/lamplighter")
    repo_cache_dir: Path = Path("/var/cache/lamplighter/repos")
    worker_id: str = Field(default_factory=socket.gethostname)
    poll_interval_s: float = 2.0
    log_level: str = "INFO"

    api_host: str = "0.0.0.0"  # noqa: S104 - luistert binnen de container
    api_port: int = 8000
    # Alleen van deze proxies (komma-gescheiden IP's/CIDR's) worden X-Forwarded-For en
    # X-Forwarded-Proto overgenomen.
    trusted_proxies: str = "127.0.0.1"
    # Gezet: /metrics vereist `Authorization: Bearer <token>`. Leeg: /metrics is open.
    metrics_token: SecretStr | None = None

    # --- retentie (dagen; 0 = nooit opruimen) ---
    retention_events_days: int = Field(default=30, ge=0)
    retention_runs_days: int = Field(default=180, ge=0)
    retention_audit_days: int = Field(default=365, ge=0)
    retention_tokens_days: int = Field(default=30, ge=0)
    retention_hour_utc: int = Field(default=3, ge=0, le=23)

    # --- reaper: runs op 'running' zonder levende worker ---
    reaper_grace_s: int = Field(default=120, ge=10)
    reaper_interval_s: int = Field(default=60, ge=5)

    ansible_host_key_checking: bool = True

    scheduler_lock_retry_s: float = 10.0
    # Maximale tijd tussen twee reconciles van schedules -> jobs (NOTIFY is sneller).
    scheduler_sync_interval_s: float = 5.0

    # Basis-URL voor links in notificaties, bv. https://scheduler.example.org
    public_url: str = "http://localhost:8000"
    # Webhook-URL's en de HMAC-secret staan in OpenBao (keys `urls` en `hmac_secret`).
    # Leeg = geen webhooks. Alleen de scheduler leest dit pad.
    webhook_openbao_path: str | None = None
    webhook_cache_s: float = 60.0

    # --- OpenBao (AppRole) ---
    openbao_addr: str | None = None
    openbao_role_id: str | None = None
    openbao_secret_id: SecretStr | None = None
    openbao_kv_mount: str = "secret"
    openbao_ca_cert: Path | None = None  # CA-bundle voor TLS; leeg = systeem-CA's

    # --- auth ---
    auth_local_enabled: bool = True
    session_cookie_secure: bool = True  # alleen in dev via http uitzetten
    session_idle_s: int = 8 * 3600
    session_max_s: int = 24 * 3600
    login_max_failures: int = 5
    login_lockout_s: int = 15 * 60

    # OIDC (Keycloak). Leeg = alleen lokale users.
    oidc_issuer: str | None = None  # zoals in de `iss`-claim, bv. https://kc/realms/x
    # Discovery via een intern adres (bv. http://keycloak:8080/...); default: van issuer.
    oidc_discovery_url: str | None = None
    oidc_client_id: str = "lamplighter"
    oidc_client_secret: SecretStr | None = None
    # Verwachte `aud` in access tokens; default de client-id.
    oidc_audience: str | None = None

    @property
    def openbao_enabled(self) -> bool:
        return bool(self.openbao_addr and self.openbao_role_id and self.openbao_secret_id)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # database_url komt uit de omgeving
