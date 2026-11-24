from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from safesink.cli import _manifest_schema, build_parser
from safesink.mcp_scan import build_manifest
from safesink.mcp_verifier import (
    MANIFEST_SCHEMA,
    MANIFEST_SCHEMA_V2,
    ManifestError,
    load_manifest,
    verify_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "examples" / "mcp-conformance"

READ_TOOL = {
    "name": "read_note",
    "annotations": {"readOnlyHint": True},
    "inputSchema": {"type": "object", "properties": {}},
}

SLOW_SERVER = '''
import json
import sys
import time


def send(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "protocolVersion": "2025-11-25",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "slow-server", "version": "1.0.0"},
        }})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": [{
            "name": "slow_read",
            "annotations": {"readOnlyHint": True},
            "inputSchema": {"type": "object", "properties": {}},
        }]}})
    elif method == "tools/call":
        time.sleep(1.0)
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "content": [{"type": "text", "text": "too late"}],
            "isError": False,
        }})
    elif request.get("id") is not None:
        send({"jsonrpc": "2.0", "id": request["id"], "result": {}})
'''


def write_manifest(root: Path, manifest: dict) -> Path:
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def base_manifest(**case: object) -> dict:
    return {
        "schemaVersion": MANIFEST_SCHEMA,
        "server": {"transport": "stdio", "command": ["server"]},
        "observers": [{"id": "sandbox", "kind": "filesystem", "root": ".", "sensitive": False}],
        "cases": [{"id": "read", "tool": "read_note", "arguments": {}, **case}],
    }


class ManifestVersionTests(unittest.TestCase):
    def test_v2_is_emitted_on_request_and_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_manifest(
                [READ_TOOL],
                command=["server"],
                observer_root=directory,
                schema_version=MANIFEST_SCHEMA_V2,
            )
            self.assertEqual(manifest["schemaVersion"], MANIFEST_SCHEMA_V2)
            loaded, _ = load_manifest(write_manifest(Path(directory), manifest))
        self.assertEqual(loaded["schemaVersion"], MANIFEST_SCHEMA_V2)

    def test_v1_is_still_the_default_and_still_loads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = build_manifest(
                [READ_TOOL], command=["server"], observer_root=directory
            )
            self.assertEqual(manifest["schemaVersion"], MANIFEST_SCHEMA)
            loaded, _ = load_manifest(write_manifest(Path(directory), manifest))
        self.assertEqual(loaded["schemaVersion"], MANIFEST_SCHEMA)

    def test_a_per_case_timeout_requires_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(Path(directory), base_manifest(timeoutSeconds=5))
            with self.assertRaises(ManifestError) as raised:
                load_manifest(path)
        self.assertIn(MANIFEST_SCHEMA_V2, str(raised.exception))
        self.assertIn("v1 manifests stay valid", str(raised.exception))

    def test_v2_rejects_a_non_positive_or_boolean_timeout(self) -> None:
        for value in (0, -1, True, "5"):
            with self.subTest(value=value):
                manifest = base_manifest(timeoutSeconds=value)
                manifest["schemaVersion"] = MANIFEST_SCHEMA_V2
                with tempfile.TemporaryDirectory() as directory:
                    path = write_manifest(Path(directory), manifest)
                    with self.assertRaises(ManifestError):
                        load_manifest(path)

    def test_an_unknown_schema_names_both_supported_versions(self) -> None:
        manifest = base_manifest()
        manifest["schemaVersion"] = "effectfence.mcp.v9"
        with tempfile.TemporaryDirectory() as directory:
            path = write_manifest(Path(directory), manifest)
            with self.assertRaises(ManifestError) as raised:
                load_manifest(path)
        self.assertIn(MANIFEST_SCHEMA, str(raised.exception))
        self.assertIn(MANIFEST_SCHEMA_V2, str(raised.exception))

    def test_the_cli_can_emit_either_schema(self) -> None:
        parser = build_parser()
        for command, tail in (
            (["mcp-check"], ["--", "server"]),
            (
                ["mcp-scan", "--observer-root", "sandbox", "--out", "m.json"],
                ["--", "server"],
            ),
            (["init", "--server", "server"], []),
        ):
            with self.subTest(command=command[0]):
                self.assertEqual(
                    parser.parse_args(command + tail).schema_version, "1"
                )
                args = parser.parse_args(
                    command + ["--schema-version", "2"] + tail
                )
                self.assertEqual(args.schema_version, "2")
        self.assertEqual(_manifest_schema("1"), MANIFEST_SCHEMA)
        self.assertEqual(_manifest_schema("2"), MANIFEST_SCHEMA_V2)


class PerCaseTimeoutTests(unittest.TestCase):
    def test_a_timeout_is_enforced_per_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sandbox").mkdir()
            (root / "slow_server.py").write_text(SLOW_SERVER, encoding="utf-8")
            manifest = {
                "schemaVersion": MANIFEST_SCHEMA_V2,
                "server": {
                    "transport": "stdio",
                    "command": [sys.executable, "slow_server.py"],
                    "cwd": ".",
                    "protocolVersion": "2025-11-25",
                },
                "observers": [
                    {"id": "sandbox", "kind": "filesystem", "root": "sandbox", "sensitive": False}
                ],
                "cases": [
                    {
                        "id": "slow-read",
                        "tool": "slow_read",
                        "arguments": {},
                        "timeoutSeconds": 0.25,
                    }
                ],
            }
            path = write_manifest(root, manifest)
            report = verify_manifest(path)
        self.assertEqual(report["verdict"], "fail")
        case = report["cases"][0]
        self.assertEqual(case["timeoutSeconds"], 0.25)
        self.assertEqual(case["violations"][0]["code"], "CASE_EXECUTION_FAILED")
        # The server sleeps for a second; a per-case timeout that reached the
        # client fails the call well before that.
        self.assertLess(case["durationMs"], 900)

    def test_a_v2_case_without_a_timeout_reports_none(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            shutil.copy(FIXTURES / "fixture_server.py", root / "fixture_server.py")
            shutil.copy(FIXTURES / "reset_fixture.py", root / "reset_fixture.py")
            manifest = json.loads((FIXTURES / "passing.json").read_text(encoding="utf-8"))
            manifest["schemaVersion"] = MANIFEST_SCHEMA_V2
            manifest["server"]["command"] = [sys.executable, "fixture_server.py"]
            manifest["server"]["cwd"] = "."
            path = write_manifest(root, manifest)
            report = verify_manifest(path)
        self.assertEqual(report["verdict"], "pass")
        self.assertNotIn("timeoutSeconds", report["cases"][0])


if __name__ == "__main__":
    unittest.main()
