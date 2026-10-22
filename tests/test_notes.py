"""Candidate notes (state that survives across iterations) and the labelled stages of the loop."""

import pytest

from devops_agent import agent_loop
from devops_agent.agent_loop import run_agent
from devops_agent.llm import LLMReply, ToolCall
from devops_agent.report import build_report
from devops_agent.schema import NOTE_FINDING, SUBMIT_REPORT
from devops_agent.trace import Trace
from tools import Toolbox

VIOLATION = {
    "rule_id": "SEC-006", "category": "security", "severity": "medium", "file": "settings.py", "line": 1,
    "description": "Debug enabled", "evidence": "DEBUG = True", "recommendation": "disable it",
    "evidence_refs": [1],
}


def call(name, arguments=None, call_id=None, parse_error=None):
    return ToolCall(id=call_id or f"id-{name}", name=name, arguments=arguments or {}, parse_error=parse_error)


def note(summary="DEBUG = True in settings", file="settings.py", step=1, call_id="n1"):
    return call(NOTE_FINDING, {"summary": summary, "file": file, "step": step}, call_id)


def submit(violations=()):
    return LLMReply("", [call(SUBMIT_REPORT, {"violations": list(violations)}, "id-submit")])


class ScriptedLLM:
    def __init__(self, script):
        self.script = script
        self.turn = 0
        self.seen = []

    def chat(self, messages, tools=None):
        self.seen.append(([dict(m) for m in messages], [t["function"]["name"] for t in tools or []]))
        self.turn += 1
        return self.script(messages, self.turn) if callable(self.script) else self.script[self.turn - 1]


@pytest.fixture
def box(tmp_path):
    (tmp_path / "README.md").write_text("# demo\n")
    (tmp_path / "settings.py").write_text("DEBUG = True\n")
    return Toolbox(tmp_path)


def run(llm, box, **kwargs):
    return run_agent(llm, box, "https://github.com/o/r", "system prompt", **kwargs)


def tool_messages(llm):
    return [m for m in llm.seen[-1][0] if m["role"] == "tool"]


def test_notes_are_stored_acknowledged_and_returned(box):
    llm = ScriptedLLM([LLMReply("", [call("list_files", call_id="a")]), LLMReply("", [note()]), submit()])
    result = run(llm, box)
    assert result.notes == [
        {"recorded_at_step": 2, "summary": "DEBUG = True in settings", "file": "settings.py", "evidence_step": 1}
    ]
    assert tool_messages(llm)[1]["content"].startswith("[step 2] Noted (1 candidate note(s) so far)")


def test_notes_are_replayed_when_the_budget_is_exhausted(box):
    def script(messages, turn):
        if turn == 1:
            return LLMReply("", [note(), call("list_files", call_id="l1")])
        if turn == 2:
            return LLMReply("", [call("read_file", {"path": "README.md"}, "r1")])
        return submit([VIOLATION])  # the forced request

    llm = ScriptedLLM(script)
    result = run(llm, box, max_steps=2)
    assert result.stop_reason == "max_steps_forced"
    final_request = llm.seen[-1][0][-1]["content"]
    assert "Candidate notes you recorded" in final_request and "DEBUG = True in settings" in final_request
    assert llm.seen[-1][1] == [SUBMIT_REPORT]


def test_invalid_notes_are_rejected_without_being_stored(box):
    bad = [
        call(NOTE_FINDING, {"summary": "  "}, "a"),
        call(NOTE_FINDING, {}, "b"),
        call(NOTE_FINDING, call_id="c", parse_error="tool arguments were not valid JSON"),
    ]
    llm = ScriptedLLM([LLMReply("", bad), submit()])
    result = run(llm, box)
    assert result.notes == []
    assert all("ERROR" in m["content"] for m in tool_messages(llm))


def test_note_limit_is_enforced(box, monkeypatch):
    monkeypatch.setattr(agent_loop, "MAX_NOTES", 2)
    calls = [note(summary=f"note {i}", call_id=f"n{i}") for i in range(3)]
    llm = ScriptedLLM([LLMReply("", calls), submit()])
    result = run(llm, box)
    assert len(result.notes) == 2 and "Note limit (2) reached" in tool_messages(llm)[2]["content"]


def test_note_with_odd_field_types_is_normalised(box):
    odd = call(NOTE_FINDING, {"summary": "x" * 900, "file": 5, "step": True}, "n")
    result = run(ScriptedLLM([LLMReply("", [odd]), submit()]), box)
    assert len(result.notes[0]["summary"]) == 300
    assert result.notes[0]["file"] is None and result.notes[0]["evidence_step"] is None


def test_notes_do_not_count_as_evidence_in_the_report(box):
    result = run(ScriptedLLM([LLMReply("", [note()]), submit([{**VIOLATION, "evidence_refs": [1]}])]), box)
    report, notes = build_report("https://github.com/o/r", result.violations, box.root, result.executed_steps)
    assert report["violations"] == [] and "no valid evidence_refs" in notes[0]


def test_notes_appear_in_the_trace(box):
    trace = Trace()
    run(ScriptedLLM([LLMReply("", [note()]), submit()]), box, trace=trace)
    assert any(e["type"] == "note_finding" and "settings.py" in e["text"] for e in trace.entries)


def test_note_finding_is_offered_to_the_model(box):
    llm = ScriptedLLM([submit()])
    run(llm, box)
    assert NOTE_FINDING in llm.seen[0][1] and "search_history" in llm.seen[0][1]


def test_every_stage_of_the_cycle_is_a_distinct_function_or_labelled_block():
    source = open(agent_loop.__file__, encoding="utf-8").read()
    for label in ("1. OBSERVE", "2. REASON", "3. SELECT TOOL", "4. EXECUTE TOOL", "5. INTERPRET RESULTS", "6. REPORT"):
        assert label in source, label
    for function in ("_observe", "_execute", "_interpret", "_record_note", "_forced_report"):
        assert callable(getattr(agent_loop, function))
