"""Secret redaction applied to everything the agent shows, logs or writes.

Tool output is redacted before it reaches the LLM, the trace and the report,
so a credential found in a target repository is reported by location and type
and its value is never reproduced.
"""

import re
from typing import Any, Iterable

_FLAGS = re.MULTILINE

# (type, pattern) whose whole match is a secret.
_TOKEN_PATTERNS = [
    ("private-key", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL)),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("stripe-key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
]

# user:password@host in a URL; only the password is replaced.
_URL_CREDENTIALS = re.compile(r"(://[^\s/:@]+:)([^\s/@]+)(@)")

# name = value assignments where the name suggests a credential.
_ASSIGNMENT = re.compile(
    r"""(?ix)
    ( [\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential|auth)[\w.-]*
      ["']?\s*[:=]\s*["']? )
    ( [^\s"',;)}\]]{8,} )
    """
)

# Values that are references or placeholders, not literal credentials.
_NOT_A_SECRET = re.compile(
    r"""(?ix)
    ^(?: \$ | % | \{ | < | \[REDACTED
      | .*(?:environ|getenv|process\.env|secrets\.|vars\.|os\.|config\.|settings\.)
      | (?:your|my|example|sample|dummy|fake|test|changeme|change_me|placeholder|replace|insert|todo|xxx|\*\*\*)
      | x+$ | \.+$ | \*+$
    )
    """
)


_OPENING_QUOTE = re.compile(r"[:=]\s*[\"']$")
_ATTRIBUTE_CHAIN = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)+$")
_SNAKE_IDENTIFIER = re.compile(r"^[a-z]+(_[a-z0-9]+)+$")
_ASSIGNED_NAME = re.compile(r"([A-Za-z_][\w-]*)[\"']?\s*[:=]")


def _looks_like_code(prefix: str, value: str) -> bool:
    """Unquoted values that read as expressions or variable names, not literal credentials."""
    if "(" in value or "[" in value:
        return True
    if _ATTRIBUTE_CHAIN.match(value) or _SNAKE_IDENTIFIER.match(value):
        return True
    name = _ASSIGNED_NAME.search(prefix)
    return bool(name) and name.group(1).lower() == value.lower()


def _assignment_sub(match: "re.Match[str]") -> str:
    prefix, value = match.group(1), match.group(2)
    if _NOT_A_SECRET.match(value):
        return match.group(0)
    if not _OPENING_QUOTE.search(prefix) and _looks_like_code(prefix, value):
        return match.group(0)
    return f"{match.group(1)}[REDACTED:credential]"


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return text with secret values replaced by [REDACTED:<type>] markers."""
    if not isinstance(text, str) or not text:
        return text
    for secret in extra_secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[REDACTED:configured-secret]")
    for kind, pattern in _TOKEN_PATTERNS:
        text = pattern.sub(f"[REDACTED:{kind}]", text)
    text = _URL_CREDENTIALS.sub(r"\1[REDACTED:url-password]\3", text)
    return _ASSIGNMENT.sub(_assignment_sub, text)


def redact_object(obj: Any, extra_secrets: Iterable[str] = ()) -> Any:
    """Recursively redact every string in a dict/list/str structure."""
    extra = tuple(extra_secrets)
    if isinstance(obj, str):
        return redact(obj, extra)
    if isinstance(obj, dict):
        return {k: redact_object(v, extra) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_object(v, extra) for v in obj]
    return obj
