"""Runtime configuration from environment variables and an optional .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Tuple

DEFAULT_MAX_STEPS = 15
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_CONTEXT_CHARS = 80_000

# provider -> (OpenAI-compatible base URL, default model). Model names change; LLM_MODEL always overrides.
_PROVIDERS = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", "gemini-flash-latest"),
    "groq": ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "anthropic": ("https://api.anthropic.com/v1/", "claude-sonnet-5-5"),
}
# Environment variables that can hold the key, in priority order, and the provider each one implies.
_KEY_VARIABLES = (
    ("LLM_API_KEY", None),
    ("OPENAI_API_KEY", "openai"),
    ("GEMINI_API_KEY", "gemini"),
    ("GOOGLE_API_KEY", "gemini"),
    ("GROQ_API_KEY", "groq"),
    ("ANTHROPIC_API_KEY", "anthropic"),
)
_URL_HOSTS = (
    ("api.openai.com", "openai"),
    ("generativelanguage.googleapis.com", "gemini"),
    ("api.groq.com", "groq"),
    ("api.anthropic.com", "anthropic"),
)


def _provider_from_key(key: str) -> Optional[str]:
    if key.startswith("sk-ant-"):
        return "anthropic"
    if key.startswith("gsk_"):
        return "groq"
    if key.startswith(("AIza", "AQ.")):
        return "gemini"
    if key.startswith("sk-"):
        return "openai"
    return None


def _provider_from_url(url: str) -> Optional[str]:
    return next((name for host, name in _URL_HOSTS if host in url), None)


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
    context_chars: int = DEFAULT_CONTEXT_CHARS
    notes: Tuple[str, ...] = ()  # how defaults were chosen, for the log

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> "Config":
        def value(name: str) -> str:
            return (environ.get(name) or "").strip()

        api_key, key_provider = "", None
        for name, provider in _KEY_VARIABLES:
            if value(name):
                api_key, key_provider = value(name), provider
                break
        base_url = value("LLM_BASE_URL")
        provider = _provider_from_url(base_url) if base_url else (key_provider or _provider_from_key(api_key))
        notes: List[str] = []
        if not base_url and provider:
            base_url = _PROVIDERS[provider][0]
            notes.append(f"LLM_BASE_URL not set; using the {provider} endpoint inferred from the API key")
        model = value("LLM_MODEL")
        if not model and provider:
            model = _PROVIDERS[provider][1]
            notes.append(f"LLM_MODEL not set; using the default {provider} model {model!r} (set LLM_MODEL to change it)")

        missing: List[str] = [
            name for name, found in (("LLM_BASE_URL", base_url), ("LLM_API_KEY", api_key), ("LLM_MODEL", model)) if not found
        ]
        if missing:
            raise ConfigError(
                "missing required setting(s): " + ", ".join(missing)
                + ". Copy .env.example to .env and fill them in, or set them as environment variables. "
                "With an OpenAI, Gemini, Groq or Anthropic key, LLM_API_KEY alone is enough; "
                "any other provider needs LLM_BASE_URL and LLM_MODEL as well."
            )
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            notes=tuple(notes),
            max_steps=_positive_int(environ, "AGENT_MAX_STEPS", DEFAULT_MAX_STEPS),
            timeout_seconds=_positive_int(environ, "AGENT_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            min_interval_seconds=_non_negative_float(environ, "LLM_MIN_INTERVAL_SECONDS", 0.0),
            context_chars=_positive_int(environ, "LLM_CONTEXT_CHARS", DEFAULT_CONTEXT_CHARS),
        )

    def secrets(self) -> List[str]:
        """Values that must never appear in logs, traces or reports."""
        return [self.api_key]


def load_config(project_root: Path, cwd: Optional[Path] = None) -> Config:
    """Read .env (current directory first, then the project root) and build a Config."""
    for directory in (Path(cwd) if cwd else Path.cwd(), project_root):
        load_env_file(directory / ".env")
    return Config.from_env(os.environ)
