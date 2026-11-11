from __future__ import annotations

import base64
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


BACKEND = Path(__file__).resolve().parents[1]
PROJECT = BACKEND.parent
DEFAULT_TOGETHER_MODEL = "zai-org/GLM-5.3-Flash"


def together_parameters(model: str) -> dict:
    # GLM-5.3 cannot disable thinking. Its documented low-effort setting fits
    # these bounded tool/JSON calls without spending the output on a long trace.
    if model in {"zai-org/GLM-5.3", "zai-org/GLM-5.3-Flash"}:
        return {"temperature": 1.0, "top_p": 0.95, "reasoning_effort": "low"}
    return {"temperature": 0}


def origins(value: str) -> tuple[str, ...]:
    result = []
    for raw in value.split(","):
        candidate = raw.strip().rstrip("/")
        if not candidate:
            continue
        try:
            parsed = urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("Use exact http or https origins for INVOICECLEAR_ALLOWED_ORIGINS.") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.path or parsed.query or parsed.fragment
                or "*" in candidate or any(c.isspace() for c in candidate)
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError("Use exact http or https origins for INVOICECLEAR_ALLOWED_ORIGINS.")
        if candidate not in result:
            result.append(candidate)
    return tuple(result)


def connection_origin(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        loopback = parsed.hostname == "localhost"
        if parsed.hostname:
            try:
                loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                pass
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username
                 and not parsed.password and parsed.path in {"", "/"} and not parsed.query
                 and not parsed.fragment and not any(c.isspace() or ord(c) < 32 for c in value) and "*" not in value
                 and (port is None or 1 <= port <= 65535) and (parsed.scheme == "https" or loopback))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError(f"{name} must be an exact HTTPS origin or HTTP loopback origin.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    database_path: Path
    web_path: Path
    mode: str = "demo"
    email: str = "finance@invoiceclear.demo"
    password: str = "invoiceclear-demo"
    host: str = "127.0.0.1"
    port: int = 8100
    allowed_origins: tuple[str, ...] = ()
    provider: str = "rules"
    together_key: str | None = None
    together_model: str = DEFAULT_TOGETHER_MODEL
    ttl_seconds: int = 43200
    backups_enabled: bool = True
    public_url: str = ""
    frontend_url: str = ""
    token_key: str = ""
    qbo_client_id: str = ""
    qbo_client_secret: str = ""
    qbo_environment: str = "sandbox"
    qbo_po_field_id: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    agent_enabled: bool = False

    def __post_init__(self):
        if type(self.agent_enabled) is not bool:
            raise ValueError("INVOICECLEAR_AGENT_ENABLED must be true or false.")
        if self.agent_enabled:
            if not isinstance(self.together_key, str) or not self.together_key.strip():
                raise ValueError("Invoice investigation requires TOGETHER_API_KEY on the backend.")
            if (not isinstance(self.together_model, str) or not self.together_model.strip()
                    or len(self.together_model) > 200 or any(ord(c) < 33 for c in self.together_model)):
                raise ValueError("Set TOGETHER_MODEL to an available tool-calling model.")
        if self.qbo_environment not in {"sandbox", "production"}:
            raise ValueError("QBO_ENVIRONMENT must be sandbox or production.")
        if self.qbo_po_field_id and not re.fullmatch(r"[0-9]{1,30}", self.qbo_po_field_id):
            raise ValueError("QBO_PO_FIELD_ID must be a numeric custom-field definition ID.")
        for name, client, secret in (("QBO", self.qbo_client_id, self.qbo_client_secret),
                                     ("GOOGLE", self.google_client_id, self.google_client_secret)):
            if bool(client) != bool(secret):
                raise ValueError(f"Set both {name}_CLIENT_ID and {name}_CLIENT_SECRET.")
            if any(not isinstance(v, str) or len(v) > 8192 or any(ord(c) < 32 for c in v)
                   for v in (client, secret)):
                raise ValueError(f"{name} client credentials are invalid.")
        for field, name in (("public_url", "INVOICECLEAR_PUBLIC_URL"), ("frontend_url", "INVOICECLEAR_FRONTEND_URL")):
            if getattr(self, field):
                object.__setattr__(self, field, connection_origin(getattr(self, field), name))
        if self.token_key:
            try:
                valid = (isinstance(self.token_key, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}=", self.token_key)
                         and len(base64.urlsafe_b64decode(self.token_key)) == 32)
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise ValueError("INVOICECLEAR_TOKEN_KEY must be a Fernet key for encrypted provider credentials.")
        if self.mode == "private" and (self.qbo_client_id or self.google_client_id):
            if not self.token_key or not self.public_url or not self.frontend_url:
                raise ValueError("Private provider connections require INVOICECLEAR_TOKEN_KEY, INVOICECLEAR_PUBLIC_URL and INVOICECLEAR_FRONTEND_URL.")

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("INVOICECLEAR_MODE", "demo").strip()
        provider = os.getenv("INVOICECLEAR_PROVIDER", "rules").strip()
        backups = os.getenv("INVOICECLEAR_BACKUPS_ENABLED", "true").strip().lower()
        agent = os.getenv("INVOICECLEAR_AGENT_ENABLED")
        if agent is not None:
            agent = agent.strip().lower()
        if mode not in {"demo", "private"}:
            raise ValueError("INVOICECLEAR_MODE must be demo or private.")
        if provider not in {"rules", "together"}:
            raise ValueError("INVOICECLEAR_PROVIDER must be rules or together.")
        if backups not in {"true", "false"}:
            raise ValueError("INVOICECLEAR_BACKUPS_ENABLED must be true or false.")
        if agent not in {None, "true", "false"}:
            raise ValueError("INVOICECLEAR_AGENT_ENABLED must be true or false.")
        email = os.getenv("INVOICECLEAR_EMAIL", "finance@invoiceclear.demo").strip()
        password = os.getenv("INVOICECLEAR_PASSWORD", "invoiceclear-demo")
        if not email or len(email) > 254 or not password or len(password) > 256:
            raise ValueError("Set a valid workspace email and password.")
        if mode == "private" and (email == "finance@invoiceclear.demo" or password == "invoiceclear-demo" or len(password) < 16):
            raise ValueError("Private mode needs your workspace email and a unique password of at least 16 characters.")
        key = (os.getenv("TOGETHER_API_KEY") or "").strip() or None
        if provider == "together" and not key:
            raise ValueError("Together mode requires TOGETHER_API_KEY on the backend.")
        port = int(os.getenv("INVOICECLEAR_PORT", os.getenv("PORT", "8100")))
        ttl = int(os.getenv("INVOICECLEAR_SESSION_TTL", "43200"))
        if not 1 <= port <= 65535 or not 300 <= ttl <= 86400:
            raise ValueError("Use a valid port and a session lifetime of 300–86400 seconds.")
        database = BACKEND / "data" / f"invoiceclear-{mode}.sqlite3"
        railway = bool(os.getenv("RAILWAY_ENVIRONMENT_ID") or os.getenv("RAILWAY_SERVICE_ID"))
        return cls(
            database_path=Path(os.getenv("INVOICECLEAR_DB_PATH", str(database))),
            web_path=PROJECT / "frontend" / "web", mode=mode, email=email, password=password,
            host=os.getenv("INVOICECLEAR_HOST", "0.0.0.0" if railway else "127.0.0.1"),
            port=port, allowed_origins=origins(os.getenv("INVOICECLEAR_ALLOWED_ORIGINS", "")),
            provider=provider, together_key=key,
            agent_enabled=bool(key) if agent is None else agent == "true",
            together_model=os.getenv("TOGETHER_MODEL", DEFAULT_TOGETHER_MODEL), ttl_seconds=ttl,
            backups_enabled=backups == "true",
            public_url=os.getenv("INVOICECLEAR_PUBLIC_URL", "").strip(),
            frontend_url=os.getenv("INVOICECLEAR_FRONTEND_URL", os.getenv("INVOICECLEAR_PUBLIC_URL", "")).strip(),
            token_key=os.getenv("INVOICECLEAR_TOKEN_KEY", "").strip(),
            qbo_client_id=os.getenv("QBO_CLIENT_ID", "").strip(),
            qbo_client_secret=os.getenv("QBO_CLIENT_SECRET", ""),
            qbo_environment=os.getenv("QBO_ENVIRONMENT", "sandbox").strip(),
            qbo_po_field_id=os.getenv("QBO_PO_FIELD_ID", "").strip(),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
        )
