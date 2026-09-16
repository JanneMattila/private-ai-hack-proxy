import math
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import unquote, urlsplit


@dataclass(frozen=True)
class Settings:
    backend_a2a_url: str
    agent_card_path: str = "/agentCard/v1.0"
    token_scope: str = "https://ai.azure.com/.default"
    public_base_url: str = "http://localhost:8000"
    connect_timeout_seconds: float = 10
    read_timeout_seconds: float = 300
    startup_timeout_seconds: float = 120
    credential_process_timeout_seconds: float = 60

    def __post_init__(self):
        for name in ("backend_a2a_url", "public_base_url"):
            value = getattr(self, name)
            parsed = urlsplit(value)
            allowed = {"https"} if name == "backend_a2a_url" else {"http", "https"}
            if (
                parsed.scheme not in allowed
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
                or any(character.isspace() for character in value)
                or "\\" in value
            ):
                raise ValueError(
                    f"{name} must be absolute, without credentials/query/fragment"
                )
            if parsed.port == 0:
                raise ValueError(f"{name} must use a nonzero port")
        suffix = unquote(self.agent_card_path)
        if (
            not suffix.startswith("/")
            or suffix.startswith("//")
            or any(part in {".", ".."} for part in suffix.split("/"))
            or any(character in suffix for character in "?#\\")
            or any(character.isspace() for character in suffix)
        ):
            raise ValueError("agent_card_path must be a suffix beneath the backend")
        if not self.token_scope.strip():
            raise ValueError("token_scope must not be empty")
        for field in fields(self):
            if field.name.endswith("_seconds"):
                value = getattr(self, field.name)
                if not math.isfinite(value) or value <= 0:
                    raise ValueError(f"{field.name} must be finite and positive")

    @property
    def card_url(self) -> str:
        return self.backend_a2a_url.rstrip("/") + self.agent_card_path

    @property
    def public_a2a_url(self) -> str:
        return self.public_base_url.rstrip("/") + "/a2a"


def load_settings(path: Path | None = None) -> Settings:
    path = path or Path(
        os.environ.get("IDENTITY_PROXY_CONFIG", Path(__file__).with_name("config.toml"))
    )
    with path.open("rb") as source:
        values = tomllib.load(source)
    for field in fields(Settings):
        override = os.environ.get(f"IDENTITY_PROXY_{field.name.upper()}")
        if override is not None:
            values[field.name] = (
                float(override) if field.name.endswith("_seconds") else override
            )
    return Settings(**values)
