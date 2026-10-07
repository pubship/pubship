"""Shared hosted account/signing lifecycle against fixed synthetic Google HTTP."""

import asyncio
import json
import time
import urllib.error
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_account_access import APP, APP_PERM, BODY, GRANT, NAME, PERM, USER, D, U
from test_hosted_grants import issue
from test_hosted_grants import provider as provider
from test_signing import APP as SIGNING_APP
from test_signing import KMS
from test_signing import body as signing_body
from test_signing import cert as cert
from test_signing import method as signing_method
from test_signing import response as signing_response

from pubship import account_access, signing
from pubship.account_access_config import READ_METHOD, WRITE_METHODS
from pubship.errors import PlayError
from pubship.hosted_edits import MAX_OPERATION_BYTES, HostedEditRegistry, _size
from pubship.hosted_grants import (
    READ_SCOPE,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
    grant_authority,
)


@pytest.fixture
def context(provider, monkeypatch):
    grant, _, _ = issue(provider)
    grant.update(
        capability_version=3,
        packages=[],
        capability_packages={READ_SCOPE: []},
        appstore_targets={},
        appstore_event_stores=[],
        account_directory_developers=[D],
        account_user_targets=[
            {
                "developer": D,
                "user": U,
                "methods": sorted(m for m in WRITE_METHODS if ".users." in m),
                "developer_permissions": [PERM],
                "affected_apps": {APP: [APP_PERM]},
                "allow_expiration_change": True,
            }
        ],
        account_grant_targets=[
            {
                "developer": D,
                "user": U,
                "app": APP,
                "methods": sorted(m for m in WRITE_METHODS if ".grants." in m),
                "app_permissions": [APP_PERM],
            }
        ],
        signing_targets={
            cap: [{"app": SIGNING_APP, "kms_version": KMS}]
            for cap in (SIGNING_ENROLL, SIGNING_ROTATE)
        },
    )
    provider.vault.put("grants", "one", grant, 3600)
    token = provider.issue_tokens("one", "client-one", sorted(grant_authority(grant).scopes))
    principal = asyncio.run(provider.load_access_token(token.access_token))
    registry = HostedEditRegistry(provider)
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open",
        Mock(side_effect=AssertionError("No real provider requests")),
    )
    yield SimpleNamespace(provider=provider, principal=principal, registry=registry, grant=grant)
    registry.close()


def change_grant(context, update):
    update(context.grant)
    context.provider.vault.put("grants", "one", context.grant, 3600)


def scenario(monkeypatch, method):
    state = SimpleNamespace(
        method=method, calls=[], before=deepcopy(USER), after=deepcopy(USER), mutate=None
    )
    family, operation = method.rsplit(".", 2)[-2:]
    if method == READ_METHOD:
        parameters, body = {"parent": "developers/" + D}, None
    elif family == "users":
        parameters = {"parent": "developers/" + D} if operation == "create" else {"name": NAME}
        body = None if operation == "delete" else deepcopy(BODY)
        if operation == "create":
            body.update(name=NAME, email=U)
            state.before = None
        elif operation == "patch":
            parameters["updateMask"] = "developerAccountPermissions"
        else:
            state.after = None
    else:
        parameters = {"parent": NAME} if operation == "create" else {"name": GRANT}
        body = None if operation == "delete" else {"appLevelPermissions": [APP_PERM]}
        if operation == "create":
            body.update(name=GRANT, packageName=APP)
            state.before["grants"] = []
        elif operation == "patch":
            parameters["updateMask"] = "appLevelPermissions"
        else:
            state.after["grants"] = []
    state.mutated = False
    state.extra = []

    def send(request, timeout):
        state.calls.append(request)
        assert request.get_header("Authorization") == "Bearer synthetic-google-access"
        assert request.full_url.startswith(
            "https://androidpublisher.googleapis.com/androidpublisher/v3/developers/"
        )
        if request.method == "GET":
            current = state.after if state.mutated else state.before
            payload = {"users": [*state.extra, *([] if current is None else [current])]}
        else:
            state.mutated = True
            payload = (
                {}
                if operation == "delete"
                else state.after
                if family == "users"
                else state.after["grants"][0]
            )
            if state.mutate:
                state.mutate()
        reply = Mock(status=200)
        raw = json.dumps(payload).encode()
        reply.read.side_effect = lambda limit: raw[:limit]
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        return reply

    monkeypatch.setattr("urllib.request.OpenerDirector.open", Mock(side_effect=send))
    state.parameters, state.body = parameters, body
    return state


def prepare(context, state):
    return context.registry.prepare(
        context.principal, "account", state.parameters, state.body, True, method=state.method
    )


def apply(context, prepared, kind="account", principal=None):
    return context.registry.apply(
        principal or context.principal, kind, prepared["operation_id"], prepared["operation_id"]
    )


@pytest.mark.parametrize("method", sorted(WRITE_METHODS))
def test_all_six_account_mutations_use_original_credential_and_single_attempt(
    context, monkeypatch, method
):
    state = scenario(monkeypatch, method)
    prepared = prepare(context, state)
    assert prepared["operation_id"].startswith("hosted_") and not prepared["executed"]
    assert all(req.method == "GET" for req in state.calls)
    change_grant(context, lambda grant: grant.update(access_token="refreshed-but-not-original"))
    result = apply(context, prepared)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert len([req for req in state.calls if req.method != "GET"]) == 1
    assert not context.registry._records
    with pytest.raises(PlayError):
        apply(context, prepared)


def test_directory_read_has_its_own_scope_and_releases_transient_quota(context, monkeypatch):
    state = scenario(monkeypatch, READ_METHOD)
    result = context.registry.read_account_access(context.principal, READ_METHOD, state.parameters)
    assert result["response"]["users"] == [USER] and not context.registry._records
    change_grant(context, lambda grant: grant.update(account_directory_developers=[]))
    with pytest.raises(PlayError):
        context.registry.read_account_access(context.principal, READ_METHOD, state.parameters)
    assert len(state.calls) == 1


@pytest.mark.parametrize("mode", ["new", "existing", "rotate"])
def test_signing_modes_share_lifecycle_and_charge_private_body(context, monkeypatch, cert, mode):
    method, body = signing_method(mode), signing_body(cert, mode)
    calls = []
    spec = signing.request_spec(method, {"name": SIGNING_APP}, body)

    def send(request, timeout):
        calls.append(request)
        assert request.method == "POST"
        assert request.get_header("Authorization") == "Bearer synthetic-google-access"
        reply = Mock(status=200)
        raw = json.dumps(signing_response(spec)).encode()
        reply.read.side_effect = lambda limit: raw[:limit]
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        return reply

    monkeypatch.setattr("urllib.request.OpenerDirector.open", Mock(side_effect=send))
    prepared = context.registry.prepare(
        context.principal, "signing", {"name": SIGNING_APP}, body, True, method=method
    )
    record = context.registry._records[prepared["operation_id"]]
    metadata = record.engine._retained_metadata(record.inner_id)
    assert (
        record.retained_bytes
        >= _size(metadata) + metadata["private_payload_bytes"] + metadata["credential_bytes"]
    )
    assert cert not in json.dumps(prepared) and not calls
    result = apply(context, prepared, "signing")
    assert result["mutation_succeeded"] and not result["readback_available"] and len(calls) == 1
    assert not context.registry._records


@pytest.mark.parametrize(
    "fault",
    [
        "requested_permission",
        "existing_permission",
        "expiry_flag",
        "affected_app",
        "affected_permission",
        "partial",
        "absent",
    ],
)
def test_user_policy_checks_each_affected_ceiling_before_mutation(context, monkeypatch, fault):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    if fault == "requested_permission":
        change_grant(
            context, lambda grant: grant["account_user_targets"][0].update(developer_permissions=[])
        )
    elif fault == "existing_permission":
        state.body = {}
        change_grant(
            context, lambda grant: grant["account_user_targets"][0].update(developer_permissions=[])
        )
    elif fault.startswith("affected") or fault == "expiry_flag":
        state.parameters["updateMask"] = "expirationTime"
        state.body = {}  # An omitted masked value requests indefinite access.
        field, value = (
            ("allow_expiration_change", False)
            if fault == "expiry_flag"
            else ("affected_apps", {} if fault == "affected_app" else {APP: []})
        )
        change_grant(context, lambda grant: grant["account_user_targets"][0].update({field: value}))
    elif fault == "partial":
        state.before["partial"] = True
    else:
        state.before = None
    with pytest.raises(PlayError):
        prepare(context, state)
    assert all(req.method == "GET" for req in state.calls)
    assert not context.registry._records
    if fault in {"requested_permission", "expiry_flag"}:
        assert not state.calls


def test_creation_initial_expiry_does_not_imply_later_expiry_changes(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.create")
    state.body["expirationTime"] = (datetime.now(UTC) + timedelta(days=3)).isoformat()
    change_grant(
        context,
        lambda grant: grant["account_user_targets"][0].update(allow_expiration_change=False),
    )
    assert prepare(context, state)["access_lifetime"] == state.body["expirationTime"]


@pytest.mark.parametrize("fault", ["unrelated_app", "permission_ceiling"])
def test_user_deletion_covers_every_existing_grant(context, monkeypatch, fault):
    state = scenario(monkeypatch, "androidpublisher.users.delete")
    if fault == "unrelated_app":
        state.before["grants"].append(
            {
                "name": NAME + "/grants/com.example.other",
                "packageName": "com.example.other",
                "appLevelPermissions": [],
            }
        )
    else:
        change_grant(
            context, lambda grant: grant["account_user_targets"][0].update(affected_apps={APP: []})
        )
    with pytest.raises(PlayError):
        prepare(context, state)
    assert not state.mutated and not context.registry._records


def test_related_app_map_never_confers_grant_method_permission(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.grants.delete")
    change_grant(context, lambda grant: grant.update(account_grant_targets=[]))
    with pytest.raises(PlayError):
        prepare(context, state)
    assert not state.calls


def test_expiry_and_narrowing_after_prepare_block_apply(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.delete")
    prepared = prepare(context, state)
    record = context.registry._records[prepared["operation_id"]]
    record.deadline = time.monotonic() - 1
    with pytest.raises(PlayError):
        apply(context, prepared)
    assert not state.mutated and not context.registry._records
    prepared = prepare(context, state)
    change_grant(context, lambda grant: grant["account_user_targets"][0].update(affected_apps={}))
    with pytest.raises(PlayError):
        apply(context, prepared)
    assert not state.mutated and not context.registry._records


def test_known_success_survives_revocation_before_readback(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state)
    state.mutate = lambda: context.provider.disconnect(context.principal.subject)
    result = apply(context, prepared)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert not context.registry._records


def test_uncertain_mutation_consumes_id_and_redacts_private_exception(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state)
    state.mutate = lambda: (_ for _ in ()).throw(urllib.error.URLError("PRIVATE_CANARY_TOKEN"))
    with pytest.raises(PlayError) as caught:
        apply(context, prepared)
    assert "PRIVATE_CANARY_TOKEN" not in str(caught.value) and "unknown" in str(caught.value)
    assert not context.registry._records


def test_cleanup_runs_outside_registry_and_grant_locks(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    original = account_access.AccountAccess._dispose
    calls = []

    def dispose(engine, operation_id):
        assert not context.registry._lock.locked()
        assert not context.provider.grant_lock(context.principal.subject)._is_owned()
        calls.append(1)
        return original(engine, operation_id)

    monkeypatch.setattr(account_access.AccountAccess, "_dispose", dispose)
    state.before["partial"] = True
    with pytest.raises(PlayError):
        prepare(context, state)
    assert calls and not context.registry._records


def test_cross_family_capacity_and_guard_are_shared(context, monkeypatch, cert):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state)
    record = context.registry._records[prepared["operation_id"]]
    signing_prepared = context.registry.prepare(
        context.principal,
        "signing",
        {"name": SIGNING_APP},
        signing_body(cert, "existing"),
        True,
        method=signing_method("existing"),
    )
    signing_record = context.registry._records[signing_prepared["operation_id"]]
    assert signing_record.guard is record.guard
    context.registry._limits = (2, 2, MAX_OPERATION_BYTES * 2, MAX_OPERATION_BYTES * 2)
    with pytest.raises(PlayError, match="capacity"):
        prepare(context, state)
    record.guard.acquire()
    try:
        with pytest.raises(PlayError, match="Another apply"):
            apply(context, prepared)
    finally:
        record.guard.release()
    assert prepared["operation_id"] not in context.registry._records and not state.mutated


def test_oversized_retained_private_accounting_fails_and_releases_reservation(
    context, monkeypatch, cert
):
    original = signing.Signing._retained_metadata

    def oversized(engine, operation_id):
        result = original(engine, operation_id)
        result["private_payload_bytes"] = MAX_OPERATION_BYTES
        return result

    monkeypatch.setattr(signing.Signing, "_retained_metadata", oversized)
    with pytest.raises(PlayError, match="retained-payload"):
        context.registry.prepare(
            context.principal,
            "signing",
            {"name": SIGNING_APP},
            signing_body(cert, "existing"),
            True,
            method=signing_method("existing"),
        )
    assert not context.registry._records


def test_foreign_connection_cannot_consume_preparation(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state)
    _, _, other = issue(context.provider, subject="other", client="other-client")
    with pytest.raises(PlayError):
        apply(context, prepared, principal=other)
    assert prepared["operation_id"] in context.registry._records
    assert apply(context, prepared)["mutation_succeeded"]


def test_unrelated_directory_records_never_escape_target_preview(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    unrelated = deepcopy(USER)
    unrelated.update(
        name=f"developers/{D}/users/PRIVATE_CANARY@example.test",
        email="PRIVATE_CANARY@example.test",
        grants=[],
    )
    state.extra = [unrelated]
    prepared = prepare(context, state)
    assert "PRIVATE_CANARY" not in json.dumps(prepared)
    assert "PRIVATE_CANARY" not in json.dumps(apply(context, prepared))


def test_large_directory_fails_before_mutation_with_shared_reservation_released(
    context, monkeypatch
):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    state.extra = [
        dict(
            USER,
            name=f"developers/{D}/users/other{i}@example.test",
            email=f"other{i}@example.test",
            grants=[],
        )
        for i in range(2000)
    ]
    with pytest.raises(PlayError):
        prepare(context, state)
    assert not state.mutated and not context.registry._records
    assert len(state.calls) == 1


@pytest.mark.parametrize("phase", ["requested", "existing"])
def test_grant_operations_enforce_only_exact_app_ceiling_for_both_versions(
    context, monkeypatch, phase
):
    state = scenario(monkeypatch, "androidpublisher.grants.patch")
    change_grant(
        context, lambda grant: grant["account_grant_targets"][0].update(app_permissions=[])
    )
    if phase == "existing":
        state.body = {}
    with pytest.raises(PlayError):
        prepare(context, state)
    assert len(state.calls) == (0 if phase == "requested" else 1)
    assert not state.mutated and not context.registry._records


def test_expiry_after_google_credential_acquisition_prevents_first_provider_request(
    context, monkeypatch
):
    from pubship.oauth import CustomerAuth

    state = scenario(monkeypatch, "androidpublisher.users.patch")
    original = CustomerAuth.token

    def token(auth, service):
        result = original(auth, service)
        record = next(iter(context.registry._records.values()))
        record.engine._execution_authorization.deadline = time.monotonic() - 1
        return result

    monkeypatch.setattr(CustomerAuth, "token", token)
    with pytest.raises(PlayError, match="expired"):
        prepare(context, state)
    assert not state.calls and not context.registry._records


def test_canceled_preparation_releases_reservation(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=asyncio.CancelledError())
    )
    with pytest.raises(asyncio.CancelledError):
        prepare(context, state)
    assert not context.registry._records


def test_network_dispatch_never_holds_global_registry_lock(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    import urllib.request

    original = urllib.request.OpenerDirector.open

    def send(*args, **kwargs):
        assert not context.registry._lock.locked()
        return original(*args, **kwargs)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", Mock(side_effect=send))
    assert apply(context, prepare(context, state))["mutation_succeeded"]


def test_private_quota_cleanup_failure_stays_charged_then_retries(context, monkeypatch):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state)
    record = context.registry._records[prepared["operation_id"]]
    original = record.engine._dispose
    record.engine._dispose = Mock(return_value="cleanup-pending")
    result = apply(context, prepared)
    assert result["mutation_succeeded"] and result["cleanup_warning"]
    assert prepared["operation_id"] in context.registry._records and record.retained_bytes > 0
    record.engine._dispose = original
    context.registry.cleanup()
    assert not context.registry._records
