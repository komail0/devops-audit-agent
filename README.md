# DevOps Repository Auditing Agent

An AI agent that inspects a public GitHub repository, finds DevOps best-practice violations, and writes an evidence-based JSON report with recommendations.

Agent cycle: Observe → Reason → Select Tool → Execute Tool → Interpret Results → Report.

**Status:** work in progress. Installation and usage instructions will be added as the implementation lands.

## Planned usage

```
python agent.py --repo <github_repository_url> --output report.json
```

## Design constraints

- Python only. The agent loop, tool dispatch and control flow are written from scratch, with no agent or orchestration frameworks.
- The LLM chooses which inspection tools to call, including follow-up calls.
- Static inspection only. Target repository code is never executed and the repository is never modified.
- Secrets are never printed or written to the report.
