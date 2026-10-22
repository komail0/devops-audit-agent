"""parse_workflow reports line numbers so findings can cite exact lines without a second read."""

import pytest

from tools import Toolbox, _index_lines

WORKFLOW = """\
name: CI
on:
  push:
    branches: [dev]
permissions: write-all
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@main
      - run: pip install .
      - name: Lint
        run: ruff check .
        continue-on-error: true
      - name: Test
        run: |
          echo start
          pytest || true
  pr:
    runs-on: x
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - run: echo "${{ github.event.pull_request.title }}"
  reuse:
    uses: org/repo/.github/workflows/x.yml@main
    secrets: inherit
"""

PR_TARGET = """\
name: Greeter
on:
  pull_request_target:
    types: [opened]
jobs:
  greet:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.sha }}
"""


def parse(tmp_path, content):
    path = tmp_path / ".github" / "workflows" / "w.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return Toolbox(tmp_path).execute("parse_workflow", {"path": ".github/workflows/w.yml"})


@pytest.mark.parametrize(
    "expected",
    [
        "triggers (line 2):",
        "permissions (workflow level) (line 5): write-all",
        "test (line 7) [",
        "step 1 (line 10): uses: actions/checkout@main",
        "step 3 (line 12): Lint",
        "step 4 (line 15): Test",
        "pr (line 19) [",
        "job test step 3 has continue-on-error: true (line 14)",
        "job test step 4 run script contains `|| true` or `exit 0`: echo start ; pytest || true (line 18)",
        "job pr step 2 interpolates ${{ github.event... }} or head_ref directly into a run script (line 25)",
        "job reuse passes all secrets to the called workflow (secrets set to inherit) (line 28)",
        "job test step 1 (line 10): actions/checkout@main",
    ],
)
def test_lines_match_the_source_file(tmp_path, expected):
    assert expected in parse(tmp_path, WORKFLOW)


def test_every_reported_line_points_at_the_text_it_describes(tmp_path):
    source = WORKFLOW.splitlines()
    assert "continue-on-error" in source[14 - 1]
    assert "pytest || true" in source[18 - 1]
    assert "github.event.pull_request.title" in source[25 - 1]
    assert "secrets: inherit" in source[28 - 1]
    assert "actions/checkout@main" in source[10 - 1]


def test_pull_request_target_checkout_is_located_at_the_ref_line(tmp_path):
    out = parse(tmp_path, PR_TARGET)
    assert "checks out the pull request head under a pull_request_target trigger (line 11)" in out
    assert "workflow uses the pull_request_target trigger (line 2)" in out


def test_single_line_run_scripts_use_the_key_line(tmp_path):
    out = parse(tmp_path, "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: pytest || true\n")
    assert "`|| true` or `exit 0`: pytest || true (line 6)" in out


def test_folded_and_literal_blocks_locate_the_matching_row(tmp_path):
    content = "on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: |\n          one\n          two\n          make || true\n"
    assert "(line 9)" in parse(tmp_path, content)


def test_index_is_empty_for_invalid_yaml_and_never_raises():
    assert _index_lines("on: [unclosed\njobs: {") == {}
    assert _index_lines("- just\n- a list\n") == {}
    assert _index_lines("") == {}


def test_output_without_a_line_index_still_works(tmp_path):
    from tools import summarize_workflow

    out = summarize_workflow("w.yml", {"on": "push", "jobs": {"a": {"runs-on": "x", "steps": [{"run": "pytest || true"}]}}})
    assert "(line" not in out and "`|| true`" in out


def test_lines_survive_the_redactor_in_execute_output(tmp_path):
    out = parse(tmp_path, WORKFLOW)
    assert "[REDACTED" not in out
