import contextlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

import agent
from devops_agent.agent_loop import AgentError
from devops_agent.config import Config, ConfigError
from devops_agent.llm import LLMError, LLMReply, ToolCall
from devops_agent.repo import CloneError
from devops_agent.schema import SUBMIT_REPORT, VIOLATION_FIELDS

PROJECT = Path(__file__).resolve().parent.parent
URL = "https://github.com/octo/demo"
KEY = "sk-cli-secret-12345678"
CONFIG = Config(base_url="http://x", api_key=KEY, model="test-model", max_steps=15, timeout_seconds=600)

VIOLATION = {
    "rule_id": "SEC-001", "category": "security", "severity": "high", "file": "config.py", "line": 1,
    "description": "Hardcoded credential", "evidence": "assigned in source",
    "recommendation": "use env vars", "evidence_refs": [1],
}


def call(name, arguments, call_id):
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ScriptedLLM:
    def __init__(self, script):
        self.script = list(script)
        self.first_system_prompt = None

    def chat(self, messages, tools=None):
        self.first_system_prompt = self.first_system_prompt or messages[0]["content"]
        reply = self.script.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (root / "config.py").write_text('TOKEN = "placeholder"\nprint(1)\n')
    return root


def fake_cloner(workspace, canonical=URL, calls=None):
    @contextlib.contextmanager
    def cloner(url):
        if calls is not None:
            calls.append(url)
        yield workspace, canonical

    return cloner


def failing_cloner(error):
    @contextlib.contextmanager
    def cloner(url):
        raise error
        yield  # pragma: no cover

    return cloner


def run_cli(tmp_path, argv_extra=(), *, llm=None, cloner=None, config=CONFIG, workspace=None, output="report.json"):
    out = tmp_path / output
    argv = ["--repo", URL, "--output", str(out), *argv_extra]
    if config is ConfigError:
        loader = lambda: (_ for _ in ()).throw(ConfigError("missing required setting(s): LLM_API_KEY"))
    else:
        loader = lambda: config
    code = agent.main(argv, config_loader=loader, llm_factory=lambda cfg: llm, cloner=cloner or fake_cloner(workspace))
    return code, out


GOOD_SCRIPT = lambda: [
    LLMReply("", [call("read_file", {"path": "config.py"}, "c1")]),
    LLMReply("", [call(SUBMIT_REPORT, {"violations": [VIOLATION]}, "c2")]),
]


# ---- success paths --------------------------------------------------------------

def test_successful_run_writes_valid_report(tmp_path, workspace, capsys):
    code, out = run_cli(tmp_path, llm=ScriptedLLM(GOOD_SCRIPT()), workspace=workspace)
    assert code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert list(report) == ["repository", "violations"] and report["repository"] == URL
    assert list(report["violations"][0]) == list(VIOLATION_FIELDS)
    assert report["violations"][0]["file"] == "config.py" and report["violations"][0]["line"] == 1
    err = capsys.readouterr().err
    assert "Done: 1 violation(s)" in err and "stop reason: submitted" in err


def test_healthy_repo_writes_empty_violations(tmp_path, workspace):
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": []}, "c1")])])
    code, out = run_cli(tmp_path, llm=llm, workspace=workspace)
    assert code == 0 and json.loads(out.read_text()) == {"repository": URL, "violations": []}


def test_unsupported_claims_are_dropped_and_reported_in_notes(tmp_path, workspace, capsys):
    bad = {**VIOLATION, "evidence_refs": [42]}
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": [bad]}, "c1")])])
    code, out = run_cli(tmp_path, llm=llm, workspace=workspace)
    assert code == 0 and json.loads(out.read_text())["violations"] == []
    assert "note: dropped violation #1" in capsys.readouterr().err


def test_overrides_reach_the_agent_and_prompt(tmp_path, workspace):
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": []}, "c1")])])
    code, _ = run_cli(tmp_path, ["--max-steps", "3", "--timeout", "50"], llm=llm, workspace=workspace)
    assert code == 0 and "at most 3 model turns" in llm.first_system_prompt


def test_nested_output_directory_is_created(tmp_path, workspace):
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": []}, "c1")])])
    code, out = run_cli(tmp_path, llm=llm, workspace=workspace, output="a/b/report.json")
    assert code == 0 and out.exists()


def test_trace_file_written_on_success_and_verbose_echoes(tmp_path, workspace, capsys):
    trace_path = tmp_path / "trace.json"
    code, _ = run_cli(tmp_path, ["--trace", str(trace_path), "--verbose"], llm=ScriptedLLM(GOOD_SCRIPT()), workspace=workspace)
    assert code == 0
    entries = json.loads(trace_path.read_text())
    assert [e["type"] for e in entries] == ["tool_call", "submit_report"] and entries[0]["tool"] == "read_file"
    assert "[step 1] read_file" in capsys.readouterr().err


# ---- failure paths and exit codes ------------------------------------------------

@pytest.mark.parametrize("bad_url", ["not a url", "https://gitlab.com/a/b", "ftp://x", "https://github.com/onlyowner"])
def test_invalid_url_exits_2_without_cloning(tmp_path, bad_url):
    cloned = []
    out = tmp_path / "r.json"
    code = agent.main(["--repo", bad_url, "--output", str(out)], config_loader=lambda: CONFIG,
                      cloner=fake_cloner(None, calls=cloned))
    assert code == 2 and cloned == [] and not out.exists()


def test_missing_config_exits_2(tmp_path, workspace, capsys):
    code, out = run_cli(tmp_path, config=ConfigError, llm=None, workspace=workspace)
    assert code == 2 and not out.exists()
    assert "LLM_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--max-steps", "0"], ["--timeout", "-5"]])
def test_non_positive_limits_exit_2(tmp_path, workspace, extra):
    assert run_cli(tmp_path, extra, llm=None, workspace=workspace)[0] == 2


def test_missing_required_arguments_exit_2():
    assert agent.main([]) == 2
    assert agent.main(["--repo", URL]) == 2


def test_help_exits_0():
    assert agent.main(["--help"]) == 0


def test_output_path_that_is_a_directory_exits_2_before_cloning(tmp_path):
    cloned = []
    directory = tmp_path / "outdir"
    directory.mkdir()
    code = agent.main(["--repo", URL, "--output", str(directory)], config_loader=lambda: CONFIG,
                      cloner=fake_cloner(None, calls=cloned))
    assert code == 2 and cloned == []


def test_unwritable_output_location_exits_2(tmp_path):
    blocker = tmp_path / "file.txt"
    blocker.write_text("x")
    code = agent.main(["--repo", URL, "--output", str(blocker / "report.json")], config_loader=lambda: CONFIG,
                      cloner=fake_cloner(None))
    assert code == 2


def test_clone_failure_exits_3(tmp_path, capsys):
    code, out = run_cli(tmp_path, llm=None, cloner=failing_cloner(CloneError("repository not found or not public")))
    assert code == 3 and not out.exists()
    assert "repository not found or not public" in capsys.readouterr().err


@pytest.mark.parametrize("error", [LLMError("LLM request failed after 5 attempts"), AgentError("stopped without producing a report")])
def test_llm_or_agent_failure_exits_4_and_writes_no_report(tmp_path, workspace, error):
    code, out = run_cli(tmp_path, llm=ScriptedLLM([error]), workspace=workspace)
    assert code == 4 and not out.exists()


def test_failed_run_keeps_existing_report_untouched(tmp_path, workspace):
    existing = tmp_path / "report.json"
    existing.write_text("previous")
    code, _ = run_cli(tmp_path, llm=ScriptedLLM([LLMError("down")]), workspace=workspace)
    assert code == 4 and existing.read_text() == "previous"


def test_unexpected_error_exits_1_with_message(tmp_path, workspace, capsys):
    llm = ScriptedLLM([RuntimeError("boom")])
    code, out = run_cli(tmp_path, llm=llm, workspace=workspace)
    assert code == 1 and not out.exists()
    assert "unexpected RuntimeError: boom" in capsys.readouterr().err


def test_keyboard_interrupt_exits_130(tmp_path, workspace):
    assert run_cli(tmp_path, llm=ScriptedLLM([KeyboardInterrupt()]), workspace=workspace)[0] == 130


def test_trace_written_even_when_the_run_fails(tmp_path, workspace):
    trace_path = tmp_path / "trace.json"
    script = [LLMReply("", [call("list_files", {}, "c1")]), LLMError("down")]
    code, _ = run_cli(tmp_path, ["--trace", str(trace_path)], llm=ScriptedLLM(script), workspace=workspace)
    assert code == 4 and json.loads(trace_path.read_text())[0]["tool"] == "list_files"


def test_unwritable_trace_path_only_warns(tmp_path, workspace, capsys):
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": []}, "c1")])])
    code, out = run_cli(tmp_path, ["--trace", str(tmp_path / "missing-dir" / "t.json")], llm=llm, workspace=workspace)
    assert code == 0 and out.exists() and "could not write trace" in capsys.readouterr().err


def test_api_key_never_appears_in_stderr_or_report(tmp_path, workspace, capsys):
    code, out = run_cli(tmp_path, llm=ScriptedLLM([LLMError(f"request rejected, key was {KEY}")]), workspace=workspace)
    assert code == 4
    assert KEY not in capsys.readouterr().err
    code, out = run_cli(tmp_path, llm=ScriptedLLM([
        LLMReply("", [call(SUBMIT_REPORT, {"violations": [{**VIOLATION, "evidence": f"saw {KEY} in file"}]}, "c1"),
                      ])]), workspace=workspace)
    assert KEY not in out.read_text()


# ---- the real entry point, as the evaluator will run it ----------------------------

def run_script(*args):
    return subprocess.run([sys.executable, str(PROJECT / "agent.py"), *args], capture_output=True, text=True, cwd=PROJECT, timeout=60)


def test_script_without_arguments_prints_usage_and_exits_2():
    result = run_script()
    assert result.returncode == 2 and "usage:" in result.stderr and "--repo" in result.stderr


def test_script_rejects_bad_url_with_message(tmp_path):
    result = run_script("--repo", "definitely not a url", "--output", str(tmp_path / "r.json"))
    assert result.returncode == 2 and "unsupported repository URL" in result.stderr
    assert not (tmp_path / "r.json").exists() and "Traceback" not in result.stderr


def test_script_help_works():
    result = run_script("--help")
    assert result.returncode == 0 and "--output" in result.stdout


def test_cli_logs_how_defaults_were_chosen(tmp_path, workspace, capsys):
    from dataclasses import replace

    config = replace(CONFIG, notes=("LLM_MODEL not set; using the default groq model 'x' (set LLM_MODEL to change it)",))
    llm = ScriptedLLM([LLMReply("", [call(SUBMIT_REPORT, {"violations": []}, "c1")])])
    code, _ = run_cli(tmp_path, llm=llm, workspace=workspace, config=config)
    assert code == 0 and "note: LLM_MODEL not set" in capsys.readouterr().err
