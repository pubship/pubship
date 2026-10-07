import asyncio
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from mcp import Client

from pubship.config import Settings
from pubship.contracts import describe_method
from pubship.errors import PlayError
from pubship.listing_preview import PATCH_METHOD, listing_preview, validate_preview
from pubship.server import create_server

PARAMETERS = {"packageName": "com.example.app", "editId": "synthetic-edit", "language": "en-US"}
SNAPSHOT = {"fetched_at": "2026-10-05T12:00:00+00:00", "data": {"language": "en-US"}}


def test_diff_preserves_missing_empty_unchanged_and_exact_proposed_text():
    snapshot = {**SNAPSHOT, "data": {"language": "en-US", "title": "", "fullDescription": "same"}}
    changes = {"title": "", "shortDescription": "  新 🐈\nline\r\n", "fullDescription": "same"}
    validate_preview(PARAMETERS, changes)
    preview = listing_preview(snapshot, PARAMETERS, changes)
    assert preview["changes"] == [
        {"field": "title", "before_present": True, "before": "", "after": "", "changed": False},
        {
            "field": "shortDescription",
            "before_present": False,
            "after": changes["shortDescription"],
            "changed": True,
        },
        {
            "field": "fullDescription",
            "before_present": True,
            "before": "same",
            "after": "same",
            "changed": False,
        },
    ]
    assert preview["proposed_request"] == {
        "method": PATCH_METHOD,
        "parameters": PARAMETERS,
        "body": changes,
    }
    assert preview["preview_only"] is True
    assert preview["data_scope"] == "edit_snapshot"
    assert preview["fetched_at"] == SNAPSHOT["fetched_at"]
    for flag in ("provider_validation_performed", "stale_check_performed", "executed"):
        assert preview[flag] is False
    assert "data" not in preview
    assert "after" not in preview
    assert snapshot["data"] == {"language": "en-US", "title": "", "fullDescription": "same"}


def test_missing_to_empty_is_a_change():
    assert listing_preview(SNAPSHOT, PARAMETERS, {"title": ""})["changes"] == [
        {"field": "title", "before_present": False, "after": "", "changed": True}
    ]


@pytest.mark.parametrize(
    "changes",
    [
        None,
        [],
        {},
        {"title": None},
        {"title": 1},
        {"title": True},
        {"title": []},
        {"title": "\ud800"},
        {"title": "\udfff"},
        {"video": "PRIVATE"},
        {"language": "PRIVATE"},
        {"title": "ok", "unknown": "PRIVATE"},
    ],
)
def test_invalid_changes_are_rejected_without_echoing_values(changes):
    with pytest.raises(PlayError) as error:
        validate_preview(PARAMETERS, changes)
    assert "PRIVATE" not in str(error.value)


def test_total_json_input_limit_includes_parameter_and_object_overhead():
    changes = {"fullDescription": "x" * 64_000}
    validate_preview(PARAMETERS, changes)
    changes["fullDescription"] = "x" * (64 * 1024)
    with pytest.raises(PlayError, match="64 KiB"):
        validate_preview(PARAMETERS, changes)
    # Bounds apply to the complete JSON, including escaped non-ASCII characters.
    with pytest.raises(PlayError, match="64 KiB"):
        validate_preview(PARAMETERS, {"fullDescription": "🐈" * 6000})


@pytest.mark.parametrize(
    "current",
    [
        None,
        [],
        {},
        {"language": "en-us"},
        {"language": "en-US", "title": None},
        {"language": "en-US", "shortDescription": []},
        {"language": "en-US", "fullDescription": "\ud800"},
        {"language": "en-US", "video": False},
    ],
)
def test_malformed_current_fields_never_become_missing_values(current):
    with pytest.raises(PlayError, match="unexpected listing"):
        listing_preview({**SNAPSHOT, "data": current}, PARAMETERS, {"title": "new"})


def test_preview_mcp_uses_exactly_one_get_without_a_body():
    async def exercise():
        response = Mock()
        response.read.return_value = json.dumps({"language": "en-US", "title": "old"}).encode()
        context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
        async with Client(
            create_server(Settings(packages=frozenset({"com.example.app"})))
        ) as client:
            with (
                patch("pubship.auth.Auth.token", return_value="synthetic") as token,
                patch("urllib.request.OpenerDirector.open", return_value=context) as request,
            ):
                result = await client.call_tool(
                    "preview_store_listing_update",
                    {"parameters": PARAMETERS, "changes": {"title": "new"}},
                )
            assert not result.is_error
            token.assert_called_once_with("publisher")
            request.assert_called_once()
            sent = request.call_args.args[0]
            assert sent.get_method() == "GET"
            assert sent.data is None
            assert sent.full_url == (
                "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
                "com.example.app/edits/synthetic-edit/listings/en-US"
            )
            assert result.structured_content["changes"][0]["before"] == "old"
            assert (
                datetime.fromisoformat(result.structured_content["fetched_at"]).tzinfo is not None
            )

    asyncio.run(exercise())


def test_preview_strict_schema_rejects_extra_arguments_before_hosted_credentials():
    async def exercise():
        factory = Mock(side_effect=AssertionError("Customer credentials must not be resolved"))
        async with Client(create_server(service_factory=factory)) as client:
            tools = (await client.list_tools()).tools
            tool = next(tool for tool in tools if tool.name == "preview_store_listing_update")
            assert tool.input_schema["additionalProperties"] is False
            assert set(tool.input_schema["properties"]) == {"parameters", "changes"}
            for extra in ({"body": "PRIVATE"}, {"method": "PRIVATE"}, {"approval": "PRIVATE"}):
                result = await client.call_tool(
                    tool.name, {"parameters": PARAMETERS, "changes": {"title": "new"}, **extra}
                )
                assert result.is_error
                assert "PRIVATE" not in str(result.content)
            factory.assert_not_called()
        assert describe_method(PATCH_METHOD)["method"]["status"] == "implemented"
        assert describe_method(PATCH_METHOD)["method"]["tool"] == "apply_store_listing_update"
        assert (
            "Local stdio opt-in" in describe_method(PATCH_METHOD)["method"]["implementation_scope"]
        )

    asyncio.run(exercise())
