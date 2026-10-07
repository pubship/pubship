"""Synthetic hosted operation isolation and bounded lifecycle evidence; no Google I/O."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship import edit_lifecycle_http, http, publishing_http, track_staging_http
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.hosted_edits import MAX_OPERATION_BYTES, HostedEditRegistry
from pubship.hosted_grants import ALL_SCOPES, ConnectionAuthorization
from pubship.publishing import _APPLY_LOCK

PACKAGE = "com.example.app"
PARAMETERS = {"packageName": PACKAGE, "editId": "edit-1", "language": "en-US"}


class Provider:
    def __init__(self):
        self.callbacks = []
        self.locks = {}
        self.revoked = set()
        self.allowed = {scope: frozenset({PACKAGE}) for scope in ALL_SCOPES}
        self.auth = Mock(token=Mock(return_value="original-synthetic-token"))
        self.services = Mock(return_value=(Settings(packages=frozenset({PACKAGE})), self.auth))

    def grant_lock(self, subject):
        return self.locks.setdefault(subject, threading.RLock())

    def authorize_current(self, principal, capability=None, package=None):
        if (
            principal is None
            or principal.subject in self.revoked
            or principal.expires <= time.time()
        ):
            raise PlayError("Authorization expired or revoked.")
        if capability and (
            capability not in self.allowed or package not in self.allowed[capability]
        ):
            raise PlayError("Capability or package denied.")
        return ConnectionAuthorization(
            principal.subject,
            principal.client_id,
            self.allowed,
            principal.expires,
            frozenset(self.allowed),
            frozenset({"publisher"}),
            False,
        )

    def register_invalidator(self, callback):
        self.callbacks.append(callback)

    def disconnect(self, subject):
        with self.grant_lock(subject):
            self.revoked.add(subject)
            for callback in self.callbacks:
                callback(subject)


def principal(subject="connection-a", client="client-a"):
    return SimpleNamespace(subject=subject, client_id=client, expires=time.time() + 3600)


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def registry(provider):
    value = HostedEditRegistry(provider)
    yield value
    value.close()


@pytest.fixture
def transport(monkeypatch):
    edit = {"id": "edit-1", "expiryTimeSeconds": str(int(time.time()) + 3600)}

    def get(url, token, **kwargs):
        if "/listings/" in url:
            return {"language": "en-US", "title": "old"}
        if "/tracks/" in url:
            return {"track": "production", "releases": []}
        return dict(edit)

    reads = Mock(side_effect=get)
    patch = Mock(return_value={"language": "en-US", "title": "new"})
    put = Mock(
        return_value={
            "track": "production",
            "releases": [{"status": "draft", "versionCodes": ["1"]}],
        }
    )
    lifecycle = Mock(
        side_effect=lambda action, parameters, token, **kwargs: (
            {} if action == "discard" else dict(edit)
        )
    )
    monkeypatch.setattr(http, "get", reads)
    monkeypatch.setattr(publishing_http, "listing_patch", patch)
    monkeypatch.setattr(track_staging_http, "execute", put)
    monkeypatch.setattr(edit_lifecycle_http, "execute", lifecycle)
    return SimpleNamespace(reads=reads, patch=patch, put=put, lifecycle=lifecycle, get=get)


def prepare(registry, who=None):
    return registry.prepare(who or principal(), "listing", PARAMETERS, {"title": "new"})


def test_original_token_exact_payload_replay_and_no_secret_output(registry, provider, transport):
    body = {"title": "new"}
    p = registry.prepare(principal(), "listing", PARAMETERS, body)
    body["title"] = "caller-mutated"
    p["proposed_request"]["body"]["title"] = "result-mutated"
    provider.auth.token.return_value = "changed-synthetic-token"
    result = registry.apply(principal(), "listing", p["operation_id"], p["operation_id"])
    assert result["operation_id"] == p["operation_id"]
    assert result["patch_attempted"] and not result["committed"]
    assert transport.patch.call_args.args[1:] == ("original-synthetic-token", {"title": "new"})
    provider.auth.token.assert_called_once_with("publisher")
    assert "synthetic-token" not in json.dumps([p, result])
    assert not registry._records
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(principal(), "listing", p["operation_id"], p["operation_id"])


@pytest.mark.parametrize("thief", [principal("connection-b"), principal(client="client-b")])
def test_stolen_ids_cannot_consume_owner(registry, transport, thief):
    p = prepare(registry)["operation_id"]
    with pytest.raises(PlayError, match="Unknown"):
        registry.apply(thief, "listing", p, p)
    assert p in registry._records
    registry.apply(principal(), "listing", p, p)
    transport.patch.assert_called_once()


def test_wrong_family_and_confirmation_do_not_consume(registry, transport):
    p = prepare(registry)["operation_id"]
    for kind, confirmation in [("track", p), ("listing", "wrong")]:
        with pytest.raises(PlayError):
            registry.apply(principal(), kind, p, confirmation)
    registry.apply(principal(), "listing", p, p)
    transport.patch.assert_called_once()


def test_cross_package_and_missing_capability_deny_before_credentials(
    registry, provider, transport
):
    with pytest.raises(PlayError, match="denied"):
        registry.prepare(
            principal(), "listing", {**PARAMETERS, "packageName": "com.other.app"}, {"title": "new"}
        )
    del provider.allowed["play:edit-listings"]
    with pytest.raises(PlayError, match="denied"):
        prepare(registry)
    provider.services.assert_not_called()
    transport.reads.assert_not_called()


def test_capability_removed_between_prepare_apply_never_dispatches(registry, provider, transport):
    p = prepare(registry)["operation_id"]
    del provider.allowed["play:edit-listings"]
    with pytest.raises(PlayError, match="denied"):
        registry.apply(principal(), "listing", p, p)
    transport.patch.assert_not_called()


def test_disconnect_erases_preparations_and_retained_engine(registry, provider, transport):
    p = prepare(registry)["operation_id"]
    engine = registry._records[p].engine
    provider.disconnect("connection-a")
    assert not registry._records and not engine._preparations
    with pytest.raises(PlayError, match="revoked"):
        registry.apply(principal(), "listing", p, p)
    transport.patch.assert_not_called()


def test_expired_authorization_before_dispatch_consumes(registry, transport):
    who = principal()
    p = prepare(registry, who)["operation_id"]

    def expiring_read(*args, **kwargs):
        result = transport.get(*args, **kwargs)
        who.expires = time.time() - 1
        return result

    transport.reads.side_effect = expiring_read
    with pytest.raises(PlayError, match="expired"):
        registry.apply(who, "listing", p, p)
    assert not registry._records
    transport.patch.assert_not_called()


def test_expiry_cleanup_and_restart_lose_preparations(registry, provider, transport):
    p = prepare(registry)["operation_id"]
    engine = registry._records[p].engine
    registry._records[p].deadline = time.monotonic() - 1
    registry.cleanup()
    assert not engine._preparations and not registry._records
    p = prepare(registry)["operation_id"]
    other = HostedEditRegistry(provider)
    try:
        with pytest.raises(PlayError, match="Unknown"):
            other.apply(principal(), "listing", p, p)
    finally:
        other.close()
    registry.close()
    assert not registry._records and not registry._worker.is_alive()
    with pytest.raises(PlayError, match="shutting down"):
        prepare(registry)


def test_periodic_cleanup(provider, transport):
    registry = HostedEditRegistry(provider, cleanup_interval=0.01)
    try:
        p = prepare(registry)["operation_id"]
        registry._records[p].deadline = 0
        deadline = time.monotonic() + 1
        while registry._records and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        assert not registry._records
    finally:
        registry.close()


@pytest.mark.parametrize(
    "limits",
    [
        {"max_per_connection": 1},
        {"max_total": 1},
        {"max_payload_bytes_per_connection": MAX_OPERATION_BYTES},
        {"max_payload_bytes_total": MAX_OPERATION_BYTES},
    ],
)
def test_count_and_payload_reservations_precede_provider_io(provider, transport, limits):
    registry = HostedEditRegistry(provider, **limits)
    try:
        prepare(registry)
        transport.reads.reset_mock()
        with pytest.raises(PlayError, match="capacity"):
            prepare(registry)
        transport.reads.assert_not_called()
        assert len(registry._records) == 1
    finally:
        registry.close()


def test_global_limit_across_connections(provider, transport):
    registry = HostedEditRegistry(provider, max_total=1)
    try:
        prepare(registry)
        with pytest.raises(PlayError, match="capacity"):
            prepare(registry, principal("connection-b"))
    finally:
        registry.close()


def test_oversize_and_failed_prepare_release_reservation(registry, provider, transport):
    with pytest.raises(PlayError, match="256 KiB"):
        registry.prepare(principal(), "listing", PARAMETERS, {"title": "a" * (256 * 1024)})
    provider.services.assert_not_called()
    transport.reads.side_effect = PlayError("synthetic read failure")
    with pytest.raises(PlayError, match="read failure"):
        prepare(registry)
    assert not registry._records


def test_baseline_drift_and_unknown_write_results_consume(registry, transport):
    p = prepare(registry)["operation_id"]

    def changed(url, token):
        result = transport.get(url, token)
        if "/listings/" in url:
            result["title"] = "external change"
        return result

    transport.reads.side_effect = changed
    with pytest.raises(PlayError, match="baseline changed"):
        registry.apply(principal(), "listing", p, p)
    transport.patch.assert_not_called()
    transport.reads.side_effect = transport.get
    p = prepare(registry)["operation_id"]
    transport.patch.side_effect = PlayError("synthetic unknown outcome")
    with pytest.raises(PlayError, match="unknown outcome"):
        registry.apply(principal(), "listing", p, p)
    assert not registry._records
    transport.patch.assert_called_once()


@pytest.mark.parametrize("action", ["create", "validate", "discard", "commit"])
def test_each_lifecycle_action_has_separate_capability(registry, provider, transport, action):
    parameters = {"packageName": PACKAGE}
    if action != "create":
        parameters["editId"] = "edit-1"
    p = registry.prepare(
        principal(), "lifecycle", parameters, action=action, acknowledge_effects=True
    )
    registry.apply(principal(), "lifecycle", p["operation_id"], p["operation_id"])
    assert transport.lifecycle.call_args.args[0] == action
    del provider.allowed["play:edit-" + action]
    with pytest.raises(PlayError, match="denied"):
        registry.prepare(
            principal(), "lifecycle", parameters, action=action, acknowledge_effects=True
        )


def test_track_full_replacement_original_token(registry, transport):
    p = registry.prepare(
        principal(),
        "track",
        {"packageName": PACKAGE, "editId": "edit-1", "track": "production"},
        [{"status": "draft", "versionCodes": ["1"]}],
        True,
    )
    result = registry.apply(principal(), "track", p["operation_id"], p["operation_id"])
    assert result["executed"] and not result["committed"]
    assert transport.put.call_args.args[-1] == "original-synthetic-token"


def test_per_connection_guards_do_not_use_local_global(registry, transport):
    a = prepare(registry)["operation_id"]
    b = prepare(registry)["operation_id"]
    c = prepare(registry, principal("connection-b"))["operation_id"]
    assert registry._records[a].guard is registry._records[b].guard
    assert registry._records[a].guard is not registry._records[c].guard
    assert registry._records[a].guard is not _APPLY_LOCK
    with _APPLY_LOCK:
        registry.apply(principal(), "listing", a, a)


def test_apply_orders_disconnect_and_allows_other_connection(registry, provider, transport):
    owner = principal()
    a = prepare(registry, owner)["operation_id"]
    b = prepare(registry, principal("connection-b"))["operation_id"]
    started, release, disconnected = threading.Event(), threading.Event(), threading.Event()

    def blocking_patch(url, token, body, **kwargs):
        if threading.current_thread().name.startswith("owner"):
            started.set()
            assert release.wait(3)
        return {"language": "en-US", "title": "new"}

    transport.patch.side_effect = blocking_patch
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="owner") as pool:
        applying = pool.submit(registry.apply, owner, "listing", a, a)
        assert started.wait(2)

        def revoke():
            provider.disconnect(owner.subject)
            disconnected.set()

        revoker = threading.Thread(target=revoke)
        revoker.start()
        try:
            assert not disconnected.wait(0.05)
            # A second customer's provider I/O is independent.
            registry.apply(principal("connection-b"), "listing", b, b)
            assert a in registry._records and registry._records[a].applying
        finally:
            release.set()
        assert applying.result(timeout=2)["executed"]
        revoker.join(2)
        assert disconnected.is_set()
    assert not registry._records


def test_concurrent_replay_one_dispatch(registry, transport):
    p = prepare(registry)["operation_id"]

    def apply():
        try:
            return registry.apply(principal(), "listing", p, p)["executed"]
        except PlayError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(lambda _: apply(), range(2))) == [False, True]
    transport.patch.assert_called_once()


def test_shutdown_during_preflight_denies_dispatch(registry, transport):
    p = prepare(registry)["operation_id"]

    def shutdown_read(*args, **kwargs):
        result = transport.get(*args, **kwargs)
        registry.close()
        return result

    transport.reads.side_effect = shutdown_read
    with pytest.raises(PlayError, match="shutting down"):
        registry.apply(principal(), "listing", p, p)
    transport.patch.assert_not_called()
    assert not registry._records


def test_inflight_apply_keeps_global_capacity_reserved(provider, transport):
    registry = HostedEditRegistry(provider, max_total=1)
    started, release = threading.Event(), threading.Event()
    try:
        operation = prepare(registry)["operation_id"]

        def block(url, token, body, **kwargs):
            started.set()
            assert release.wait(3)
            return {"language": "en-US", "title": "new"}

        transport.patch.side_effect = block
        with ThreadPoolExecutor(max_workers=1) as pool:
            applied = pool.submit(registry.apply, principal(), "listing", operation, operation)
            assert started.wait(2)
            try:
                with pytest.raises(PlayError, match="capacity"):
                    prepare(registry, principal("connection-b"))
                registry._records[operation].deadline = 0
                registry.cleanup()
                assert operation in registry._records
            finally:
                release.set()
            assert applied.result(timeout=2)["executed"]
        assert not registry._records
    finally:
        release.set()
        registry.close()


def test_inflight_prepare_reserves_before_read_and_close_clears(provider, transport):
    registry = HostedEditRegistry(provider, max_total=1)
    started, release = threading.Event(), threading.Event()

    def block(url, token):
        started.set()
        assert release.wait(3)
        return transport.get(url, token)

    transport.reads.side_effect = block
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            prepared = pool.submit(prepare, registry)
            assert started.wait(2)
            try:
                with pytest.raises(PlayError, match="capacity"):
                    prepare(registry, principal("connection-b"))
                registry.close()
            finally:
                release.set()
            with pytest.raises(PlayError, match="stopped|shutting down"):
                prepared.result(timeout=2)
        assert not registry._records
    finally:
        release.set()
        registry.close()


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf"), True])
def test_cleanup_interval_is_finite_and_positive(provider, interval):
    with pytest.raises(ValueError):
        HostedEditRegistry(provider, cleanup_interval=interval)
    assert not provider.callbacks


def test_real_provider_same_google_account_distinct_connection_and_fresh_access_token(
    tmp_path, transport
):
    import asyncio

    from cryptography.fernet import Fernet
    from test_hosted_grants import APP, issue

    from pubship.hosted_config import HostedSettings
    from pubship.oauth import PlayOAuth
    from pubship.vault import Vault

    tmp_path.chmod(0o700)
    settings = HostedSettings(
        "https://mcp.example.test",
        "synthetic-client",
        "synthetic-secret",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
    )
    vault = Vault(settings.database, settings.encryption_key)
    provider = PlayOAuth(settings, vault)
    registry = HostedEditRegistry(provider)
    try:
        _, _, owner = issue(provider)
        _, _, thief = issue(provider, subject="second-connection", client="same-account-client")
        parameters = {**PARAMETERS, "packageName": APP}
        operation = registry.prepare(owner, "listing", parameters, {"title": "new"})["operation_id"]
        with pytest.raises(PlayError, match="Unknown"):
            registry.apply(thief, "listing", operation, operation)
        token = provider.issue_tokens(owner.subject, owner.client_id, owner.scopes)
        refreshed = asyncio.run(provider.load_access_token(token.access_token))
        vault.delete("access", owner.token)
        with pytest.raises(PlayError):
            registry.apply(owner, "listing", operation, operation)
        result = registry.apply(refreshed, "listing", operation, operation)
        assert result["executed"]
        assert transport.patch.call_args.args[1] == "synthetic-google-access"
        operation = registry.prepare(refreshed, "listing", parameters, {"title": "new"})[
            "operation_id"
        ]
        provider.disconnect(owner.subject)
        assert not registry._records
        with pytest.raises(PlayError):
            registry.apply(refreshed, "listing", operation, operation)
    finally:
        registry.close()
        vault.close()


def test_access_expiring_during_google_refresh_denies_snapshot_reads(registry, provider, transport):
    who = principal()

    def expire(service):
        who.expires = 0
        return "synthetic-token"

    provider.auth.token.side_effect = expire
    with pytest.raises(PlayError, match="expired"):
        prepare(registry, who)
    transport.reads.assert_not_called()
    assert not registry._records


def _prepare_family(registry, who, family):
    if family == "listing":
        return "listing", prepare(registry, who)["operation_id"]
    if family == "track":
        result = registry.prepare(
            who,
            "track",
            {"packageName": PACKAGE, "editId": "edit-1", "track": "production"},
            [{"status": "draft", "versionCodes": ["1"]}],
            True,
        )
        return "track", result["operation_id"]
    parameters = {"packageName": PACKAGE}
    if family != "create":
        parameters["editId"] = "edit-1"
    result = registry.prepare(
        who,
        "lifecycle",
        parameters,
        action=family,
        acknowledge_effects=True,
    )
    return "lifecycle", result["operation_id"]


@pytest.mark.parametrize("overrun", [0, 1], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize(
    "family,phase",
    [
        (family, phase)
        for family in ("listing", "track", "validate", "discard", "commit", "create")
        for phase in ("preflight", "authorization")
        if family != "create" or phase == "authorization"
    ],
)
def test_effective_expiry_survives_refreshed_access_and_slow_preflight(
    registry, provider, transport, monkeypatch, family, phase, overrun
):
    from pubship import hosted_edits

    # Move only the registry's monotonic clock. Engines keep a clock which has
    # not reached their former 600s TTL: this isolates the effective-expiry gate.
    clock = [time.monotonic()]
    monkeypatch.setattr(
        hosted_edits,
        "time",
        SimpleNamespace(
            monotonic=lambda: clock[0],
            time=time.time,
        ),
    )
    original = principal()
    original.expires = time.time() + 5
    kind, operation = _prepare_family(registry, original, family)
    record = registry._records[operation]
    engine = record.engine
    deadline = record.deadline
    assert deadline < clock[0] + 6
    other = principal("connection-b")
    other_kind, other_operation = _prepare_family(registry, other, family)
    other_engine = registry._records[other_operation].engine
    refreshed = principal()
    clock[0] = deadline - 0.1

    authorize = provider.authorize_current

    def slow_authorize(who, capability=None, package=None):
        result = authorize(who, capability, package)
        # The final hook runs after consumption, including create with no GET.
        # Advance after current-authority lookup returns, while it remains valid.
        if (
            phase == "authorization"
            and who.subject == original.subject
            and capability == record.capability
            and not engine._preparations
        ):
            clock[0] = deadline + overrun
        return result

    monkeypatch.setattr(provider, "authorize_current", slow_authorize)

    def slow_read(*args, **kwargs):
        result = transport.get(*args, **kwargs)
        if phase == "preflight":
            clock[0] = deadline + overrun
        return result

    transport.reads.side_effect = slow_read
    with pytest.raises(PlayError, match="expired"):
        registry.apply(refreshed, kind, operation, operation)
    for mutation in (transport.patch, transport.put, transport.lifecycle):
        mutation.assert_not_called()
    assert operation not in registry._records
    assert not engine._preparations
    assert engine._execution_authorization.principal is None
    with pytest.raises(PlayError, match="consumed"):
        registry.apply(refreshed, kind, operation, operation)

    # Neither the preparation nor execution guard of a different connection is
    # invalidated by the first connection's shorter original authorization.
    assert registry._records[other_operation].engine is other_engine
    result = registry.apply(other, other_kind, other_operation, other_operation)
    assert result["executed"]
    assert sum(m.call_count for m in (transport.patch, transport.put, transport.lifecycle)) == 1
    assert not registry._records
