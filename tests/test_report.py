import json
import os

import pytest

from devops_agent.report import build_report, write_report
from devops_agent.schema import VIOLATION_FIELDS

URL = "https://github.com/o/r"
STEPS = {1: "list_files", 2: "read_file", 3: "submit_report"}
FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "config.py").write_text("a = 1\nb = 2\nc = 3\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.yml").write_text("on: push\n")
    return tmp_path


def violation(**overrides):
    base = {
        "rule_id": "sec-001",
        "category": "security",
        "severity": "high",
        "file": "config.py",
        "line": 2,
        "description": "Hardcoded API credential",
        "evidence": "Credential assigned in source",
        "recommendation": "Use an environment variable",
        "evidence_refs": [2],
    }
    base.update(overrides)
    return base


def build(root, *violations, steps=STEPS):
    return build_report(URL, list(violations), root, steps)


def test_valid_violation_passes_through_with_exact_fields(root):
    report, notes = build(root, violation())
    assert list(report) == ["repository", "violations"] and report["repository"] == URL
    item = report["violations"][0]
    assert list(item) == list(VIOLATION_FIELDS)  # exactly the contract, evidence_refs stripped
    assert item["rule_id"] == "SEC-001" and item["file"] == "config.py" and item["line"] == 2
    assert notes == []


def test_empty_submission_gives_empty_array(root):
    report, notes = build(root)
    assert report == {"repository": URL, "violations": []} and notes == []


@pytest.mark.parametrize("refs", [[], None, "2", [99], [3], [True], ["x"]])
def test_violations_without_valid_evidence_are_dropped(root, refs):
    report, notes = build(root, violation(evidence_refs=refs))
    assert report["violations"] == []
    assert "no valid evidence_refs" in notes[0]


def test_partially_valid_refs_are_enough(root):
    report, _ = build(root, violation(evidence_refs=[99, 1]))
    assert len(report["violations"]) == 1


@pytest.mark.parametrize("field", ["rule_id", "description", "evidence", "recommendation"])
@pytest.mark.parametrize("bad", [None, "", "   ", 5])
def test_missing_text_fields_drop_the_violation(root, field, bad):
    report, notes = build(root, violation(**{field: bad}))
    assert report["violations"] == [] and field in notes[0]


@pytest.mark.parametrize(
    "given, expected",
    [("ci", "ci"), ("CI/CD", "ci"), ("GitHub Actions", "ci"), ("Repository Hygiene", "hygiene"),
     ("dependency_management", "dependencies"), ("Build & Release", "build")],
)
def test_category_aliases(root, given, expected):
    report, _ = build(root, violation(category=given))
    assert report["violations"][0]["category"] == expected


@pytest.mark.parametrize("given, expected", [("HIGH", "high"), ("major", "high"), ("Info", "low"), ("blocker", "critical")])
def test_severity_aliases(root, given, expected):
    report, _ = build(root, violation(severity=given))
    assert report["violations"][0]["severity"] == expected


@pytest.mark.parametrize("field, bad", [("category", "vibes"), ("severity", "scary"), ("category", None)])
def test_unknown_enum_values_drop_the_violation(root, field, bad):
    report, notes = build(root, violation(**{field: bad}))
    assert report["violations"] == [] and "unknown" in notes[0]


def test_non_object_entries_dropped(root):
    report, notes = build(root, "oops", 5, None)
    assert report["violations"] == [] and len(notes) == 3


# ---- file and line handling -------------------------------------------------------

@pytest.mark.parametrize("given", ["./config.py", "config.py ", ".\\config.py"])
def test_file_paths_are_normalised(root, given):
    assert build(root, violation(file=given))[0]["violations"][0]["file"] == "config.py"


def test_nested_paths_use_forward_slashes(root):
    item = build(root, violation(file="sub\\x.yml", line=1))[0]["violations"][0]
    assert item["file"] == "sub/x.yml" and item["line"] == 1


@pytest.mark.parametrize("given", [None, "", "null", "N/A", "none"])
def test_null_like_files_become_null_without_notes(root, given):
    report, notes = build(root, violation(file=given, line=5))
    item = report["violations"][0]
    assert item["file"] is None and item["line"] is None and notes == []


@pytest.mark.parametrize("given", ["missing.py", "/etc/passwd", "../outside.py", "C:\\x.py", ".git/config", "sub", 7])
def test_nonexistent_or_unsafe_files_clear_location_but_keep_supported_finding(root, given):
    report, notes = build(root, violation(file=given, line=1))
    item = report["violations"][0]
    assert item["file"] is None and item["line"] is None
    assert "location cleared" in notes[0]


@pytest.mark.parametrize("given, expected", [(1, 1), (3, 3), ("2", 2), (4, None), (0, None), (-1, None), (True, None), ("x", None), (None, None)])
def test_line_must_be_inside_the_file(root, given, expected):
    assert build(root, violation(line=given))[0]["violations"][0]["line"] == expected


def test_symlinked_file_rejected(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "secret.txt"
    outside.write_text("x")
    link = root / "link.txt"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert build(root, violation(file="link.txt"))[0]["violations"][0]["file"] is None


# ---- hygiene of the output ---------------------------------------------------------

def test_secrets_are_redacted_everywhere(root):
    report, _ = build(root, violation(evidence=f"found {FAKE_AWS} in config", description=f"key {FAKE_AWS}",
                                      recommendation="rotate hunter2-hunter2"))
    text = json.dumps(report)
    assert FAKE_AWS not in text and "[REDACTED:aws-access-key]" in text
    clean, _ = build_report(URL, [violation(recommendation="rotate hunter2-hunter2")], root, STEPS, secrets=["hunter2-hunter2"])
    assert "hunter2-hunter2" not in json.dumps(clean)


def test_long_text_is_truncated(root):
    item = build(root, violation(evidence="e" * 5000, description="d" * 5000, recommendation="r" * 5000))[0]["violations"][0]
    assert len(item["evidence"]) == 600 and len(item["description"]) == 300 and len(item["recommendation"]) == 500


def test_duplicates_collapse(root):
    same_place = build(root, violation(), violation(description="Different words entirely"))
    assert len(same_place[0]["violations"]) == 1 and "duplicate" in same_place[1][0]
    same_text = build(root, violation(), violation(rule_id="SEC-009", line=3, description="hardcoded api credential."))
    assert len(same_text[0]["violations"]) == 1 and "duplicate" in same_text[1][0]
    different = build(root, violation(), violation(line=3, description="Another key on another line"))
    assert len(different[0]["violations"]) == 2


def test_same_defect_in_different_files_is_not_a_duplicate(root):
    report, _ = build(root, violation(file="config.py", line=1), violation(file="sub/x.yml", line=1))
    assert len(report["violations"]) == 2


def test_sorted_by_severity_then_location(root):
    report, _ = build(
        root,
        violation(severity="low", description="a low one", rule_id="HYG-001", file=None, line=None),
        violation(severity="critical", description="crit", rule_id="SEC-009", line=1),
        violation(severity="high", description="high b", rule_id="SEC-002", file="sub/x.yml", line=1),
        violation(severity="high", description="high a", rule_id="SEC-003", file="config.py", line=3),
    )
    assert [v["severity"] for v in report["violations"]] == ["critical", "high", "high", "low"]
    assert [v["file"] for v in report["violations"][1:3]] == ["config.py", "sub/x.yml"]


# ---- writing ---------------------------------------------------------------------

def test_write_report_is_valid_json_and_leaves_no_temp_files(root, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    report, _ = build(root, violation())
    write_report(out / "report.json", report)
    assert json.loads((out / "report.json").read_text(encoding="utf-8")) == report
    assert [p.name for p in out.iterdir()] == ["report.json"]


def test_write_report_overwrites_existing_file(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("old")
    write_report(target, {"repository": URL, "violations": []})
    assert json.loads(target.read_text())["violations"] == []


def test_write_report_keeps_unicode(tmp_path):
    write_report(tmp_path / "r.json", {"repository": URL, "violations": [], "note": "caf\u00e9"})
    assert "caf\u00e9" in (tmp_path / "r.json").read_text(encoding="utf-8")


def test_write_failure_cleans_up_and_keeps_original(tmp_path):
    target = tmp_path / "report.json"
    target.write_text("original")
    with pytest.raises(TypeError):
        write_report(target, {"bad": object()})
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["report.json"]


def test_write_to_missing_directory_raises(tmp_path):
    with pytest.raises(OSError):
        write_report(tmp_path / "nope" / "report.json", {"repository": URL, "violations": []})
