"""Independent support-operation abuse cases; all provider traffic is synthetic."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest
from mcp import Client

from pubship import support_http
from pubship.config import Settings
from pubship.coverage import SUPPORT_METHODS
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK
from pubship.server import create_server
from pubship.support import SupportOperations

APP = "com.example.app"
METHOD = "androidpublisher.reviews.reply"
PARAMETERS = {"packageName": APP, "reviewId": "review-1"}
BODY = {"replyText": "Thanks for your feedback."}
BEFORE = {
    "reviewId": "review-1",
    "comments": [
        {"userComment": {"text": "Useful", "lastModified": {"seconds": "1"}, "starRating": 5}}
    ],
}
MUTATION = {"result": {"replyText": BODY["replyText"], "lastEdited": {"seconds": "2"}}}


def configured(**overrides):
    values = {
        "packages": frozenset({APP}),
        "support_packages": frozenset({APP}),
        "support_methods": SUPPORT_METHODS,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture(autouse=True)
def forbid_real_provider_requests():
    with patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("No live traffic")):
        yield


@pytest.fixture
def service(monkeypatch):
    auth = Mock()
    auth.token.return_value = "synthetic-original-private"
    svc = SupportOperations(configured(), auth, local=True)
    observation = Mock(side_effect=lambda *a, **k: deepcopy(BEFORE))
    mutation = Mock(return_value=deepcopy(MUTATION))
    monkeypatch.setattr(support_http, "observe", observation)
    monkeypatch.setattr(support_http, "execute", mutation)
    return svc, auth, observation, mutation


def prepare(svc):
    return svc.prepare(METHOD, deepcopy(PARAMETERS), deepcopy(BODY), True)


def apply(svc, prepared):
    op = prepared["operation_id"]
    return svc.apply(op, op)


@pytest.mark.parametrize(
    "overrides",
    [
        {"support_packages": frozenset()},
        {"support_methods": frozenset()},
        {"support_methods": frozenset({"androidpublisher.applications.dataSafety"})},
        {"packages": frozenset(), "support_packages": frozenset()},
    ],
)
def test_missing_independent_grants_fail_before_auth_or_provider(service, overrides):
    svc, auth, observed, mutation = service
    svc.settings = configured(**overrides)
    with pytest.raises(PlayError):
        prepare(svc)
    auth.token.assert_not_called()
    observed.assert_not_called()
    mutation.assert_not_called()


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true", [], {}])
def test_acknowledgement_is_explicit_boolean_before_auth(service, ack):
    svc, auth, observed, mutation = service
    with pytest.raises(PlayError):
        svc.prepare(METHOD, PARAMETERS, BODY, ack)
    auth.token.assert_not_called()
    observed.assert_not_called()
    mutation.assert_not_called()


@pytest.mark.parametrize("local", [False, None, 1, "true"])
def test_local_capability_cannot_be_enabled_by_truthy_values(service, local):
    svc, auth, observed, mutation = service
    svc.local = local
    with pytest.raises(PlayError):
        prepare(svc)
    auth.token.assert_not_called()
    observed.assert_not_called()
    mutation.assert_not_called()


def test_reviewed_intent_and_original_credential_survive_caller_and_preview_mutation(service):
    svc, auth, observed, mutation = service
    parameters, body = deepcopy(PARAMETERS), deepcopy(BODY)
    prepared = svc.prepare(METHOD, parameters, body, True)
    parameters["reviewId"] = "unreviewed-target"
    body["replyText"] = "unreviewed-text"
    prepared["proposed_request"]["parameters"]["reviewId"] = "tampered-preview"
    prepared["proposed_request"]["body"]["replyText"] = "tampered-preview"
    auth.token.return_value = "synthetic-other-account"
    result = apply(svc, prepared)
    mutation.assert_called_once_with(METHOD, PARAMETERS, BODY, "synthetic-original-private")
    auth.token.assert_called_once_with("publisher")
    assert all(call.args[1] == "synthetic-original-private" for call in observed.call_args_list)
    assert result["executed"] and result["mutation_succeeded"]
    serialized = json.dumps(prepared) + json.dumps(result)
    assert "synthetic-original-private" not in serialized
    assert "synthetic-other-account" not in serialized
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    mutation.assert_called_once()


@pytest.mark.parametrize("grant", ["support_packages", "support_methods", "packages"])
def test_revoked_scope_at_apply_consumes_without_dispatch(service, grant):
    svc, auth, observed, mutation = service
    prepared = prepare(svc)
    updates = {grant: frozenset()}
    if grant == "packages":
        updates["support_packages"] = frozenset()
    svc.settings = replace(svc.settings, **updates)
    observed.reset_mock()
    with pytest.raises(PlayError):
        apply(svc, prepared)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    observed.assert_not_called()
    mutation.assert_not_called()
    auth.token.assert_called_once()


def test_bad_confirmation_does_not_consume_preparation(service):
    svc, _, _, mutation = service
    prepared = prepare(svc)
    with pytest.raises(PlayError):
        svc.apply(prepared["operation_id"], prepared["operation_id"] + "x")
    mutation.assert_not_called()
    assert apply(svc, prepared)["mutation_succeeded"]
    mutation.assert_called_once()


def test_expired_preparation_is_consumed_before_provider_access(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    svc._preparations[prepared["operation_id"]].deadline = 0
    observed.reset_mock()
    with pytest.raises(PlayError, match="expired"):
        apply(svc, prepared)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    observed.assert_not_called()
    mutation.assert_not_called()


def test_expiry_during_preflight_prevents_mutation(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)

    def expire(*args, **kwargs):
        retained.deadline = 0
        return deepcopy(BEFORE)

    retained = svc._preparations[prepared["operation_id"]]
    observed.side_effect = expire
    with pytest.raises(PlayError, match="expired"):
        apply(svc, prepared)
    mutation.assert_not_called()


def test_stale_review_content_consumes_operation(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    changed = deepcopy(BEFORE)
    changed["comments"][0]["userComment"]["text"] = "A changed complaint"
    observed.side_effect = lambda *a, **k: changed
    with pytest.raises(PlayError, match="changed|drift|stale"):
        apply(svc, prepared)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    mutation.assert_not_called()


def test_global_publishing_lock_consumes_contending_operation(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    observed.reset_mock()
    assert _APPLY_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(PlayError, match="in progress"):
            apply(svc, prepared)
    finally:
        _APPLY_LOCK.release()
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    observed.assert_not_called()
    mutation.assert_not_called()


def test_concurrent_replay_dispatches_once(service):
    svc, _, _, mutation = service
    prepared = prepare(svc)
    entered, release = threading.Event(), threading.Event()

    def slow_write(*args):
        entered.set()
        assert release.wait(5)
        return deepcopy(MUTATION)

    mutation.side_effect = slow_write
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(apply, svc, prepared)
        try:
            assert entered.wait(2)
            with pytest.raises(PlayError, match="consumed"):
                apply(svc, prepared)
        finally:
            release.set()
        assert pending.result(timeout=2)["mutation_succeeded"]
    mutation.assert_called_once()
    assert _APPLY_LOCK.acquire(blocking=False)
    _APPLY_LOCK.release()


def test_capacity_rejects_before_auth_and_expired_entries_release_capacity(service, monkeypatch):
    svc, auth, observed, mutation = service
    monkeypatch.setattr("pubship.support.MAX_PREPARATIONS", 1)
    first = prepare(svc)
    with pytest.raises(PlayError, match="capacity"):
        prepare(svc)
    auth.token.assert_called_once()
    observed.assert_called_once()
    mutation.assert_not_called()
    svc._preparations[first["operation_id"]].deadline = 0
    second = prepare(svc)
    assert second["operation_id"] != first["operation_id"]
    assert first["operation_id"] not in svc._preparations


def test_readback_failure_preserves_confirmed_success_and_redacts_provider_error(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    observed.side_effect = [deepcopy(BEFORE), RuntimeError("PRIVATE provider payload")]
    result = apply(svc, prepared)
    assert result["executed"] and result["mutation_succeeded"]
    assert result["readback_succeeded"] is False
    assert "PRIVATE" not in json.dumps(result)
    assert "consumed" in result["observation_error"]
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    mutation.assert_called_once()


def test_uncertain_mutation_does_not_retry_and_releases_global_lock(service):
    svc, _, _, mutation = service
    prepared = prepare(svc)
    mutation.side_effect = PlayError("Support mutation outcome unknown; no retry")
    with pytest.raises(PlayError, match="unknown"):
        apply(svc, prepared)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    mutation.assert_called_once()
    assert _APPLY_LOCK.acquire(blocking=False)
    _APPLY_LOCK.release()


def test_no_supported_observation_never_claims_stale_or_readback_verification(service):
    svc, _, observed, mutation = service
    observed.side_effect = None
    observed.return_value = None
    mutation.return_value = {}
    prepared = svc.prepare(
        "androidpublisher.applications.dataSafety",
        {"packageName": APP},
        {"safetyLabels": "question_id,response\nsynthetic-question,synthetic-answer"},
        True,
    )
    assert prepared["before"] is None
    result = apply(svc, prepared)
    assert result["mutation_succeeded"]
    assert result["stale_check_performed"] is False
    assert result["readback_available"] is False
    assert result["readback_succeeded"] is None
    assert "No readback API" in result["observation_note"]


def test_hosted_factory_never_exposes_support_tools_even_with_process_opt_ins(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_SUPPORT_PACKAGES", APP)
    monkeypatch.setenv("GOOGLE_PLAY_SUPPORT_METHODS", ",".join(sorted(SUPPORT_METHODS)))
    factory = Mock(side_effect=AssertionError("Discovery must not load customer credentials"))

    async def exercise():
        async with Client(create_server(service_factory=factory)) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert not any("support" in name for name in names)
            assert len(names) == 11

    asyncio.run(exercise())
    factory.assert_not_called()


def test_public_vote_counters_do_not_invalidate_reviewed_reply(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    changed = deepcopy(BEFORE)
    changed["comments"][0]["userComment"].update(thumbsUpCount=42, thumbsDownCount=3)
    observed.side_effect = lambda *a, **k: changed
    assert apply(svc, prepared)["mutation_succeeded"]
    mutation.assert_called_once()


def test_concurrent_developer_reply_invalidates_reviewed_reply(service):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    changed = deepcopy(BEFORE)
    changed["comments"].append(
        {"developerComment": {"text": "A colleague replied", "lastModified": {"seconds": "3"}}}
    )
    observed.side_effect = lambda *a, **k: changed
    with pytest.raises(PlayError, match="changed|drift|stale"):
        apply(svc, prepared)
    mutation.assert_not_called()


def test_preparation_expiry_during_authentication_never_observes(service, monkeypatch):
    svc, auth, observed, mutation = service
    now = [100.0]
    monkeypatch.setattr("pubship.support.time.monotonic", lambda: now[0])

    def expired_auth(*args):
        now[0] += 601
        return "synthetic-original-private"

    auth.token.side_effect = expired_auth
    with pytest.raises(PlayError, match="expired"):
        prepare(svc)
    assert not svc._preparations
    observed.assert_not_called()
    mutation.assert_not_called()


@pytest.mark.parametrize("response", [{}, {"result": {}}, {"result": {"replyText": "x"}}])
def test_inconclusive_write_response_is_unknown_consumed_and_not_retried(service, response):
    svc, _, observed, mutation = service
    prepared = prepare(svc)
    observed.reset_mock()
    mutation.return_value = response
    with pytest.raises(PlayError, match="unknown"):
        apply(svc, prepared)
    with pytest.raises(PlayError, match="consumed"):
        apply(svc, prepared)
    mutation.assert_called_once()
    observed.assert_called_once()


@pytest.mark.parametrize("already_all_users", [False, True])
def test_recovery_can_expand_to_all_users_but_cannot_expand_existing_all_users(
    service, already_all_users
):
    svc, _, observed, mutation = service
    targeting = {"versionList": {"versionCodes": ["42"]}}
    targeting.update(
        {"allUsers": {"isAllUsersRequested": True}}
        if already_all_users
        else {"regions": {"regionCode": ["US"]}}
    )
    observed.side_effect = lambda *a, **k: {
        "appRecoveryId": "7",
        "status": "RECOVERY_STATUS_ACTIVE",
        "lastUpdateTime": "2026-01-01T00:00:00Z",
        "targeting": targeting,
    }
    mutation.return_value = {}

    def prepare_expansion():
        return svc.prepare(
            "androidpublisher.apprecovery.addTargeting",
            {"packageName": APP, "appRecoveryId": "7"},
            {"targetingUpdate": {"allUsers": {"isAllUsersRequested": True}}},
            True,
            observation_version_code="42",
        )

    if already_all_users:
        with pytest.raises(PlayError):
            prepare_expansion()
        mutation.assert_not_called()
    else:
        assert apply(svc, prepare_expansion())["mutation_succeeded"]
        mutation.assert_called_once()
