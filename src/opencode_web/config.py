from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}")
    return value


def _float(name: str, default: float, *, minimum: float = 0.1) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum:g}")
    return value


def _csv(name: str, default: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())
    if not values:
        raise ConfigError(f"{name} must not be empty")
    return values


@dataclass(frozen=True, slots=True)
class Settings:
    host: str
    port: int
    base_url: str
    database_path: Path
    cookie_secure: bool
    max_active_sessions: int
    max_active_sessions_hard_limit: int
    max_active_sessions_per_user: int
    max_active_sessions_per_user_hard_limit: int
    max_queued_messages: int
    allowed_workspace_roots: tuple[Path, ...]
    allow_shared_workspaces: bool
    opencode_bin: Path
    opencode_host: str
    opencode_port_start: int
    opencode_port_end: int
    opencode_username: str
    opencode_password: str
    default_agent: str
    allowed_agents: tuple[str, ...]
    default_model: str | None
    allowed_models: tuple[str, ...]
    opencode_start_timeout: int
    session_secret: str
    auth_session_ttl_hours: int
    bootstrap_admin_login: str | None
    bootstrap_admin_password_file: Path | None
    log_level: str
    opencode_idle_timeout_seconds: int = 900
    mattermost_url: str = "https://mattermost.example.com"
    mattermost_token_file: Path | None = None
    mattermost_ca_bundle: Path | None = None
    mattermost_timeout_seconds: float = 30.0
    mattermost_notification_max_attempts: int = 8
    # Interactive API docs (/docs, /redoc, /openapi.json); off in production.
    enable_docs: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        host = os.getenv("APP_HOST", "127.0.0.1").strip()
        try:
            ipaddress.ip_address(host)
        except ValueError as exc:
            raise ConfigError("APP_HOST must be an IPv4 or IPv6 address") from exc

        hard_limit = _int("MAX_ACTIVE_SESSIONS_HARD_LIMIT", 10)
        initial_limit = _int("MAX_ACTIVE_SESSIONS", 1)
        if initial_limit > hard_limit:
            raise ConfigError("MAX_ACTIVE_SESSIONS exceeds MAX_ACTIVE_SESSIONS_HARD_LIMIT")

        per_user_hard_limit = _int("MAX_ACTIVE_SESSIONS_PER_USER_HARD_LIMIT", hard_limit)
        per_user_limit = _int("MAX_ACTIVE_SESSIONS_PER_USER", 0, minimum=0)
        if per_user_limit > per_user_hard_limit:
            raise ConfigError(
                "MAX_ACTIVE_SESSIONS_PER_USER exceeds MAX_ACTIVE_SESSIONS_PER_USER_HARD_LIMIT"
            )

        raw_roots = _csv("ALLOWED_WORKSPACE_ROOTS", str(Path.cwd()))
        roots: list[Path] = []
        for raw_root in raw_roots:
            root = Path(raw_root).expanduser().resolve()
            if not root.is_absolute() or root == Path("/"):
                raise ConfigError(f"Unsafe workspace root: {root}")
            if not root.is_dir():
                raise ConfigError(f"Workspace root does not exist: {root}")
            roots.append(root)

        start = _int("OPENCODE_PORT_RANGE_START", 4100)
        end = _int("OPENCODE_PORT_RANGE_END", 4199)
        if start > end or end > 65535:
            raise ConfigError("Invalid OpenCode port range")

        secret = os.getenv("SESSION_SECRET", "")
        if len(secret) < 32:
            raise ConfigError("SESSION_SECRET must contain at least 32 characters")

        agents = _csv("OPENCODE_ALLOWED_AGENTS", "build,plan")
        default_agent = os.getenv("OPENCODE_DEFAULT_AGENT", "build").strip().lower()
        if default_agent not in agents:
            raise ConfigError("OPENCODE_DEFAULT_AGENT must be allowed")

        password_file = os.getenv("BOOTSTRAP_ADMIN_PASSWORD_FILE", "").strip()
        mattermost_url = os.getenv(
            "MATTERMOST_URL", "https://mattermost.example.com"
        ).rstrip("/")
        if not mattermost_url.startswith(("http://", "https://")):
            raise ConfigError("MATTERMOST_URL must be an HTTP(S) URL")
        mattermost_token_file = os.getenv("MATTERMOST_TOKEN_FILE", "").strip()
        mattermost_ca_bundle = os.getenv("MATTERMOST_CA_BUNDLE", "").strip()
        return cls(
            host=host,
            port=_int("APP_PORT", 8080),
            base_url=os.getenv("APP_BASE_URL", f"http://{host}:8080").rstrip("/"),
            database_path=Path(os.getenv("APP_DATABASE_PATH", "./data/opencode-web.db")),
            cookie_secure=_bool("APP_COOKIE_SECURE", False),
            max_active_sessions=initial_limit,
            max_active_sessions_hard_limit=hard_limit,
            max_active_sessions_per_user=per_user_limit,
            max_active_sessions_per_user_hard_limit=per_user_hard_limit,
            max_queued_messages=_int("MAX_QUEUED_MESSAGES", 1000),
            allowed_workspace_roots=tuple(roots),
            allow_shared_workspaces=_bool("ALLOW_SHARED_WORKSPACES", False),
            opencode_bin=Path(os.getenv("OPENCODE_BIN", "/usr/bin/opencode")),
            opencode_host=os.getenv("OPENCODE_HOST", "127.0.0.1"),
            opencode_port_start=start,
            opencode_port_end=end,
            opencode_username=os.getenv("OPENCODE_SERVER_USERNAME", "opencode"),
            opencode_password=os.getenv("OPENCODE_SERVER_PASSWORD", ""),
            default_agent=default_agent,
            allowed_agents=agents,
            default_model=os.getenv("OPENCODE_DEFAULT_MODEL", "").strip() or None,
            allowed_models=_csv("OPENCODE_ALLOWED_MODELS", "*"),
            opencode_start_timeout=_int("OPENCODE_START_TIMEOUT_SECONDS", 120),
            session_secret=secret,
            auth_session_ttl_hours=_int("AUTH_SESSION_TTL_HOURS", 24),
            bootstrap_admin_login=os.getenv("BOOTSTRAP_ADMIN_LOGIN", "admin").strip() or None,
            bootstrap_admin_password_file=Path(password_file) if password_file else None,
            opencode_idle_timeout_seconds=_int("OPENCODE_IDLE_TIMEOUT_SECONDS", 900, minimum=0),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            mattermost_url=mattermost_url,
            mattermost_token_file=(
                Path(mattermost_token_file) if mattermost_token_file else None
            ),
            mattermost_ca_bundle=(
                Path(mattermost_ca_bundle) if mattermost_ca_bundle else None
            ),
            mattermost_timeout_seconds=_float("MATTERMOST_TIMEOUT_SECONDS", 30),
            mattermost_notification_max_attempts=_int(
                "MATTERMOST_NOTIFICATION_MAX_ATTEMPTS", 8
            ),
            enable_docs=_bool("APP_ENABLE_DOCS", False),
        )

    def resolve_workspace_path(self, raw_path: str | Path) -> Path:
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            if len(self.allowed_workspace_roots) != 1:
                raise ConfigError(
                    "Relative workspace paths require exactly one ALLOWED_WORKSPACE_ROOTS entry"
                )
            candidate = self.allowed_workspace_roots[0] / candidate
        try:
            path = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ConfigError("Workspace path cannot be resolved") from exc
        if not any(
            path == root or path.is_relative_to(root) for root in self.allowed_workspace_roots
        ):
            raise ConfigError("Workspace is outside ALLOWED_WORKSPACE_ROOTS")
        return path

    def validate_workspace(self, raw_path: str | Path) -> Path:
        path = self.resolve_workspace_path(raw_path)
        if not path.is_dir():
            raise ConfigError("Workspace must be an existing directory")
        return path

    def ensure_workspace(self, raw_path: str | Path) -> Path:
        path = self.resolve_workspace_path(raw_path)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except FileExistsError as exc:
            raise ConfigError("Workspace path is not a directory") from exc
        except OSError as exc:
            detail = exc.strerror or type(exc).__name__
            raise ConfigError(f"Cannot create workspace: {detail}") from exc
        return self.validate_workspace(path)
