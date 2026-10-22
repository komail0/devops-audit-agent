"""The agent loop: observe, reason, select a tool, execute it, interpret the result, report.

The LLM decides every tool call and when to stop. This module only executes
those calls, feeds the observations back, keeps the candidate-finding notes,
and enforces budgets. Each stage of the cycle is a labelled block below:

    1. OBSERVE            _observe(): repository overview and the opening message
    2. REASON             llm.chat(): the model reads everything seen so far
    3. SELECT TOOL        the model's reply names the tool calls (never a fixed order)
    4. EXECUTE TOOL       _execute(): dispatch to the read-only toolbox
    5. INTERPRET RESULTS  _interpret(): label, bound and store the observation; notes persist
    6. REPORT             submit_report, or _forced_report() when a budget is exhausted
"""

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from devops_agent.llm import LLMClient, ToolCall
from devops_agent.schema import NOTE_FINDING, NOTE_FINDING_SPEC, SUBMIT_REPORT, SUBMIT_REPORT_SPEC
from devops_agent.trace import Trace
from tools import TOOL_SPECS, Toolbox

MAX_TOOL_CALLS_PER_TURN = 8
MAX_CONSECUTIVE_NUDGES = 3
MAX_CONTEXT_CHARS = 80_000
MIN_RESULT_CHARS = 1500
MAX_NOTES = 40
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
    notes: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class _State:
    """What survives across iterations: step counter, executed calls and the model's candidate notes."""

    step: int = 0
    executed: Dict[int, str] = field(default_factory=dict)
    seen_calls: Dict[str, int] = field(default_factory=dict)
    meta: Dict[str, str] = field(default_factory=dict)
    notes: List[Dict[str, Any]] = field(default_factory=list)


# ---- 1. OBSERVE ---------------------------------------------------------------------------

def _observe(toolbox: Toolbox, repo_url: str, system_prompt: str) -> List[Dict[str, Any]]:
    """Opening observation: the repository overview. No findings and no instructions about which tools to call."""
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Audit this repository: {repo_url}\n\n"
                f"Overview (observation only):\n{toolbox.overview()}\n\n"
                "Investigate with the tools, then call submit_report. Tool results are labelled [step N]; "
                "cite those numbers in evidence_refs."
            ),
        },
    ]


# ---- 4. EXECUTE TOOL ----------------------------------------------------------------------

def _execute(toolbox: Toolbox, call: ToolCall, state: _State, trace: Trace) -> str:
    """Run one tool call chosen by the model and return the raw observation (or an error message)."""
    key = f"{call.name}:{json.dumps(call.arguments, sort_keys=True)}"
    if key in state.seen_calls:
        return (
            f"Duplicate of step {state.seen_calls[key]}; the result is unchanged. "
            "Use different arguments, a different tool, or call submit_report."
        )
    state.seen_calls[key] = state.step
    started = time.monotonic()
    result = toolbox.execute(call.name, call.arguments)
    trace.tool_call(state.step, call.name, call.arguments, result, time.monotonic() - started)
    return result


# ---- 5. INTERPRET RESULTS -----------------------------------------------------------------

def _interpret(observation: str, state: _State, context_chars: int) -> str:
    """Label the observation with its step number and bound it so it fits the context budget."""
    content = f"[step {state.step}] {observation}"
    limit = max(MIN_RESULT_CHARS, context_chars // 4)
    if len(content) > limit:
        content = content[:limit] + "\n[truncated to fit the context budget; use a narrower path, pattern or line range]"
    return content


def _record_note(call: ToolCall, state: _State, trace: Trace) -> str:
    """Store a candidate finding the model flagged. Unlike tool output, notes are never dropped by compaction."""
    summary = call.arguments.get("summary") if not call.parse_error else None
    if not isinstance(summary, str) or not summary.strip():
        return f"[step {state.step}] ERROR: note_finding needs a non-empty 'summary' string."
    if len(state.notes) >= MAX_NOTES:
        return f"[step {state.step}] Note limit ({MAX_NOTES}) reached; stop noting and call submit_report."
    file = call.arguments.get("file")
    source_step = call.arguments.get("step")
    note = {
        "recorded_at_step": state.step,
        "summary": summary.strip()[:300],
        "file": file if isinstance(file, str) and file.strip() else None,
        "evidence_step": source_step if isinstance(source_step, int) and not isinstance(source_step, bool) else None,
    }
    state.notes.append(note)
    trace.note("note_finding", f"{note['file'] or '-'}: {note['summary']}")
    return f"[step {state.step}] Noted ({len(state.notes)} candidate note(s) so far). Keep investigating or submit_report."


def _compact(messages: List[Dict[str, Any]], meta: Dict[str, str], limit: int) -> None:
    """Replace the oldest tool outputs with one-line stubs until their total size fits the budget.

    Results from the latest turn are never stubbed: the model has not seen them yet.
    """
    tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    total = sum(len(messages[i]["content"]) for i in tool_indexes)
    if total <= limit:
        return
    last_assistant = max((i for i, m in enumerate(messages) if m.get("role") == "assistant"), default=-1)
    for index in tool_indexes:
        if index > last_assistant:
            break
        message = messages[index]
        if OMITTED_MARKER in message["content"]:
            continue
        label = message["content"].split("]", 1)[0] + "]"
        stub = f"{label} {OMITTED_MARKER}: {meta.get(message['tool_call_id'], 'earlier call')})"
        total -= len(message["content"]) - len(stub)
        message["content"] = stub
        if total <= limit:
            return


# ---- 6. REPORT ----------------------------------------------------------------------------

def _extract_submission(reply) -> Optional[List[Any]]:
    for call in reply.tool_calls:
        if call.name == SUBMIT_REPORT and not call.parse_error:
            violations = call.arguments.get("violations")
            if isinstance(violations, list):
                return violations
    return None


def _notes_text(notes: List[Dict[str, Any]]) -> str:
    return "\n".join(
        f"- (noted at step {n['recorded_at_step']}"
        + (f", evidence step {n['evidence_step']}" if n["evidence_step"] else "")
        + f") {n['file'] or 'repository-wide'}: {n['summary']}"
        for n in notes
    )


def _forced_report(llm, messages, stop_reason, turns, state: _State, trace) -> AgentResult:
    """Budget exhausted: ask once for a report from the evidence already gathered, replaying the notes."""
    trace.note("budget", f"stopping investigation ({stop_reason}); requesting final report")
    request = (
        "The investigation budget is exhausted. Call submit_report now with only the violations "
        "you have already confirmed from tool results (an empty list if none)."
    )
    if state.notes:
        request += "\n\nCandidate notes you recorded (confirm each against the evidence before reporting it):\n" + _notes_text(
            state.notes
        )
    messages.append({"role": "user", "content": request})
    reply = llm.chat(messages, [SUBMIT_REPORT_SPEC])
    violations = _extract_submission(reply)
    if violations is None:
        raise AgentError(f"the agent stopped ({stop_reason}) without producing a report")
    trace.note("submit_report", f"{len(violations)} violation(s) submitted after budget stop")
    return AgentResult(violations, turns, state.step, f"{stop_reason}_forced", state.executed, state.notes)


def run_agent(
    llm: LLMClient,
    toolbox: Toolbox,
    repo_url: str,
    system_prompt: str,
    max_steps: int = 15,
    timeout_seconds: int = 600,
    context_chars: int = MAX_CONTEXT_CHARS,
    trace: Optional[Trace] = None,
    clock: Callable[[], float] = time.monotonic,
) -> AgentResult:
    """Run the investigation. max_steps is the maximum number of LLM turns."""
    trace = trace or Trace()
    tools = TOOL_SPECS + [NOTE_FINDING_SPEC, SUBMIT_REPORT_SPEC]
    messages = _observe(toolbox, repo_url, system_prompt)  # 1. OBSERVE
    state = _State()
    nudges = 0
    stop_reason = "max_steps"
    deadline = clock() + timeout_seconds
    turns = 0

    while turns < max_steps:
        if clock() > deadline:
            stop_reason = "timeout"
            break
        turns += 1
        reply = llm.chat(messages, tools)  # 2. REASON and 3. SELECT TOOL: the model picks the calls
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
            state.step += 1
            state.executed[state.step] = call.name
            state.meta[call.id] = f"{call.name} {json.dumps(call.arguments, sort_keys=True)}"

            if call.name == SUBMIT_REPORT:  # 6. REPORT
                violations = call.arguments.get("violations") if not call.parse_error else None
                if isinstance(violations, list):
                    trace.note("submit_report", f"{len(violations)} violation(s) submitted")
                    return AgentResult(violations, turns, state.step, "submitted", state.executed, state.notes)
                observation = (
                    "ERROR: submit_report needs a JSON object with a 'violations' list"
                    + (f" ({call.parse_error})" if call.parse_error else "")
                    + ". Call it again with valid arguments."
                )
            elif call.parse_error:
                observation = f"ERROR: {call.parse_error}. Resend the call with a valid JSON object of arguments."
            elif position >= MAX_TOOL_CALLS_PER_TURN:
                observation = f"ERROR: too many tool calls in one turn (limit {MAX_TOOL_CALLS_PER_TURN}); call this again next turn."
            elif call.name == NOTE_FINDING:  # 5. INTERPRET: the model records what it noticed
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": _record_note(call, state, trace)}
                )
                continue
            else:
                observation = _execute(toolbox, call, state, trace)  # 4. EXECUTE TOOL
            messages.append(  # 5. INTERPRET RESULTS: the observation goes back to the model
                {"role": "tool", "tool_call_id": call.id, "content": _interpret(observation, state, context_chars)}
            )
        _compact(messages, state.meta, context_chars)

    return _forced_report(llm, messages, stop_reason, turns, state, trace)
