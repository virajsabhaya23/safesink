from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).parents[1]
SCHEMAS = ROOT / "safesink" / "schemas"
V1 = "mcp-manifest-v1.schema.json"
V2 = "mcp-manifest-v2.schema.json"


def load_generator() -> ModuleType:
    """Import ``scripts/schema_gen.py``, which is a script, not a package."""
    spec = importlib.util.spec_from_file_location(
        "schema_gen", ROOT / "scripts" / "schema_gen.py"
    )
    if spec is None or spec.loader is None:
        raise AssertionError("scripts/schema_gen.py could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ManifestSchemaDerivationTests(unittest.TestCase):
    """The v2 files must stay a generated derivative of v1, not a second copy."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.generator = load_generator()
        cls.v1 = json.loads((SCHEMAS / V1).read_text(encoding="utf-8"))
        cls.derived = cls.generator.derive_v2(cls.v1)

    def committed(self, package: str, name: str) -> dict:
        path = ROOT / package / "schemas" / name
        return json.loads(path.read_text(encoding="utf-8"))

    def test_both_committed_v2_copies_are_the_derived_schema(self) -> None:
        for package in ("safesink", "effectfence"):
            with self.subTest(package=package):
                self.assertEqual(self.committed(package, V2), self.derived)

    def test_the_shipped_v2_files_are_the_generator_output_byte_for_byte(self) -> None:
        expected = self.generator.render(self.derived)
        for package in ("safesink", "effectfence"):
            with self.subTest(package=package):
                self.assertEqual(
                    (ROOT / package / "schemas" / V2).read_text(encoding="utf-8"),
                    expected,
                )

    def test_v2_adds_only_the_case_timeout_and_identity(self) -> None:
        """A v2-only rule that is not in the derivation would be hand-written drift."""
        v2 = copy.deepcopy(self.derived)
        self.assertEqual(
            v2["$defs"]["case"]["properties"].pop("timeoutSeconds"),
            self.generator.V2_CASE_TIMEOUT,
        )
        v1 = copy.deepcopy(self.v1)
        for schema in (v1, v2):
            schema.pop("$id")
            schema.pop("title")
            schema["properties"]["schemaVersion"].pop("const")
        self.assertEqual(v2, v1)

    def test_deriving_leaves_the_v1_schema_unchanged(self) -> None:
        before = copy.deepcopy(self.v1)
        self.generator.derive_v2(self.v1)
        self.assertEqual(self.v1, before)

    def test_the_generator_reports_the_committed_copies_as_current(self) -> None:
        self.assertEqual(self.generator.stale_copies(), [])


if __name__ == "__main__":
    unittest.main()
