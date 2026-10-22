import json

import pytest

from devops_agent import agent_loop
from devops_agent.agent_loop import AgentError, run_agent
from devops_agent.llm import LLMError, LLMReply, ToolCall
from devops_agent.schema import SUBMIT_REPORT, SUBMIT_REPORT_SPEC
from devops_agent.trace import Trace
from tools import Toolbox

FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
VIOLATION = {
    "rule_id": "CI-001",
    "category": "ci",
    "severity": "medium",
    "file": ".github/workflows/ci.yml",
    "line": None,
    "description": "d",
    "evidence": "e",
    "recommendation": "r",
    "evidence_refs": [1],
}


def call(name, arguments=None, call_id=None, parse_error=None):
    return ToolCall(id=call_id or f"id-{name}", name=name, arguments=arguments or {}, parse_error=parse_error)


def submit(violations=(), call_id="id-submit"):
    return LLMReply("", [call(SUBMIT_REPORT, {"violations": list(violations)}, call_id)])


class ScriptedLLM:
    """Stands in for LLMClient: replies come from a list or a policy(messages, turn) function."""

    def __init__(self, script):
        self.script = script
        self.turn = 0
        self.seen = []  # (messages snapshot, tool names offered)

    def chat(self, messages, tools=None):
        self.seen.append((json.loads(json.dumps(messages)), [t["function"]["name"] for t in tools or []]))
        self.turn += 1
        if callable(self.script):
            return self.script(messages, self.turn)
        reply = self.script[self.turn - 1]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture
def box(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "app.py").write_text(f'KEY = "{FAKE_AWS}"\n')
    return Toolbox(tmp_path)


@pytest.fixture
def box_with_ci(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "ci.yml").write_text("name: CI\non: push\njobs:\n  t:\n    runs-on: x\n    steps:\n      - run: pytest || true\n")
    return Toolbox(tmp_path)


def run(llm, box, **kwargs):
    return run_agent(llm, box, "https://github.com/o/r", "system prompt", **kwargs)


def tool_messages(llm_or_messages):
    messages = llm_or_messages if isinstance(llm_or_messages, list) else llm_or_messages.seen[-1][0]
    return [m for m in messages if m["role"] == "tool"]


# ---- the loop is driven by observations ------------------------------------

def policy(messages, turn):
    """A tiny 'LLM': follow up on whatever the last observation revealed."""
    tools = tool_messages(messages)
    if not tools:
        return LLMReply("", [call("list_files")])
    last = tools[-1]["content"]
    if "ci.yml" in last and "parse_workflow" not in str(messages[-3:]):
        return LLMReply("saw a workflow, inspecting it", [call("parse_workflow", {"path": ".github/workflows/ci.yml"})])
    if "`|| true`" in last:
        return submit([{**VIOLATION, "evidence_refs": [2]}])
    return submit([])


def test_next_tool_depends_on_previous_observation(box_with_ci):
    llm = ScriptedLLM(policy)
    result = run(llm, box_with_ci)
    assert [result.executed_steps[i] for i in (1, 2)] == ["list_files", "parse_workflow"]
    assert result.stop_reason == "submitted"
    assert result.violations[0]["rule_id"] == "CI-001"


def test_same_policy_takes_different_path_without_workflow(box):
    result = run(ScriptedLLM(policy), box)
    assert "parse_workflow" not in result.executed_steps.values()
    assert result.violations == []


def test_first_message_contains_overview_and_system_prompt(box):
    llm = ScriptedLLM([submit()])
    run(llm, box)
    messages, offered = llm.seen[0]
    assert messages[0] == {"role": "system", "content": "system prompt"}
    assert "https://github.com/o/r" in messages[1]["content"] and "Top level:" in messages[1]["content"]
    assert set(offered) == {"list_files", "read_file", "search_code", "git_log", "git_status", "parse_workflow", SUBMIT_REPORT}


def test_tool_results_are_labelled_with_step_numbers_and_redacted(box):
    llm = ScriptedLLM([LLMReply("", [call("read_file", {"path": "app.py"})]), submit()])
    run(llm, box)
    content = tool_messages(llm)[0]["content"]
    assert content.startswith("[step 1]")
    assert FAKE_AWS not in content and "[REDACTED:aws-access-key]" in content


def test_every_tool_call_gets_a_response_in_history(box):
    script = [
        LLMReply("", [call("list_files", call_id="a"), call("nope", call_id="b"), call("read_file", {"path": "README.md"}, "c")]),
        submit(),
    ]
    llm = ScriptedLLM(script)
    run(llm, box)
    messages = llm.seen[-1][0]
    asked = [c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]
    answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
    assert asked == ["a", "b", "c"] and answered == ["a", "b", "c"]


# ---- recoverable problems ------------------------------------------------------

def test_unknown_tool_and_bad_arguments_become_observations(box):
    script = [
        LLMReply("", [call("rm_rf", call_id="a")]),
        LLMReply("", [call("read_file", call_id="b", parse_error="tool arguments were not valid JSON")]),
        LLMReply("", [call("read_file", {"path": "missing.txt"}, "c")]),
        submit(),
    ]
    llm = ScriptedLLM(script)
    result = run(llm, box)
    contents = [m["content"] for m in tool_messages(llm)]
    assert "unknown tool" in contents[0]
    assert "not valid JSON" in contents[1] and "Resend" in contents[1]
    assert "no such file" in contents[2]
    assert result.stop_reason == "submitted"


def test_duplicate_call_not_reexecuted(box):
    script = [
        LLMReply("", [call("list_files", call_id="a")]),
        LLMReply("", [call("list_files", call_id="b")]),
        submit(),
    ]
    llm = ScriptedLLM(script)
    run(llm, box)
    assert "Duplicate of step 1" in tool_messages(llm)[1]["content"]


def test_too_many_calls_in_one_turn(box, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_TOOL_CALLS_PER_TURN", 2)
    calls = [call("read_file", {"path": "README.md", "start_line": i}, f"c{i}") for i in (1, 1, 1)]
    calls[1] = call("list_files", call_id="c1")
    calls[2] = call("git_status", call_id="c2")
    llm = ScriptedLLM([LLMReply("", calls), submit()])
    run(llm, box)
    assert "too many tool calls" in tool_messages(llm)[2]["content"]


def test_text_only_reply_is_nudged_not_accepted_as_report(box):
    llm = ScriptedLLM([LLMReply("I think the repo is fine."), LLMReply("", [call("list_files")]), submit()])
    result = run(llm, box)
    assert result.stop_reason == "submitted" and result.tool_calls == 2
    assert "Plain text is not a report" in llm.seen[1][0][-1]["content"]


def test_invalid_submission_is_rejected_then_retried(box):
    bad = LLMReply("", [call(SUBMIT_REPORT, {"violations": "none"}, "bad")])
    llm = ScriptedLLM([bad, submit()])
    result = run(llm, box)
    assert result.stop_reason == "submitted"
    assert "needs a JSON object with a 'violations' list" in tool_messages(llm)[0]["content"]


def test_submit_after_other_calls_in_same_turn(box):
    reply = LLMReply("", [call("list_files", call_id="a"), call(SUBMIT_REPORT, {"violations": [VIOLATION]}, "b")])
    result = run(ScriptedLLM([reply]), box)
    assert result.stop_reason == "submitted" and result.executed_steps == {1: "list_files", 2: SUBMIT_REPORT}


# ---- budgets -------------------------------------------------------------------

def endless_listing(messages, turn):
    return LLMReply("", [call("list_files", {"max_depth": turn}, f"c{turn}")])


def test_max_steps_forces_final_report(box):
    def script(messages, turn):
        if turn > 3:  # the forced request: only submit_report is offered
            return submit([VIOLATION])
        return endless_listing(messages, turn)

    llm = ScriptedLLM(script)
    result = run(llm, box, max_steps=3)
    assert result.stop_reason == "max_steps_forced" and result.turns == 3
    assert llm.seen[-1][1] == [SUBMIT_REPORT]
    assert "budget is exhausted" in llm.seen[-1][0][-1]["content"]


def test_budget_stop_without_report_raises(box):
    with pytest.raises(AgentError, match="without producing a report"):
        run(ScriptedLLM(endless_listing), box, max_steps=2)


def test_timeout_stops_before_next_turn(box):
    times = iter([0, 0, 1000, 1000, 1000])
    llm = ScriptedLLM(lambda m, t: LLMReply("", [call("list_files", {"max_depth": t}, f"c{t}")]) if t == 1 else submit())
    result = run(llm, box, timeout_seconds=10, clock=lambda: next(times))
    assert result.stop_reason == "timeout_forced" and result.turns == 1


def test_repeated_text_only_replies_stop_the_loop(box):
    llm = ScriptedLLM(lambda m, t: submit() if t > 3 else LLMReply("hmm"))
    result = run(llm, box)
    assert result.stop_reason == "no_tool_calls_forced"


def test_llm_failure_propagates(box):
    with pytest.raises(LLMError):
        run(ScriptedLLM([LLMError("down")]), box)


def test_old_tool_outputs_are_compacted_but_stay_citable(box, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_CONTEXT_CHARS", 600)
    monkeypatch.setattr(agent_loop, "KEEP_RECENT_TOOL_MESSAGES", 1)
    (box.root / "big.txt").write_text("x" * 500)
    script = [
        LLMReply("", [call("read_file", {"path": "big.txt"}, "c1")]),
        LLMReply("", [call("read_file", {"path": "big.txt", "start_line": 1, "end_line": 1}, "c2")]),
        LLMReply("", [call("list_files", call_id="c3")]),
        submit(),
    ]
    llm = ScriptedLLM(script)
    run(llm, box)
    contents = [m["content"] for m in tool_messages(llm)]
    assert contents[0].startswith("[step 1]") and "omitted to save context" in contents[0]
    assert "read_file" in contents[0]
    assert "omitted" not in contents[-1]


# ---- trace ---------------------------------------------------------------------

def test_trace_records_calls_and_redacts_secrets(box, tmp_path):
    trace = Trace(secrets=["hunter2-hunter2"])
    llm = ScriptedLLM([
        LLMReply("looking at app.py hunter2-hunter2", [call("read_file", {"path": "app.py"})]),
        submit([VIOLATION]),
    ])
    run(llm, box, trace=trace)
    kinds = [e["type"] for e in trace.entries]
    assert kinds == ["assistant", "tool_call", "submit_report"]
    entry = trace.entries[1]
    assert entry["step"] == 1 and entry["tool"] == "read_file" and entry["arguments"] == {"path": "app.py"}
    dumped = json.dumps(trace.entries)
    assert "hunter2-hunter2" not in dumped and FAKE_AWS not in dumped
    out = tmp_path / "trace.json"
    trace.write(out)
    assert json.loads(out.read_text())[1]["tool"] == "read_file"


def test_submit_spec_is_well_formed():
    function = SUBMIT_REPORT_SPEC["function"]
    item = function["parameters"]["properties"]["violations"]["items"]
    assert function["name"] == SUBMIT_REPORT
    assert set(item["required"]) >= {"rule_id", "category", "severity", "file", "line", "evidence_refs"}
    assert item["properties"]["severity"]["enum"] == ["critical", "high", "medium", "low"]
