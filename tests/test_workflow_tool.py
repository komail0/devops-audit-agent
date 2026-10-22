import pytest

import tools
from tools import Toolbox

SHA = "a" * 40

HEALTHY = f"""\
name: CI
on:
  push:
    branches: [main]
  pull_request:
permissions:
  contents: read
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@{SHA}
        with:
          persist-credentials: false
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt
      - run: ruff check .
      - run: pytest -q
"""

RISKY = """\
name: Risky
on:
  pull_request_target:
    types: [opened]
jobs:
  build:
    runs-on: ubuntu-latest
    continue-on-error: true
    steps:
      - uses: actions/checkout@main
        with:
          ref: ${{ github.event.pull_request.head.sha }}
      - uses: some/action@v1.2.3
      - name: greet
        run: echo "${{ github.event.pull_request.title }}"
      - run: pytest || true
        continue-on-error: true
      - run: deploy.sh
        env:
          TOKEN: ${{ secrets.DEPLOY_TOKEN }}
  reuse:
    uses: org/repo/.github/workflows/x.yml@main
    secrets: inherit
"""


def _box(tmp_path, content, name=".github/workflows/ci.yml"):
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return Toolbox(tmp_path)


def test_healthy_workflow_facts(tmp_path):
    out = _box(tmp_path, HEALTHY).parse_workflow(".github/workflows/ci.yml")
    assert "# workflow .github/workflows/ci.yml: CI" in out
    assert "push (branches: ['main'])" in out and "  - pull_request" in out
    assert "permissions (workflow level) (line 6): {'contents': 'read'}" in out
    assert "commit sha" in out and "version tag" in out
    assert "test commands found by keyword match in run steps: job test step 5: pytest" in out
    assert "lint commands found by keyword match in run steps: job test step 4: ruff" in out
    assert "build commands found by keyword match in run steps: none (they may run via make" in out
    for risky in ("pull_request_target", "continue-on-error", "`|| true`", "not pinned", "no explicit permissions"):
        assert risky not in out


def test_risky_workflow_facts(tmp_path):
    out = _box(tmp_path, RISKY).parse_workflow(".github/workflows/ci.yml")
    assert "workflow uses the pull_request_target trigger" in out
    assert "job build step 1 checks out the pull request head under a pull_request_target trigger" in out
    assert "job build step 3 interpolates" in out
    assert "job build step 4 run script contains `|| true`" in out
    assert "job build step 4 has continue-on-error: true" in out
    assert "job build has continue-on-error: true" in out
    assert "no explicit permissions set at workflow or job level" in out
    assert "actions not pinned to a tag or sha (branch or other ref)" in out
    assert "build step 1 (line 10): actions/checkout@main" in out
    assert "secrets referenced: DEPLOY_TOKEN" in out
    assert "job reuse passes all secrets to the called workflow (secrets set to inherit)" in out


def test_on_key_boolean_quirk_and_scalar_forms(tmp_path):
    scalar = "on: push\njobs:\n  a:\n    runs-on: x\n    steps: []\n"
    assert "  - push" in _box(tmp_path, scalar).parse_workflow(".github/workflows/ci.yml")
    as_list = "on: [push, pull_request]\njobs: {}\n"
    out = _box(tmp_path, as_list).parse_workflow(".github/workflows/ci.yml")
    assert "  - push" in out and "  - pull_request" in out


def test_job_level_permissions_suppress_missing_permissions_fact(tmp_path):
    content = "on: push\njobs:\n  a:\n    runs-on: x\n    permissions:\n      contents: read\n    steps:\n      - run: echo hi\n"
    out = _box(tmp_path, content).parse_workflow(".github/workflows/ci.yml")
    assert "no explicit permissions" not in out


def test_workflow_without_jobs(tmp_path):
    out = _box(tmp_path, "on: push\n").parse_workflow(".github/workflows/ci.yml")
    assert "(none found)" in out


@pytest.mark.parametrize(
    "uses, kind",
    [
        ("./local-action", "local"),
        ("docker://alpine:3", "docker"),
        (f"actions/checkout@{SHA}", "commit sha"),
        ("actions/checkout@v4", "version tag"),
        ("actions/checkout@v4.1.2", "version tag"),
        ("actions/checkout@main", "branch or other ref"),
        ("actions/checkout", "no ref"),
    ],
)
def test_pin_kinds(uses, kind):
    assert tools._pin_kind(uses) == kind


def test_errors(tmp_path):
    box = _box(tmp_path, HEALTHY)
    with pytest.raises(tools.ToolError, match="no such file"):
        box.parse_workflow(".github/workflows/missing.yml")
    with pytest.raises(tools.ToolError, match="directory"):
        box.parse_workflow(".github/workflows")
    with pytest.raises(tools.ToolError):
        box.parse_workflow("../x.yml")
    bad = _box(tmp_path, "on: [unclosed\njobs: {", name="bad.yml")
    with pytest.raises(tools.ToolError, match="not valid YAML"):
        bad.parse_workflow("bad.yml")
    with pytest.raises(tools.ToolError, match="not a workflow"):
        _box(tmp_path, "- just\n- a list\n", name="list.yml").parse_workflow("list.yml")


def test_oversized_workflow_not_parsed(tmp_path, monkeypatch):
    box = _box(tmp_path, HEALTHY)
    monkeypatch.setattr(tools, "MAX_READ_BYTES", 10)
    with pytest.raises(tools.ToolError, match="larger than"):
        box.parse_workflow(".github/workflows/ci.yml")


def test_execute_redacts_secrets_in_run_lines(tmp_path):
    fake = "AKIA" + "ABCDEFGHIJKLMNOP"
    content = f"on: push\njobs:\n  a:\n    runs-on: x\n    steps:\n      - run: echo {fake}\n"
    out = _box(tmp_path, content).execute("parse_workflow", {"path": ".github/workflows/ci.yml"})
    assert fake not in out and "[REDACTED:aws-access-key]" in out


def test_hostile_yaml_does_not_execute(tmp_path):
    content = "on: !!python/object/apply:os.system ['echo pwned']\njobs: {}\n"
    out = _box(tmp_path, content).execute("parse_workflow", {"path": ".github/workflows/ci.yml"})
    assert out.startswith("ERROR")


def test_facts_survive_the_redactor_when_called_through_execute(tmp_path):
    """The agent only ever sees execute() output, which is redacted: facts must not be mangled by it."""
    out = _box(tmp_path, RISKY).execute("parse_workflow", {"path": ".github/workflows/ci.yml"})
    assert "[REDACTED" not in out
    assert "secrets set to inherit" in out and "secrets referenced: DEPLOY_TOKEN" in out
    assert "`|| true`" in out and "continue-on-error: true" in out
