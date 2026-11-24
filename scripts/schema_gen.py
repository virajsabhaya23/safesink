"""Derive ``mcp-manifest-v2.schema.json`` from the v1 schema.

v2 is the v1 document plus ``cases[].timeoutSeconds``, so the shared rules are
written once, in :func:`derive_v2`, and never copied by hand. The unit suite
(``tests/test_manifest_schemas.py``) compares both committed v2 copies against
that function's output: an edit to v1 that is not reflected in v2, or a v2 rule
that was never added to the derivation, fails the suite. Rewrite the copies
after an intentional change:

    python3 scripts/schema_gen.py          # rewrite both v2 copies
    python3 scripts/schema_gen.py --check  # exit 1 when a copy is stale
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIRS = (ROOT / "safesink" / "schemas", ROOT / "effectfence" / "schemas")
V1_NAME = "mcp-manifest-v1.schema.json"
V2_NAME = "mcp-manifest-v2.schema.json"
V1_SCHEMA_VERSION = "effectfence.mcp.v1"
V2_SCHEMA_VERSION = "effectfence.mcp.v2"

# The only rule v2 adds: every other server, observer, policy and case rule is
# inherited from v1 unchanged, which is what keeps v1 manifests valid.
V2_CASE_TIMEOUT = {
    "type": "number",
    "exclusiveMinimum": 0,
    "description": (
        "Per-call timeout for this case's tools/call requests; v2 only, so v1 "
        "manifests stay valid unchanged."
    ),
}


def load(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: a schema must be a JSON object")
    return data


def derive_v2(v1: dict[str, Any]) -> dict[str, Any]:
    """The v2 schema, derived from a v1 schema document without mutating it."""
    declared = v1.get("properties", {}).get("schemaVersion", {}).get("const")
    if declared != V1_SCHEMA_VERSION:
        raise ValueError(f"derive_v2 needs a {V1_SCHEMA_VERSION} schema, not {declared!r}")
    v2 = copy.deepcopy(v1)
    v2["$id"] = str(v1["$id"]).replace("mcp-manifest-v1", "mcp-manifest-v2")
    v2["title"] = str(v1["title"]).replace("manifest v1", "manifest v2")
    v2["properties"]["schemaVersion"]["const"] = V2_SCHEMA_VERSION
    v2["$defs"]["case"]["properties"]["timeoutSeconds"] = copy.deepcopy(V2_CASE_TIMEOUT)
    return v2


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def derived_v2() -> dict[str, Any]:
    return derive_v2(load(SCHEMA_DIRS[0] / V1_NAME))


def stale_copies() -> list[Path]:
    """The committed v2 copies that no longer match the v1 derivative."""
    expected = render(derived_v2())
    stale = []
    for directory in SCHEMA_DIRS:
        path = directory / V2_NAME
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            stale.append(path)
    return stale


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if "--check" in arguments:
        stale = stale_copies()
        if stale:
            print(
                "\n".join(
                    f"{path}: stale, run python3 scripts/schema_gen.py" for path in stale
                )
            )
            return 1
        print("manifest schema v2 matches the v1 derivative: PASS")
        return 0
    expected = render(derived_v2())
    for directory in SCHEMA_DIRS:
        (directory / V2_NAME).write_text(expected, encoding="utf-8")
    print("wrote " + ", ".join(str(directory / V2_NAME) for directory in SCHEMA_DIRS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
