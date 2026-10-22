# DevOps Repository Auditing Agent

An AI agent that inspects a public GitHub repository, finds DevOps best-practice violations, and writes an evidence-based JSON report with recommendations.

Agent cycle: Observe → Reason → Select Tool → Execute Tool → Interpret Results → Report.

**Status:** work in progress. The CLI, inspection tools and agent loop are not implemented yet, so the audit command below does not run yet.

## Planned usage

```
python agent.py --repo <github_repository_url> --output report.json
```

## Setup

Requires Python 3.10+ and `git`.

```
python -m venv .venv
.venv\Scripts\activate        # Windows; use `source .venv/bin/activate` on Linux/macOS
pip install -r requirements.txt
copy .env.example .env        # use `cp` on Linux/macOS, then fill in the values
```

Configuration is read from `.env` (never committed):

| Variable | Meaning |
|---|---|
| `LLM_BASE_URL` | Base URL of an OpenAI-compatible chat API |
| `LLM_API_KEY` | API key for that provider |
| `LLM_MODEL` | Model name; it must support tool calling |
| `AGENT_MAX_STEPS` | Optional cap on LLM turns (default 15) |
| `AGENT_TIMEOUT_SECONDS` | Optional wall-clock limit for a run |
| `LLM_MIN_INTERVAL_SECONDS` | Optional minimum gap between LLM requests, for providers with strict per-minute quotas (default 0) |

`.env` is read from the current directory, then the project root; real environment variables take precedence. Defaults: 15 steps, 600 seconds. The API key is never printed, and is redacted from error messages.

## What exists so far

- `devops_agent/repo.py`: validates GitHub URLs and clones the target into a throwaway temp folder, with git hooks, submodules, LFS downloads, the file protocol and credential prompts disabled, and a clone timeout. The folder is deleted afterwards.
- `devops_agent/redact.py`: replaces secret values (known token formats, private keys, credential assignments, URL passwords) with `[REDACTED:<type>]` markers. It is meant to run on all tool output, logs and report text. Quoted literals and unquoted secret-looking values are redacted, while code such as `password = get_password(x)` is left readable so evidence is not mangled. Limitation: an unquoted lowercase snake_case secret is indistinguishable from a variable name and is not redacted.
- `devops_agent/config.py`: loads and validates the settings above; a missing required variable fails with a message naming it.
- `devops_agent/llm.py`: thin client for any OpenAI-compatible chat API with tool calling. It sends messages, parses tool calls (tolerating malformed argument JSON), retries rate-limit, timeout, connection and 5xx errors with backoff (waiting as long as the server asks, up to 90 s), and fails fast on authentication or bad-request errors or when the server asks for a much longer wait, such as an exhausted daily quota. Error messages keep enough detail to show which quota was hit. It echoes provider-specific tool-call metadata (such as Gemini thought signatures) back unchanged, as those APIs require. It makes no decisions about which tools to call.
- `devops_agent/agent_loop.py`: the agent loop. The LLM chooses every tool call from the observations so far and ends the audit by calling `submit_report`. The loop executes calls, labels each result `[step N]` so findings can cite their evidence, and returns tool errors as observations so the model can recover. It also handles:
  - plain-text replies (nudged to call a tool, never accepted as a report)
  - malformed arguments
  - duplicate calls
  - at most 8 calls per turn
  - compaction of old tool output
  - a step cap (`AGENT_MAX_STEPS` counts LLM turns) and a wall-clock timeout; when either is reached it asks once for a final report from the evidence gathered, and raises an error rather than inventing one if none comes
- `devops_agent/prompts.py`: the system prompt, where the audit judgement lives: inspection areas, acceptable alternatives (so optional files with equivalents are not flagged), evidence rules, a severity rubric, a rule-id catalogue, and an instruction to batch tool calls to save requests. Tools report facts only; the model decides what is a violation.
- `devops_agent/schema.py`: severity and category vocabulary and the `submit_report` tool definition.
- `devops_agent/trace.py`: redacted record of every assistant note and tool call, for auditing a run.
- `tools.py`: read-only inspection tools bound to the cloned repository, with JSON schemas (`TOOL_SPECS`) for LLM tool calling. Tools return plain-text observations, never raise, and apply redaction and size caps to all output.

  | Tool | Purpose |
  |---|---|
  | `list_files(path, max_depth)` | Repository structure with file sizes; skips `.git` |
  | `read_file(path, start_line, end_line)` | Line-numbered file contents, 400 lines per call; refuses binary files |
  | `search_code(pattern, path_glob, case_insensitive)` | Regex search returning `path:line: text`, max 100 matches |
  | `git_log(max_count, path, show_files)` | Recent commits; notes when history is shallow |
  | `git_status()` | Tracked files matching common generated, temporary or sensitive patterns, and whether a root `.gitignore` exists |
  | `parse_workflow(path)` | Parses a GitHub Actions workflow with `yaml.safe_load` into triggers, permissions, jobs, steps and action pinning, plus factual observations (`continue-on-error`, `\|\| true`, `pull_request_target`, event data interpolated into scripts, secrets referenced, test/lint/build commands found by keyword) |

  Paths are confined to the repository: absolute paths, `..`, `.git` and symlinks are rejected. Tools report facts only; deciding what is a violation is left to the agent.

## Tests

```
python -m pytest -q
```

Tests need no network access and no API key.

## Design constraints

- Python only. The agent loop, tool dispatch and control flow are written from scratch, with no agent or orchestration frameworks.
- The LLM chooses which inspection tools to call, including follow-up calls.
- Static inspection only. Target repository code is never executed and the repository is never modified.
- Secrets are never printed or written to the report.
