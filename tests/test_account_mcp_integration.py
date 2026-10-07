"""Production registration and all seven account routes through an MCP client."""

import asyncio
from copy import deepcopy
from unittest.mock import Mock

import pytest
from mcp import Client

from pubship import account_access_http as http
from pubship.account_access_config import METHODS
from pubship.config import Settings
from pubship.server import create_server

DEV = "123456789"
EMAIL = "synthetic@example.test"
APP = "com.example.app"
NAME = f"developers/{DEV}/users/{EMAIL}"
GRANT = {"name": NAME + "/grants/" + APP, "packageName": APP, "appLevelPermissions": []}
USER = {"name": NAME, "email": EMAIL, "grants": [GRANT]}


@pytest.mark.parametrize(
    "family,action",
    [("users", "list")]
    + [
        (family, action)
        for family in ("users", "grants")
        for action in ("create", "patch", "delete")
    ],
)
def test_all_account_routes_real_mcp(monkeypatch, family, action):
    for key, value in {
        "DEVELOPERS": DEV,
        "METHODS": ",".join(METHODS),
        "USERS": EMAIL,
        "APPS": APP,
    }.items():
        monkeypatch.setenv("GOOGLE_PLAY_ACCOUNT_ACCESS_" + key, value)
    auth = Mock(return_value="synthetic-original-token")
    monkeypatch.setattr("pubship.auth.Auth.token", auth)
    method = f"androidpublisher.{family}.{action}"
    resource = NAME if family == "users" else GRANT["name"]
    parameters = (
        {"parent": "developers/" + DEV if family == "users" else NAME}
        if action in {"create", "list"}
        else {"name": resource}
    )
    if action == "patch":
        field = "developerAccountPermissions" if family == "users" else "appLevelPermissions"
        parameters["updateMask"] = field
        body = {field: []}
    else:
        body = (
            ({"name": NAME, "email": EMAIL} if family == "users" else GRANT)
            if action == "create"
            else None
        )
    baseline = (
        None
        if family == "users" and action == "create"
        else {**USER, "grants": []}
        if action == "create"
        else USER
    )
    calls = []

    def request(spec, token):
        calls.append((spec["method"], token))
        if spec["method"] == "androidpublisher.users.list":
            return {"users": [deepcopy(baseline)] if baseline else []}
        if action == "delete":
            return {}
        return deepcopy(USER if family == "users" else GRANT)

    monkeypatch.setattr(http, "request", request)
    server = create_server(Settings())

    async def check():
        async with Client(server) as client:
            if action == "list":
                result = await client.call_tool(
                    "read_account_access", {"method": method, "parameters": parameters}
                )
                assert not result.is_error
            else:
                result = await client.call_tool(
                    "prepare_account_access",
                    {
                        "method": method,
                        "parameters": parameters,
                        "body": body,
                        "acknowledge_access_effects": True,
                    },
                )
                assert not result.is_error, result.content
                ident = result.structured_content["operation_id"]
                result = await client.call_tool(
                    "apply_account_access", {"operation_id": ident, "confirmation": ident}
                )
                assert not result.is_error, result.content
                assert result.structured_content["mutation_succeeded"]
                assert not result.structured_content["permission_propagation_verified"]
                replay = await client.call_tool(
                    "apply_account_access", {"operation_id": ident, "confirmation": ident}
                )
                assert replay.is_error
        assert sum(m == method for m, _ in calls) == 1
        assert all(token == "synthetic-original-token" for _, token in calls)
        auth.assert_called_once_with("publisher")

    asyncio.run(check())


def test_hosted_factory_never_loads_privileged_account_environment(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_ACCOUNT_ACCESS_DEVELOPERS", "malformed-but-must-not-be-read")

    async def check():
        async with Client(create_server(service_factory=lambda: (Settings(), Mock()))) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert (
                not {"read_account_access", "prepare_account_access", "apply_account_access"}
                & names
            )

    asyncio.run(check())
