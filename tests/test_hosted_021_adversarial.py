"""Independent network authorization, observation, lifetime and privacy failures."""

import asyncio
import io
import json
import threading
import urllib.error
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_hosted_020_loopback import client, successful
from test_hosted_020_loopback import loopback as loopback  # noqa: F401
from test_hosted_021_loopback import (
    APP,
    APP2,
    APP_PERM,
    D2,
    GRANT,
    KMS,
    KMS2,
    NAME,
    U2,
    UNRELATED,
    D,
    P,
    U,
    account_case,
    account_provider,
    apply_args,
    connect,
    consent_fields,
    prepare_account,
    prepare_signing,
    signing_case,
    signing_provider,
)
from test_signing import cert as cert  # noqa: F401

from pubship.hosted_grants import ACCOUNT_DIRECTORY, ADMIN_SCOPES


def arguments(state, *, signing=False):
    return {
        "method": state.method,
        "parameters": state.parameters,
        "body": state.body,
        "acknowledge_signing_effects" if signing else "acknowledge_access_effects": True,
    }


def change_grant(network, token, change):
    provider = network.app.state.oauth
    access = provider.vault.get("access", token["access_token"])
    grant = provider.vault.get("grants", access["subject"])
    change(grant)
    provider.vault.put("grants", access["subject"], grant, 3600)


@pytest.mark.parametrize(
    "operation,fault",
    [
        (operation, fault)
        for operation in ("delete", "expiry")
        for fault in ("other-app", "other-app-permission", "partial")
    ]
    + [("expiry", "expiry-consent")],
)
def test_user_deletion_and_masked_expiry_require_every_original_app_ceiling(
    loopback, monkeypatch, operation, fault
):
    fields = consent_fields()
    if fault == "expiry-consent":
        fields["account-users[0][allow_expiration_change]"] = ""
    _, token = connect(loopback, fields=fields)
    state = account_case(P + "users." + ("patch" if operation == "expiry" else "delete"))
    if operation == "expiry":
        state.parameters["updateMask"] = "expirationTime"
        state.body = {}
    if fault == "other-app":
        state.before["grants"].append(
            {
                "name": NAME + "/grants/" + APP2,
                "packageName": APP2,
                "appLevelPermissions": [APP_PERM],
            }
        )
    elif fault == "other-app-permission":
        state.before["grants"][0]["appLevelPermissions"] = ["CAN_MANAGE_PUBLIC_APKS"]
    elif fault == "partial":
        state.before["partial"] = True
    account_provider(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            result = await mcp.call_tool("prepare_account_access", arguments(state))
            assert result.is_error
            expected = {
                "other-app": "outside this user's exact consent",
                "other-app-permission": "permission ceiling",
                "expiry-consent": "separate explicit consent",
                "partial": "Partial or owner",
            }[fault]
            assert expected in result.model_dump_json()
            assert all(c[0] == "GET" for c in state.calls)
            assert UNRELATED["email"] not in result.model_dump_json()
            assert not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())


def test_omitted_masked_expiry_is_explicit_indefinite_and_mutation_does_not_grant_raw_directory(
    loopback, monkeypatch
):
    _, token = connect(loopback, scopes=ADMIN_SCOPES - {ACCOUNT_DIRECTORY})
    state = account_case(P + "users.patch")
    state.parameters["updateMask"] = "expirationTime"
    state.body = {}
    state.before["expirationTime"] = "2099-01-01T00:00:00Z"
    account_provider(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            raw = await mcp.call_tool(
                "read_account_access",
                {"method": P + "users.list", "parameters": {"parent": "developers/" + D}},
            )
            assert raw.is_error and not state.calls
            prepared = await prepare_account(mcp, state)
            assert prepared["access_lifetime"] == "indefinite"
            assert prepared["proposed_request"]["body"] == {}
            result = await successful(mcp, "apply_account_access", apply_args(prepared))
            assert result["mutation_succeeded"]
            assert [c[3] for c in state.calls if c[0] == "PATCH"] == [{}]
            count = len(state.calls)
            assert (await mcp.call_tool("list_reviews", {"package": APP})).is_error
            assert len(state.calls) == count

    asyncio.run(exercise())


@pytest.mark.parametrize("fault", ["developer", "user", "app", "method", "key", "signing-app"])
def test_cross_product_authority_and_unselected_methods_never_reach_provider(
    loopback, monkeypatch, cert, fault
):
    fields = consent_fields()
    fields["account-users[0][methods]"] = P + "users.delete"
    _, token = connect(loopback, fields=fields)
    signing = fault in {"key", "signing-app"}
    state = (
        signing_case(cert)
        if signing
        else account_case(P + "grants.patch" if fault == "app" else P + "users.delete")
    )
    if fault == "developer":
        state.parameters["name"] = NAME.replace(D, D2)
    elif fault == "user":
        state.parameters["name"] = NAME.replace(U, U2)
    elif fault == "app":
        state.parameters["name"] = GRANT.replace(APP, APP2)
    elif fault == "method":
        state.method = P + "users.patch"
        state.parameters["updateMask"] = "developerAccountPermissions"
        state.body = {}
    elif fault == "key":
        state.body["enrollExistingApp"]["cloudKmsKey"]["cryptoKeyVersionResource"] = KMS2
    else:
        state.parameters["name"] = APP2
    if signing:
        signing_provider(monkeypatch, state, cert)
    else:
        account_provider(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            result = await mcp.call_tool(
                "prepare_signing_operation" if signing else "prepare_account_access",
                arguments(state, signing=signing),
            )
            assert result.is_error and not state.calls
            assert not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["account", "signing"])
def test_foreign_connection_cannot_consume_operation_even_with_same_client_and_authority(
    loopback, monkeypatch, cert, kind
):
    request, token = connect(loopback)
    _, foreign = connect(loopback, request=request)
    state = account_case(P + "users.patch") if kind == "account" else signing_case(cert)
    account_provider(monkeypatch, state) if kind == "account" else signing_provider(
        monkeypatch, state, cert
    )
    tool = "apply_account_access" if kind == "account" else "apply_signing_operation"

    async def exercise():
        async with client(loopback, token) as (mcp, _), client(loopback, foreign) as (other, _):
            prepared = await (
                prepare_account(mcp, state) if kind == "account" else prepare_signing(mcp, state)
            )
            count = len(state.calls)
            assert (await other.call_tool(tool, apply_args(prepared))).is_error
            assert len(state.calls) == count
            assert (await successful(mcp, tool, apply_args(prepared)))["mutation_succeeded"]

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "fault", ["permission", "partial", "absent", "duplicate", "cycle", "late-oversize"]
)
def test_full_scan_and_baseline_changes_fail_without_mutation_or_unrelated_output(
    loopback, monkeypatch, fault
):
    _, token = connect(loopback)
    state = account_provider(monkeypatch, account_case(P + "users.patch"))

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            prepared = await prepare_account(mcp, state)
            if fault == "permission":
                state.before["developerAccountPermissions"] = []
            elif fault == "partial":
                state.before["partial"] = True
            elif fault == "absent":
                state.before = None
            elif fault == "duplicate":
                state.page_override = lambda page, payload: (
                    {"users": [deepcopy(UNRELATED)]} if page else payload
                )
            elif fault == "cycle":
                state.page_override = lambda page, payload: {
                    "users": [],
                    "nextPageToken": "synthetic-page-two",
                }
            else:
                # The second provider page exceeds the smaller hosted response budget.
                state.page_override = lambda page, payload: (
                    {"users": [], "nextPageToken": "x" * (256 * 1024)} if page else payload
                )
            result = await mcp.call_tool("apply_account_access", apply_args(prepared))
            assert result.is_error and all(c[0] == "GET" for c in state.calls)
            assert UNRELATED["email"] not in result.model_dump_json()
            assert not loopback.app.state.hosted_registry._records
            count = len(state.calls)
            assert (await mcp.call_tool("apply_account_access", apply_args(prepared))).is_error
            assert len(state.calls) == count

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("fault", ["revoked", "expired"])
def test_authority_or_original_deadline_loss_after_first_page_stops_second_page(
    loopback, monkeypatch, phase, fault
):
    _, token = connect(loopback)
    state = account_provider(monkeypatch, account_case(P + "users.patch"))
    registry = loopback.app.state.hosted_registry

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            prepared = await prepare_account(mcp, state) if phase == "apply" else None
            start = len(state.calls)

            def lose(_):
                if fault == "revoked":
                    change_grant(
                        loopback, token, lambda grant: grant["account_user_targets"].pop(0)
                    )
                else:
                    deadline = max(record.deadline for record in registry._records.values())
                    monkeypatch.setattr(
                        "pubship.hosted_administration.time",
                        SimpleNamespace(monotonic=lambda: deadline),
                    )

            state.on_read = lose
            result = (
                await mcp.call_tool("apply_account_access", apply_args(prepared))
                if prepared
                else await mcp.call_tool("prepare_account_access", arguments(state))
            )
            assert result.is_error and len(state.calls) == start + 1
            assert all(c[0] == "GET" for c in state.calls)
            assert not registry._records

    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["provider-error", "revoke", "expiry"])
def test_confirmed_write_survives_unavailable_readback_and_never_retries(
    loopback, monkeypatch, failure, caplog
):
    _, token = connect(loopback)
    state = account_provider(monkeypatch, account_case(P + "users.patch"))
    registry = loopback.app.state.hosted_registry

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            prepared = await prepare_account(mcp, state)

            def after_write():
                if failure == "provider-error":

                    def unavailable(_):
                        raise urllib.error.HTTPError(
                            "https://private-provider.invalid",
                            403,
                            "PRIVATE-PROVIDER-ERROR",
                            {},
                            io.BytesIO(b"private-provider-body"),
                        )

                    state.on_read = unavailable
                elif failure == "revoke":
                    change_grant(
                        loopback, token, lambda grant: grant["account_user_targets"].pop(0)
                    )
                else:
                    deadline = registry._records[prepared["operation_id"]].deadline
                    monkeypatch.setattr(
                        "pubship.hosted_administration.time",
                        SimpleNamespace(monotonic=lambda: deadline),
                    )

            state.on_write = after_write
            result = await successful(mcp, "apply_account_access", apply_args(prepared))
            assert result["mutation_succeeded"] and not result["readback_succeeded"]
            assert len([c for c in state.calls if c[0] != "GET"]) == 1
            assert "PRIVATE-PROVIDER" not in json.dumps(result) + caplog.text
            assert not registry._records

    asyncio.run(exercise())


def test_new_families_share_legacy_quota_before_any_new_provider_observation(
    loopback, monkeypatch, cert
):
    scopes = ADMIN_SCOPES | {"play:edit-listings"}
    fields = consent_fields(scopes)
    fields.update(packages=APP)
    fields["capability_packages[play:edit-listings]"] = APP
    _, token = connect(loopback, scopes=scopes, fields=fields)
    registry = loopback.app.state.hosted_registry
    registry._limits = (2, *registry._limits[1:])
    account = account_provider(monkeypatch, account_case(P + "users.patch"))
    signer = signing_case(cert)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            first = await prepare_account(mcp, account)
            signing_provider(monkeypatch, signer, cert)
            second = await prepare_signing(mcp, signer)
            assert (
                registry._records[first["operation_id"]].guard
                is registry._records[second["operation_id"]].guard
            )
            count = len(account.calls)
            denied = await mcp.call_tool(
                "prepare_store_listing_update",
                {
                    "parameters": {"packageName": APP, "editId": "edit1", "language": "en-US"},
                    "changes": {"title": "synthetic"},
                },
            )
            assert denied.is_error and "capacity" in denied.model_dump_json()
            assert len(account.calls) == count and not signer.calls
            assert (await successful(mcp, "apply_signing_operation", apply_args(second)))[
                "mutation_succeeded"
            ]
            account_provider(monkeypatch, account)
            third = await prepare_account(mcp, account)
            assert third["operation_id"] != first["operation_id"] and len(registry._records) == 2

    asyncio.run(exercise())


def test_concurrent_cross_family_applies_use_one_lane_and_retain_both_single_attempts(
    loopback, monkeypatch, cert
):
    _, token = connect(loopback)
    account = account_provider(monkeypatch, account_case(P + "users.patch"))
    account_open = urllib.request.OpenerDirector.open
    signer = signing_case(cert)
    signing_provider(monkeypatch, signer, cert)
    signing_open = urllib.request.OpenerDirector.open
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        lambda self, request, timeout: (
            signing_open if "/appSigning:" in request.full_url else account_open
        )(self, request, timeout),
    )
    entered, release = threading.Event(), threading.Event()

    def hold_write():
        entered.set()
        assert release.wait(5), "Test did not release synthetic provider mutation"

    account.on_write = hold_write

    async def exercise():
        async with client(loopback, token) as (mcp, _), client(loopback, token) as (second, http):
            one = await prepare_account(mcp, account)
            two = await prepare_signing(second, signer)
            # Two stateless connections sharing one authenticated client must
            # use distinct active IDs for unambiguous cancellation routing.
            await second.list_tools()
            started = asyncio.Event()

            async def audit(request):
                if request.method == "POST" and b"apply_signing_operation" in await request.aread():
                    started.set()

            http.event_hooks["request"].append(audit)
            first = asyncio.create_task(successful(mcp, "apply_account_access", apply_args(one)))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                other = asyncio.create_task(
                    successful(second, "apply_signing_operation", apply_args(two))
                )
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.sleep(0.05)
                assert not signer.calls and not other.done()
            finally:
                release.set()
            results = await asyncio.gather(first, other)
            assert all(result["mutation_succeeded"] for result in results)
            assert len(signer.calls) == 1 and len([c for c in account.calls if c[0] != "GET"]) == 1
            assert not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["hash-mismatch", "transport"])
def test_signing_uncertain_result_is_consumed_and_private_payload_stays_out_of_errors(
    loopback, monkeypatch, cert, caplog, outcome
):
    _, token = connect(loopback)
    state = signing_case(cert, "new", True)
    result = signing_provider(monkeypatch, state, cert)
    if outcome == "hash-mismatch":
        result["signingCertificate"]["certificateHashSha256"] = "00" * 32
    else:

        def fail():
            raise urllib.error.URLError("PRIVATE-PROVIDER-ERROR-" + KMS)

        state.on_write = fail

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            prepared = await prepare_signing(mcp, state)
            failed = await mcp.call_tool("apply_signing_operation", apply_args(prepared))
            assert failed.is_error and len(state.calls) == 1
            output = failed.model_dump_json() + caplog.text
            assert (
                cert not in output and "PRIVATE-PROVIDER-ERROR" not in output and KMS not in output
            )
            assert (await mcp.call_tool("apply_signing_operation", apply_args(prepared))).is_error
            assert len(state.calls) == 1 and not loopback.app.state.hosted_registry._records

    asyncio.run(exercise())
