import hashlib
import hmac
import threading
import time

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from . import db
from .config import Settings


api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="reader or admin key")


def hash_key(k: str) -> str:
    return hashlib.sha256(k.encode()).hexdigest()


def seed_keys(s: Settings) -> None:
    c = db.connect(s.db_path)
    for name, key, role in (("bootstrap-admin", s.admin_api_key, "admin"), ("bootstrap-reader", s.reader_api_key, "reader")):
        c.execute("INSERT INTO api_keys(key_hash,name,role) VALUES (?,?,?) ON CONFLICT(key_hash) DO UPDATE SET role=excluded.role", (hash_key(key), name, role))
    c.commit()
    c.close()


class RateLimiter:
    """Token bucket per API key; in-memory (single process) by design, see README."""

    def __init__(self, per_min: int):
        self.rate, self.cap = per_min / 60.0, float(per_min)
        self.state: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def check(self, key: str) -> float:
        """Returns 0 if allowed else seconds to wait."""
        with self.lock:
            tokens, last = self.state.get(key, (self.cap, time.monotonic()))
            now = time.monotonic()
            tokens = min(self.cap, tokens + (now - last) * self.rate)
            if tokens < 1:
                self.state[key] = (tokens, now)
                return (1 - tokens) / self.rate
            self.state[key] = (tokens - 1, now)
            return 0


def _principal(request: Request, key: str | None) -> dict:
    key = key or ""
    row = request.app.state.db().execute("SELECT name, role, key_hash FROM api_keys WHERE key_hash=?", (hash_key(key),)).fetchone() if key else None
    if not row or not hmac.compare_digest(row["key_hash"], hash_key(key)):
        raise HTTPException(401, "Missing or invalid API key (send X-API-Key).")
    wait = request.app.state.limiter.check(row["key_hash"])
    if wait:
        raise HTTPException(429, "Rate limit exceeded.", headers={"Retry-After": str(int(wait) + 1)})
    return dict(row)


def reader(request: Request, key: str | None = Security(api_key_header)) -> dict:
    return _principal(request, key)


def admin(request: Request, key: str | None = Security(api_key_header)) -> dict:
    p = _principal(request, key)
    if p["role"] != "admin":
        raise HTTPException(403, "Admin role required.")
    return p
