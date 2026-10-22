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

import yaml

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
MAX_HISTORY_MATCHES = 40
GIT_TIMEOUT_SECONDS = 20
MAX_READ_CHARS = 12_000
MAX_OUTPUT_CHARS = 16_000
REDACTION_NOTE = (
    "\n[note: [REDACTED:...] marks a literal secret value that is present in the repository and hidden from you. "
    "It is evidence of a hardcoded credential unless the file is a test fixture, an example or a placeholder.]"
)


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


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_TAG_RE = re.compile(r"^v?\d+(\.\d+){0,2}([-.+][\w.]+)?$")
_EVENT_INTERPOLATION_RE = re.compile(r"\$\{\{\s*github\.(?:head_ref|event\.[^}]*)\}\}")
_SECRET_REF_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
_FAIL_SWALLOW_RE = re.compile(r"\|\|\s*(?:true|:)\b|\bexit\s+0\b")
_COMMAND_KINDS = {
    "test": re.compile(r"\b(pytest|unittest|tox|nox|npm (?:run )?test|yarn test|pnpm test|go test|cargo test|mvn(?:w)? .*test|gradle(?:w)? .*test|rspec|phpunit|dotnet test)\b"),
    "lint": re.compile(r"\b(ruff|flake8|pylint|black|isort|mypy|eslint|prettier|golangci-lint|rubocop|pre-commit|shellcheck|hadolint|yamllint)\b"),
    "build": re.compile(r"\b(python -m build|docker build|docker/build-push-action|npm run build|yarn build|cargo build|go build|mvn(?:w)? .*package|gradle(?:w)? .*build|make build|setup\.py (?:sdist|bdist))\b"),
}
_CHECKOUT_WITH_KEYS = ("ref", "repository", "persist-credentials", "fetch-depth", "token")


def _pin_kind(uses: str) -> str:
    if uses.startswith("./"):
        return "local"
    if uses.startswith("docker://"):
        return "docker"
    ref = uses.rsplit("@", 1)[1] if "@" in uses else ""
    if not ref:
        return "no ref"
    if _SHA_RE.match(ref):
        return "commit sha"
    if _TAG_RE.match(ref):
        return "version tag"
    return "branch or other ref"


def _event_summary(name: str, config: Any) -> str:
    if not isinstance(config, dict) or not config:
        return str(name)
    details = ", ".join(f"{k}: {v}" for k, v in config.items())
    return f"{name} ({details})"


def _workflow_triggers(data: Dict[Any, Any]) -> Dict[str, Any]:
    # PyYAML (YAML 1.1) parses the bare key `on` as boolean True.
    raw = data.get("on", data.get(True))
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    return {}


def _one_line(text: Any, limit: int = 200) -> str:
    return " ; ".join(str(text).strip().splitlines())[:limit]


def _index_lines(text: str) -> Dict[tuple, Any]:
    """1-based line numbers of jobs, steps and their keys, taken from the YAML node tree (the data stays untouched)."""
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return {}
    index: Dict[tuple, Any] = {}

    def pairs(node: Any) -> List[tuple]:
        if not isinstance(node, yaml.MappingNode):
            return []
        return [(k.value, k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode)]

    for key, key_node, value_node in pairs(root):
        index[("top", key)] = key_node.start_mark.line + 1
        if key != "jobs":
            continue
        for job_id, job_key, job_node in pairs(value_node):
            index[("job", job_id)] = job_key.start_mark.line + 1
            for name, name_node, node in pairs(job_node):
                index[("job", job_id, name)] = name_node.start_mark.line + 1
                if name != "steps" or not isinstance(node, yaml.SequenceNode):
                    continue
                for number, step_node in enumerate(node.value, start=1):
                    index[("step", job_id, number)] = step_node.start_mark.line + 1
                    for skey, skey_node, svalue in pairs(step_node):
                        index[("step", job_id, number, skey)] = skey_node.start_mark.line + 1
                        if skey == "run" and isinstance(svalue, yaml.ScalarNode):
                            index[("runnode", job_id, number)] = svalue
                        if skey == "with":
                            for wkey, wkey_node, _ in pairs(svalue):
                                index[("step", job_id, number, "with." + wkey)] = wkey_node.start_mark.line + 1
    return index


def _run_line(index: Dict[tuple, Any], job_id: Any, number: int, pattern: "re.Pattern[str]") -> Optional[int]:
    """Line of the first row of a step's run script that matches pattern (the key line for one-line scripts)."""
    node = index.get(("runnode", job_id, number))
    if node is None:
        return index.get(("step", job_id, number, "run"))
    if node.style in ("|", ">"):
        for offset, row in enumerate(str(node.value).splitlines()):
            if pattern.search(row):
                return node.start_mark.line + 2 + offset
    return node.start_mark.line + 1


def summarize_workflow(rel_path: str, data: Dict[Any, Any], index: Optional[Dict[tuple, Any]] = None) -> str:
    """Structured, verdict-free description of a GitHub Actions workflow, with line numbers when available."""
    index = index or {}

    def at(*key: Any) -> str:
        line = index.get(key)
        return f" (line {line})" if isinstance(line, int) else ""

    triggers = _workflow_triggers(data)
    lines = [f"# workflow {rel_path}: {data.get('name', '(unnamed)')}", f"triggers{at('top', 'on')}:"]
    lines += [f"  - {_event_summary(n, c)}" for n, c in triggers.items()] or ["  (none found)"]
    top_permissions = data.get("permissions")
    if "permissions" in data:
        lines.append(f"permissions (workflow level){at('top', 'permissions')}: {top_permissions}")
    else:
        lines.append("permissions (workflow level): not set")
    facts: List[str] = []
    detected: Dict[str, List[str]] = {kind: [] for kind in _COMMAND_KINDS}
    pins: Dict[str, List[str]] = {}
    any_job_permissions = False

    jobs = data.get("jobs")
    lines.append("jobs:")
    if not isinstance(jobs, dict) or not jobs:
        lines.append("  (none found)")
        jobs = {}
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        header = [f"runs-on: {job.get('runs-on')}"] if "runs-on" in job else []
        for key in ("needs", "if", "permissions", "environment", "timeout-minutes", "continue-on-error", "uses", "secrets"):
            if key in job:
                header.append(f"{key}: {_one_line(job[key], 120)}")
        lines.append(f"  {job_id}{at('job', job_id)} [{'; '.join(header)}]")
        any_job_permissions = any_job_permissions or "permissions" in job
        if job.get("continue-on-error") is True:
            facts.append(f"job {job_id} has continue-on-error: true{at('job', job_id, 'continue-on-error')}")
        if job.get("secrets") == "inherit":
            facts.append(
                f"job {job_id} passes all secrets to the called workflow (secrets set to inherit){at('job', job_id, 'secrets')}"
            )
        if "uses" in job:
            pins.setdefault(_pin_kind(str(job["uses"])), []).append(f"{job_id}{at('job', job_id, 'uses')}: {job['uses']}")

        steps = job.get("steps")
        for number, step in enumerate(steps if isinstance(steps, list) else [], start=1):
            if not isinstance(step, dict):
                continue
            where = f"job {job_id} step {number}"
            parts: List[str] = []
            if "name" in step:
                parts.append(_one_line(step["name"], 80))
            if "uses" in step:
                uses = str(step["uses"])
                kind = _pin_kind(uses)
                pins.setdefault(kind, []).append(f"{where}{at('step', job_id, number, 'uses')}: {uses}")
                parts.append(f"uses: {uses} ({kind})")
                with_block = step.get("with") if isinstance(step.get("with"), dict) else {}
                shown = {k: with_block[k] for k in _CHECKOUT_WITH_KEYS if k in with_block}
                if shown:
                    parts.append(f"with: {shown}")
                ref = str(with_block.get("ref", ""))
                if "pull_request_target" in triggers and ("pull_request.head" in ref or "head_ref" in ref):
                    facts.append(
                        f"{where} checks out the pull request head under a pull_request_target trigger"
                        f"{at('step', job_id, number, 'with.ref')}"
                    )
            run = step.get("run")
            if run is not None:
                parts.append(f"run: {_one_line(run)}")
                run_text = str(run)
                if _EVENT_INTERPOLATION_RE.search(run_text):
                    line = _run_line(index, job_id, number, _EVENT_INTERPOLATION_RE)
                    facts.append(
                        f"{where} interpolates ${{{{ github.event... }}}} or head_ref directly into a run script"
                        f"{f' (line {line})' if line else ''}"
                    )
                if _FAIL_SWALLOW_RE.search(run_text):
                    line = _run_line(index, job_id, number, _FAIL_SWALLOW_RE)
                    facts.append(
                        f"{where} run script contains `|| true` or `exit 0`: {_one_line(run_text, 100)}"
                        f"{f' (line {line})' if line else ''}"
                    )
                for kind, pattern in _COMMAND_KINDS.items():
                    found = pattern.search(run_text)
                    if found:
                        detected[kind].append(f"{where}: {found.group(0)}")
            for key in ("if", "continue-on-error"):
                if key in step:
                    parts.append(f"{key}: {_one_line(step[key], 100)}")
            if step.get("continue-on-error") is True:
                facts.append(f"{where} has continue-on-error: true{at('step', job_id, number, 'continue-on-error')}")
            lines.append(f"    step {number}{at('step', job_id, number)}: " + " | ".join(parts))

    if "pull_request_target" in triggers:
        facts.append(f"workflow uses the pull_request_target trigger{at('top', 'on')}")
    if "permissions" not in data and not any_job_permissions:
        facts.append("no explicit permissions set at workflow or job level (repository default token permissions apply)")
    for kind in ("branch or other ref", "no ref"):
        if pins.get(kind):
            facts.append(f"actions not pinned to a tag or sha ({kind}): " + "; ".join(pins[kind][:10]))
    if pins.get("version tag"):
        facts.append(f"{len(pins['version tag'])} action reference(s) pinned to a version tag, "
                     f"{len(pins.get('commit sha', []))} to a commit sha")
    secret_names = sorted(set(_SECRET_REF_RE.findall(yaml.safe_dump(data, default_flow_style=False))))
    if secret_names:
        facts.append("secrets referenced: " + ", ".join(secret_names))
    for kind, hits in detected.items():
        found = "; ".join(hits[:8]) if hits else "none (they may run via make, scripts or other tools; read those before concluding)"
        facts.append(f"{kind} commands found by keyword match in run steps: {found}")

    lines.append("facts:")
    lines += [f"  - {fact}" for fact in facts] or ["  (none)"]
    return "\n".join(lines)


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
            "parse_workflow": self.parse_workflow,
            "search_history": self.search_history,
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
        if "[REDACTED:" in result:
            result += REDACTION_NOTE
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

    def overview(self) -> str:
        """Short orientation for the first message: file count, common extensions, top-level entries.

        This is not an LLM-callable tool; it just saves the agent a wasted opening call.
        """
        counts: Dict[str, int] = {}
        total = 0
        for file_path, _ in self._walk_files(self.root):
            total += 1
            key = file_path.suffix.lower() or file_path.name
            counts[key] = counts.get(key, 0) + 1
        common = ", ".join(f"{k} ({v})" for k, v in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:8])
        top = sorted(
            f"{p.name}/" if p.is_dir() else p.name for p in self.root.iterdir() if p.name != ".git" and not p.is_symlink()
        )
        text = f"{total} file(s). Most common: {common or 'none'}.\nTop level: {', '.join(top[:40]) or '(empty)'}"
        try:
            commits = int(self._git(["rev-list", "--count", "HEAD"]).strip())
        except (ToolError, ValueError):
            commits = 0
        if commits:
            text += f"\nGit history: {commits} commit(s) available{' (shallow clone)' if self._shallow_note() else ''}."
        return text

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
        rendered: List[str] = []
        used = 0
        for i, line in enumerate(all_lines[start - 1 : end]):
            row = f"{start + i:>5}| {line[:MAX_LINE_CHARS]}{'...' if len(line) > MAX_LINE_CHARS else ''}"
            if rendered and used + len(row) > MAX_READ_CHARS:
                break
            rendered.append(row)
            used += len(row) + 1
        body = "\n".join(rendered)
        last = start + len(rendered) - 1
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
        out = self._git_history(cmd)
        if not out.strip():
            return "(no commits yet)"
        return f"# last {count} commit(s) max\n{out.strip()}{self._shallow_note()}"

    def search_history(self, pattern: str, path: Optional[str] = None, max_commits: int = 50) -> str:
        """Search commit diffs for a regex: lines added (+) or removed (-), including secrets deleted from source later."""
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern must be a non-empty string")
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ToolError(f"pattern is longer than {MAX_PATTERN_CHARS} characters")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"invalid regular expression: {exc}") from None
        count = min(max(_to_int(max_commits, "max_commits"), 1), MAX_GIT_LOG_COUNT)
        base = ["--no-pager", "log", f"--max-count={count}", "-p", "-U0", "--no-color", "--no-ext-diff",
                "--no-textconv", "--date=short", "--format=@@@ %h %ad"]
        tail = ["--", self._relative(self._resolve(path))] if path else []
        try:  # git's own -G prefilter (POSIX regex) keeps the output small
            out = self._git_history([*base[:3], f"-G{pattern}", *base[3:], *tail])
        except ToolError as exc:
            if "egular expression" not in str(exc):
                raise
            out = ""
        if not out.strip():
            # Python syntax such as \d is not POSIX, so the prefilter can miss: scan unfiltered and match in Python.
            out = self._git_history([*base, *tail])
        matches: List[str] = []
        header, current_file, in_hunk = "", "", False
        for line in out.splitlines():
            if line.startswith("@@@ "):
                header, current_file, in_hunk = line[4:], "", False
            elif line.startswith("diff --git "):
                current_file, in_hunk = line.rsplit(" b/", 1)[-1], False
            elif line.startswith("@@ "):
                in_hunk = True
            elif in_hunk and line[:1] in ("+", "-") and regex.search(line[1:]):
                matches.append(f"{header} {current_file}: {line[0]}{line[1:].strip()[:200]}")
        shown = matches[:MAX_HISTORY_MATCHES]
        head = (
            f"# {len(matches)} match(es) in history for /{pattern}/, newest commit first. "
            "'-' lines were removed by that commit, so they exist in older history; '+' lines were added."
        )
        if len(matches) > len(shown):
            head += f" Showing the first {len(shown)}."
        if not out.strip():
            return "(no commits yet)"
        return head + ("\n" + "\n".join(shown) if shown else "") + self._shallow_note()

    def _shallow_note(self) -> str:
        if (self.root / ".git" / "shallow").exists():
            return "\n[history is shallow: only recent commits are available]"
        return ""

    def _git_history(self, args: List[str]) -> str:
        """Run a git history command; a repository with no commits yet gives empty output, not an error."""
        try:
            return self._git(args)
        except ToolError as exc:
            if "does not have any commits" in str(exc):
                return ""
            raise

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

    def parse_workflow(self, path: str) -> str:
        """Parse a GitHub Actions workflow YAML into triggers, permissions, jobs, steps and factual observations."""
        target = self._resolve(path)
        if not target.exists():
            raise ToolError(f"no such file: {path!r}. Workflows live in .github/workflows/; use list_files.")
        if not target.is_file():
            raise ToolError(f"{path!r} is a directory; pass a workflow file")
        if target.stat().st_size > MAX_READ_BYTES:
            raise ToolError(f"{path!r} is larger than {MAX_READ_BYTES} bytes and was not parsed")
        text = target.read_text(encoding="utf-8", errors="replace")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            problem = getattr(exc, "problem", None) or "invalid YAML"
            mark = getattr(exc, "problem_mark", None)
            where = f" at line {mark.line + 1}" if mark else ""
            raise ToolError(f"{path!r} is not valid YAML: {problem}{where}") from None
        if not isinstance(data, dict):
            raise ToolError(f"{path!r} does not contain a YAML mapping, so it is not a workflow")
        return summarize_workflow(self._relative(target), data, _index_lines(text))

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
            "name": "parse_workflow",
            "description": "Parse a GitHub Actions workflow file (.github/workflows/*.yml) into triggers, permissions, jobs, steps, action pinning and factual observations (e.g. continue-on-error, `|| true`, pull_request_target, event data interpolated into scripts). Facts only; you decide what is a violation. Use read_file for exact line numbers.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Workflow file path relative to the repo root."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_history",
            "description": "Search git history (commit diffs) for a regex, including lines that were later removed, for example a secret deleted from source but still present in older commits. Returns commit, date, file and the matching added (+) or removed (-) line. Secret values are redacted. History may be shallow.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression matched against changed lines."},
                    "path": {"type": "string", "description": "Only look at changes to this path."},
                    "max_commits": {"type": "integer", "description": "Commits to scan, 1-100, default 50."},
                },
                "required": ["pattern"],
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
