import subprocess

import pytest

import tools
from tools import Toolbox


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _init(root):
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")


@pytest.fixture
def history_repo(tmp_path):
    root = tmp_path / "hist"
    _init(root)
    (root / "cfg.py").write_text('API_TOKEN = "supersecret-value-12345"\nSTART = 1\n')
    (root / "other.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "add config")
    (root / "cfg.py").write_text('import os\nAPI_TOKEN = os.environ["API_TOKEN"]\nSTART = 1\n')
    _git(root, "commit", "-qam", "remove secret from source")
    return Toolbox(root)


def test_finds_a_secret_that_was_removed_from_the_source(history_repo):
    out = history_repo.execute("search_history", {"pattern": "API_TOKEN"})
    assert "3 match(es)" in out
    assert 'cfg.py: -API_TOKEN = "[REDACTED:credential]"' in out  # the removed line, value hidden
    assert 'cfg.py: +API_TOKEN = os.environ["API_TOKEN"]' in out
    assert "supersecret-value-12345" not in out and "removed by that commit" in out


def test_reports_commit_hash_and_date(history_repo):
    out = history_repo.execute("search_history", {"pattern": "API_TOKEN"})
    newest = history_repo.git_log().splitlines()[1].split(" | ")[0]
    assert any(line.startswith(newest) and "cfg.py: -API_TOKEN" in line for line in out.splitlines())


def test_path_filter_and_no_match(history_repo):
    assert "0 match(es)" in history_repo.execute("search_history", {"pattern": "API_TOKEN", "path": "other.py"})
    assert "0 match(es)" in history_repo.execute("search_history", {"pattern": "never-there"})


def test_accepts_python_regex_syntax_that_posix_lacks(history_repo):
    out = history_repo.execute("search_history", {"pattern": r"\d{5}"})
    assert "match(es)" in out and "0 match(es)" not in out and "cfg.py" in out


def test_errors_come_back_as_messages(history_repo):
    assert history_repo.execute("search_history", {"pattern": "("}).startswith("ERROR: invalid regular expression")
    assert history_repo.execute("search_history", {"pattern": ""}).startswith("ERROR")
    assert history_repo.execute("search_history", {"pattern": "a" * 1000}).startswith("ERROR")
    assert history_repo.execute("search_history", {"pattern": "x", "path": "../x"}).startswith("ERROR")
    assert history_repo.execute("search_history", {"pattern": "x", "max_commits": "many"}).startswith("ERROR")


def test_pattern_is_never_treated_as_a_git_option(history_repo):
    out = history_repo.execute("search_history", {"pattern": "--all"})
    assert not out.startswith("ERROR") and "0 match(es)" in out


def test_match_cap(history_repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_HISTORY_MATCHES", 1)
    out = history_repo.execute("search_history", {"pattern": "API_TOKEN"})
    assert "3 match(es)" in out and "Showing the first 1" in out and out.count("cfg.py:") == 1


def test_changed_lines_that_look_like_diff_headers_are_not_misread(tmp_path):
    root = tmp_path / "dash"
    _init(root)
    (root / "q.sql").write_text("-- comment marker\nselect 1;\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sql")
    (root / "q.sql").write_text("select 1;\n")
    _git(root, "commit", "-qam", "drop comment")
    out = Toolbox(root).execute("search_history", {"pattern": "comment marker"})
    assert "q.sql: --- comment marker" in out  # removed line that itself starts with dashes
    assert "q.sql: +-- comment marker" in out  # the same line when it was added
    assert "2 match(es)" in out


def test_shallow_history_is_flagged(history_repo, tmp_path, monkeypatch):
    from devops_agent import repo as repo_module

    monkeypatch.setattr(repo_module, "CLONE_DEPTH", 1)
    dest = tmp_path / "shallow"
    repo_module.run_clone(history_repo.root.as_uri(), dest, allow_file=True)
    out = Toolbox(dest).execute("search_history", {"pattern": "API_TOKEN"})
    assert "history is shallow" in out and "supersecret" not in out


def test_repository_with_no_commits(tmp_path):
    empty = tmp_path / "empty"
    _init(empty)
    box = Toolbox(empty)
    assert box.execute("git_log") == "(no commits yet)"
    assert box.execute("search_history", {"pattern": "x"}) == "(no commits yet)"


def test_tool_spec_is_registered(history_repo):
    names = {spec["function"]["name"] for spec in tools.TOOL_SPECS}
    assert "search_history" in names and "search_history" in history_repo._registry
