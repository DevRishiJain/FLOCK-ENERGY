import os
from dataclasses import dataclass, field


def _b(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() in ("1", "true", "yes")


@dataclass
class Settings:
    portal_url: str = field(default_factory=lambda: os.getenv("PORTAL_URL", "https://urja-ops.flockenergy.tech"))
    portal_email: str = field(default_factory=lambda: os.getenv("PORTAL_EMAIL", "operator@urja.local"))
    portal_password: str = field(default_factory=lambda: os.getenv("PORTAL_PASSWORD", ""))
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "urja.db"))
    admin_api_key: str = field(default_factory=lambda: os.getenv("ADMIN_API_KEY", "dev-admin-key"))
    reader_api_key: str = field(default_factory=lambda: os.getenv("READER_API_KEY", "dev-reader-key"))
    sync_interval_hours: float = field(default_factory=lambda: float(os.getenv("SYNC_INTERVAL_HOURS", "2")))
    sync_on_start: bool = field(default_factory=lambda: _b("SYNC_ON_START", "true"))
    rate_limit_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MIN", "120")))
    request_delay_s: float = field(default_factory=lambda: float(os.getenv("PORTAL_REQUEST_DELAY_S", "0.15")))
    portal_max_per_min: int = field(default_factory=lambda: int(os.getenv("PORTAL_MAX_PER_MIN", "100")))
