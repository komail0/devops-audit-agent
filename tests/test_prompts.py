import re

import pytest

from devops_agent.prompts import build_system_prompt
from devops_agent.schema import CATEGORIES, CATEGORY_PREFIXES, SEVERITIES
from tools import TOOL_SPECS


@pytest.fixture(scope="module")
def prompt():
    return build_system_prompt(15)


def test_placeholders_are_filled(prompt):
    assert not re.search(r"\{[a-z_]+\}", prompt)
    assert "at most 15 model turns" in prompt


def test_vocabulary_matches_schema(prompt):
    for value in (*SEVERITIES, *CATEGORIES):
        assert value in prompt


def test_every_category_has_guidance_and_a_rule_prefix(prompt):
    prefixes = CATEGORY_PREFIXES
    for category, prefix in prefixes.items():
        assert f"- {category}:" in prompt
        assert f"{prefix}-001" in prompt


def test_prompt_states_the_evidence_and_precision_rules(prompt):
    for phrase in (
        "branch protection",
        "equivalent",
        "evidence_refs",
        "Never write a secret value",
        "An empty list is a valid",
        "batch independent tool calls",
        "read it before concluding",
    ):
        assert phrase in prompt


def test_prompt_mentions_only_tools_that_exist(prompt):
    named = set(re.findall(r"\b(list_files|read_file|search_code|search_history|git_log|git_status|parse_workflow|note_finding|submit_report|run_pytest|run_ruff)\b", prompt))
    available = {spec["function"]["name"] for spec in TOOL_SPECS} | {"submit_report", "note_finding"}
    assert named <= available


def test_prompt_has_no_repository_specific_content(prompt):
    assert "github.com/" not in prompt
    assert not re.search(r"psf/|komail|octocat", prompt, re.IGNORECASE)


def test_prompt_treats_visible_literal_secrets_as_confirmed_and_asks_for_a_final_review(prompt):
    assert "is itself the confirmation" in prompt
    assert "DEBUG = True" in prompt
    assert "4. Review:" in prompt and "5. Report:" in prompt
    assert "test fixtures are not credentials" in prompt


def test_prompt_does_not_call_fully_pinned_requirements_a_missing_lockfile(prompt):
    assert "all pinned with `==`" in prompt and "not a missing lockfile" in prompt
    assert "no lockfile for an application" not in prompt


def test_prompt_calibrates_severity_per_rule_and_warns_against_marking_everything_high(prompt):
    assert "Do not mark everything high" in prompt
    for rule, severity in (("HYG-003", "low"), ("CI-002", "high"), ("SEC-004", "critical"), ("CFG-001", "medium")):
        assert re.search(rf"{rule}[^;\[]*\[{severity}", prompt), rule


def test_prompt_asks_for_separate_findings_per_location(prompt):
    assert "separate finding each with its own line" in prompt


def test_prompt_stays_compact_for_small_token_quotas(prompt):
    assert len(prompt) < 11_500  # roughly 2,900 tokens; it is sent with every request


def test_prompt_ties_workflow_fact_lines_to_the_review_step_and_forbids_double_reporting_env_files(prompt):
    assert "fact line from parse_workflow" in prompt
    assert "not also HYG-003" in prompt
