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
| `AGENT_MAX_STEPS` | Optional cap on agent steps |
| `AGENT_TIMEOUT_SECONDS` | Optional wall-clock limit for a run |

## What exists so far

- `devops_agent/repo.py`: validates GitHub URLs and clones the target into a throwaway temp folder, with git hooks, submodules, LFS downloads, the file protocol and credential prompts disabled, and a clone timeout. The folder is deleted afterwards.
- `devops_agent/redact.py`: replaces secret values (known token formats, private keys, credential assignments, URL passwords) with `[REDACTED:<type>]` markers. It is meant to run on all tool output, logs and report text.

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
