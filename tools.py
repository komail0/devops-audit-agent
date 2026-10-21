"""Read-only inspection tools the audit agent can call.

Tools return observations as plain text and never raise: problems are
returned as "ERROR: ..." strings so the LLM can adapt its next step. Tools
only report facts; judging whether something is a violation is the agent's
job. All output is redacted and size-capped before it leaves this module.
"""

import fnmatch
import inspect
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional

from devops_agent.redact import redact

MAX_LIST_ENTRIES = 2000
MAX_READ_BYTES = 200_000
MAX_READ_LINES = 400
MAX_LINE_CHARS = 500
MAX_SEARCH_MATCHES = 100
MAX_SEARCH_FILE_BYTES = 1_000_000
MAX_PATTERN_CHARS = 300
SEARCH_TIME_BUDGET_SECONDS = 10
MAX_GIT_LOG_COUNT = 100
GIT_TIMEOUT_SECONDS = 20
MAX_OUTPUT_CHARS = 30_000


class ToolError(Exception):
    """A problem with a tool call that is reported back to the agent."""


def _is_binary(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return b"\0" in handle.read(8000)
    except OSError:
        return True


def _to_int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be an integer, got {value!r}") from None


_ENV_TEMPLATE_SUFFIXES = {"example", "sample", "template", "dist", "defaults", "default"}
_TOOL_CACHES = {".pytest_cache", ".ruff_cache", ".mypy_cache", ".tox", ".nox", ".hypothesis"}


def _suspicious_reason(path: str) -> Optional[str]:
    """Why a tracked path is commonly something that should not be committed."""
    parts = PurePosixPath(path).parts
    name = parts[-1]
    suffix = PurePosixPath(name).suffix.lower()
    if "__pycache__" in parts or suffix in {".pyc", ".pyo"}:
        return "Python bytecode cache"
    if "node_modules" in parts:
        return "installed dependencies"
    if name in {".DS_Store", "Thumbs.db"}:
        return "OS metadata file"
    if name == ".env" or (name.startswith(".env.") and name.rsplit(".", 1)[-1] not in _ENV_TEMPLATE_SUFFIXES):
        return "environment file (may hold secrets)"
    if suffix in {".log", ".tmp", ".swp", ".bak"}:
        return "temporary or log file"
    if any(p in {".idea", ".vscode"} for p in parts[:-1]):
        return "local editor settings (sometimes intentionally shared)"
    if any(p.endswith(".egg-info") for p in parts) or (parts[0] in {"dist", "build"} and len(parts) > 1):
        return "possible build output"
    if any(p in {".venv", "venv"} for p in parts[:-1]):
        return "virtual environment"
    if any(p in _TOOL_CACHES for p in parts) or name == ".coverage":
        return "tool cache or coverage data"
    if suffix in {".pem", ".p12", ".pfx"} or name in {"id_rsa", "id_ed25519"}:
        return "private key or certificate file (inspect before concluding)"
    return None


class Toolbox:
    """Inspection tools bound to one cloned repository directory."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self._registry: Dict[str, Callable[..., str]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_code": self.search_code,
            "git_log": self.git_log,
            "git_status": self.git_status,
        }

    # ---- dispatch ---------------------------------------------------------

    def execute(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> str:
        """Run a tool by name; always returns a redacted, size-capped string."""
        arguments = arguments or {}
        func = self._registry.get(name)
        if func is None:
            return f"ERROR: unknown tool {name!r}. Available tools: {', '.join(sorted(self._registry))}"
        if not isinstance(arguments, dict):
            return "ERROR: tool arguments must be a JSON object"
        allowed = set(inspect.signature(func).parameters)
        unknown = set(arguments) - allowed
        if unknown:
            return f"ERROR: unexpected argument(s) {sorted(unknown)} for {name}; accepted: {sorted(allowed)}"
        try:
            result = func(**arguments)
        except ToolError as exc:
            result = f"ERROR: {exc}"
        except TypeError as exc:
            result = f"ERROR: invalid arguments for {name}: {exc}"
        except Exception as exc:  # a tool bug must not kill the agent loop
            result = f"ERROR: {name} failed unexpectedly: {type(exc).__name__}"
        result = redact(result)
        if len(result) > MAX_OUTPUT_CHARS:
            result = result[:MAX_OUTPUT_CHARS] + "\n[output truncated]"
        return result

    # ---- path safety ------------------------------------------------------

    def _resolve(self, rel: Any) -> Path:
        if not isinstance(rel, str):
            raise ToolError("path must be a string")
        rel = rel.strip().replace("\\", "/")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
            raise ToolError("absolute paths are not allowed; use a path relative to the repository root")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if ".." in parts:
            raise ToolError("'..' is not allowed in paths")
        if ".git" in parts:
            raise ToolError("the .git directory is not accessible through this tool; use git_log or git_status")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise ToolError(f"symbolic links are not followed: {part}")
        resolved = current.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError:
            raise ToolError("path escapes the repository") from None
        return resolved

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _walk_files(self, start: Path):
        """Yield (file_path, depth) in sorted order, skipping .git and symlinks."""
        base_depth = len(start.relative_to(self.root).parts)
        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d != ".git" and not (Path(dirpath) / d).is_symlink())
            depth = len(Path(dirpath).relative_to(self.root).parts) - base_depth + 1
            for filename in sorted(filenames):
                file_path = Path(dirpath) / filename
                if not file_path.is_symlink():
                    yield file_path, depth

    # ---- tools ------------------------------------------------------------

    def list_files(self, path: str = "", max_depth: Optional[int] = None) -> str:
        """List files (with sizes) under a directory, relative to the repository root."""
        start = self._resolve(path)
        if not start.exists():
            raise ToolError(f"no such directory: {path!r}")
        if not start.is_dir():
            raise ToolError(f"{path!r} is a file; use read_file")
        limit_depth = _to_int(max_depth, "max_depth") if max_depth is not None else None
        lines: List[str] = []
        total = 0
        for file_path, depth in self._walk_files(start):
            if limit_depth is not None and depth > limit_depth:
                continue
            total += 1
            if len(lines) < MAX_LIST_ENTRIES:
                try:
                    size = file_path.stat().st_size
                except OSError:
                    size = -1
                note = "  [large]" if size > MAX_READ_BYTES else ""
                lines.append(f"{self._relative(file_path)}  ({size} bytes){note}")
        header = f"# {total} file(s) under {path or '.'}"
        if total > MAX_LIST_ENTRIES:
            header += f" (showing first {MAX_LIST_ENTRIES}; narrow with path or max_depth)"
        return header + ("\n" + "\n".join(lines) if lines else "\n(no files)")

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> str:
        """Read a text file with line numbers (at most 400 lines per call)."""
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such file: {path!r}. Use list_files to see what exists.")
        if target.is_dir():
            raise ToolError(f"{path!r} is a directory; use list_files")
        if _is_binary(target):
            raise ToolError(f"{path!r} looks like a binary file and was not read")
        start = _to_int(start_line, "start_line")
        if start < 1:
            raise ToolError("start_line must be >= 1")
        end = _to_int(end_line, "end_line") if end_line is not None else start + MAX_READ_LINES - 1
        if end < start:
            raise ToolError("end_line must be >= start_line")
        end = min(end, start + MAX_READ_LINES - 1)

        with open(target, "rb") as handle:
            raw = handle.read(MAX_READ_BYTES + 1)
        byte_truncated = len(raw) > MAX_READ_BYTES
        text = raw[:MAX_READ_BYTES].decode("utf-8", errors="replace")
        all_lines = text.splitlines()
        if start > len(all_lines):
            raise ToolError(f"start_line {start} is past the end of the file ({len(all_lines)} lines)")
        chunk = all_lines[start - 1 : end]
        body = "\n".join(
            f"{start + i:>5}| {line[:MAX_LINE_CHARS]}{'...' if len(line) > MAX_LINE_CHARS else ''}"
            for i, line in enumerate(chunk)
        )
        last = start + len(chunk) - 1
        total_note = f"{len(all_lines)}+" if byte_truncated else str(len(all_lines))
        out = f"# {self._relative(target)} (lines {start}-{last} of {total_note})\n{body}"
        if last < len(all_lines):
            out += f"\n[more lines: call read_file with start_line={last + 1}]"
        elif byte_truncated:
            out += f"\n[file truncated at {MAX_READ_BYTES} bytes]"
        return out

    def search_code(self, pattern: str, path_glob: Optional[str] = None, case_insensitive: bool = False) -> str:
        """Regex search over text files; returns path:line: text (max 100 matches)."""
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern must be a non-empty string")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ToolError(f"pattern is longer than {MAX_PATTERN_CHARS} characters")
        try:
            regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from None

        deadline = time.monotonic() + SEARCH_TIME_BUDGET_SECONDS
        matches: List[str] = []
        scanned = 0
        stopped = ""
        for file_path, _ in self._walk_files(self.root):
            rel = self._relative(file_path)
            if path_glob and not (fnmatch.fnmatch(rel, path_glob) or fnmatch.fnmatch(file_path.name, path_glob)):
                continue
            try:
                if file_path.stat().st_size > MAX_SEARCH_FILE_BYTES or _is_binary(file_path):
                    continue
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1
            for number, line in enumerate(content.splitlines(), start=1):
                if regex.search(line[:2000]):
                    snippet = line.strip()[:300]
                    matches.append(f"{rel}:{number}: {snippet}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        stopped = f"stopped after {MAX_SEARCH_MATCHES} matches; refine the pattern or path_glob"
                        break
            if stopped:
                break
            if time.monotonic() > deadline:
                stopped = f"stopped after {SEARCH_TIME_BUDGET_SECONDS}s time budget; results are incomplete"
                break
        header = f"# {len(matches)} match(es) for /{pattern}/ in {scanned} file(s)"
        if stopped:
            header += f" ({stopped})"
        return header + ("\n" + "\n".join(matches) if matches else "")

    def git_log(self, max_count: int = 30, path: Optional[str] = None, show_files: bool = False) -> str:
        """Recent commits (hash, author, date, subject); optionally restricted to a path or with changed files."""
        count = min(max(_to_int(max_count, "max_count"), 1), MAX_GIT_LOG_COUNT)
        cmd = ["--no-pager", "log", f"--max-count={count}", "--date=short", "--format=%h | %an | %ad | %s"]
        if show_files:
            cmd.append("--name-only")
        if path:
            cmd += ["--", self._relative(self._resolve(path))]
        out = self._git(cmd)
        if not out.strip():
            return "(no commits found)"
        note = ""
        if (self.root / ".git" / "shallow").exists():
            note = "\n[history is shallow: only recent commits are available]"
        return f"# last {count} commit(s) max\n{out.strip()}{note}"

    def git_status(self) -> str:
        """Tracked files that commonly should not be committed (observations only, judge by context)."""
        out = self._git(["--no-pager", "ls-files"])
        tracked = [line for line in out.splitlines() if line]
        flagged = []
        for item in tracked:
            reason = _suspicious_reason(item)
            if reason:
                flagged.append(f"{item}  <- {reason}")
        has_gitignore = (self.root / ".gitignore").is_file()
        lines = [
            f"# {len(tracked)} tracked file(s); .gitignore at repository root: {'present' if has_gitignore else 'absent'}",
            f"# {len(flagged)} tracked file(s) matching common generated/temporary/sensitive patterns:",
        ]
        lines += flagged[:200] or ["(none)"]
        if len(flagged) > 200:
            lines.append(f"[{len(flagged) - 200} more not shown]")
        return "\n".join(lines)

    def _git(self, args: List[str]) -> str:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=GIT_TIMEOUT_SECONDS,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise ToolError("git command timed out") from None
        except FileNotFoundError:
            raise ToolError("git executable not found") from None
        if result.returncode != 0:
            raise ToolError(f"git failed: {result.stderr.strip().splitlines()[0] if result.stderr.strip() else 'unknown error'}")
        return result.stdout


TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files in the repository with sizes. Start here to learn the structure. Skips .git.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory relative to the repo root (default: root)."},
                    "max_depth": {"type": "integer", "description": "Only list files at most this many levels below path."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file with line numbers (max 400 lines per call). Use start_line/end_line to page. Use the line numbers when citing evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the repo root."},
                    "start_line": {"type": "integer"},
                    "end_line": {"type": "integer"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "Regex search across text files. Returns path:line: text (max 100 matches). Secret values in results are redacted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path_glob": {"type": "string", "description": "Optional glob such as '*.yml' or '.github/**'."},
                    "case_insensitive": {"type": "boolean"},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Recent commit history (hash, author, date, subject). History may be shallow. show_files lists changed files per commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {"type": "integer", "description": "1-100, default 30."},
                    "path": {"type": "string", "description": "Only commits touching this path."},
                    "show_files": {"type": "boolean"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": "Tracked files that match common generated, temporary or sensitive patterns, plus whether a root .gitignore exists. Observations only; judge by context.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]
