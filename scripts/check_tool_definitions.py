"""Gate upstream TDQS lint without turning heuristic candidates into score claims."""

import argparse
import json
from pathlib import Path

from tdqs.lint_server import lint_server
from tdqs.parse_tool_definitions import parse_tool_definitions

PROFILES = {"local-default": 13, "local-all-opt-ins": 41, "self-host": 46}
# Reviewed in docs/tool-definition-quality.md. Keep raw upstream reports intact.
SHADOW_DISPOSITIONS = {
    "local-default": {("read_report", "get_review", 6, 2)},
    "local-all-opt-ins": {("read_report", "apply_account_access", 6, 2)},
    "self-host": {
        ("begin_upload_transfer", "describe_api_method", 5, 1),
        ("read_report", "apply_account_access", 6, 2),
    },
}


def check_report(profile: str, report: dict, definitions: dict) -> int:
    """Bind the report to the exported tools, then reject unreviewed lint findings."""
    if profile not in PROFILES:
        raise ValueError("Unknown tool-definition profile")
    tools = parse_tool_definitions(definitions).tools
    if len(tools) != PROFILES[profile]:
        raise ValueError(f"{profile}: unexpected exported tool count")
    # Lint is a pure, offline calculation. Reproduce every finding, input hash,
    # coverage signal and candidate instead of trusting report summary fields.
    expected = lint_server(server_name=profile, tools=tools).model_dump(by_alias=True)
    if expected["specVersion"] != "1.2":
        raise ValueError("Review the changed TDQS specification before accepting this report.")
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("generatedAt"), str)
        or not report["generatedAt"].strip()
    ):
        raise ValueError(f"{profile}: missing report generation timestamp")
    # The timestamp is the only nondeterministic field. Compare serialized JSON
    # so Python's bool/int and int/float equality cannot conceal malformed types.
    expected["generatedAt"] = report["generatedAt"]
    if json.dumps(report, sort_keys=True, allow_nan=False) != json.dumps(
        expected, sort_keys=True, allow_nan=False
    ):
        raise ValueError(f"{profile}: report does not match the exported tool definitions")
    for tool in expected["tools"]:
        if tool["flags"] or tool["contextSignals"]["schemaDescriptionCoverage"] != 100:
            raise ValueError(f"{profile}/{tool['name']}: flags or missing parameter descriptions")
    candidates = expected["shadowCandidates"]
    keys = {
        (c["tool"], c["cheaperSibling"], c["invocationCost"], c["cheaperSiblingInvocationCost"])
        for c in candidates
    }
    if not keys <= SHADOW_DISPOSITIONS[profile]:
        raise ValueError(f"{profile}: new or changed shadow candidate requires review")
    if any(finding["rule"] != "shadow-candidate" for finding in expected["findings"]):
        raise ValueError(f"{profile}: unresolved upstream lint finding")
    return len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args()
    for profile, count in PROFILES.items():
        report = json.loads((args.reports_dir / f"{profile}-lint.json").read_text())
        definitions = json.loads((args.reports_dir / f"{profile}.json").read_text())
        dispositions = check_report(profile, report, definitions)
        print(
            f"{profile}: {count} tools; 100% parameter descriptions; "
            f"{dispositions} reviewed structural candidates; no unresolved lint findings"
        )
    print("Offline definition gate passed. This is not a model-scored TDQS grade.")


if __name__ == "__main__":
    main()
