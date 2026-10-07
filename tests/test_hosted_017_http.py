"""Authenticated hosted StreamableHTTP integration; no real Google calls or writes."""

import base64
import hashlib
from contextlib import asynccontextmanager

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from test_hosted import hosted as hosted  # noqa: F401
from test_hosted_consent import APP, OTHER, choose, finish, request_consent
from test_hosted_edits import transport as transport  # noqa: F401

from pubship.hosted_grants import ORDINARY_SCOPES, READ_SCOPE

# This existing listing lifecycle fixture authorizes ordinary app capabilities.
# Independent v2 tests exercise explicit store dimensions.
CAPABILITIES = ORDINARY_SCOPES - {READ_SCOPE}
ALL_SCOPES = ORDINARY_SCOPES

METADATA = "play:publisher-metadata"
LISTINGS = "play:edit-listings"
PARAMETERS = {"packageName": APP, "editId": "edit-1", "language": "en-US"}


def connect(browser, settings, *, request=None, capabilities=CAPABILITIES):
    if request is None:
        connect_url, _, request = request_consent(browser, settings, sorted(ALL_SCOPES))
    else:
        request = dict(request)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(request["verifier"].encode()).digest())
            .decode()
            .rstrip("=")
        )
        response = browser.get(
            "/authorize",
            params={
                "client_id": request["client_id"],
                "redirect_uri": request["redirect"],
                "response_type": "code",
                "scope": " ".join(sorted(ALL_SCOPES)),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "client-state",
                "resource": settings.resource,
            },
        )
        assert response.status_code == 302, response.text
        connect_url = response.headers["location"]
        assert browser.get(connect_url).status_code == 200
    selected = choose(
        browser,
        settings,
        connect_url,
        capabilities=list(capabilities),
        fields={name: APP for name in capabilities},
        packages=APP + "," + OTHER,
    )
    return request, finish(browser, settings, request, selected)


@asynccontextmanager
async def mcp_client(app, settings, token):
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        headers={"Authorization": "Bearer " + token["access_token"]},
    ) as http_client:
        async with Client(
            streamable_http_client(settings.resource, http_client=http_client)
        ) as client:
            yield client


def test_real_hosted_http_catalog_metadata_and_isolated_listing_lifecycle(hosted, transport):
    settings, app, browser = hosted
    request, owner = connect(browser, settings)
    _, other_connection = connect(browser, settings, request=request)

    def read(url, token, **kwargs):
        if "/subscriptions" in url:
            return {"subscriptions": [{"productId": "synthetic-monthly"}]}
        return transport.get(url, token, **kwargs)

    transport.reads.side_effect = read

    async def exercise():
        owner_principal = await app.state.oauth.load_access_token(owner["access_token"])
        other_principal = await app.state.oauth.load_access_token(other_connection["access_token"])
        assert owner_principal.client_id == other_principal.client_id
        assert owner_principal.subject != other_principal.subject
        async with (
            mcp_client(app, settings, owner) as client,
            mcp_client(app, settings, other_connection) as other,
        ):
            assert len((await client.list_tools()).tools) == 46
            catalog = await client.call_tool("list_api_methods", {"limit": 200})
            assert not catalog.is_error
            data = catalog.structured_content
            assert data["execution_mode"] == "hosted"
            assert data["implementation_scope"] == "local"
            assert sum(method["execution"]["implemented"] for method in data["methods"]) == 170
            account = next(m for m in data["methods"] if m["id"] == "androidpublisher.users.list")
            assert not account["execution"]["available_for_connection"]
            assert account["execution"]["implemented"]
            capabilities = await client.call_tool("get_connection_capabilities", {})
            assert not capabilities.is_error
            assert capabilities.structured_content["scopes"] == sorted(ALL_SCOPES)
            assert capabilities.structured_content["capability_packages"] == {
                READ_SCOPE: sorted([APP, OTHER]),
                **{name: [APP] for name in CAPABILITIES},
            }
            transport.reads.assert_not_called()

            metadata = await client.call_tool(
                "read_publisher",
                {
                    "method": "androidpublisher.monetization.subscriptions.list",
                    "parameters": {"packageName": APP},
                },
            )
            assert not metadata.is_error
            assert metadata.structured_content["data"]["subscriptions"] == [
                {"productId": "synthetic-monthly"}
            ]
            assert transport.reads.call_count == 1
            prepared = await client.call_tool(
                "prepare_store_listing_update",
                {"parameters": PARAMETERS, "changes": {"title": "new"}},
            )
            assert not prepared.is_error, prepared.content
            operation = prepared.structured_content["operation_id"]
            arguments = {"operation_id": operation, "confirmation": operation}
            transport.patch.assert_not_called()
            transport.reads.reset_mock()
            stolen = await other.call_tool("apply_store_listing_update", arguments)
            assert stolen.is_error
            transport.reads.assert_not_called()
            transport.patch.assert_not_called()
            applied = await client.call_tool("apply_store_listing_update", arguments)
            assert not applied.is_error, applied.content
            assert applied.structured_content["executed"]
            assert not applied.structured_content["committed"]
            assert applied.structured_content["operation_id"] == operation
            transport.patch.assert_called_once()
            assert transport.patch.call_args.args[1:] == (
                "PRIVATE-GOOGLE-ALICE",
                {"title": "new"},
            )
            transport.reads.reset_mock()
            replay = await client.call_tool("apply_store_listing_update", arguments)
            assert replay.is_error
            transport.reads.assert_not_called()
            transport.patch.assert_called_once()

    browser.portal.call(exercise)
    transport.put.assert_not_called()
    transport.lifecycle.assert_not_called()


@pytest.mark.parametrize("denial", ["missing-capability", "outside-capability-packages"])
def test_real_hosted_http_denies_before_provider_dispatch(hosted, transport, denial):
    settings, app, browser = hosted
    _, token = connect(
        browser, settings, capabilities=() if denial == "missing-capability" else CAPABILITIES
    )
    package = APP if denial == "missing-capability" else OTHER

    async def exercise():
        async with mcp_client(app, settings, token) as client:
            capabilities = await client.call_tool("get_connection_capabilities", {})
            assert not capabilities.is_error
            granted = capabilities.structured_content["capability_packages"]
            assert package in granted[READ_SCOPE]
            assert package not in granted.get(LISTINGS, [])
            assert package not in granted.get(METADATA, [])
            for name, arguments in (
                (
                    "read_publisher",
                    {
                        "method": "androidpublisher.monetization.subscriptions.list",
                        "parameters": {"packageName": package},
                    },
                ),
                (
                    "prepare_store_listing_update",
                    {
                        "parameters": {**PARAMETERS, "packageName": package},
                        "changes": {"title": "new"},
                    },
                ),
            ):
                result = await client.call_tool(name, arguments)
                assert result.is_error

    browser.portal.call(exercise)
    for provider_call in (transport.reads, transport.patch, transport.put, transport.lifecycle):
        provider_call.assert_not_called()
