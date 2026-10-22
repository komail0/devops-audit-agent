"""The prompt must steer the model towards the defect classes in the assignment, without naming any repository."""

import re

import pytest

from devops_agent.prompts import build_system_prompt


@pytest.fixture(scope="module")
def prompt():
    return build_system_prompt(15)


@pytest.mark.parametrize(
    "phrase",
    [
        "search_history",  # secrets removed from source but still in history
        "still in git history",
        "note_finding",
        "large binary files committed",
        "no dependency manifest at all",
        "never run by CI",  # lint config present but not enforced
        "secrets passed on a command line",
        "Dockerfile using a `latest` or unpinned base image, or running as root",
        "unpinned third-party actions",
        "pull_request_target",
        "write-all",
        "tests that CI never runs",
    ],
)
def test_prompt_covers_defect_class(prompt, phrase):
    assert phrase in prompt


@pytest.mark.parametrize("rule", ["HYG-004", "DEP-004", "QUAL-003", "SEC-007", "SEC-008", "BUILD-003"])
def test_new_rules_are_in_the_catalogue_with_a_default_severity(prompt, rule):
    assert re.search(rf"{rule}[^;\[]*\[(low|medium|high|critical)", prompt)


def test_prompt_tells_the_model_to_use_history_search_for_removed_secrets(prompt):
    hunt = prompt.split("2. Hunt:")[1].split("3. Verify:")[0]
    assert "search_history" in hunt and "later removed" in hunt


def test_prompt_stays_compact_even_with_the_new_guidance(prompt):
    assert len(prompt) < 11_000
