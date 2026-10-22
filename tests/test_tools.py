import os
import subprocess

import pytest

import tools
from tools import TOOL_SPECS, Toolbox

# Fake secret assembled at runtime so this file holds no secret-shaped literal.
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    (root / "README.md").write_text("# Demo\n")
    (root / "app.py").write_text(f'import os\nKEY = "{FAKE_AWS}"\nprint("hi")\n')
    (root / "src").mkdir()
    (root / "src" / "util.py").write_text("def f():\n    return 1\n")
    (root / ".github" / "workflows").mkdir(parents=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: CI\non: push\n")
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "app.cpython-311.pyc").write_bytes(b"\x00pyc")
    (root / ".env").write_text("X=1\n")
    (root / ".env.example").write_text("X=\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial commit")
    (root / "src" / "util.py").write_text("def f():\n    return 2\n")
    _git(root, "commit", "-q", "-am", "change util")
    return Toolbox(root)


# ---- list_files -----------------------------------------------------------

def test_list_files_shows_structure_and_skips_git(repo):
    out = repo.list_files()
    assert "README.md" in out and "src/util.py" in out and ".github/workflows/ci.yml" in out
    assert ".git/" not in out and ".git\\" not in out


def test_list_files_subdirectory_and_depth(repo):
    assert "util.py" in repo.list_files("src")
    assert "ci.yml" not in repo.list_files(max_depth=1)
    assert "README.md" in repo.list_files(max_depth=1)


def test_list_files_errors(repo):
    with pytest.raises(tools.ToolError):
        repo.list_files("nope")
    with pytest.raises(tools.ToolError):
        repo.list_files("README.md")


def test_list_files_truncates(repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_LIST_ENTRIES", 2)
    out = repo.list_files()
    assert "showing first 2" in out


# ---- read_file ------------------------------------------------------------

def test_read_file_numbers_lines(repo):
    out = repo.read_file("src/util.py")
    assert "    1| def f():" in out and "    2|     return 2" in out
    assert "lines 1-2 of 2" in out


def test_read_file_range_and_paging(repo, tmp_path):
    big = repo.root / "big.txt"
    big.write_text("\n".join(f"line{i}" for i in range(1, 1001)))
    out = repo.read_file("big.txt")
    assert "lines 1-400 of 1000" in out
    assert "start_line=401" in out
    out = repo.read_file("big.txt", start_line=10, end_line=12)
    assert "   10| line10" in out and "   12| line12" in out and "line13" not in out


def test_read_file_errors(repo):
    for kwargs in (
        {"path": "missing.txt"},
        {"path": "src"},
        {"path": "blob.bin"},
        {"path": "app.py", "start_line": 0},
        {"path": "app.py", "start_line": 99},
        {"path": "app.py", "start_line": 3, "end_line": 1},
        {"path": "app.py", "start_line": "abc"},
    ):
        with pytest.raises(tools.ToolError):
            repo.read_file(**kwargs)


def test_read_file_truncates_long_lines_and_large_files(repo, monkeypatch):
    (repo.root / "wide.txt").write_text("x" * 2000 + "\n")
    assert "..." in repo.read_file("wide.txt")
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 10)
    (repo.root / "long.txt").write_text("aaaa\nbbbb\ncccc\ndddd\n")
    assert "truncated" in repo.read_file("long.txt", start_line=1, end_line=50)


@pytest.mark.parametrize(
    "bad", ["../outside.txt", "src/../../outside.txt", "/etc/passwd", "C:\\Windows\\win.ini", ".git/config", "src/../.git/HEAD"]
)
def test_path_escape_rejected(repo, bad):
    with pytest.raises(tools.ToolError):
        repo.read_file(bad)


def test_symlink_not_followed(repo, tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("top secret")
    link = repo.root / "link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this system")
    with pytest.raises(tools.ToolError):
        repo.read_file("link.txt")
    assert "link.txt" not in repo.list_files()
    assert "top secret" not in repo.search_code("secret")


# ---- search_code ----------------------------------------------------------

def test_search_finds_with_file_and_line(repo):
    out = repo.search_code(r"return \d")
    assert "src/util.py:2:" in out


def test_search_skips_binary_and_git(repo):
    out = repo.search_code("binary")
    assert "blob.bin" not in out and ".git/" not in out


def test_search_glob_and_case(repo):
    assert "ci.yml" in repo.search_code("NAME", path_glob="*.yml", case_insensitive=True)
    assert "0 match" in repo.search_code("NAME", path_glob="*.yml")


def test_search_invalid_and_oversized_pattern(repo):
    with pytest.raises(tools.ToolError):
        repo.search_code("(unclosed")
    with pytest.raises(tools.ToolError):
        repo.search_code("a" * 1000)
    with pytest.raises(tools.ToolError):
        repo.search_code("")


def test_search_limits_matches(repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_SEARCH_MATCHES", 3)
    (repo.root / "many.txt").write_text("hit\n" * 50)
    out = repo.search_code("hit")
    assert "3 match" in out and "stopped after 3" in out


# ---- git tools ------------------------------------------------------------

def test_git_log(repo):
    out = repo.git_log()
    assert "change util" in out and "initial commit" in out
    assert "Dev" in out


def test_git_log_path_count_and_files(repo):
    out = repo.git_log(path="README.md")
    assert "initial commit" in out and "change util" not in out
    assert "src/util.py" in repo.git_log(max_count=1, show_files=True)
    assert "initial commit" not in repo.git_log(max_count=1)


def test_git_log_marks_shallow_history(repo, tmp_path, monkeypatch):
    from devops_agent import repo as repo_module

    monkeypatch.setattr(repo_module, "CLONE_DEPTH", 1)
    dest = tmp_path / "shallow-copy"
    repo_module.run_clone(repo.root.as_uri(), dest, allow_file=True)
    out = Toolbox(dest).git_log()
    assert "change util" in out and "initial commit" not in out
    assert "history is shallow" in out


def test_git_status_flags_tracked_generated_files(repo):
    out = repo.git_status()
    assert "__pycache__/app.cpython-311.pyc  <- Python bytecode cache" in out
    assert ".env  <- environment file" in out
    assert ".env.example" not in out
    assert ".gitignore at repository root: absent" in out


def test_git_status_clean_repo(tmp_path):
    root = tmp_path / "clean"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "d@example.com")
    _git(root, "config", "user.name", "D")
    (root / ".gitignore").write_text(".env\n")
    (root / "main.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    out = Toolbox(root).git_status()
    assert "(none)" in out and "present" in out


def test_git_tools_outside_git_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    out = Toolbox(plain).execute("git_log")
    assert out.startswith("ERROR")


# ---- execute / dispatch ---------------------------------------------------

def test_execute_redacts_secrets(repo):
    for out in (repo.execute("read_file", {"path": "app.py"}), repo.execute("search_code", {"pattern": "KEY"})):
        assert FAKE_AWS not in out
        assert "[REDACTED:aws-access-key]" in out


def test_execute_returns_errors_not_exceptions(repo):
    assert repo.execute("nope").startswith("ERROR: unknown tool")
    assert repo.execute("read_file", {"path": "missing"}).startswith("ERROR")
    assert repo.execute("read_file", {}).startswith("ERROR: invalid arguments")
    assert repo.execute("read_file", {"path": "app.py", "bogus": 1}).startswith("ERROR: unexpected argument")
    assert repo.execute("list_files", ["not", "a", "dict"]).startswith("ERROR")


def test_execute_caps_output(repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_OUTPUT_CHARS", 50)
    assert repo.execute("list_files").endswith("[output truncated]")


def test_execute_hides_unexpected_exception_details(repo, monkeypatch):
    def boom(**_):
        raise RuntimeError("secret internals")

    monkeypatch.setitem(repo._registry, "git_status", boom)
    out = repo.execute("git_status")
    assert out.startswith("ERROR") and "secret internals" not in out


def test_tool_specs_match_registry(repo):
    names = {spec["function"]["name"] for spec in TOOL_SPECS}
    assert names == set(repo._registry)
    for spec in TOOL_SPECS:
        assert spec["type"] == "function"
        assert spec["function"]["parameters"]["type"] == "object"


def test_read_file_stops_at_char_budget_and_points_to_next_line(repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_READ_CHARS", 300)
    (repo.root / "wordy.txt").write_text("\n".join("w" * 80 for _ in range(50)))
    out = repo.read_file("wordy.txt")
    shown = [line for line in out.splitlines() if "| w" in line]
    assert 1 <= len(shown) < 10
    assert f"start_line={len(shown) + 1}]" in out
    assert f"lines 1-{len(shown)} of 50" in out


def test_read_file_always_returns_at_least_one_line(repo, monkeypatch):
    monkeypatch.setattr(tools, "MAX_READ_CHARS", 1)
    (repo.root / "one.txt").write_text("a\nb\n")
    assert "    1| a" in repo.read_file("one.txt")


def test_redaction_adds_an_explanatory_note_only_when_something_was_redacted(repo):
    with_secret = repo.execute("read_file", {"path": "app.py"})
    assert "[REDACTED:aws-access-key]" in with_secret and "hardcoded credential" in with_secret
    assert with_secret.count("[note: [REDACTED:...]") == 1
    clean = repo.execute("read_file", {"path": "README.md"})
    assert "[note:" not in clean
