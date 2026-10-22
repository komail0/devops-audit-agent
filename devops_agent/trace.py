"""Record of everything the agent did, so a run can be audited afterwards."""

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TextIO

from devops_agent.redact import redact_object

PREVIEW_CHARS = 300


class Trace:
    """Ordered log of assistant notes and tool calls. Everything stored is redacted."""

    def __init__(self, secrets: Iterable[str] = (), echo: Optional[TextIO] = None):
        self._secrets = tuple(secrets)
        self._echo = echo
        self.entries: List[Dict[str, Any]] = []

    def _add(self, entry: Dict[str, Any]) -> None:
        entry = redact_object(entry, self._secrets)
        entry["time"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.entries.append(entry)

    def note(self, kind: str, text: str) -> None:
        self._add({"type": kind, "text": text})
        if self._echo:
            print(f"[{kind}] {self.entries[-1]['text'][:PREVIEW_CHARS]}", file=self._echo)

    def tool_call(self, step: int, name: str, arguments: Dict[str, Any], result: str, seconds: float) -> None:
        self._add(
            {
                "type": "tool_call",
                "step": step,
                "tool": name,
                "arguments": arguments,
                "result_chars": len(result),
                "result_preview": result[:PREVIEW_CHARS],
                "seconds": round(seconds, 3),
            }
        )
        if self._echo:
            shown = self.entries[-1]
            print(f"[step {step}] {name} {shown['arguments']} -> {len(result)} chars", file=self._echo)

    def write(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.entries, indent=2), encoding="utf-8")


def stderr_trace(secrets: Iterable[str] = ()) -> Trace:
    return Trace(secrets=secrets, echo=sys.stderr)
