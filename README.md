# DevOps Repository Auditing Agent

An AI agent that inspects a public GitHub repository, finds DevOps best-practice violations, and writes an evidence-based JSON report with recommendations.

Agent cycle: Observe → Reason → Select Tool → Execute Tool → Interpret Results → Report.

**Status:** the agent is implemented end to end. It has been exercised against a live model only partially so far (see Limitations), and `sample_report.json` has not been generated yet.

## Usage

```
python agent.py --repo https://github.com/<owner>/<name> --output report.json
```

Optional flags: `--max-steps N` (maximum LLM turns), `--timeout SECONDS`, `--trace trace.json` (write the redacted tool-call trace, also on failed runs), `--verbose` (print each tool call to stderr as it happens). The command never prompts and never reads stdin. Progress goes to stderr; the result is the JSON file.

Output format: `{"repository": "...", "violations": [...]}`. Each violation has `rule_id`, `category`, `severity` (`critical`, `high`, `medium`, `low`), `file`, `line` (both may be `null`), `description`, `evidence` and `recommendation`. An empty `violations` array means nothing defensible was found. Categories: `hygiene`, `dependencies`, `code_quality`, `testing`, `ci`, `security`, `configuration`, `collaboration`, `build`.

| Exit code | Meaning |
|---|---|
| 0 | Report written (possibly with no violations) |
| 2 | Invalid arguments, repository URL, output path or configuration |
| 3 | Clone failed (not found, private, network, timeout) |
| 4 | LLM failure (unreachable, rejected, quota exhausted), or the agent could not produce a report. No report file is written and any existing one is left untouched |
| 1 | Unexpected internal error |
| 130 | Interrupted |

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
| `LLM_CONTEXT_CHARS` | Optional budget for tool output kept in the conversation (default 80000). Older output is replaced by one-line stubs and each result is capped to a quarter of the budget. Use about 10000 for providers limited to a few thousand tokens per minute |

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
- `devops_agent/report.py`: turns the model's submitted violations into the final JSON. It checks them against what the agent actually did:
  - every violation must cite `[step N]` tool results that were really executed, or it is dropped
  - the file must exist in the repository, otherwise the location is cleared to `null`
  - the line must be inside the file, otherwise it is cleared to `null`
  - category and severity are normalised to the allowed values
  - secrets are redacted and long text is truncated
  - duplicates are removed
  - the report is sorted by severity and written atomically

  Every change or drop is returned as a note so it can be logged; nothing is silently invented.
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

## Limitations

- Static inspection only. Target code is never executed, so failing tests cannot be detected; `run_pytest` and `run_ruff` are intentionally not implemented.
- Remote repository settings (branch protection, required reviews, secret scanning) are not visible from the files and are never inferred.
- Results depend on the model's tool-calling quality. Free API tiers can have very small daily quotas (for example 20 requests per day per model on one provider), which a full audit can exhaust; the agent then exits with code 4 instead of guessing. Set `LLM_MIN_INTERVAL_SECONDS` for per-minute limits.
- Clones are shallow (last 100 commits), so `git_log` may not show older history.

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
