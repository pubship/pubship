"""Gate upstream TDQS lint without turning heuristic candidates into score claims."""

import argparse
import json
from pathlib import Path

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


def check_report(profile: str, report: dict) -> int:
    """Reject new findings, missing coverage and unreviewed structural candidates."""
    if report.get("specVersion") != "1.2":
        raise ValueError("Review the changed TDQS specification before accepting this report.")
    if report.get("server") != {"name": profile, "toolCount": PROFILES[profile]}:
        raise ValueError(f"{profile}: unexpected server identity or tool count")
    tools = report.get("tools", [])
    if len(tools) != PROFILES[profile] or len({tool["name"] for tool in tools}) != len(tools):
        raise ValueError(f"{profile}: incomplete or duplicate tool results")
    for tool in tools:
        if tool.get("flags") or tool["contextSignals"]["schemaDescriptionCoverage"] != 100:
            raise ValueError(f"{profile}/{tool['name']}: flags or missing parameter descriptions")
    candidates = report.get("shadowCandidates", [])
    keys = {
        (c["tool"], c["cheaperSibling"], c["invocationCost"], c["cheaperSiblingInvocationCost"])
        for c in candidates
    }
    if len(keys) != len(candidates) or not keys <= SHADOW_DISPOSITIONS[profile]:
        raise ValueError(f"{profile}: new or changed shadow candidate requires review")
    expected_prefixes = {
        tool: f"{tool} (invocation cost {cost}) may be shadowed by {sibling} (cost {other}): "
        for tool, sibling, cost, other in keys
    }
    findings = report.get("findings", [])
    if len(findings) != len(keys) or len({f.get("tool") for f in findings}) != len(keys):
        raise ValueError(f"{profile}: inconsistent shadow-candidate findings")
    for finding in findings:
        if (
            finding.get("rule") != "shadow-candidate"
            or finding.get("severity") != "warning"
            or finding.get("tool") not in expected_prefixes
            or not finding.get("message", "").startswith(
                expected_prefixes.get(finding.get("tool"), "\0")
            )
        ):
            raise ValueError(f"{profile}: unresolved upstream lint finding")
    # Also inspect per-tool findings, so a malformed aggregate cannot hide a warning.
    for tool in tools:
        for finding in tool.get("findings", []):
            if finding not in report.get("findings", []):
                raise ValueError(f"{profile}/{tool['name']}: unreported per-tool finding")
    return len(candidates)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reports-dir", type=Path, required=True)
    args = parser.parse_args()
    for profile, count in PROFILES.items():
        report = json.loads((args.reports_dir / f"{profile}-lint.json").read_text())
        dispositions = check_report(profile, report)
        print(
            f"{profile}: {count} tools; 100% parameter descriptions; "
            f"{dispositions} reviewed structural candidates; no unresolved lint findings"
        )
    print("Offline definition gate passed. This is not a model-scored TDQS grade.")


if __name__ == "__main__":
    main()
