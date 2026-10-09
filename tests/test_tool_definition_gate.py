"""The offline gate must not silently accept new upstream findings."""

import runpy
from pathlib import Path

import pytest

_GATE = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check_tool_definitions.py"))
check_report = _GATE["check_report"]


def clean_report():
    return {
        "specVersion": "1.2",
        "server": {"name": "local-default", "toolCount": 13},
        "tools": [
            {"name": f"tool_{i}", "flags": [], "contextSignals": {"schemaDescriptionCoverage": 100}}
            for i in range(13)
        ],
        "findings": [],
        "shadowCandidates": [],
    }


def reviewed_report():
    report = clean_report()
    assert check_report("local-default", report) == 0
    report["shadowCandidates"] = [
        {
            "tool": "read_report",
            "cheaperSibling": "get_review",
            "invocationCost": 6,
            "cheaperSiblingInvocationCost": 2,
        }
    ]
    report["findings"] = [
        {
            "rule": "shadow-candidate",
            "severity": "warning",
            "tool": "read_report",
            "message": "read_report (invocation cost 6) may be shadowed by get_review (cost 2): reviewed",
        }
    ]
    assert check_report("local-default", report) == 1
    return report


def test_clean_report_and_known_reviewed_candidate():
    reviewed_report()


@pytest.mark.parametrize("change", ["missing", "duplicate", "sibling", "cost"])
def test_gate_rejects_inconsistent_shadow_findings(change):
    report = reviewed_report()
    if change == "missing":
        report["findings"] = []
    elif change == "duplicate":
        report["findings"] *= 2
    elif change == "sibling":
        report["findings"][0]["message"] = report["findings"][0]["message"].replace(
            "get_review", "unreviewed_tool"
        )
    else:
        report["findings"][0]["message"] = report["findings"][0]["message"].replace(
            "cost 2", "cost 1"
        )
    with pytest.raises(ValueError):
        check_report("local-default", report)


@pytest.mark.parametrize(
    "change", ["warning", "flag", "coverage", "duplicate", "spec", "shadow", "hidden"]
)
def test_gate_rejects_regressions_and_unreviewed_findings(change):
    report = clean_report()
    if change == "warning":
        report["findings"].append(
            {"rule": "undocumented-parameters", "severity": "warning", "tool": "tool_0"}
        )
    elif change == "flag":
        report["tools"][0]["flags"] = ["No Description"]
    elif change == "coverage":
        report["tools"][0]["contextSignals"]["schemaDescriptionCoverage"] = 99
    elif change == "duplicate":
        report["tools"][0]["name"] = "tool_1"
    elif change == "spec":
        report["specVersion"] = "future"
    elif change == "shadow":
        report["shadowCandidates"] = [
            {
                "tool": "read_report",
                "cheaperSibling": "new_tool",
                "invocationCost": 6,
                "cheaperSiblingInvocationCost": 1,
            }
        ]
    else:
        report["tools"][0]["findings"] = [{"rule": "undocumented-parameters"}]
    with pytest.raises(ValueError):
        check_report("local-default", report)
