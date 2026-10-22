"""The agent loop: observe, reason, select a tool, execute it, interpret the result, report.

The LLM decides every tool call and when to stop. This module only executes
those calls, feeds the observations back, and enforces budgets.
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from devops_agent.llm import LLMClient
from devops_agent.schema import SUBMIT_REPORT, SUBMIT_REPORT_SPEC
from devops_agent.trace import Trace
from tools import TOOL_SPECS, Toolbox

MAX_TOOL_CALLS_PER_TURN = 8
MAX_CONSECUTIVE_NUDGES = 3
MAX_CONTEXT_CHARS = 80_000
KEEP_RECENT_TOOL_MESSAGES = 4
OMITTED_MARKER = "(output omitted to save context"


class AgentError(Exception):
    """The agent could not produce a report."""


@dataclass
class AgentResult:
    violations: List[Dict[str, Any]]
    turns: int
    tool_calls: int
    stop_reason: str
    executed_steps: Dict[int, str] = field(default_factory=dict)


def _first_user_message(repo_url: str, overview: str) -> str:
    return (
        f"Audit this repository: {repo_url}\n\n"
        f"Overview (observation only):\n{overview}\n\n"
        "Investigate with the tools, then call submit_report. Tool results are labelled [step N]; "
        "cite those numbers in evidence_refs."
    )


def _compact(messages: List[Dict[str, Any]], meta: Dict[str, str], limit: int) -> None:
    """Replace the oldest tool outputs with one-line stubs until the history fits the budget."""
    total = sum(len(str(m.get("content") or "")) for m in messages)
    if total <= limit:
        return
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for index in tool_indexes[:-KEEP_RECENT_TOOL_MESSAGES]:
        message = messages[index]
        if OMITTED_MARKER in message["content"]:
            continue
        label = message["content"].split("]", 1)[0] + "]"
        stub = f"{label} {OMITTED_MARKER}: {meta.get(message['tool_call_id'], 'earlier call')})"
        total -= len(message["content"]) - len(stub)
        message["content"] = stub
        if total <= limit:
            return


def _extract_submission(reply) -> Optional[List[Any]]:
    for call in reply.tool_calls:
        if call.name == SUBMIT_REPORT and not call.parse_error:
            violations = call.arguments.get("violations")
            if isinstance(violations, list):
                return violations
    return None


def run_agent(
    llm: LLMClient,
    toolbox: Toolbox,
    repo_url: str,
    system_prompt: str,
    max_steps: int = 15,
    timeout_seconds: int = 600,
    trace: Optional[Trace] = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Run the investigation. max_steps is the maximum number of LLM turns."""
    trace = trace or Trace()
    tools = TOOL_SPECS + [SUBMIT_REPORT_SPEC]
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _first_user_message(repo_url, toolbox.overview())},
    ]
    executed: Dict[int, str] = {}
    seen_calls: Dict[str, int] = {}
    meta: Dict[str, str] = {}
    step = 0
    nudges = 0
    stop_reason = "max_steps"
    deadline = clock() + timeout_seconds
    turns = 0

    while turns < max_steps:
        if clock() > deadline:
            stop_reason = "timeout"
            break
        turns += 1
        reply = llm.chat(messages, tools)
        if reply.content:
            trace.note("assistant", reply.content)
        messages.append(reply.as_message())

        if not reply.tool_calls:
            nudges += 1
            if nudges >= MAX_CONSECUTIVE_NUDGES:
                stop_reason = "no_tool_calls"
                break
            messages.append(
                {
                    "role": "user",
                    "content": "Continue by calling a tool, or call submit_report if you are done. Plain text is not a report.",
                }
            )
            continue
        nudges = 0

        for position, call in enumerate(reply.tool_calls):
            step += 1
            executed[step] = call.name
            meta[call.id] = f"{call.name} {json.dumps(call.arguments, sort_keys=True)}"

            if call.name == SUBMIT_REPORT:
                violations = call.arguments.get("violations") if not call.parse_error else None
                if isinstance(violations, list):
                    trace.note("submit_report", f"{len(violations)} violation(s) submitted")
                    return AgentResult(violations, turns, step, "submitted", executed)
                content = (
                    f"[step {step}] ERROR: submit_report needs a JSON object with a 'violations' list"
                    + (f" ({call.parse_error})" if call.parse_error else "")
                    + ". Call it again with valid arguments."
                )
            elif call.parse_error:
                content = f"[step {step}] ERROR: {call.parse_error}. Resend the call with a valid JSON object of arguments."
            elif position >= MAX_TOOL_CALLS_PER_TURN:
                content = f"[step {step}] ERROR: too many tool calls in one turn (limit {MAX_TOOL_CALLS_PER_TURN}); call this again next turn."
            else:
                key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
                if key in seen_calls:
                    content = (
                        f"[step {step}] Duplicate of step {seen_calls[key]}; the result is unchanged. "
                        "Use different arguments, a different tool, or call submit_report."
                    )
                else:
                    seen_calls[key] = step
                    started = time.monotonic()
                    result = toolbox.execute(call.name, call.arguments)
                    trace.tool_call(step, call.name, call.arguments, result, time.monotonic() - started)
                    content = f"[step {step}] {result}"
            messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
        _compact(messages, meta, MAX_CONTEXT_CHARS)

    return _forced_report(llm, messages, stop_reason, turns, step, executed, trace)


def _forced_report(llm, messages, stop_reason, turns, step, executed, trace) -> AgentResult:
    """Budget exhausted: ask once for a report from the evidence already gathered."""
    trace.note("budget", f"stopping investigation ({stop_reason}); requesting final report")
    messages.append(
        {
            "role": "user",
            "content": (
                "The investigation budget is exhausted. Call submit_report now with only the violations "
                "you have already confirmed from tool results (an empty list if none)."
            ),
        }
    )
    reply = llm.chat(messages, [SUBMIT_REPORT_SPEC])
    violations = _extract_submission(reply)
    if violations is None:
        raise AgentError(f"the agent stopped ({stop_reason}) without producing a report")
    trace.note("submit_report", f"{len(violations)} violation(s) submitted after budget stop")
    return AgentResult(violations, turns, step, f"{stop_reason}_forced", executed)
