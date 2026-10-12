"""Adversarial acceptance of actual upstream lint and exported SDK definitions."""

import asyncio
import copy
import json
import runpy
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from tdqs.lint_server import lint_server
from tdqs.parse_tool_definitions import parse_tool_definitions
from tdqs.tdqs_input_error import TdqsInputError

_ROOT = Path(__file__).parents[1]
_GATE_PATH = _ROOT / "scripts/check_tool_definitions.py"
_GATE = runpy.run_path(str(_GATE_PATH))
check_report = _GATE["check_report"]


def report_for(profile, definitions):
    return lint_server(
        server_name=profile, tools=parse_tool_definitions(definitions).tools
    ).model_dump(by_alias=True)


@pytest.fixture(scope="module")
def actual_definitions():
    exporter = runpy.run_path(str(_ROOT / "scripts/export_tool_definitions.py"))
    return asyncio.run(exporter["export_definitions"]())


@pytest.mark.parametrize(
    ("profile", "candidates"),
    [("local-default", 1), ("local-all-opt-ins", 1), ("self-host", 2)],
)
def test_actual_upstream_reports_match_all_profiles(actual_definitions, profile, candidates):
    definitions = actual_definitions[profile]
    report = report_for(profile, definitions)
    assert check_report(profile, report, definitions) == candidates
    report["generatedAt"] = "2026-10-01T00:00:00.000Z"
    assert check_report(profile, report, definitions) == candidates


# Each pair removes or changes a field that the former summary-only gate trusted.
MALFORMED_FIELDS = [
    ((), None),
    ((), []),
    (("generatedAt",), None),
    (("generatedAt",), ""),
    (("generatedAt",), "  "),
    (("generatedAt",), 1),
    (("specVersion",), "future"),
    (("server", "name"), "wrong-profile"),
    (("server", "toolCount"), 13.0),
    (("tools",), []),
    (("tools",), None),
    (("tools", 0, "name"), "substituted-tool"),
    (("tools", 0, "flags"), None),
    (("tools", 0, "flags"), False),
    (("tools", 0, "flags"), ["No Description"]),
    (("tools", 0, "findings"), None),
    (("tools", 0, "findings"), [{"rule": "undocumented-parameters"}]),
    (("tools", 0, "contextSignals"), {}),
    (("tools", 0, "contextSignals", "schemaDescriptionCoverage"), 99),
    (("tools", 0, "contextSignals", "schemaDescriptionCoverage"), 100.0),
    (("tools", 0, "contextSignals", "paramCount"), 0),
    (("tools", 0, "contextSignals", "inputHash"), "stale-input"),
    (("findings",), []),
    (("findings",), None),
    (("findings", 0, "severity"), "info"),
    (("findings", 0, "rule"), "no-output-schema"),
    (("findings", 0, "message"), "a substituted message"),
    (("findings", 0, "tool"), "missing-tool"),
    (("shadowCandidates",), []),
    (("shadowCandidates",), None),
    (("shadowCandidates", 0, "tool"), "missing-tool"),
    (("shadowCandidates", 0, "cheaperSibling"), "missing-sibling"),
    (("shadowCandidates", 0, "invocationCost"), 999),
    (("shadowCandidates", 0, "cheaperSiblingInvocationCost"), True),
]


@pytest.mark.parametrize("profile", tuple(_GATE["PROFILES"]))
@pytest.mark.parametrize(("path", "value"), MALFORMED_FIELDS)
def test_rejects_mutated_report(actual_definitions, profile, path, value):
    definitions = actual_definitions[profile]
    report = report_for(profile, definitions)
    if not path:
        report = value
    else:
        container = report
        for part in path[:-1]:
            container = container[part]
        container[path[-1]] = value
    with pytest.raises(ValueError):
        check_report(profile, report, definitions)


@pytest.mark.parametrize("profile", tuple(_GATE["PROFILES"]))
@pytest.mark.parametrize(
    "path",
    [
        ("generatedAt",),
        ("findings",),
        ("shadowCandidates",),
        ("tools", 0, "flags"),
        ("tools", 0, "findings"),
        ("tools", 0, "contextSignals", "inputHash"),
    ],
)
def test_rejects_missing_required_fields(actual_definitions, profile, path):
    definitions = actual_definitions[profile]
    report = report_for(profile, definitions)
    container = report
    for part in path[:-1]:
        container = container[part]
    del container[path[-1]]
    with pytest.raises(ValueError):
        check_report(profile, report, definitions)


@pytest.mark.parametrize("profile", tuple(_GATE["PROFILES"]))
@pytest.mark.parametrize("change", ["omissions", "duplicates", "reorder", "extra", "stale"])
def test_rejects_consistent_looking_but_unbound_report(actual_definitions, profile, change):
    definitions = copy.deepcopy(actual_definitions[profile])
    report = report_for(profile, definitions)
    if change == "omissions":
        del report["findings"]
        del report["shadowCandidates"]
        for tool in report["tools"]:
            del tool["findings"]
    elif change == "duplicates":
        report["tools"][1] = copy.deepcopy(report["tools"][0])
    elif change == "reorder":
        report["tools"].reverse()
    elif change == "extra":
        report["unknownDiagnostic"] = {"severity": "error"}
    else:
        definitions["tools"][0]["description"] += " New input guidance."
    with pytest.raises(ValueError):
        check_report(profile, report, definitions)


def clean_definitions():
    return {
        "tools": [
            {
                "name": f"tool_{i}",
                "description": "Retrieve the saved review identified by its opaque identifier.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "Saved review ID."}},
                    "required": ["id"],
                },
                "outputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            }
            for i in range(13)
        ]
    }


def test_clean_no_candidate_report():
    definitions = clean_definitions()
    assert check_report("local-default", report_for("local-default", definitions), definitions) == 0


@pytest.mark.parametrize("change", ["description", "coverage", "annotations", "output", "shadow"])
def test_rejects_authentic_lint_for_regressed_definitions(change):
    definitions = clean_definitions()
    tool = definitions["tools"][0]
    if change == "description":
        tool["description"] = ""
    elif change == "coverage":
        del tool["inputSchema"]["properties"]["id"]["description"]
    elif change == "annotations":
        del tool["annotations"]
    elif change == "output":
        del tool["outputSchema"]
    else:
        tool["inputSchema"]["properties"] = {
            f"id_{i}": {"type": "string", "description": "Saved review ID."} for i in range(6)
        }
        tool["inputSchema"]["required"] = list(tool["inputSchema"]["properties"])
    with pytest.raises(ValueError):
        check_report("local-default", report_for("local-default", definitions), definitions)


def test_unknown_profile_and_definition_count():
    definitions = clean_definitions()
    with pytest.raises(ValueError, match="Unknown"):
        check_report("unknown", {}, definitions)
    definitions["tools"].pop()
    with pytest.raises(ValueError, match="tool count"):
        check_report("local-default", {}, definitions)


@pytest.mark.parametrize("change", ["empty", "duplicate", "malformed"])
def test_rejects_invalid_definition_payload(change):
    definitions = clean_definitions()
    if change == "empty":
        definitions["tools"] = []
    elif change == "duplicate":
        definitions["tools"][1] = definitions["tools"][0]
    else:
        definitions = None
    with pytest.raises(TdqsInputError):
        check_report("local-default", {}, definitions)


def test_future_upstream_spec_requires_review():
    definitions = clean_definitions()
    linted = lint_server(
        server_name="local-default", tools=parse_tool_definitions(definitions).tools
    )
    linted.spec_version = "future"
    with patch.dict(check_report.__globals__, lint_server=lambda **kwargs: linted):
        with pytest.raises(ValueError, match="changed TDQS specification"):
            check_report("local-default", {}, definitions)


def test_cli_binds_all_reports_to_adjacent_exports(
    actual_definitions, tmp_path, monkeypatch, capsys
):
    for profile, definitions in actual_definitions.items():
        (tmp_path / f"{profile}.json").write_text(json.dumps(definitions))
        (tmp_path / f"{profile}-lint.json").write_text(json.dumps(report_for(profile, definitions)))
    monkeypatch.setattr(sys, "argv", [str(_GATE_PATH), "--reports-dir", str(tmp_path)])
    runpy.run_path(str(_GATE_PATH), run_name="__main__")
    output = capsys.readouterr().out
    for profile, count in _GATE["PROFILES"].items():
        assert f"{profile}: {count} tools; 100% parameter descriptions" in output
    assert "This is not a model-scored TDQS grade." in output
    (tmp_path / "local-default.json").unlink()
    with pytest.raises(FileNotFoundError):
        _GATE["main"]()
