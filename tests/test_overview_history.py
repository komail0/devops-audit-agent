import subprocess

from devops_agent.prompts import build_system_prompt
from devops_agent.report import build_report
from tools import Toolbox


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(root, commits):
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "d@example.com")
    _git(root, "config", "user.name", "D")
    for number in range(commits):
        (root / "f.txt").write_text(f"version {number}\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", f"commit {number}")
    return Toolbox(root)


def test_overview_states_how_many_commits_the_history_has(tmp_path):
    assert "Git history: 3 commit(s) available." in _repo(tmp_path / "r", 3).overview()


def test_overview_omits_the_history_line_for_a_repository_without_commits(tmp_path):
    assert "Git history" not in _repo(tmp_path / "empty", 0).overview()


def test_overview_flags_a_shallow_clone(tmp_path):
    from devops_agent import repo as repo_module

    source = _repo(tmp_path / "src", 3)
    repo_module.CLONE_DEPTH, original = 1, repo_module.CLONE_DEPTH
    try:
        repo_module.run_clone(source.root.as_uri(), tmp_path / "shallow", allow_file=True)
    finally:
        repo_module.CLONE_DEPTH = original
    assert "(shallow clone)" in Toolbox(tmp_path / "shallow").overview()


def test_prompt_asks_for_one_history_sweep_when_there_is_more_than_one_commit():
    hunt = build_system_prompt(15).split("2. Hunt:")[1].split("3. Verify:")[0]
    assert "more than one commit" in hunt and "search_history sweep" in hunt and "password|secret|token|api_key" in hunt


def test_cleared_line_note_does_not_blame_the_file_length(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01binary")
    violation = {
        "rule_id": "HYG-003", "category": "hygiene", "severity": "low", "file": "blob.bin", "line": 1,
        "description": "Tracked binary", "evidence": "file is tracked", "recommendation": "remove it",
        "evidence_refs": [1],
    }
    report, notes = build_report("https://github.com/o/r", [violation], tmp_path, {1: "git_status"})
    assert report["violations"][0]["line"] is None
    assert any("not a valid line for that file" in note for note in notes)
