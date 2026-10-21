import subprocess
from pathlib import Path

import pytest

from devops_agent import repo
from devops_agent.repo import CloneError, InvalidRepoUrl, parse_github_url, run_clone


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/octocat/Hello-World",
        "https://github.com/octocat/Hello-World/",
        "https://github.com/octocat/Hello-World.git",
        "https://github.com/octocat/Hello-World/tree/main/src",
        "  https://github.com/octocat/Hello-World  ",
    ],
)
def test_valid_urls_normalise(url):
    owner, name, canonical = parse_github_url(url)
    assert (owner, name) == ("octocat", "Hello-World")
    assert canonical == "https://github.com/octocat/Hello-World"


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "http://github.com/octocat/Hello-World",
        "https://gitlab.com/octocat/Hello-World",
        "https://github.com/octocat",
        "https://github.com/octocat/..",
        "file:///etc/passwd",
        "git@github.com:octocat/Hello-World.git",
        "https://github.com/octo cat/repo",
        "https://github.com/octocat/repo;rm -rf /",
        "https://github.com/octocat/repo?x=1",
        "https://github.com.evil.com/octocat/repo",
        "--upload-pack=evil",
    ],
)
def test_invalid_urls_rejected(url):
    with pytest.raises(InvalidRepoUrl):
        parse_github_url(url)


def test_non_string_rejected():
    with pytest.raises(InvalidRepoUrl):
        parse_github_url(None)


class _Result:
    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr


def test_clone_command_disables_risky_features(tmp_path):
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"], captured["kwargs"] = cmd, kwargs
        return _Result()

    run_clone("https://github.com/o/r.git", tmp_path / "x", runner=fake_runner)
    cmd = captured["cmd"]
    assert "--no-recurse-submodules" in cmd
    assert "protocol.file.allow=never" in cmd
    assert "core.symlinks=false" in cmd
    assert captured["kwargs"]["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert captured["kwargs"]["env"]["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert captured["kwargs"]["timeout"] == repo.CLONE_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    "stderr, expected",
    [
        ("fatal: repository 'x' not found", "not found or not public"),
        ("fatal: could not read Username for 'https://github.com'", "not found or not public"),
        ("fatal: unable to access 'x': Could not resolve host: github.com", "network error"),
        ("fatal: something odd happened", "something odd happened"),
        ("", "unknown error"),
    ],
)
def test_clone_failure_messages(tmp_path, stderr, expected):
    runner = lambda cmd, **kw: _Result(returncode=128, stderr=stderr)
    with pytest.raises(CloneError, match=expected):
        run_clone("https://github.com/o/r.git", tmp_path / "x", runner=runner)


def test_clone_timeout(tmp_path):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    with pytest.raises(CloneError, match="timed out"):
        run_clone("https://github.com/o/r.git", tmp_path / "x", timeout=5, runner=runner)


def test_missing_git_executable(tmp_path):
    def runner(cmd, **kw):
        raise FileNotFoundError()

    with pytest.raises(CloneError, match="git executable not found"):
        run_clone("https://github.com/o/r.git", tmp_path / "x", runner=runner)


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_real_local_clone_and_file_protocol_blocked(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.email", "t@example.com")
    _git(source, "config", "user.name", "t")
    (source / "README.md").write_text("hello\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "init")

    dest = tmp_path / "dest"
    run_clone(source.as_uri(), dest, allow_file=True)
    assert (dest / "README.md").read_text().strip() == "hello"

    with pytest.raises(CloneError):
        run_clone(source.as_uri(), tmp_path / "blocked")


def test_cloned_repo_cleans_up(tmp_path, monkeypatch):
    seen = {}

    def fake_clone(url, dest, timeout=0):
        dest.mkdir(parents=True)
        (dest / "f.txt").write_text("x")
        seen["dest"] = dest
        return "https://github.com/o/r"

    monkeypatch.setattr(repo, "clone_repo", fake_clone)
    with repo.cloned_repo("https://github.com/o/r") as (path, canonical):
        assert Path(path).exists()
        assert canonical == "https://github.com/o/r"
    assert not seen["dest"].exists()


def test_cloned_repo_cleans_up_on_failure(monkeypatch):
    seen = {}

    def failing_clone(url, dest, timeout=0):
        seen["root"] = dest.parent
        raise CloneError("boom")

    monkeypatch.setattr(repo, "clone_repo", failing_clone)
    with pytest.raises(CloneError):
        with repo.cloned_repo("https://github.com/o/r"):
            pass
    assert not seen["root"].exists()
