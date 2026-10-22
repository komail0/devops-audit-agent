"""Runtime configuration from environment variables and an optional .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional

DEFAULT_MAX_STEPS = 30
DEFAULT_TIMEOUT_SECONDS = 600


class ConfigError(Exception):
    """Required configuration is missing or invalid."""


def load_env_file(path: Path, environ: Optional[Dict[str, str]] = None) -> None:
    """Load KEY=VALUE lines from path into environ without overriding existing variables."""
    target = os.environ if environ is None else environ
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in target:
            target[key] = value


def _positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a positive integer, got {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{name} must be a positive integer, got {raw!r}")
    return value


def _non_negative_float(environ: Mapping[str, str], name: str, default: float) -> float:
    raw = (environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a non-negative number, got {raw!r}") from None
    if value < 0:
        raise ConfigError(f"{name} must be a non-negative number, got {raw!r}")
    return value


@dataclass(frozen=True)
class Config:
    base_url: str
    api_key: str = field(repr=False)
    model: str
    max_steps: int = DEFAULT_MAX_STEPS
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    min_interval_seconds: float = 0.0

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Config":
        missing: List[str] = [
            name for name in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL") if not (environ.get(name) or "").strip()
        ]
        if missing:
            raise ConfigError(
                "missing required setting(s): " + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in, or set them as environment variables."
            )
        return cls(
            base_url=environ["LLM_BASE_URL"].strip(),
            api_key=environ["LLM_API_KEY"].strip(),
            model=environ["LLM_MODEL"].strip(),
            max_steps=_positive_int(environ, "AGENT_MAX_STEPS", DEFAULT_MAX_STEPS),
            timeout_seconds=_positive_int(environ, "AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            min_interval_seconds=_non_negative_float(environ, "LLM_MIN_INTERVAL_SECONDS", 0.0),
        )

    def secrets(self) -> List[str]:
        """Values that must never appear in logs, traces or reports."""
        return [self.api_key]


def load_config(project_root: Path, cwd: Optional[Path] = None) -> Config:
    """Read .env (current directory first, then the project root) and build a Config."""
    for directory in (Path(cwd) if cwd else Path.cwd(), project_root):
        load_env_file(directory / ".env")
    return Config.from_env(os.environ)
