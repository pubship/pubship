"""Trusted hosted hooks preserve local engines and stop work at dispatch boundaries."""

import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pubship import edit_writes, monetization
from pubship.config import Settings
from pubship.coverage import EDIT_WRITE_METHODS, MONETIZATION_WRITE_METHODS
from pubship.errors import PlayError
from pubship.monetization_contracts import observations, request_spec
from pubship.publishing import _APPLY_LOCK

APP = "com.example.hooks"


@pytest.fixture(params=["edit", "commerce"])
def engine(request, monkeypatch):
    state = SimpleNamespace(revoked=False, calls=0, expire=False)
    reads, writes = [], []
    auth = Mock(token=Mock(return_value="synthetic-original-token"))
    clock = [time.monotonic()]
    module = edit_writes if request.param == "edit" else monetization
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0], time=time.time))

    def authorize():
        state.calls += 1
        if state.revoked:
            raise PlayError("Synthetic authority revoked")
        if state.expire:
            clock[0] += 601

    if request.param == "edit":
        method = "androidpublisher.edits.details.patch"
        parameters = {"packageName": APP, "editId": "synthetic-edit"}
        body = {"contactEmail": "new@example.invalid"}
        current = {"defaultLanguage": "en-US", "contactEmail": "old@example.invalid"}
        edit = {"id": parameters["editId"], "expiryTimeSeconds": str(int(time.time()) + 3600)}
        settings = Settings(
            packages=frozenset({APP}),
            edit_write_packages=frozenset({APP}),
            edit_write_methods=EDIT_WRITE_METHODS,
        )
        cls = edit_writes.EditWrites

        def observe(url, token):
            reads.append((url, token))
            return deepcopy(current if url.endswith("/details") else edit)

        def execute(*args, **kwargs):
            writes.append(args)
            return {**current, **body}

        read_mock = Mock(side_effect=observe)
        write_mock = Mock(side_effect=execute)
        monkeypatch.setattr(edit_writes.http, "get", read_mock)
        monkeypatch.setattr(edit_writes.edit_writes_http, "execute", write_mock)
    else:
        method = "androidpublisher.monetization.subscriptions.patch"
        parameters = {
            "packageName": APP,
            "productId": "premium",
            "updateMask": "listings",
            "regionsVersion.version": "2022/02",
        }
        body = {
            "packageName": APP,
            "productId": "premium",
            "listings": [{"languageCode": "en-US", "title": "Premium"}],
        }
        current = deepcopy(body)
        settings = Settings(
            packages=frozenset({APP}),
            monetization_packages=frozenset({APP}),
            monetization_methods=MONETIZATION_WRITE_METHODS,
        )
        cls = monetization.Monetization

        def observe(url, token):
            reads.append((url, token))
            return {"http_status": 200, "data": deepcopy(current)}

        def execute(*args, **kwargs):
            writes.append(args)
            return {"packageName": APP, "productId": "premium"}

        read_mock = Mock(side_effect=observe)
        write_mock = Mock(side_effect=execute)
        monkeypatch.setattr(monetization.monetization_http, "observe", read_mock)
        monkeypatch.setattr(monetization.monetization_http, "execute", write_mock)
    service = cls(settings, auth, _execution_authorization=authorize, _apply_guard=threading.Lock())
    return SimpleNamespace(
        service=service,
        cls=cls,
        settings=settings,
        auth=auth,
        method=method,
        parameters=parameters,
        body=body,
        reads=reads,
        writes=writes,
        read_mock=read_mock,
        write_mock=write_mock,
        observe=observe,
        execute=execute,
        authorize=authorize,
        state=state,
        clock=clock,
        current=current,
    )


def prepare(engine):
    return engine.service.prepare(engine.method, engine.parameters, engine.body, True)[
        "operation_id"
    ]


def test_hosted_path_requires_hook_and_preserves_original_credential(engine):
    with pytest.raises(PlayError, match="local"):
        engine.cls(engine.settings, engine.auth).prepare(
            engine.method, engine.parameters, engine.body, True
        )
    operation = prepare(engine)
    engine.auth.token.return_value = "synthetic-replacement-token"
    result = engine.service.apply(operation, operation)
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    engine.auth.token.assert_called_once_with("publisher")
    assert all(token == "synthetic-original-token" for _, token in engine.reads)
    assert engine.writes[0][-1] == "synthetic-original-token"


def test_revocation_during_credential_acquisition_prevents_first_read(engine):
    def token(_):
        engine.state.revoked = True
        return "synthetic-original-token"

    engine.auth.token.side_effect = token
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    assert not engine.reads and not engine.writes and not engine.service._preparations


def test_revocation_after_prepare_read_does_not_retain_preparation(engine):
    def read(*args):
        value = engine.observe(*args)
        engine.state.revoked = True
        return value

    engine.read_mock.side_effect = read
    with pytest.raises(PlayError, match="revoked"):
        prepare(engine)
    assert len(engine.reads) == 1 and not engine.service._preparations


@pytest.mark.parametrize("engine", ["edit"], indirect=True)
@pytest.mark.parametrize("overrun", [0, 1], ids=["at-deadline", "after-deadline"])
@pytest.mark.parametrize("phase", ["credential", "edit-authorization", "resource-authorization"])
def test_edit_prepare_rechecks_effective_deadline_before_each_get(
    engine, monkeypatch, phase, overrun
):
    engine.clock[0] = 1000
    monkeypatch.setattr(
        edit_writes,
        "time",
        SimpleNamespace(monotonic=lambda: engine.clock[0], time=lambda: engine.clock[0]),
    )

    def observe(*args):
        value = engine.observe(*args)
        if "expiryTimeSeconds" in value:
            value["expiryTimeSeconds"] = "1001"
        return value

    def authorize():
        engine.authorize()
        if phase == "edit-authorization" and engine.state.calls == 2:
            engine.clock[0] = 1600 + overrun
        elif phase == "resource-authorization" and engine.state.calls == 3:
            engine.clock[0] = 1001 + overrun

    def token(_):
        if phase == "credential":
            engine.clock[0] = 1600 + overrun
        return "synthetic-original-token"

    engine.auth.token.side_effect = token
    engine.service._execution_authorization = authorize
    engine.read_mock.side_effect = observe
    with pytest.raises(PlayError, match="expired"):
        prepare(engine)
    # An expired credential/authorization wait must not start the edit GET;
    # an expired edit deadline after its GET must not start the resource GET.
    assert len(engine.reads) == (1 if phase == "resource-authorization" else 0)
    assert all(not url.endswith("/details") for url, _ in engine.reads)
    assert not engine.service._preparations and not engine.writes
    engine.auth.token.assert_called_once_with("publisher")


@pytest.mark.parametrize("denial", ["revoked", "expired"])
def test_final_authority_check_stops_dispatch_and_consumes_id(engine, denial):
    operation = prepare(engine)
    engine.reads.clear()
    expected_reads = 2 if engine.cls is edit_writes.EditWrites else 1

    def read(*args):
        value = engine.observe(*args)
        if len(engine.reads) == expected_reads:
            if denial == "revoked":
                engine.state.revoked = True
            else:
                engine.state.expire = True
        return value

    engine.read_mock.side_effect = read
    with pytest.raises(PlayError, match="revoked|expired"):
        engine.service.apply(operation, operation)
    assert not engine.writes and not engine.service._preparations
    engine.state.revoked = engine.state.expire = False
    with pytest.raises(PlayError, match="consumed"):
        engine.service.apply(operation, operation)
    assert engine.service._apply_guard.acquire(blocking=False)
    engine.service._apply_guard.release()


def test_readback_lost_authority_keeps_confirmed_mutation(engine):
    operation = prepare(engine)

    def execute(*args, **kwargs):
        value = engine.execute(*args, **kwargs)
        engine.state.revoked = True
        return value

    engine.write_mock.side_effect = execute
    engine.reads.clear()
    result = engine.service.apply(operation, operation)
    assert result["mutation_succeeded"] and not result["readback_succeeded"]
    assert len(engine.writes) == 1
    assert len(engine.reads) == (2 if engine.cls is edit_writes.EditWrites else 1)
    assert "Synthetic authority" not in str(result)


def test_hosted_apply_guard_does_not_use_global_local_lock(engine):
    operation = prepare(engine)
    assert _APPLY_LOCK.acquire(blocking=False)
    try:
        assert engine.service.apply(operation, operation)["mutation_succeeded"]
    finally:
        _APPLY_LOCK.release()
    assert engine.cls(engine.settings, engine.auth, local=True)._apply_guard is _APPLY_LOCK


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5])
def test_invalid_snapshot_ceiling_is_rejected(engine, limit):
    with pytest.raises(PlayError, match="Snapshot limit"):
        engine.cls(engine.settings, engine.auth, _max_snapshot_bytes=limit)


@pytest.mark.parametrize("engine", ["edit"], indirect=True)
def test_edit_aggregate_ceiling_counts_edit_and_resource(engine):
    # Each observation is below the cap, but their combined retained JSON is not.
    original = engine.observe
    sizes = []

    def read(*args):
        data = original(*args)
        sizes.append(len(edit_writes.bounded_json(data).encode()))
        return data

    engine.read_mock.side_effect = read
    engine.service._max_snapshot_bytes = 100
    with pytest.raises(PlayError, match="snapshots exceed"):
        prepare(engine)
    assert len(sizes) == 2 and max(sizes) < 100 < sum(sizes)
    assert not engine.service._preparations and not engine.writes


def test_commerce_multitarget_bound_stops_before_next_observation(monkeypatch):
    method = "androidpublisher.monetization.subscriptions.basePlans.offers.patch"
    params = {
        "packageName": APP,
        "productId": "premium",
        "basePlanId": "monthly",
        "offerId": "intro",
        "updateMask": "offerTags",
        "regionsVersion.version": "2022/02",
    }
    body = {k: params[k] for k in ("packageName", "productId", "basePlanId", "offerId")} | {
        "offerTags": []
    }
    spec = request_spec(method, params, body)
    targets = list(observations(spec))
    calls = []

    def observe(url, token):
        calls.append(url)
        return {
            "http_status": 200,
            "data": {
                "packageName": APP,
                "productId": "premium",
                "basePlans": [{"basePlanId": "monthly"}],
                "padding": "x" * 1000,
            },
        }

    monkeypatch.setattr(monetization.monetization_http, "observe", observe)
    svc = monetization.Monetization(
        Settings(
            packages=frozenset({APP}),
            monetization_packages=frozenset({APP}),
            monetization_methods=frozenset({method}),
        ),
        Mock(token=Mock(return_value="synthetic-token")),
        _execution_authorization=lambda: None,
        _max_snapshot_bytes=512,
    )
    with pytest.raises(PlayError, match="hosted preparation limit"):
        svc.prepare(method, params, body, True)
    assert calls == targets[:1] and len(targets) == 2
    assert not svc._preparations


def test_local_commerce_snapshot_limit_remains_two_mib():
    assert len(monetization.snapshot({"padding": "x" * (512 * 1024)})) > 512 * 1024
    with pytest.raises(PlayError, match="2 MiB"):
        monetization.snapshot({"padding": "x" * (2 * 1024 * 1024)})


def test_commerce_aggregate_limit_counts_prior_targets(monkeypatch):
    method = "androidpublisher.monetization.subscriptions.batchUpdate"
    requests = [
        {
            "subscription": {
                "packageName": APP,
                "productId": product,
                "listings": [{"languageCode": "en-US", "title": "Synthetic"}],
            },
            "updateMask": "listings",
            "regionsVersion": {"version": "2022/02"},
        }
        for product in ("first", "second", "third")
    ]
    parameters, body = {"packageName": APP}, {"requests": requests}
    expected = observations(request_spec(method, parameters, body))
    values = {
        url: {"http_status": 200, "data": {**item["identity"], "padding": "x" * 100}}
        for url, item in expected.items()
    }
    individual_sizes = [
        len(monetization.snapshot({url: value}).encode()) for url, value in values.items()
    ]
    ceiling = max(individual_sizes) + 10
    reads = []

    def observe(url, token):
        reads.append(url)
        return deepcopy(values[url])

    monkeypatch.setattr(monetization.monetization_http, "observe", observe)
    service = monetization.Monetization(
        Settings(
            packages=frozenset({APP}),
            monetization_packages=frozenset({APP}),
            monetization_methods=frozenset({method}),
        ),
        Mock(token=Mock(return_value="synthetic-token")),
        _execution_authorization=lambda: None,
        _max_snapshot_bytes=ceiling,
    )
    with pytest.raises(PlayError, match="hosted preparation limit"):
        service.prepare(method, parameters, body, True)
    assert reads == list(expected)[:2] and len(expected) == 3
    assert not service._preparations


def test_query_rechecks_authority_after_token_refresh(monkeypatch):
    state = {"revoked": False}

    def authorize():
        if state["revoked"]:
            raise PlayError("Synthetic authority revoked")

    def token(_):
        state["revoked"] = True
        return "synthetic-token"

    query = Mock()
    monkeypatch.setattr(monetization.monetization_http, "query", query)
    svc = monetization.Monetization(
        Settings(packages=frozenset({APP})),
        SimpleNamespace(token=token),
        _execution_authorization=authorize,
    )
    with pytest.raises(PlayError, match="revoked"):
        svc.query(
            "androidpublisher.monetization.convertRegionPrices",
            {"packageName": APP},
            {"price": {"currencyCode": "USD", "units": "5"}},
        )
    query.assert_not_called()
