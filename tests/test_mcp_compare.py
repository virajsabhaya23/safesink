from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from safesink.cli import main as cli_main
from safesink.mcp_compare import CompareError, compare_reports, load_report


def report(
    *,
    verdict: str = "pass",
    score: int = 100,
    cases: list | None = None,
    policy: list | None = None,
    fatal: dict | None = None,
    version: str = "1.0.0",
) -> dict:
    return {
        "schemaVersion": "effectfence.mcp.report.v1",
        "verdict": verdict,
        "generatedAt": "2026-11-24T00:00:00Z",
        "server": {"name": "fixture-server", "version": version},
        "score": {"value": score, "version": "1"},
        "cases": cases or [],
        "policyViolations": policy or [],
        "fatalError": fatal,
        "certificateSha256": "a" * 64,
    }


def case(
    case_id: str,
    tool: str,
    *,
    violations: tuple[str, ...] = (),
    inconclusive: tuple[str, ...] = (),
    deltas: dict | None = None,
) -> dict:
    return {
        "id": case_id,
        "tool": tool,
        "violations": [{"code": code} for code in violations],
        "inconclusive": [{"code": code} for code in inconclusive],
        "deltas": deltas or {"firstCall": [], "retry": []},
        "passed": not violations and not inconclusive,
    }


def delta(observer: str, *, changed: bool, destructive: bool = False, counts: dict | None = None) -> dict:
    return {
        "observer_id": observer,
        "kind": "filesystem",
        "changed": changed,
        "destructive": destructive,
        "counts": counts or {"created": 0, "modified": 0, "deleted": 0},
    }


class CompareReportTests(unittest.TestCase):
    def test_a_new_finding_is_a_regression(self) -> None:
        left = report(cases=[case("create-note", "create_note")])
        right = report(
            verdict="fail",
            score=75,
            cases=[
                case(
                    "create-note",
                    "create_note",
                    violations=("READ_ONLY_HINT_MISMATCH",),
                )
            ],
        )
        comparison = compare_reports(left, right)
        self.assertTrue(comparison["regression"])
        self.assertTrue(comparison["verdict"]["regressed"])
        self.assertEqual(comparison["verdict"]["left"], "pass")
        self.assertEqual(comparison["verdict"]["right"], "fail")
        self.assertEqual(comparison["score"]["delta"], -25)
        self.assertEqual(
            [(item["caseId"], item["code"]) for item in comparison["findings"]["added"]],
            [("create-note", "READ_ONLY_HINT_MISMATCH")],
        )
        self.assertIn(
            "NEW_FINDING create-note:READ_ONLY_HINT_MISMATCH", comparison["summary"]
        )

    def test_a_fixed_finding_is_reported_but_is_not_a_regression(self) -> None:
        left = report(
            verdict="fail",
            score=75,
            cases=[case("delete-note", "delete_note", violations=("NON_DESTRUCTIVE_HINT_MISMATCH",))],
        )
        right = report(cases=[case("delete-note", "delete_note")])
        comparison = compare_reports(left, right)
        self.assertFalse(comparison["regression"])
        self.assertEqual(
            [item["code"] for item in comparison["findings"]["removed"]],
            ["NON_DESTRUCTIVE_HINT_MISMATCH"],
        )
        self.assertIn(
            "FINDING_FIXED delete-note:NON_DESTRUCTIVE_HINT_MISMATCH",
            comparison["summary"],
        )

    def test_the_two_sides_are_identified_by_version(self) -> None:
        comparison = compare_reports(
            report(version="1.0.0"), report(version="2.0.0"), left_path="a.json"
        )
        self.assertEqual(comparison["left"]["version"], "1.0.0")
        self.assertEqual(comparison["right"]["version"], "2.0.0")
        self.assertEqual(comparison["left"]["path"], "a.json")
        self.assertIsNone(comparison["right"]["path"])

    def test_which_observed_effect_changed_is_reported(self) -> None:
        left = report(
            cases=[
                case(
                    "update-row",
                    "update_row",
                    deltas={
                        "firstCall": [
                            delta(
                                "sandbox",
                                changed=True,
                                counts={"created": 0, "modified": 1, "deleted": 0},
                            )
                        ],
                        "retry": [],
                    },
                )
            ]
        )
        right = report(
            cases=[
                case(
                    "update-row",
                    "update_row",
                    deltas={
                        "firstCall": [
                            delta(
                                "sandbox",
                                changed=True,
                                destructive=True,
                                counts={"created": 0, "modified": 0, "deleted": 1},
                            )
                        ],
                        "retry": [],
                    },
                )
            ]
        )
        comparison = compare_reports(left, right)
        self.assertFalse(comparison["regression"])
        self.assertEqual(len(comparison["effects"]), 1)
        effect = comparison["effects"][0]
        self.assertEqual(effect["caseId"], "update-row")
        change = effect["observers"][0]
        self.assertEqual(change["observer"], "sandbox")
        self.assertEqual(change["left"]["modified"], 1)
        self.assertEqual(change["right"]["deleted"], 1)
        self.assertTrue(change["right"]["destructive"])
        self.assertIn("EFFECT_CHANGED update-row", comparison["summary"])

    def test_an_effect_that_appears_on_one_side_is_reported(self) -> None:
        left = report(cases=[case("read", "read_note")])
        right = report(
            cases=[
                case(
                    "read",
                    "read_note",
                    deltas={"firstCall": [delta("sandbox", changed=True)], "retry": []},
                )
            ]
        )
        comparison = compare_reports(left, right)
        self.assertEqual(len(comparison["effects"]), 1)
        change = comparison["effects"][0]["observers"][0]
        self.assertFalse(change["left"]["changed"])
        self.assertTrue(change["right"]["changed"])

    def test_a_missing_unchanged_delta_is_not_an_effect_change(self) -> None:
        left = report(
            cases=[
                case(
                    "read",
                    "read_note",
                    deltas={"firstCall": [delta("sandbox", changed=False)], "retry": []},
                )
            ]
        )
        right = report(cases=[case("read", "read_note")])
        self.assertEqual(compare_reports(left, right)["effects"], [])

    def test_a_changed_observed_boundary_is_reported(self) -> None:
        left = report()
        left["observers"] = [
            {"id": "sandbox", "kind": "filesystem", "world": "closed", "sensitive": True}
        ]
        right = report()
        right["observers"] = [
            {"id": "orders", "kind": "sqlite-query", "world": "closed", "sensitive": True}
        ]
        comparison = compare_reports(left, right)
        self.assertEqual(comparison["boundary"]["added"], ["orders"])
        self.assertEqual(comparison["boundary"]["removed"], ["sandbox"])
        self.assertIn("OBSERVED_BOUNDARY_CHANGED", comparison["summary"])
        self.assertFalse(comparison["regression"])

    def test_added_and_removed_cases_are_reported(self) -> None:
        left = report(cases=[case("a", "read_a"), case("b", "read_b")])
        right = report(cases=[case("b", "read_b"), case("c", "read_c")])
        comparison = compare_reports(left, right)
        self.assertEqual(comparison["cases"]["added"], ["c"])
        self.assertEqual(comparison["cases"]["removed"], ["a"])
        self.assertIn("CASES_ADDED 1", comparison["summary"])
        self.assertIn("CASES_REMOVED 1", comparison["summary"])

    def test_a_status_change_is_reported(self) -> None:
        left = report(cases=[case("read", "read_note", inconclusive=("NO_DECLARED_ANNOTATIONS",))])
        right = report(cases=[case("read", "read_note")])
        comparison = compare_reports(left, right)
        self.assertEqual(
            comparison["cases"]["statusChanges"],
            [
                {
                    "caseId": "read",
                    "tool": "read_note",
                    "left": "inconclusive",
                    "right": "pass",
                }
            ],
        )

    def test_a_new_fatal_error_is_a_regression(self) -> None:
        comparison = compare_reports(
            report(),
            report(verdict="fail", score=75, fatal={"message": "boom", "phase": "startup"}),
        )
        self.assertTrue(comparison["regression"])
        self.assertIn("FATAL_ERROR_ADDED", comparison["summary"])

    def test_a_new_policy_violation_is_a_regression(self) -> None:
        comparison = compare_reports(
            report(),
            report(
                verdict="fail",
                policy=[{"code": "TOOL_COVERAGE_BELOW_MINIMUM", "message": "..."}],
            ),
        )
        self.assertTrue(comparison["regression"])
        self.assertIn(
            "POLICY_VIOLATION_ADDED TOOL_COVERAGE_BELOW_MINIMUM", comparison["summary"]
        )

    def test_a_score_drop_alone_is_reported_but_not_a_regression(self) -> None:
        left = report(verdict="inconclusive", score=90, cases=[case("a", "read_a")])
        right = report(verdict="inconclusive", score=80, cases=[case("a", "read_a")])
        comparison = compare_reports(left, right)
        self.assertFalse(comparison["regression"])
        self.assertEqual(comparison["score"]["delta"], -10)
        self.assertIn("SCORE_DROPPED 10", comparison["summary"])

    def test_an_identical_report_has_no_changes(self) -> None:
        same = report(cases=[case("a", "read_a", deltas={"firstCall": [delta("s", changed=False)], "retry": []})])
        comparison = compare_reports(same, same)
        self.assertFalse(comparison["regression"])
        self.assertEqual(comparison["summary"], [])
        self.assertEqual(comparison["effects"], [])

    def test_load_report_rejects_a_non_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"hello": "world"}', encoding="utf-8")
            with self.assertRaises(CompareError):
                load_report(path)
            path.write_text("{", encoding="utf-8")
            with self.assertRaises(CompareError):
                load_report(path)


class CompareCliTests(unittest.TestCase):
    def write(self, root: Path, name: str, value: dict) -> str:
        path = root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def test_a_regression_exits_2_and_writes_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = self.write(root, "left.json", report(cases=[case("a", "create_note")]))
            right = self.write(
                root,
                "right.json",
                report(
                    verdict="fail",
                    score=75,
                    cases=[case("a", "create_note", violations=("READ_ONLY_HINT_MISMATCH",))],
                ),
            )
            markdown = root / "diff.md"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                exit_code = cli_main(["compare", left, right, "--markdown", str(markdown)])
            self.assertEqual(exit_code, 2)
            self.assertIn("regression: yes", stdout.getvalue())
            text = markdown.read_text(encoding="utf-8")
            self.assertIn("REGRESSION", text)
            self.assertIn("READ_ONLY_HINT_MISMATCH", text)
            self.assertIn("1.0.0", text)

    def test_no_regression_exits_0_and_json_has_the_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = self.write(root, "left.json", report(version="1.0.0"))
            right = self.write(root, "right.json", report(version="2.0.0"))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
                exit_code = cli_main(["compare", left, right, "--json"])
            self.assertEqual(exit_code, 0)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["schemaVersion"], "effectfence.mcp.compare.v1")
            self.assertFalse(payload["regression"])

    def test_max_score_drop_turns_a_score_drop_into_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            left = self.write(root, "left.json", report(verdict="inconclusive", score=90))
            right = self.write(root, "right.json", report(verdict="inconclusive", score=70))
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
                io.StringIO()
            ):
                self.assertEqual(cli_main(["compare", left, right]), 0)
                self.assertEqual(
                    cli_main(["compare", left, right, "--max-score-drop", "5"]), 2
                )
                self.assertEqual(
                    cli_main(["compare", left, right, "--max-score-drop", "30"]), 0
                )

    def test_an_unreadable_report_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            good = self.write(root, "good.json", report())
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as refused:
                    cli_main(["compare", good, str(root / "missing.json")])
            self.assertEqual(refused.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
