"""Turn the model's submitted violations into the final, validated JSON report.

The model's claims are checked against what the agent actually did: every
violation must cite tool results that exist, the file must exist in the
repository, and the line must be inside the file. Anything that cannot be
supported is dropped (and listed in the returned notes), never invented.
"""

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from devops_agent.redact import redact_object
from devops_agent.schema import CATEGORIES, CATEGORY_PREFIXES, SEVERITIES, SUBMIT_REPORT, VIOLATION_FIELDS

MAX_DESCRIPTION_CHARS = 300
MAX_EVIDENCE_CHARS = 600
MAX_RECOMMENDATION_CHARS = 500
MAX_LINE_CHECK_BYTES = 2_000_000

_CATEGORY_ALIASES = {
    "repository_hygiene": "hygiene", "repo_hygiene": "hygiene",
    "dependency": "dependencies", "dependency_management": "dependencies",
    "quality": "code_quality", "test": "testing", "tests": "testing",
    "ci_cd": "ci", "cicd": "ci", "github_actions": "ci", "workflow": "ci", "workflows": "ci",
    "sec": "security", "config": "configuration", "collab": "collaboration",
    "build_and_release": "build", "build_release": "build", "release": "build",
}
_SEVERITY_ALIASES = {
    "blocker": "critical", "major": "high", "moderate": "medium", "minor": "low",
    "info": "low", "informational": "low",
}
_NO_FILE = {"", "null", "none", "n/a", "na", "-", "."}
_SEVERITY_RANK = {name: rank for rank, name in enumerate(SEVERITIES)}


def _normalise_token(value: Any) -> str:
    return re.sub(r"[\s/&-]+", "_", str(value).strip().lower())


_RULE_ID = re.compile(r"([A-Za-z]{2,6})\s*-\s*(\d{1,4})")


def _clean_rule_id(value: Any, category: str) -> Tuple[str, bool]:
    """Return (PREFIX-NNN, changed). Garbled or missing ids fall back to the category's catch-all PREFIX-099."""
    raw = value.strip() if isinstance(value, str) else ""
    match = _RULE_ID.search(raw)
    if match:
        cleaned = f"{match.group(1).upper()}-{int(match.group(2)):03d}"
    else:
        cleaned = f"{CATEGORY_PREFIXES[category]}-099"
    return cleaned, cleaned != raw.upper()


def _text(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _valid_refs(value: Any, executed_steps: Dict[int, str]) -> List[int]:
    if not isinstance(value, list):
        return []
    refs = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        tool = executed_steps.get(number)
        if tool and tool != SUBMIT_REPORT and number not in refs:
            refs.append(number)
    return refs


def _clean_file(value: Any, repo_root: Path) -> Tuple[Optional[str], Optional[str]]:
    """Return (repo-relative path or None, note explaining why a path was cleared)."""
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "file was not a string"
    cleaned = value.strip().replace("\\", "/")
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    if cleaned.lower() in _NO_FILE:
        return None, None
    parts = [p for p in cleaned.split("/") if p]
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned) or ".." in parts or ".git" in parts:
        return None, f"file {cleaned!r} is not a valid repository-relative path"
    target = repo_root.joinpath(*parts)
    if target.is_symlink() or not target.is_file():
        return None, f"file {cleaned!r} does not exist in the repository"
    return "/".join(parts), None


def _clean_line(value: Any, file: Optional[str], repo_root: Path) -> Optional[int]:
    if file is None or value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return None
    target = repo_root.joinpath(*file.split("/"))
    try:
        if target.stat().st_size <= MAX_LINE_CHECK_BYTES:
            data = target.read_bytes()
            if b"\0" in data[:8000]:  # binary file: line numbers are meaningless
                return None
            total = len(data.decode("utf-8", errors="replace").splitlines())
            if number > total:
                return None
    except OSError:
        return None
    return number


def _description_key(file: Optional[str], description: str) -> Tuple[Optional[str], str]:
    return file, " ".join(re.findall(r"[a-z0-9]+", description.lower()))


def build_report(
    repo_url: str,
    raw_violations: Iterable[Any],
    repo_root: Path,
    executed_steps: Dict[int, str],
    secrets: Iterable[str] = (),
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate, clean and de-duplicate violations. Returns (report, notes about what was changed or dropped)."""
    repo_root = Path(repo_root)
    notes: List[str] = []
    kept: List[Dict[str, Any]] = []
    seen_location, seen_text = set(), set()

    for index, raw in enumerate(raw_violations, start=1):
        label = f"violation #{index}"
        if not isinstance(raw, dict):
            notes.append(f"dropped {label}: not an object")
            continue
        description = _text(raw.get("description"), MAX_DESCRIPTION_CHARS)
        evidence = _text(raw.get("evidence"), MAX_EVIDENCE_CHARS)
        recommendation = _text(raw.get("recommendation"), MAX_RECOMMENDATION_CHARS)
        missing = [n for n, v in (("description", description), ("evidence", evidence),
                                  ("recommendation", recommendation)) if v is None]
        if missing:
            notes.append(f"dropped {label}: missing or empty {', '.join(missing)}")
            continue

        category = _normalise_token(raw.get("category", ""))
        category = _CATEGORY_ALIASES.get(category, category)
        severity = _normalise_token(raw.get("severity", ""))
        severity = _SEVERITY_ALIASES.get(severity, severity)
        if category not in CATEGORIES:
            notes.append(f"dropped {label}: unknown category {raw.get('category')!r}")
            continue
        if severity not in SEVERITIES:
            notes.append(f"dropped {label}: unknown severity {raw.get('severity')!r}")
            continue
        rule_id, rule_changed = _clean_rule_id(raw.get("rule_id"), category)
        if rule_changed:
            notes.append(f"{label}: rule_id {raw.get('rule_id')!r} normalised to {rule_id}")

        if not _valid_refs(raw.get("evidence_refs"), executed_steps):
            notes.append(f"dropped {label} ({rule_id}): no valid evidence_refs pointing at executed tool calls")
            continue

        file, file_note = _clean_file(raw.get("file"), repo_root)
        if file_note:
            notes.append(f"{label} ({rule_id}): {file_note}; location cleared")
        line = _clean_line(raw.get("line"), file, repo_root)
        if raw.get("line") is not None and file is not None and line is None:
            notes.append(f"{label} ({rule_id}): line {raw.get('line')!r} is outside the file; line cleared")

        violation = {
            "rule_id": rule_id,
            "category": category,
            "severity": severity,
            "file": file,
            "line": line,
            "description": description,
            "evidence": evidence,
            "recommendation": recommendation,
        }
        violation = redact_object(violation, secrets)

        location_key = (violation["rule_id"], file, line)
        text_key = _description_key(file, violation["description"])
        if location_key in seen_location or text_key in seen_text:
            notes.append(f"dropped {label} ({rule_id}): duplicate of an earlier finding")
            continue
        seen_location.add(location_key)
        seen_text.add(text_key)
        kept.append({name: violation[name] for name in VIOLATION_FIELDS})

    kept.sort(key=lambda v: (_SEVERITY_RANK[v["severity"]], v["file"] or "", v["line"] or 0, v["rule_id"]))
    return {"repository": repo_url, "violations": kept}, notes


def write_report(path: Path, report: Dict[str, Any]) -> None:
    """Write the report atomically so a crash never leaves a half-written file."""
    path = Path(path)
    handle, temp_name = tempfile.mkstemp(dir=path.resolve().parent, prefix=".report-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(report, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temp_name, path)
    except BaseException:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
        raise
