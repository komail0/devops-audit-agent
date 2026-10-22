import json
from types import SimpleNamespace

import httpx
import openai
import pytest

from devops_agent.config import Config
from devops_agent.llm import LLMClient, LLMError, LLMReply, ToolCall

CONFIG = Config(base_url="http://x", api_key="sk-secretvalue12345", model="test-model")
REQUEST = httpx.Request("POST", "http://x/chat/completions")


def _response(content=None, tool_calls=None, finish="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


def _call(name="read_file", arguments='{"path": "a.py"}', call_id="c1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(outcomes, delays=(0, 0, 0)):
    completions = FakeCompletions(outcomes)
    sleeps = []
    fake = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LLMClient(CONFIG, client=fake, sleep=sleeps.append, retry_delays=delays), completions, sleeps


def _status_error(cls, status, message="boom"):
    return cls(message, response=httpx.Response(status, request=REQUEST), body=None)


def test_request_shape_and_tool_call_parsing():
    client, completions, _ = _client([_response(tool_calls=[_call()], finish="tool_calls")])
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    reply = client.chat([{"role": "user", "content": "hi"}], tools)
    sent = completions.requests[0]
    assert sent["model"] == "test-model" and sent["temperature"] == 0
    assert sent["tools"] == tools and sent["tool_choice"] == "auto"
    assert reply.tool_calls == [ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})]
    assert reply.finish_reason == "tool_calls"


def test_no_tools_omits_tool_parameters():
    client, completions, _ = _client([_response(content="done")])
    reply = client.chat([{"role": "user", "content": "hi"}])
    assert "tools" not in completions.requests[0] and "tool_choice" not in completions.requests[0]
    assert reply.content == "done" and reply.tool_calls == []


@pytest.mark.parametrize(
    "raw, expected_args, has_error",
    [
        ("", {}, False),
        (None, {}, False),
        ('{"a": 1}', {"a": 1}, False),
        ("{not json", {}, True),
        ("[1, 2]", {}, True),
    ],
)
def test_argument_parsing(raw, expected_args, has_error):
    client, _, _ = _client([_response(tool_calls=[_call(arguments=raw)])])
    call = client.chat([]).tool_calls[0]
    assert call.arguments == expected_args
    assert bool(call.parse_error) is has_error


def test_missing_tool_call_id_gets_generated():
    client, _, _ = _client([_response(tool_calls=[_call(call_id=None), _call(call_id="")])])
    assert [c.id for c in client.chat([]).tool_calls] == ["call_0", "call_1"]


def test_empty_choices_become_empty_reply():
    client, _, _ = _client([SimpleNamespace(choices=[])])
    reply = client.chat([])
    assert reply.content == "" and reply.tool_calls == []


def test_as_message_roundtrips_tool_calls():
    reply = LLMReply(content="", tool_calls=[ToolCall(id="c1", name="list_files", arguments={"path": "src"})])
    message = reply.as_message()
    assert message["role"] == "assistant" and message["content"] is None
    call = message["tool_calls"][0]
    assert call["id"] == "c1" and call["type"] == "function"
    assert json.loads(call["function"]["arguments"]) == {"path": "src"}
    assert "tool_calls" not in LLMReply(content="hi").as_message()


def test_retries_transient_errors_then_succeeds():
    errors = [
        _status_error(openai.RateLimitError, 429),
        openai.APIConnectionError(request=REQUEST),
        _status_error(openai.InternalServerError, 503),
    ]
    client, completions, sleeps = _client(errors + [_response(content="ok")], delays=(1, 2, 3))
    assert client.chat([]).content == "ok"
    assert len(completions.requests) == 4 and sleeps == [1, 2, 3]


def test_gives_up_after_retries_exhausted():
    errors = [_status_error(openai.RateLimitError, 429)] * 4
    client, completions, _ = _client(errors)
    with pytest.raises(LLMError, match="after 4 attempts"):
        client.chat([])
    assert len(completions.requests) == 4


def test_timeout_is_retried():
    client, _, sleeps = _client([openai.APITimeoutError(request=REQUEST), _response(content="ok")])
    assert client.chat([]).content == "ok" and len(sleeps) == 1


@pytest.mark.parametrize(
    "error",
    [
        _status_error(openai.AuthenticationError, 401),
        _status_error(openai.NotFoundError, 404),
        _status_error(openai.BadRequestError, 400),
    ],
)
def test_client_errors_fail_fast_without_retry(error):
    client, completions, sleeps = _client([error, _response(content="never")])
    with pytest.raises(LLMError, match="rejected"):
        client.chat([])
    assert len(completions.requests) == 1 and sleeps == []


def test_error_messages_never_contain_the_api_key():
    error = _status_error(openai.AuthenticationError, 401, message=f"bad key {CONFIG.api_key}")
    client, _, _ = _client([error])
    with pytest.raises(LLMError) as info:
        client.chat([])
    assert CONFIG.api_key not in str(info.value)
    assert "HTTP 401" in str(info.value)
