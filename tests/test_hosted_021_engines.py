"""Trusted hosted hooks, bounded observations and one-shot original-credential execution."""

import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship import account_access_http as http
from pubship import signing
from pubship.account_access import AccountAccess
from pubship.account_access_config import READ_METHOD, AccountAccessSettings
from pubship.account_access_contracts import request_spec
from pubship.errors import PlayError

D = "123"
U = "target@example.test"
NAME = f"developers/{D}/users/{U}"
APP = "com.example.target"
OTHER_APP = "com.example.other"
PERM = "CAN_VIEW_NON_FINANCIAL_DATA"
USER = {
    "name": NAME,
    "email": U,
    "grants": [
        {"name": NAME + "/grants/" + APP, "packageName": APP, "appLevelPermissions": [PERM]}
    ],
}
METHOD = "androidpublisher.users.patch"
PARAMS = {"name": NAME, "updateMask": "expirationTime"}
KMS = "projects/example-project/locations/global/keyRings/ring/cryptoKeys/key/cryptoKeyVersions/1"
SIGN_METHOD = "androidpublisher.appsigning.enrollApp"
SIGN_BODY = {"enrollExistingApp": {"cloudKmsKey": {"cryptoKeyVersionResource": KMS}}}
HASH_RESPONSE = {"signingCertificate": {"certificateHashSha256": "AB" * 32}}
TOKEN = "synthetic-021-original-private-token"


class Authorization:
    def __init__(self):
        self.deadline = time.monotonic() + 200
        self.active = True

    def __call__(self):
        if not self.active:
            raise PlayError("Connection revoked")


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    monkeypatch.setattr(
        "urllib.request.OpenerDirector.open", Mock(side_effect=AssertionError("No Google calls"))
    )


class AccountAuthority:
    """Synthetic exact adapter, deliberately independent per affected app."""

    def __init__(self):
        self.allow_expiry = True
        self.apps = {APP: frozenset({PERM}), OTHER_APP: frozenset()}
        self.calls = []

    def __call__(self, spec, before):
        self.calls.append((deepcopy(spec), deepcopy(before)))
        if spec["developer"] != D or spec["user"] not in (U, None):
            raise PlayError("Wrong exact target")
        expiry = "expirationTime" in spec["parameters"].get("updateMask", "").split(",")
        if expiry and not self.allow_expiry:
            raise PlayError("Expiry changes not authorized")
        if before and (expiry or spec["operation"] == "delete"):
            for grant in before.get("grants", []):
                app = grant["name"].rsplit("/", 1)[1]
                if app not in self.apps or not set(grant.get("appLevelPermissions", [])).issubset(
                    self.apps[app]
                ):
                    raise PlayError("Related app ceiling exceeded")


def account(monkeypatch, *, snapshot_limit=256 * 1024):
    auth = Authorization()
    target = AccountAuthority()
    credential = SimpleNamespace(token=Mock(return_value=TOKEN))
    engine = AccountAccess(
        AccountAccessSettings(),
        credential,
        _execution_authorization=auth,
        _target_authorization=target,
        _apply_guard=threading.Lock(),
        _max_snapshot_bytes=snapshot_limit,
    )
    request = Mock(
        side_effect=lambda spec, token, **kw: (
            {"users": [deepcopy(USER)]} if spec["method"] == READ_METHOD else deepcopy(USER)
        )
    )
    monkeypatch.setattr(http, "request", request)
    return engine, auth, target, request


def signer(monkeypatch):
    auth = Authorization()

    def target(spec):
        if (spec["method"], spec["app"], spec["kms"]) != (SIGN_METHOD, APP, KMS):
            raise PlayError("Wrong signing pair")

    engine = signing.Signing(
        signing.SigningSettings(),
        SimpleNamespace(token=Mock(return_value=TOKEN)),
        _execution_authorization=auth,
        _target_authorization=target,
        _apply_guard=threading.Lock(),
        _max_snapshot_bytes=256 * 1024,
    )
    request = Mock(return_value=deepcopy(HASH_RESPONSE))
    monkeypatch.setattr(signing, "execute", request)
    return engine, auth, request


def prepare_account(engine):
    return engine.prepare(METHOD, PARAMS, {}, True)


def prepare_signing(engine):
    return engine.prepare(SIGN_METHOD, {"name": APP}, SIGN_BODY, True)


def apply(engine, preparation):
    return engine.apply(preparation["operation_id"], preparation["operation_id"])


@pytest.mark.parametrize("family", ["account", "signing"])
def test_deadline_original_token_and_safe_retained_metadata(monkeypatch, family):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        prepared = prepare_account(engine)
        assert prepared["access_lifetime"] == "indefinite"
    else:
        engine, auth, request = signer(monkeypatch)
        prepared = prepare_signing(engine)
    p = engine._preparations[prepared["operation_id"]]
    assert p.deadline == auth.deadline
    metadata = engine._retained_metadata(prepared["operation_id"])
    assert TOKEN not in json.dumps(metadata)
    assert metadata["credential_bytes"] == len(TOKEN)
    if family == "signing":
        assert "body" not in metadata["spec"]
        assert metadata["private_payload_bytes"] == len(json.dumps(SIGN_BODY).encode())
    auth.deadline += 1000
    engine.auth.token.return_value = "different-token"
    assert apply(engine, prepared)["mutation_succeeded"]
    assert all(call.args[1] == TOKEN for call in request.call_args_list)
    assert engine.auth.token.call_count == 1
    assert p.token == "" and p.spec == {}
    with pytest.raises(PlayError, match="consumed"):
        apply(engine, prepared)


@pytest.mark.parametrize("family", ["account", "signing"])
def test_credentials_and_target_hook_revocation_prevent_dispatch(monkeypatch, family):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        prepare = prepare_account
    else:
        engine, auth, request = signer(monkeypatch)
        prepare = prepare_signing

    def acquire(_):
        auth.active = False
        return TOKEN

    engine.auth.token.side_effect = acquire
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    request.assert_not_called()
    assert not engine._preparations
    auth.active = True
    engine.auth.token.side_effect = None
    original = engine._target_authorization

    def revoke(*args):
        original(*args)
        auth.active = False

    engine._target_authorization = revoke
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    request.assert_not_called()


@pytest.mark.parametrize("phase", ["prepare", "apply"])
@pytest.mark.parametrize("failure", ["expiry", "other_app", "partial", "absent"])
def test_account_expiry_related_app_ceiling_and_structural_guards(monkeypatch, phase, failure):
    engine, auth, target, request = account(monkeypatch)
    prepared = prepare_account(engine) if phase == "apply" else None
    if failure == "expiry":
        target.allow_expiry = False
    elif failure == "other_app":
        changed = deepcopy(USER)
        changed["grants"].append(
            {
                "name": NAME + "/grants/" + OTHER_APP,
                "packageName": OTHER_APP,
                "appLevelPermissions": [PERM],
            }
        )
        request.side_effect = lambda *a, **kw: {"users": [changed]}
    elif failure == "partial":
        request.side_effect = lambda *a, **kw: {"users": [{**USER, "partial": True}]}
    else:
        request.side_effect = lambda *a, **kw: {"users": []}
    with pytest.raises(PlayError):
        apply(engine, prepared) if prepared else prepare_account(engine)
    assert all(call.args[0]["method"] == READ_METHOD for call in request.call_args_list)


def test_each_page_rechecks_authority_and_stops_on_revocation(monkeypatch):
    engine, auth, target, request = account(monkeypatch)

    def page(*a, **kw):
        auth.active = False
        return {"nextPageToken": "second"}

    request.side_effect = page
    with pytest.raises(PlayError, match="revoked"):
        prepare_account(engine)
    assert request.call_count == 1


@pytest.mark.parametrize("limit", [40, 400])
def test_aggregate_budget_counts_discarded_users_and_target_preview(monkeypatch, limit):
    engine, auth, target, request = account(monkeypatch, snapshot_limit=limit)
    other = {"name": f"developers/{D}/users/other@example.test", "email": "other@example.test"}
    request.side_effect = [{"users": [other], "nextPageToken": "next"}, {"users": [deepcopy(USER)]}]
    with pytest.raises(PlayError, match="capacity"):
        prepare_account(engine)
    assert all(call.args[0]["method"] == READ_METHOD for call in request.call_args_list)
    assert not engine._preparations


def test_known_account_success_survives_expiry_before_readback(monkeypatch):
    engine, auth, target, request = account(monkeypatch)
    prepared = prepare_account(engine)

    def response(spec, token, **kw):
        if spec["method"] == READ_METHOD:
            return {"users": [deepcopy(USER)]}
        auth.active = False
        return deepcopy(USER)

    request.side_effect = response
    result = apply(engine, prepared)
    assert result["mutation_succeeded"] and result["readback_succeeded"] is False
    assert (
        len([call for call in request.call_args_list if call.args[0]["method"] != READ_METHOD]) == 1
    )


def test_signing_known_success_survives_revocation_without_get(monkeypatch):
    engine, auth, request = signer(monkeypatch)
    prepared = prepare_signing(engine)

    def execute(*a, **kw):
        auth.active = False
        return deepcopy(HASH_RESPONSE)

    request.side_effect = execute
    result = apply(engine, prepared)
    assert result["mutation_succeeded"] and result["readback_available"] is False
    assert request.call_count == 1


@pytest.mark.parametrize("family", ["account", "signing"])
@pytest.mark.parametrize("action", ["dispose", "close", "guard_busy", "expired"])
def test_disposal_and_failure_consume_private_material(monkeypatch, family, action):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        prepared = prepare_account(engine)
    else:
        engine, auth, request = signer(monkeypatch)
        prepared = prepare_signing(engine)
    p = engine._preparations[prepared["operation_id"]]
    if action == "dispose":
        assert engine._dispose(prepared["operation_id"]) == "disposed"
    elif action == "close":
        engine.close()
    else:
        if action == "guard_busy":
            engine._apply_guard.acquire()
        else:
            p.deadline = 0
        try:
            with pytest.raises(PlayError):
                apply(engine, prepared)
        finally:
            if action == "guard_busy":
                engine._apply_guard.release()
    assert p.token == "" and p.spec == {}
    assert not engine._preparations


def test_signing_credential_acquisition_does_not_hold_state_lock(monkeypatch):
    engine, auth, request = signer(monkeypatch)

    def token(_):
        assert engine._lock.acquire(blocking=False)
        engine._lock.release()
        return TOKEN

    engine.auth.token.side_effect = token
    prepare_signing(engine)


def test_signing_exact_pair_denial_before_credentials(monkeypatch):
    engine, auth, request = signer(monkeypatch)
    with pytest.raises(PlayError, match="pair"):
        engine.prepare(SIGN_METHOD, {"name": OTHER_APP}, SIGN_BODY, True)
    engine.auth.token.assert_not_called()
    request.assert_not_called()


class Response:
    status = 200

    def __init__(self, data, hook=lambda: None):
        self.data, self.hook = json.dumps(data).encode(), hook
        self.read_size = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, size):
        self.read_size = size
        self.hook()
        return self.data[:size]


@pytest.mark.parametrize("mutation", [False, True])
def test_real_account_transport_bounded_and_preserves_completed_write(monkeypatch, mutation):
    auth = Authorization()
    spec = (
        request_spec(METHOD, PARAMS, {})
        if mutation
        else request_spec(READ_METHOD, {"parent": f"developers/{D}"})
    )
    reply = Response(
        USER if mutation else {"users": [USER]}, lambda: setattr(auth, "active", False)
    )
    opener = Mock(handlers=[])
    opener.open.return_value = reply
    monkeypatch.setattr(http.urllib.request, "build_opener", Mock(return_value=opener))
    if mutation:
        assert (
            http.request(
                spec, TOKEN, deadline=auth.deadline, _max_bytes=1024, _execution_authorization=auth
            )
            == USER
        )
    else:
        with pytest.raises(PlayError):
            http.request(
                spec, TOKEN, deadline=auth.deadline, _max_bytes=1024, _execution_authorization=auth
            )
    assert reply.read_size == 1025
    assert opener.open.call_count == 1


def test_real_signing_transport_checks_hook_before_post(monkeypatch):
    auth = Authorization()
    opener = Mock(handlers=[])
    monkeypatch.setattr(signing.urllib.request, "build_opener", Mock(return_value=opener))
    auth.active = False
    spec = signing.request_spec(SIGN_METHOD, {"name": APP}, SIGN_BODY)
    with pytest.raises(PlayError, match="revoked"):
        signing.execute(spec, TOKEN, deadline=auth.deadline, _execution_authorization=auth)
    opener.open.assert_not_called()


@pytest.mark.parametrize("family", ["account", "signing"])
def test_target_hook_cannot_mutate_validated_private_intent(monkeypatch, family):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        original = engine._target_authorization

        def hook(spec, before):
            original(spec, before)
            spec["body"]["developerAccountPermissions"] = ["CAN_MANAGE_PERMISSIONS_GLOBAL"]
            if before:
                before["grants"].clear()

        engine._target_authorization = hook
        prepared = prepare_account(engine)
        assert prepared["proposed_request"]["body"] == {}
        assert prepared["before"]["grants"] == USER["grants"]
    else:
        engine, auth, request = signer(monkeypatch)
        original = engine._target_authorization

        def hook(spec):
            original(spec)
            spec["body"]["enrollExistingApp"]["cloudKmsKey"]["cryptoKeyVersionResource"] = "other"

        engine._target_authorization = hook
        prepared = prepare_signing(engine)
    assert apply(engine, prepared)["mutation_succeeded"]
    mutation = next(
        call.args[0] for call in request.call_args_list if call.args[0]["method"] != READ_METHOD
    )
    assert mutation["body"] == ({} if family == "account" else SIGN_BODY)


@pytest.mark.parametrize("family", ["account", "signing"])
def test_unknown_outcome_consumes_and_clears_without_retry(monkeypatch, family):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        prepared = prepare_account(engine)

        def fail(spec, token, **kw):
            if spec["method"] == READ_METHOD:
                return {"users": [deepcopy(USER)]}
            raise PlayError(http.UNKNOWN_OUTCOME)

        request.side_effect = fail
    else:
        engine, auth, request = signer(monkeypatch)
        prepared = prepare_signing(engine)
        request.side_effect = PlayError(signing.UNKNOWN)
    p = engine._preparations[prepared["operation_id"]]
    with pytest.raises(PlayError, match="unknown"):
        apply(engine, prepared)
    assert p.spec == {} and p.token == ""
    calls = request.call_count
    with pytest.raises(PlayError, match="consumed"):
        apply(engine, prepared)
    assert request.call_count == calls


@pytest.mark.parametrize("family", ["account", "signing"])
def test_expiry_during_credentials_never_sends_provider_request(monkeypatch, family):
    if family == "account":
        engine, auth, target, request = account(monkeypatch)
        prepare = prepare_account
    else:
        engine, auth, request = signer(monkeypatch)
        prepare = prepare_signing
    # The original deadline is clamped before credential acquisition.
    engine.auth.token.side_effect = lambda _: (
        monkeypatch.setattr(time, "monotonic", lambda: auth.deadline + 1) or TOKEN
    )
    with pytest.raises(PlayError, match="expired"):
        prepare(engine)
    request.assert_not_called()
    assert not engine._preparations
