# DevOps Repository Auditing Agent

An AI agent that inspects a public GitHub repository, finds DevOps best-practice violations, and writes an evidence-based JSON report with recommendations.

Agent cycle: Observe → Reason → Select Tool → Execute Tool → Interpret Results → Report.

The agent loop, tool dispatch and control flow are plain Python written for this project; no agent or orchestration framework is used. The only third-party packages are the `openai` SDK (used as a generic client for any OpenAI-compatible chat API), `PyYAML` (to parse GitHub Actions workflows) and `pytest`.

## Quick start

Requires Python 3.10+ and `git`.

```
git clone https://github.com/komail0/devops-audit-agent.git
cd devops-audit-agent
python -m venv .venv
.venv\Scripts\activate          # Windows. On Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env          # On Linux/macOS: cp .env.example .env
```

Edit `.env` and set `LLM_API_KEY` (with an OpenAI, Gemini, Groq or Anthropic key, that is all you need; see Configuration), then run:

```
python agent.py --repo https://github.com/<owner>/<name> --output report.json
```

### Configuration

Settings come from `.env` (never committed) or real environment variables, which take precedence. `.env` is read from the current directory, then the project root.

| Variable | Required | Meaning |
|---|---|---|
| `LLM_API_KEY` | yes | API key for the provider. `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GROQ_API_KEY` and `ANTHROPIC_API_KEY` are accepted as alternatives |
| `LLM_BASE_URL` | only for other providers | Base URL of an OpenAI-compatible chat completions API. Inferred from the key (prefix `sk-`, `sk-ant-`, `AIza`/`AQ.`, `gsk_`) or the variable name when omitted |
| `LLM_MODEL` | only for other providers | Model name. It must support tool (function) calling. A default is used for OpenAI, Gemini, Groq and Anthropic when omitted; model names change, so set it if the default is rejected |
| `AGENT_MAX_STEPS` | no | Maximum LLM turns (default 15) |
| `AGENT_TIMEOUT_SECONDS` | no | Wall-clock limit for the investigation (default 600) |
| `LLM_MIN_INTERVAL_SECONDS` | no | Minimum gap between LLM requests, for providers with strict per-minute quotas (default 0) |
| `LLM_CONTEXT_CHARS` | no | Budget for tool output kept in the conversation (default 80000). Older output is replaced by one-line stubs and each result is capped to a quarter of the budget. Use about 10000 for providers limited to a few thousand tokens per minute |

Example `.env` values (any provider with an OpenAI-compatible endpoint and tool calling works):

| Provider | `LLM_BASE_URL` | Example `LLM_MODEL` |
|---|---|---|
| Google Gemini | `https://generativelanguage.googleapis.com/v1beta/openai/` | a Flash-class Gemini model |
| Groq | `https://api.groq.com/openai/v1` | `openai/gpt-oss-120b` |
| OpenAI | `https://api.openai.com/v1` | any tool-calling model |

Model availability and free-tier limits change; check your provider's current list. Free tiers can have small quotas, see Limitations.

## Usage

```
python agent.py --repo <github_repository_url> --output <report.json> [options]
```

| Option | Meaning |
|---|---|
| `--max-steps N` | Maximum LLM turns (overrides `AGENT_MAX_STEPS`) |
| `--timeout SECONDS` | Investigation time limit (overrides `AGENT_TIMEOUT_SECONDS`) |
| `--trace FILE` | Also write the redacted tool-call trace as JSON (written on failed runs too) |
| `--verbose` | Print each tool call to stderr as it happens |

The command never prompts and never reads stdin. Progress goes to stderr; the result is the JSON file.

### Output

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

`severity` is one of `critical`, `high`, `medium`, `low`. `file` and `line` are `null` for repository-wide findings. `category` is one of `hygiene`, `dependencies`, `code_quality`, `testing`, `ci`, `security`, `configuration`, `collaboration`, `build`. An empty `violations` array means nothing defensible was found.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Report written (possibly with no violations) |
| 2 | Invalid arguments, repository URL, output path or configuration |
| 3 | Clone failed (not found, private, network, timeout) |
| 4 | LLM failure (unreachable, rejected, quota exhausted), or the agent could not produce a report. No report file is written and an existing one is left untouched |
| 1 | Unexpected internal error |
| 130 | Interrupted |

## How it works

```
agent.py (CLI)
   │  validate URL, load config, check output path
   ▼
repo.py ── shallow, hook-free clone into a temp folder (deleted afterwards)
   ▼
agent_loop.py ◄──────────────► llm.py ── OpenAI-compatible chat API (tool calling)
   │   the LLM picks a tool call from what it has seen so far          system prompt = prompts.py
   ▼
tools.py ── list_files · read_file · search_code · git_log · git_status · parse_workflow
   │   read-only, path-confined, redacted, size-capped; return facts, never verdicts
   ▼
LLM calls submit_report(violations) with [step N] evidence references
   ▼
report.py ── check evidence, file and line against what really happened; redact; de-duplicate
   ▼
report.json (written atomically)            trace.py ── redacted record of every call (--trace)
```

1. **Observe.** The first message gives the repository URL and a short overview (file count, common extensions, top-level entries).
2. **Reason and select a tool.** The LLM decides what to inspect next. Nothing is scripted: it reads manifests, parses workflows, searches for risky patterns, and follows up on what it finds. Several independent calls can be batched in one turn.
3. **Execute and interpret.** Each result comes back labelled `[step N]`. Tool errors are returned as text so the model can recover. The model interprets the results and decides whether to dig further.
4. **Report.** The model ends the audit by calling `submit_report`, citing the step numbers that support each finding.
5. **Validate.** `report.py` drops findings that cite no executed tool call, clears file or line values that do not exist, normalises enums and rule ids, redacts secrets, removes duplicates and writes the JSON atomically.

Audit judgement (what counts as a violation, acceptable alternatives, severity) lives in the system prompt, not in code: for example `pyproject.toml` or a fully pinned `requirements.txt` are accepted as dependency specifications, linting may come from any tool or CI, and branch protection is never inferred from repository files.

The six stages are separate, labelled functions or blocks in `devops_agent/agent_loop.py`: `_observe` (opening overview), the model call (reason and select tool), `_execute` (dispatch to the toolbox), `_interpret` (label and bound the observation) and the report step (`submit_report`, or `_forced_report` when a step or time budget is exhausted). The loop also keeps state across iterations: the step counter, the executed calls, duplicate-call detection and the model's candidate notes.

### How severity is chosen

The model assigns severity, guided by a rubric in the system prompt:

| Severity | Meaning | Typical examples |
|---|---|---|
| `critical` | Live credential or private key exposed, or a path to code execution or secret theft | `pull_request_target` running untrusted PR code, a live cloud key in source or history |
| `high` | A security or CI defect that defeats protection | A hardcoded credential, checks that can never fail (`\|\| true`, `continue-on-error`), script injection, deploy with no gate |
| `medium` | Reliability or reproducibility defects | Unpinned actions, tests not run in CI, machine-specific paths, inconsistent dependency specs |
| `low` | Hygiene and documentation gaps | Missing `.gitignore`, tracked cache files, missing review guidance |

Each rule id in the prompt's catalogue carries a default severity (for example `SEC-004` critical, `CI-002` high, `HYG-003` low) that the model keeps unless the evidence clearly justifies raising or lowering it. `report.py` then checks that the value is one of the four allowed levels.

### Safety

- The target is cloned shallowly into a temp folder with git hooks, submodules, LFS downloads, the file protocol and credential prompts disabled, then deleted. Nothing from the repository is executed or imported, and the repository is never modified.
- Tools cannot leave the repository: absolute paths, `..`, `.git` and symlinks are rejected. Workflow YAML is parsed with `yaml.safe_load`.
- All tool output, trace entries, logs and report text pass through a redactor. Secret values are replaced by `[REDACTED:<type>]`; the agent reports location and type only. The configured API key is also scrubbed from error messages.
- If a check cannot be made, nothing is invented: a failed run exits non-zero without writing a report.

## Modules

| File | Role |
|---|---|
| `agent.py` | CLI entry point, exit codes |
| `tools.py` | The inspection tools and their JSON schemas (`TOOL_SPECS`) |
| `devops_agent/agent_loop.py` | The agent loop: dispatch, duplicate detection, context budget, step and time limits, forced final report |
| `devops_agent/llm.py` | OpenAI-compatible client: tool-call parsing, retries that honour the server's retry hints, request spacing, provider metadata (such as Gemini thought signatures) echoed back |
| `devops_agent/prompts.py` | The system prompt: inspection areas, acceptable alternatives, evidence rules, severity rubric, rule-id catalogue |
| `devops_agent/report.py` | Validation, normalisation, de-duplication and atomic writing of the report |
| `devops_agent/repo.py` | URL validation and safe cloning |
| `devops_agent/redact.py` | Secret redaction |
| `devops_agent/config.py` | Settings loading and validation |
| `devops_agent/schema.py` | Severity and category vocabulary, the `submit_report` tool definition |
| `devops_agent/trace.py` | Redacted tool-call trace |

### Tools

| Tool | Purpose |
|---|---|
| `list_files(path, max_depth)` | Repository structure with file sizes; skips `.git` |
| `read_file(path, start_line, end_line)` | Line-numbered file contents, 400 lines or 12,000 characters per call; refuses binary files |
| `search_code(pattern, path_glob, case_insensitive)` | Regex search returning `path:line: text`, max 100 matches |
| `git_log(max_count, path, show_files)` | Recent commits; notes when history is shallow |
| `git_status()` | Tracked files matching common generated, temporary or sensitive patterns, and whether a root `.gitignore` exists |
| `search_history(pattern, path, max_commits)` | Regex search over commit diffs, showing added (`+`) and removed (`-`) lines with commit, date and file. Finds secrets that were deleted from the source but remain in older commits; values are redacted |
| `parse_workflow(path)` | A GitHub Actions workflow as triggers, permissions, jobs, steps and action pinning, with line numbers (including the exact row inside a multi-line `run:` block), plus factual observations (`continue-on-error`, `\|\| true`, `pull_request_target`, event data interpolated into scripts, secrets referenced, test/lint/build commands found by keyword) |
| `note_finding(summary, file, step)` | Records a candidate finding in the loop's own state (handled by the loop). Notes survive context compaction and are replayed if a budget stops the run |
| `submit_report(violations)` | Ends the audit with the findings (handled by the loop) |

`run_pytest` and `run_ruff` are intentionally not implemented: running a target's tests means executing untrusted code.

## Tests

```
python -m pytest -q
```

About 350 tests; none needs network access or an API key. The agent loop is tested with a scripted stand-in for the LLM, including a test that shows the next tool call depends on the previous observation. `.github/workflows/tests.yml` runs the suite on every push and pull request.

## Sample output

`sample_report.json` is the unedited result of a real run:

```
python agent.py --repo https://github.com/adeyosemanputra/pygoat --output sample_report.json
```

with `gemini-3.1-flash-lite`. `pygoat` is a deliberately vulnerable Django training app; the report lists its hardcoded `SECRET_KEY` and `DEBUG = True`. Output varies between runs and models, so a different run may report more or fewer findings.

## Validation

Results from live runs. They are single runs, so treat them as indicative: output varies between runs and models.

| Target | Model | Result |
|---|---|---|
| `psf/requests` (well-maintained) | `gemini-3.5-flash-lite` | 0 findings in every run (expected: none) |
| `adeyosemanputra/pygoat` (vulnerable teaching app) | `gemini-3.1-flash-lite` | Hardcoded `SECRET_KEY` and `DEBUG = True`, both with correct lines. An early run also reported "no lockfile" for a fully pinned `requirements.txt`, a false positive that led to a prompt change |
| A purpose-built test repository with 13 injected defects (tracked `.env` and bytecode, no `.gitignore`, hardcoded password, machine-specific path, CI triggered only on `dev`, `\|\| true` and `continue-on-error` gates, `write-all` permissions, `@main` action, `pull_request_target` with PR checkout, script injection, `curl \| bash` deploy) plus decoys that must not be flagged (`pyproject.toml` instead of `requirements.txt`, ruff configured, tests present) | `gemini-3.5-flash-lite` | Typically 12 of 13 defects, each run missing a different one; no findings on the decoys; severities spread from critical to low |

Smaller models were noticeably less consistent: on `pygoat`, the smallest model tried missed the hardcoded secret in 3 of 3 runs.

## Limitations

- Static inspection only. Target code is never executed, so failing tests cannot be detected.
- Remote repository settings (branch protection, required reviews, secret scanning) are not visible from the files and are never inferred.
- Results depend on the model's tool-calling quality and can vary between runs. Small models may miss findings.
- Free API tiers can have very small quotas (for example 20 requests per day per model on one provider, or a few thousand tokens per minute on another). When a quota is exhausted the agent exits with code 4 instead of guessing. Use `LLM_MIN_INTERVAL_SECONDS` and `LLM_CONTEXT_CHARS` for per-minute limits.
- Clones are shallow (last 100 commits), so `git_log` may not show older history.
- Redaction is pattern-based. An unquoted lowercase snake_case secret is indistinguishable from a variable name and is not redacted.
