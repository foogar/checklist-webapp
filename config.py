"""Application configuration.

Loaded once at import, so a bad deployment fails immediately and loudly rather
than on the first request. A ~25 line .env reader replaces python-dotenv; real
environment variables always win over the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Placeholders that have been published in this repository at some point. A
# secret copied from a README is not a secret.
INSECURE_SECRETS = {
    "change_me_to_something_super_secure",
    "super-secret-homelab-key",
    "change_me",
    "changeme",
    "secret",
}

MIN_SECRET_LENGTH = 32
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def load_env_file(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE file.

    Supports comments, blank lines, an optional ``export`` prefix, and one pair
    of matching single or double quotes. That is the whole grammar; anything
    more elaborate belongs in the process environment.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            # A variable already set in the process environment is deliberate
            # (systemd, a container, a test harness) and must not be overridden.
            os.environ.setdefault(key, value)


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


def _resolve_database_path(configured: str) -> Path:
    path = Path(configured).expanduser() if configured else Path("checklist.db")
    if not path.is_absolute():
        # Anchored to the code, not the working directory: under systemd the
        # working directory is not something to rely on.
        path = BASE_DIR / path
    return path


@dataclass(frozen=True)
class Settings:
    base_dir: Path
    database_path: Path
    session_secret: str
    session_cookie_secure: bool
    csp_enabled: bool
    dev_auth_enabled: bool
    google_client_id: str
    google_client_secret: str
    public_base_url: str
    rate_limit_enabled: bool
    rate_limit_requests: int
    rate_limit_window: int
    rate_limit_auth_requests: int
    rate_limit_auth_window: int
    host: str
    port: int

    @property
    def cookie_name(self) -> str:
        # The __Host- prefix is only legal with Secure, Path=/, and no Domain.
        # Using it when secure means a sibling subdomain cannot plant a cookie
        # that this app would read.
        return "__Host-checklist" if self.session_cookie_secure else "checklist_session"

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


def load_settings() -> Settings:
    load_env_file(BASE_DIR / ".env")

    secret = os.getenv("SESSION_SECRET", "").strip()
    if not secret:
        raise RuntimeError("SESSION_SECRET must be set in .env")
    if secret.lower() in INSECURE_SECRETS:
        raise RuntimeError(
            "SESSION_SECRET is a known placeholder. Generate a real one with: "
            'python -c "import secrets; print(secrets.token_urlsafe(48))"'
        )
    if len(secret) < MIN_SECRET_LENGTH:
        raise RuntimeError(
            f"SESSION_SECRET must be at least {MIN_SECRET_LENGTH} characters "
            f"(got {len(secret)})."
        )

    cookie_secure = _flag("SESSION_COOKIE_SECURE", False)
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not public_base_url:
        scheme = "https" if cookie_secure else "http"
        public_base_url = f"{scheme}://localhost:{_int('PORT', 8000)}"

    return Settings(
        base_dir=BASE_DIR,
        database_path=_resolve_database_path(os.getenv("DATABASE_PATH", "").strip()),
        session_secret=secret,
        session_cookie_secure=cookie_secure,
        csp_enabled=_flag("CSP_ENABLED", True),
        dev_auth_enabled=_flag("DEV_AUTH_ENABLED", False),
        google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", "").strip(),
        public_base_url=public_base_url,
        rate_limit_enabled=_flag("RATE_LIMIT_ENABLED", True),
        rate_limit_requests=_int("RATE_LIMIT_REQUESTS", 600),
        rate_limit_window=_int("RATE_LIMIT_WINDOW", 60),
        rate_limit_auth_requests=_int("RATE_LIMIT_AUTH_REQUESTS", 20),
        rate_limit_auth_window=_int("RATE_LIMIT_AUTH_WINDOW", 60),
        host=os.getenv("HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_int("PORT", 8000),
    )


settings = load_settings()
