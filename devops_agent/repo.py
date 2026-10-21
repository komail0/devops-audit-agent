"""Safe retrieval of a public GitHub repository into a throwaway workspace.

The clone is read-only input for the inspection tools. Nothing from the
repository is ever executed or imported.
"""

import contextlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Optional, Tuple

CLONE_TIMEOUT_SECONDS = 120
CLONE_DEPTH = 100

_NAME = r"[A-Za-z0-9_.-]+"
_URL_RE = re.compile(
    rf"^https://github\.com/({_NAME})/({_NAME}?)(?:\.git)?(?:/(?:tree|blob)/\S*)?/?$"
)


class RepoError(Exception):
    """Base class for repository retrieval problems."""


class InvalidRepoUrl(RepoError):
    """The argument is not a supported public GitHub repository URL."""


class CloneError(RepoError):
    """The repository could not be cloned."""


def parse_github_url(url: str) -> Tuple[str, str, str]:
    """Return (owner, repo, canonical_url) or raise InvalidRepoUrl."""
    if not isinstance(url, str) or not url.strip():
        raise InvalidRepoUrl("repository URL is empty")
    match = _URL_RE.match(url.strip())
    if not match:
        raise InvalidRepoUrl(
            f"unsupported repository URL: expected https://github.com/<owner>/<repo>, got {url!r}"
        )
    owner, repo = match.group(1), match.group(2)
    if owner in (".", "..") or repo in (".", "..") or not repo:
        raise InvalidRepoUrl(f"invalid owner or repository name in {url!r}")
    return owner, repo, f"https://github.com/{owner}/{repo}"


def _clone_command(clone_url: str, dest: Path, allow_file: bool) -> list:
    hooks_dir = tempfile.gettempdir()
    return [
        "git",
        "-c", f"core.hooksPath={hooks_dir}",
        "-c", "core.symlinks=false",
        "-c", f"protocol.file.allow={'always' if allow_file else 'never'}",
        "-c", "protocol.ext.allow=never",
        "clone",
        "--depth", str(CLONE_DEPTH),
        "--no-tags",
        "--single-branch",
        "--no-recurse-submodules",
        "--quiet",
        clone_url,
        str(dest),
    ]


def run_clone(
    clone_url: str,
    dest: Path,
    timeout: int = CLONE_TIMEOUT_SECONDS,
    allow_file: bool = False,
    runner: Callable = subprocess.run,
) -> None:
    """Clone clone_url into dest with hooks, submodules, LFS and prompts disabled."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1")
    try:
        result = runner(
            _clone_command(clone_url, dest, allow_file),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CloneError(f"clone timed out after {timeout}s") from None
    except FileNotFoundError:
        raise CloneError("git executable not found; install git and retry") from None
    if result.returncode != 0:
        raise CloneError(_explain_failure(result.stderr or ""))


def _explain_failure(stderr: str) -> str:
    lowered = stderr.lower()
    if "not found" in lowered or "could not read username" in lowered or "authentication failed" in lowered:
        return "repository not found or not public"
    if "could not resolve host" in lowered or "unable to access" in lowered:
        return "network error while cloning the repository"
    first_line = stderr.strip().splitlines()[0] if stderr.strip() else "unknown error"
    return f"git clone failed: {first_line}"


def clone_repo(url: str, dest: Path, timeout: int = CLONE_TIMEOUT_SECONDS) -> str:
    """Validate url, clone it into dest and return the canonical URL."""
    _, _, canonical = parse_github_url(url)
    run_clone(canonical + ".git", dest, timeout=timeout)
    return canonical


def _make_writable_and_retry(func: Callable, path: str, _exc: Optional[object]) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


@contextlib.contextmanager
def cloned_repo(url: str, timeout: int = CLONE_TIMEOUT_SECONDS) -> Iterator[Tuple[Path, str]]:
    """Yield (workspace_path, canonical_url); the workspace is always deleted afterwards."""
    root = Path(tempfile.mkdtemp(prefix="devops-audit-"))
    try:
        canonical = clone_repo(url, root / "repo", timeout=timeout)
        yield root / "repo", canonical
    finally:
        shutil.rmtree(root, onerror=_make_writable_and_retry)
