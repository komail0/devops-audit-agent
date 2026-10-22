import re

import pytest

from devops_agent.prompts import build_system_prompt
from devops_agent.schema import CATEGORIES, SEVERITIES
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
    prefixes = {"hygiene": "HYG", "dependencies": "DEP", "code_quality": "QUAL", "testing": "TEST", "ci": "CI",
                "security": "SEC", "configuration": "CFG", "collaboration": "COLLAB", "build": "BUILD"}
    assert set(prefixes) == set(CATEGORIES)
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
    named = set(re.findall(r"\b(list_files|read_file|search_code|git_log|git_status|parse_workflow|submit_report|run_pytest|run_ruff)\b", prompt))
    available = {spec["function"]["name"] for spec in TOOL_SPECS} | {"submit_report"}
    assert named <= available


def test_prompt_has_no_repository_specific_content(prompt):
    assert "github.com/" not in prompt
    assert not re.search(r"psf/|komail|octocat", prompt, re.IGNORECASE)
