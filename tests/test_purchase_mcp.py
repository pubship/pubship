"""Client-facing grants, strict input and private output contracts."""

import asyncio
import json
from unittest.mock import Mock

from mcp import Client

from pubship.config import Settings
from pubship.coverage import PURCHASE_ACTION_METHODS
from pubship.server import create_server

APP = "com.example.app"
PACKAGES = frozenset({APP})
METHOD = "androidpublisher.purchases.products.acknowledge"
SECRET = "SYNTHETIC_PRIVATE_PURCHASE_TOKEN"
PARAMS = {"packageName": APP, "productId": "coins", "token": SECRET}


def configured():
    return Settings(
        packages=PACKAGES,
        sensitive_read_packages=PACKAGES,
        purchase_packages=PACKAGES,
        purchase_methods=PURCHASE_ACTION_METHODS,
    )


def test_hosted_registration_cannot_inherit_any_purchase_grant(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    for family in ("PURCHASE", "EXTERNAL_TRANSACTION", "PRICE_MIGRATION", "SENSITIVE_READ"):
        monkeypatch.setenv("GOOGLE_PLAY_" + family + "_PACKAGES", APP)
        monkeypatch.setenv("GOOGLE_PLAY_" + family + "_METHODS", METHOD)
    factory = Mock(side_effect=AssertionError("Discovery must not authenticate"))

    async def run():
        async with Client(create_server(service_factory=factory)) as client:
            names = {t.name for t in (await client.list_tools()).tools}
            assert len(names) == 11
            assert not any("purchase_operation" in name for name in names)

    asyncio.run(run())
    factory.assert_not_called()


def test_mcp_wrong_scope_confirmation_and_strict_arguments_fail_before_auth(monkeypatch):
    auth = Mock(side_effect=AssertionError("Rejected requests must not resolve credentials"))
    monkeypatch.setattr("pubship.server.Auth.token", auth)

    async def run():
        async with Client(create_server(configured())) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert not tools["prepare_purchase_operation"].annotations.read_only_hint
            assert tools["apply_purchase_operation"].annotations.destructive_hint
            assert not tools["apply_purchase_operation"].annotations.idempotent_hint
            for name, arguments in (
                (
                    "prepare_purchase_operation",
                    {
                        "method": METHOD,
                        "parameters": {**PARAMS, "packageName": "com.other.app"},
                        "body": {},
                        "acknowledge_live_effects": True,
                    },
                ),
                (
                    "prepare_purchase_operation",
                    {
                        "method": METHOD,
                        "parameters": PARAMS,
                        "body": {},
                        "acknowledge_live_effects": "true",
                    },
                ),
                (
                    "prepare_purchase_operation",
                    {
                        "method": METHOD,
                        "parameters": PARAMS,
                        "body": {"unexpected": SECRET},
                        "acknowledge_live_effects": True,
                    },
                ),
                ("apply_purchase_operation", {"operation_id": SECRET, "confirmation": SECRET}),
            ):
                result = await client.call_tool(name, arguments)
                assert result.is_error
                assert SECRET not in json.dumps(result.model_dump(mode="json"))

    asyncio.run(run())
    auth.assert_not_called()


def test_real_mcp_preparation_apply_and_replay_do_not_expose_customer_token(monkeypatch):
    token = Mock(return_value="SYNTHETIC_PRIVATE_CREDENTIAL")
    monkeypatch.setattr("pubship.server.Auth.token", token)
    before = {
        "purchaseState": 0,
        "acknowledgementState": 0,
        "consumptionState": 0,
        "purchaseToken": SECRET,
        "developerPayload": "PRIVATE_PAYLOAD",
    }
    observe = Mock(side_effect=[before, before, {**before, "acknowledgementState": 1}])
    mutation = Mock(return_value={})
    monkeypatch.setattr("pubship.purchase_http.observe", observe)
    monkeypatch.setattr("pubship.purchase_http.execute", mutation)

    async def run():
        async with Client(create_server(configured())) as client:
            prepared = await client.call_tool(
                "prepare_purchase_operation",
                {
                    "method": METHOD,
                    "parameters": PARAMS,
                    "body": {"developerPayload": "PRIVATE_PAYLOAD"},
                    "acknowledge_live_effects": True,
                },
            )
            assert not prepared.is_error, prepared.content
            operation = prepared.structured_content["operation_id"]
            applied = await client.call_tool(
                "apply_purchase_operation", {"operation_id": operation, "confirmation": operation}
            )
            assert not applied.is_error, applied.content
            assert applied.structured_content["mutation_succeeded"]
            assert applied.structured_content["readback_succeeded"]
            replay = await client.call_tool(
                "apply_purchase_operation", {"operation_id": operation, "confirmation": operation}
            )
            assert replay.is_error
            for result in (prepared, applied, replay):
                text = json.dumps(result.model_dump(mode="json"))
                assert SECRET not in text
                assert "PRIVATE_PAYLOAD" not in text
                assert "SYNTHETIC_PRIVATE_CREDENTIAL" not in text

    asyncio.run(run())
    token.assert_called_once()
    mutation.assert_called_once()
    assert mutation.call_args.args[-1] == "SYNTHETIC_PRIVATE_CREDENTIAL"
