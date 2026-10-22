"""Shared vocabulary for violations and the submit_report tool definition."""

from typing import Any, Dict

SEVERITIES = ("critical", "high", "medium", "low")
CATEGORIES = (
    "hygiene",
    "dependencies",
    "code_quality",
    "testing",
    "ci",
    "security",
    "configuration",
    "collaboration",
    "build",
)
CATEGORY_PREFIXES = {
    "hygiene": "HYG",
    "dependencies": "DEP",
    "code_quality": "QUAL",
    "testing": "TEST",
    "ci": "CI",
    "security": "SEC",
    "configuration": "CFG",
    "collaboration": "COLLAB",
    "build": "BUILD",
}
VIOLATION_FIELDS = (
    "rule_id",
    "category",
    "severity",
    "file",
    "line",
    "description",
    "evidence",
    "recommendation",
)

SUBMIT_REPORT = "submit_report"

SUBMIT_REPORT_SPEC: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SUBMIT_REPORT,
        "description": (
            "Finish the audit by submitting every confirmed violation. Call this once, when you are done "
            "investigating. An empty list is valid and expected for a healthy repository."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "violations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rule_id": {
                                "type": "string",
                                "description": "Stable id such as SEC-001, CI-002, HYG-001, DEP-001, TEST-001, QUAL-001, CFG-001, COLLAB-001, BUILD-001.",
                            },
                            "category": {"type": "string", "enum": list(CATEGORIES)},
                            "severity": {"type": "string", "enum": list(SEVERITIES)},
                            "file": {"type": ["string", "null"], "description": "Repo-relative path, or null for repository-wide findings."},
                            "line": {"type": ["integer", "null"], "description": "Line number you saw in a tool result, or null."},
                            "description": {"type": "string", "description": "What is wrong, in one sentence."},
                            "evidence": {"type": "string", "description": "What you observed that proves it. Never include secret values."},
                            "recommendation": {"type": "string", "description": "Concrete remediation."},
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "integer"},
                                "description": "The [step N] numbers of the tool results that support this finding.",
                            },
                        },
                        "required": [*VIOLATION_FIELDS, "evidence_refs"],
                    },
                }
            },
            "required": ["violations"],
        },
    },
}
