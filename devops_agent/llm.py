"""Thin wrapper over an OpenAI-compatible chat API with tool calling.

The wrapper only sends messages and parses the reply. Deciding which tool to
call and what to do with results is the agent loop's job.
"""

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

import openai
from openai import OpenAI

from devops_agent.config import Config
from devops_agent.redact import redact

REQUEST_TIMEOUT_SECONDS = 90
RETRY_DELAYS_SECONDS = (5, 15, 30, 60)
MAX_RETRY_WAIT_SECONDS = 90  # a longer server-requested wait (e.g. daily quota) is not worth stalling for
ERROR_DETAIL_CHARS = 700
_RETRY_HINT = re.compile(r"retry(?:\s*delay)?['\"]?\s*(?:in|:)\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)


class LLMError(Exception):
    """The LLM could not be reached or rejected the request; the audit cannot continue."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]
    parse_error: Optional[str] = None
    # Provider metadata that must be echoed back with the call (e.g. Gemini thought signatures).
    extra_content: Optional[Dict[str, Any]] = None


@dataclass
class LLMReply:
    content: str
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None

    @staticmethod
    def _call_message(call: ToolCall) -> Dict[str, Any]:
        entry: Dict[str, Any] = {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
        }
        if call.extra_content:
            entry["extra_content"] = call.extra_content
        return entry

    def as_message(self) -> Dict[str, Any]:
        """The assistant message to append to the conversation history."""
        message: Dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            message["tool_calls"] = [self._call_message(call) for call in self.tool_calls]
        return message


_RETRIABLE = (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError)


def _server_retry_hint(exc: Exception) -> Optional[float]:
    """Seconds the server asked us to wait (Retry-After header or a retry delay in the error body)."""
    response = getattr(exc, "response", None)
    header = response.headers.get("retry-after") if response is not None else None
    try:
        if header is not None:
            return float(header)
    except ValueError:
        pass
    match = _RETRY_HINT.search(str(exc))
    return float(match.group(1)) if match else None


def _parse_arguments(raw: Any) -> "tuple[Dict[str, Any], Optional[str]]":
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}, "tool arguments were not valid JSON"
    if not isinstance(parsed, dict):
        return {}, "tool arguments must be a JSON object"
    return parsed, None


class LLMClient:
    def __init__(
        self,
        config: Config,
        client: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_delays: Sequence[float] = RETRY_DELAYS_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._client = client or OpenAI(
            api_key=config.api_key, base_url=config.base_url, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=0
        )
        self._sleep = sleep
        self._retry_delays = tuple(retry_delays)
        self._clock = clock
        self._last_request: Optional[float] = None

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> LLMReply:
        kwargs: Dict[str, Any] = {"model": self._config.model, "messages": messages, "temperature": 0}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")
        self._respect_min_interval()
        response = self._request_with_retries(kwargs)
        return self._parse(response)

    def _respect_min_interval(self) -> None:
        """Space requests out for providers with strict per-minute quotas."""
        interval = self._config.min_interval_seconds
        now = self._clock()
        if interval > 0 and self._last_request is not None:
            wait = self._last_request + interval - now
            if wait > 0:
                self._sleep(wait)
                now = self._clock()
        self._last_request = now

    def _request_with_retries(self, kwargs: Dict[str, Any]) -> Any:
        attempts = len(self._retry_delays) + 1
        for attempt in range(attempts):
            try:
                return self._client.chat.completions.create(**kwargs)
            except _RETRIABLE as exc:
                if attempt == attempts - 1:
                    raise LLMError(f"LLM request failed after {attempts} attempts: {self._describe(exc)}") from None
                delay = self._retry_delays[attempt]
                hint = _server_retry_hint(exc) if isinstance(exc, openai.RateLimitError) else None
                if hint is not None:
                    if hint > MAX_RETRY_WAIT_SECONDS:
                        raise LLMError(
                            f"rate limited and the server asks to wait {hint:.0f}s (more than {MAX_RETRY_WAIT_SECONDS}s): "
                            f"{self._describe(exc)}"
                        ) from None
                    delay = hint + 1
                self._sleep(delay)
            except openai.OpenAIError as exc:
                raise LLMError(f"LLM request rejected: {self._describe(exc)}") from None
        raise LLMError("LLM request failed")  # unreachable; keeps type checkers satisfied

    def _describe(self, exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        detail = redact(str(getattr(exc, "message", None) or exc), self._config.secrets())[:ERROR_DETAIL_CHARS]
        prefix = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
        return f"{prefix}: {detail}"

    @staticmethod
    def _parse(response: Any) -> LLMReply:
        choices = getattr(response, "choices", None) or []
        if not choices:
            return LLMReply(content="", finish_reason=None)
        choice = choices[0]
        message = choice.message
        calls: List[ToolCall] = []
        for index, raw_call in enumerate(getattr(message, "tool_calls", None) or []):
            function = raw_call.function
            arguments, error = _parse_arguments(getattr(function, "arguments", None))
            extra = getattr(raw_call, "extra_content", None)
            calls.append(
                ToolCall(
                    id=getattr(raw_call, "id", None) or f"call_{index}",
                    name=getattr(function, "name", "") or "",
                    arguments=arguments,
                    parse_error=error,
                    extra_content=extra if isinstance(extra, dict) else None,
                )
            )
        return LLMReply(
            content=getattr(message, "content", None) or "",
            tool_calls=calls,
            finish_reason=getattr(choice, "finish_reason", None),
        )
