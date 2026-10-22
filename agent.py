"""Command-line entry point.

    python agent.py --repo <github_repository_url> --output report.json

Runs unattended: no prompts, no stdin. Progress and problems go to stderr;
the result is the JSON file at --output.

Exit codes: 0 report written (possibly with no violations), 2 invalid
arguments/URL/configuration, 3 clone failed, 4 LLM failure or no report could
be produced, 1 unexpected internal error, 130 interrupted.
"""

import argparse
import dataclasses
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, Optional

from devops_agent.agent_loop import AgentError, run_agent
from devops_agent.config import Config, ConfigError, load_config
from devops_agent.llm import LLMClient, LLMError
from devops_agent.prompts import build_system_prompt
from devops_agent.redact import redact
from devops_agent.report import build_report, write_report
from devops_agent.repo import CloneError, InvalidRepoUrl, cloned_repo, parse_github_url
from devops_agent.trace import Trace
from tools import Toolbox

EXIT_OK, EXIT_INTERNAL, EXIT_USAGE, EXIT_CLONE, EXIT_LLM, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
PROJECT_ROOT = Path(__file__).resolve().parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent.py",
        description="Audit a public GitHub repository for DevOps best-practice violations.",
    )
    parser.add_argument("--repo", required=True, help="public GitHub repository URL, e.g. https://github.com/owner/name")
    parser.add_argument("--output", required=True, help="path of the JSON report to write")
    parser.add_argument("--max-steps", type=int, help="maximum LLM turns (overrides AGENT_MAX_STEPS)")
    parser.add_argument("--timeout", type=int, help="wall-clock limit in seconds (overrides AGENT_TIMEOUT_SECONDS)")
    parser.add_argument("--trace", help="also write the tool-call trace to this JSON file")
    parser.add_argument("--verbose", action="store_true", help="print each tool call to stderr as it happens")
    return parser


def _default_config() -> Config:
    return load_config(PROJECT_ROOT)


def _check_output_path(path: Path) -> Optional[str]:
    """Return a problem description if the report cannot be written there, before any work is done."""
    if path.is_dir():
        return f"output path {str(path)!r} is a directory"
    try:
        path.resolve().parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.resolve().parent, prefix=".write-test-"):
            pass
    except OSError as exc:
        return f"cannot write to {str(path)!r}: {exc.strerror or type(exc).__name__}"
    return None


def main(
    argv: Optional[List[str]] = None,
    config_loader: Callable[[], Config] = _default_config,
    llm_factory: Callable[[Config], LLMClient] = LLMClient,
    cloner: Callable = cloned_repo,
) -> int:
    secrets: List[str] = []

    def log(message: str) -> None:
        print(redact(message, secrets), file=sys.stderr)

    def fail(code: int, message: str) -> int:
        log(f"error: {message}")
        return code

    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:  # argparse exits itself on bad arguments and on --help
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        parse_github_url(args.repo)
    except InvalidRepoUrl as exc:
        return fail(EXIT_USAGE, str(exc))
    try:
        config = config_loader()
    except ConfigError as exc:
        return fail(EXIT_USAGE, str(exc))
    secrets.extend(config.secrets())
    for note in config.notes:
        log(f"note: {note}")
    overrides = {}
    if args.max_steps is not None:
        overrides["max_steps"] = args.max_steps
    if args.timeout is not None:
        overrides["timeout_seconds"] = args.timeout
    if any(value < 1 for value in overrides.values()):
        return fail(EXIT_USAGE, "--max-steps and --timeout must be positive integers")
    config = dataclasses.replace(config, **overrides)

    output = Path(args.output)
    problem = _check_output_path(output)
    if problem:
        return fail(EXIT_USAGE, problem)

    trace = Trace(secrets=secrets, echo=sys.stderr if args.verbose else None)
    try:
        log(f"Cloning {args.repo} ...")
        with cloner(args.repo) as (workspace, canonical_url):
            log(f"Auditing with model {config.model} (at most {config.max_steps} turns, {config.timeout_seconds}s) ...")
            result = run_agent(
                llm_factory(config),
                Toolbox(workspace),
                canonical_url,
                build_system_prompt(config.max_steps),
                max_steps=config.max_steps,
                timeout_seconds=config.timeout_seconds,
                context_chars=config.context_chars,
                trace=trace,
            )
            report, notes = build_report(canonical_url, result.violations, workspace, result.executed_steps, secrets)
        for note in notes:
            log(f"note: {note}")
        write_report(output, report)
        log(
            f"Done: {len(report['violations'])} violation(s) written to {output} "
            f"({result.tool_calls} tool calls over {result.turns} turns, stop reason: {result.stop_reason})"
        )
        return EXIT_OK
    except CloneError as exc:
        return fail(EXIT_CLONE, f"could not clone {args.repo}: {exc}")
    except (LLMError, AgentError) as exc:
        return fail(EXIT_LLM, str(exc))
    except KeyboardInterrupt:
        return fail(EXIT_INTERRUPTED, "interrupted")
    except Exception as exc:  # last resort: a clear message and a nonzero exit, never a half-written report
        return fail(EXIT_INTERNAL, f"unexpected {type(exc).__name__}: {exc}")
    finally:
        if args.trace:
            try:
                trace.write(Path(args.trace))
            except OSError as exc:
                log(f"warning: could not write trace to {args.trace}: {exc.strerror or type(exc).__name__}")


if __name__ == "__main__":
    sys.exit(main())
