"""Compare two conformance reports, so an upgrade is a diff and not a rerun.

``--baseline`` answers "is this finding new for this project?". This module
answers a different question: between two runs - usually two pinned versions of
one server - what changed? It compares findings, case verdicts and the observed
effect of each call, so a report diff can say "``delete_note`` now removes a row
it used to modify" instead of only "the score moved".

It is a pure function over two reports: no server is started, nothing is
observed, and nothing is inferred. A change in the observed effect is a change
in what an observer saw, not proof of intent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .mcp_verifier import REPORT_SCHEMA

COMPARE_SCHEMA = "effectfence.mcp.compare.v1"

# ``fail`` is worse than ``inconclusive``, which is worse than ``pass``. An
# unverified case is not a clean one, so it ranks above pass but below a finding.
VERDICT_RANK = {"pass": 0, "inconclusive": 1, "fail": 2}
CALL_KINDS = ("firstCall", "retry")


class CompareError(ValueError):
    """A report that cannot be compared; reported before anything is printed."""


def load_report(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise CompareError(f"could not read report: {error}") from error
    except json.JSONDecodeError as error:
        raise CompareError(f"report is invalid JSON: {error}") from error
    if not isinstance(value, dict):
        raise CompareError("report root must be an object")
    if value.get("schemaVersion") != REPORT_SCHEMA:
        raise CompareError(
            f"report schemaVersion must be {REPORT_SCHEMA!r}, got "
            f"{value.get('schemaVersion')!r}; is this a conformance report?"
        )
    if not isinstance(value.get("cases"), list):
        raise CompareError("report has no cases array; is this a conformance report?")
    return value


def compare_reports(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    left_path: str | None = None,
    right_path: str | None = None,
) -> dict[str, Any]:
    """Diff two reports: verdict, findings, case statuses and observed effects."""
    left_side = _side(left, left_path)
    right_side = _side(right, right_path)
    left_findings = _finding_keys(left)
    right_findings = _finding_keys(right)
    finding_keys = {(item["caseId"], item["code"]) for item in left_findings}
    right_keys = {(item["caseId"], item["code"]) for item in right_findings}
    added = [item for item in right_findings if (item["caseId"], item["code"]) not in finding_keys]
    removed = [item for item in left_findings if (item["caseId"], item["code"]) not in right_keys]

    left_cases = _cases(left)
    right_cases = _cases(right)
    added_cases = sorted(set(right_cases) - set(left_cases))
    removed_cases = sorted(set(left_cases) - set(right_cases))
    status_changes: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    for case_id in sorted(set(left_cases) & set(right_cases)):
        before = left_cases[case_id]
        after = right_cases[case_id]
        before_status = _case_status(before)
        after_status = _case_status(after)
        if before_status != after_status:
            status_changes.append(
                {
                    "caseId": case_id,
                    "tool": after.get("tool") or before.get("tool"),
                    "left": before_status,
                    "right": after_status,
                }
            )
        changed = _effect_changes(before, after)
        if changed:
            effects.append(
                {
                    "caseId": case_id,
                    "tool": after.get("tool") or before.get("tool"),
                    "observers": changed,
                }
            )

    # A comparison is only meaningful inside the same observed boundary: a run
    # that watched fewer observers cannot be read as an improvement.
    left_observers = _observers(left)
    right_observers = _observers(right)
    boundary = {
        "added": sorted(set(right_observers) - set(left_observers)),
        "removed": sorted(set(left_observers) - set(right_observers)),
        "changed": sorted(
            observer_id
            for observer_id in set(left_observers) & set(right_observers)
            if left_observers[observer_id] != right_observers[observer_id]
        ),
    }

    left_violations = _policy_codes(left)
    right_violations = _policy_codes(right)
    policy_added = sorted(right_violations - left_violations)
    policy_removed = sorted(left_violations - right_violations)
    left_verdict = str(left.get("verdict", "unknown"))
    right_verdict = str(right.get("verdict", "unknown"))
    verdict_regressed = VERDICT_RANK.get(right_verdict, 2) > VERDICT_RANK.get(left_verdict, 2)
    fatal_added = bool(right.get("fatalError")) and not bool(left.get("fatalError"))
    score = {
        "left": _score_value(left),
        "right": _score_value(right),
    }
    score["delta"] = (
        score["right"] - score["left"]
        if isinstance(score["left"], int) and isinstance(score["right"], int)
        else None
    )

    summary: list[str] = []
    if verdict_regressed:
        summary.append(f"VERDICT_REGRESSION {left_verdict} -> {right_verdict}")
    if fatal_added:
        summary.append("FATAL_ERROR_ADDED")
    summary.extend(f"POLICY_VIOLATION_ADDED {code}" for code in policy_added)
    summary.extend(f"NEW_FINDING {item['caseId']}:{item['code']}" for item in added)
    summary.extend(f"FINDING_FIXED {item['caseId']}:{item['code']}" for item in removed)
    summary.extend(f"POLICY_VIOLATION_FIXED {code}" for code in policy_removed)
    summary.extend(
        f"EFFECT_CHANGED {item['caseId']}" for item in effects
    )
    summary.extend(
        f"CASE_STATUS_CHANGED {item['caseId']} {item['left']} -> {item['right']}"
        for item in status_changes
    )
    if boundary["added"] or boundary["changed"]:
        summary.append("OBSERVED_BOUNDARY_CHANGED")
    if boundary["removed"]:
        summary.append("OBSERVED_BOUNDARY_SHRUNK " + ", ".join(boundary["removed"]))
    if added_cases:
        summary.append(f"CASES_ADDED {len(added_cases)}")
    if removed_cases:
        summary.append(f"CASES_REMOVED {len(removed_cases)}")
    if isinstance(score["delta"], int) and score["delta"]:
        direction = "SCORE_IMPROVED" if score["delta"] > 0 else "SCORE_DROPPED"
        summary.append(f"{direction} {abs(score['delta'])}")
    # Only a new contradiction, a worse verdict, a new policy violation, a new
    # fatal error or a boundary the earlier run had is a regression. A score drop
    # alone is reported, not gated: it can come from coverage a run chose not to
    # have. A verdict obtained by watching less is not an improvement, so dropping
    # an observer gates; adding one does not.
    regression = bool(
        added or policy_added or verdict_regressed or fatal_added or boundary["removed"]
    )
    return {
        "schemaVersion": COMPARE_SCHEMA,
        "left": left_side,
        "right": right_side,
        "verdict": {
            "left": left_verdict,
            "right": right_verdict,
            "changed": left_verdict != right_verdict,
            "regressed": verdict_regressed,
        },
        "score": score,
        "findings": {"added": added, "removed": removed},
        "cases": {
            "added": added_cases,
            "removed": removed_cases,
            "statusChanges": status_changes,
        },
        "effects": effects,
        "boundary": boundary,
        "policyViolations": {"added": policy_added, "removed": policy_removed},
        "regression": regression,
        "summary": summary,
    }


def _side(report: dict[str, Any], path: str | None) -> dict[str, Any]:
    server = report.get("server") if isinstance(report.get("server"), dict) else {}
    score = report.get("score") if isinstance(report.get("score"), dict) else {}
    return {
        "path": str(path) if path else None,
        "verdict": str(report.get("verdict", "unknown")),
        "score": score.get("value"),
        "server": server.get("name"),
        "version": server.get("version"),
        "generatedAt": report.get("generatedAt"),
        "certificateSha256": report.get("certificateSha256"),
        "cases": len(report.get("cases", [])),
    }


def _score_value(report: dict[str, Any]) -> int | None:
    score = report.get("score")
    if not isinstance(score, dict):
        return None
    value = score.get("value")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _cases(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for case in report.get("cases", []):
        if isinstance(case, dict) and case.get("id") is not None:
            result[str(case["id"])] = case
    return result


def _case_status(case: dict[str, Any]) -> str:
    if case.get("violations"):
        return "fail"
    if case.get("inconclusive"):
        return "inconclusive"
    return "pass"


def _finding_keys(report: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for case in report.get("cases", []):
        if not isinstance(case, dict):
            continue
        for violation in case.get("violations", []) or []:
            if isinstance(violation, dict):
                findings.append(
                    {
                        "caseId": str(case.get("id")),
                        "tool": case.get("tool"),
                        "code": str(violation.get("code", "UNKNOWN")),
                    }
                )
    return findings


def _observers(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for observer in report.get("observers", []) or []:
        if isinstance(observer, dict) and observer.get("id") is not None:
            result[str(observer["id"])] = {
                "kind": observer.get("kind"),
                "world": observer.get("world"),
                "sensitive": bool(observer.get("sensitive", True)),
            }
    return result


def _policy_codes(report: dict[str, Any]) -> set[str]:
    return {
        str(violation.get("code", "POLICY_FAILURE"))
        for violation in report.get("policyViolations", []) or []
        if isinstance(violation, dict)
    }


def _effect_shape(delta: dict[str, Any]) -> dict[str, Any]:
    counts = delta.get("counts") if isinstance(delta.get("counts"), dict) else {}
    return {
        "changed": bool(delta.get("changed", False)),
        "destructive": bool(delta.get("destructive", False)),
        "created": int(counts.get("created", 0) or 0),
        "modified": int(counts.get("modified", 0) or 0),
        "deleted": int(counts.get("deleted", 0) or 0),
    }


def _effect_changes(
    before: dict[str, Any], after: dict[str, Any]
) -> list[dict[str, Any]]:
    """Which observer saw a different effect on the same call between runs."""
    changes: list[dict[str, Any]] = []
    for call in CALL_KINDS:
        left_deltas = _deltas(before, call)
        right_deltas = _deltas(after, call)
        for observer_id in sorted(set(left_deltas) | set(right_deltas)):
            left_shape = left_deltas.get(observer_id, _empty_shape())
            right_shape = right_deltas.get(observer_id, _empty_shape())
            if left_shape != right_shape:
                changes.append(
                    {
                        "observer": observer_id,
                        "call": call,
                        "left": left_shape,
                        "right": right_shape,
                    }
                )
    return changes


def _deltas(case: dict[str, Any], call: str) -> dict[str, dict[str, Any]]:
    deltas = case.get("deltas") if isinstance(case.get("deltas"), dict) else {}
    items = deltas.get(call) if isinstance(deltas.get(call), list) else []
    return {
        str(item["observer_id"]): _effect_shape(item)
        for item in items
        if isinstance(item, dict) and item.get("observer_id") is not None
    }


def _empty_shape() -> dict[str, Any]:
    return {"changed": False, "destructive": False, "created": 0, "modified": 0, "deleted": 0}
