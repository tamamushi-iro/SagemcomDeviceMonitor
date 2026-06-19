"""Configuration for BellDeviceMonitor.

Values are loaded (highest precedence first) from: explicit init args, ``BCDM_``
environment variables, a ``.env`` file, then ``config.toml`` at the project root.
``config.toml`` is gitignored and holds the router password.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.toml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file=CONFIG_FILE,
        env_prefix="BCDM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Router (Bell / Sagemcom F@st gateway) ---
    router_host: str = "192.168.2.1"
    router_user: str = "admin"
    router_password: str = ""
    encryption: str = "SHA512"  # "SHA512" (Home Hub) or "MD5"
    verify_ssl: bool = True

    # --- Poller ---
    poll_interval_seconds: float = 30.0
    retention_days: int = 365

    # --- Dashboard auth (HTTP Basic) ---
    auth_user: str = "admin"
    auth_password: str = "admin"

    # --- ntfy alerts ---
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str = "bcdm-alerts"
    ntfy_token: str = ""  # optional bearer token for protected topics
    alert_new_device: bool = True

    # --- Dev-only Windows route helper (no-op on the Pi) ---
    manage_dev_route: bool = False
    dev_route_dest: str = "192.168.2.0"
    dev_route_mask: str = "255.255.255.0"
    dev_route_gw: str = "192.168.7.1"

    # --- Storage ---
    db_path: str = "bcdm.db"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: init args > env vars > .env > config.toml > file secrets.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    @property
    def db_file(self) -> Path:
        """Absolute path to the SQLite DB (relative paths resolve from project root)."""
        p = Path(self.db_path)
        return p if p.is_absolute() else PROJECT_ROOT / p


@lru_cache
def get_settings() -> Settings:
    return Settings()
