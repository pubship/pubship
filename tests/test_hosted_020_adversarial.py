"""Independent network boundaries around the complete hosted transfer lifecycle."""

import asyncio
import hashlib
import threading
import time
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock

import httpx2
import pytest
from test_hosted_020_loopback import (
    APP,
    CAPS,
    DATA,
    OTHER,
    OTHER_STORE,
    P,
    client,
    connect,
    fixture_case,
    google,
    prepare,
    successful,
    upload,
)
from test_hosted_020_loopback import loopback as loopback  # noqa: F401

from pubship.appstore_contracts import GET, LIST, POLICY
from pubship.hosted_grants import APPSTORE_SCOPES


async def reserve(mcp, state):
    return await successful(
        mcp,
        "begin_upload_transfer",
        {
            "method": state.method,
            "parameters": state.parameters,
            "mime_type": state.mime,
            "size_bytes": len(DATA),
            "sha256": hashlib.sha256(DATA).hexdigest(),
            "body": state.body,
        },
    )


@pytest.mark.parametrize("method", [P + "edits.apks.upload", POLICY])
@pytest.mark.parametrize("same_client", [True, False])
def test_stolen_transfer_and_operation_ids_do_not_consume_owner_work(
    loopback, monkeypatch, method, same_client
):
    request, owner = connect(loopback)
    _, foreign = connect(loopback, request=request if same_client else None)
    state = fixture_case(method)
    calls = google(monkeypatch, state)

    async def exercise():
        async with (
            client(loopback, owner) as (mcp, http),
            client(loopback, foreign) as (other, other_http),
        ):
            transfer = await reserve(mcp, state)
            handle = transfer["transfer_id"]
            path = loopback.settings.issuer + f"/transfers/{handle}/content"
            loopback.events.clear()
            denied = await other_http.put(path, content=DATA, headers={"Content-Type": state.mime})
            assert denied.status_code in {400, 401}
            assert not loopback.events
            for name in ("get_transfer_status", "discard_transfer"):
                assert (await other.call_tool(name, {"transfer_id": handle})).is_error
            assert (await successful(mcp, "get_transfer_status", {"transfer_id": handle}))[
                "state"
            ] == "RESERVED"
            accepted = await http.put(path, content=DATA, headers={"Content-Type": state.mime})
            assert accepted.status_code == 200
            # The attacker has identical grants but owns a different connection.
            stolen = await other.call_tool(
                "prepare_" + state.kind,
                {
                    "method": method,
                    "parameters": state.parameters,
                    "upload_handle": handle,
                    "mime_type": state.mime,
                    "body": state.body,
                    "acknowledge_live_effects" if method == POLICY else "acknowledge_effects": True,
                },
            )
            assert stolen.is_error and not calls
            assert (await successful(mcp, "get_transfer_status", {"transfer_id": handle}))[
                "state"
            ] == "READY"
            operation = (await prepare(mcp, state, handle))["operation_id"]
            arguments = {"operation_id": operation, "confirmation": operation}
            count = len(calls)
            assert (await other.call_tool("apply_" + state.kind, arguments)).is_error
            assert len(calls) == count
            result = await successful(mcp, "apply_" + state.kind, arguments)
            assert result["mutation_succeeded"]
            assert len([call for call in calls if call[0] == "POST"]) == 1
            assert not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "problem",
    [
        "missing",
        "duplicate",
        "resource",
        "client",
        "expired",
        "revoked",
        "capability",
        "store-pair",
        "origin",
        "head",
    ],
)
def test_actual_oauth_and_outer_middleware_deny_before_body_receive(loopback, problem):
    _, token = connect(loopback)
    state = fixture_case(POLICY)
    provider = loopback.app.state.oauth

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            handle = (await reserve(mcp, state))["transfer_id"]
        headers = [
            ("Authorization", "Bearer " + token["access_token"]),
            ("Content-Type", state.mime),
        ]
        record = provider.vault.get("access", token["access_token"])
        if problem == "missing":
            headers.pop(0)
        elif problem == "duplicate":
            headers.append(headers[0])
        elif problem in {"resource", "client", "expired"}:
            record[
                {"resource": "resource", "client": "client_id", "expired": "expires_at"}[problem]
            ] = {
                "resource": "https://foreign.example/mcp",
                "client": "other-client",
                "expired": int(time.time()) - 1,
            }[problem]
            provider.vault.put("access", token["access_token"], record, 3600)
        elif problem == "revoked":
            provider.vault.delete("access", token["access_token"])
        elif problem in {"capability", "store-pair"}:
            grant = provider.vault.get("grants", record["subject"])
            if problem == "capability":
                grant["appstore_targets"].pop("play:appstore-media")
            else:
                grant["appstore_targets"]["play:appstore-media"] = {OTHER_STORE: [OTHER]}
            provider.vault.put("grants", record["subject"], grant, 3600)
        elif problem == "origin":
            headers.append(("Origin", "https://foreign.example"))
        loopback.events.clear()
        async with httpx2.AsyncClient(verify=loopback.tls, trust_env=False) as http:
            result = await http.request(
                "HEAD" if problem == "head" else "PUT",
                loopback.settings.issuer + f"/transfers/{handle}/content",
                headers=headers,
                content=DATA,
            )
        assert result.status_code in {400, 401, 405}
        assert not loopback.events
        assert result.headers["cache-control"] == "no-store"
        assert handle in provider.transfers._records
        for secret in (
            token["access_token"],
            "PRIVATE-GOOGLE",
            str(loopback.settings.transfer_directory),
        ):
            assert secret not in result.text

    asyncio.run(exercise())


@pytest.mark.parametrize("field", ["appStorePackageName", "packageName"])
def test_network_store_cross_pairs_rejected_without_allocating_or_dispatch(
    loopback, monkeypatch, field
):
    _, token = connect(loopback)
    state = fixture_case(POLICY)
    calls = google(monkeypatch, state)
    state.parameters[field] = OTHER_STORE if field == "appStorePackageName" else OTHER

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            denied = await mcp.call_tool(
                "begin_upload_transfer",
                {
                    "method": state.method,
                    "parameters": state.parameters,
                    "mime_type": state.mime,
                    "size_bytes": len(DATA),
                    "sha256": hashlib.sha256(DATA).hexdigest(),
                    "body": state.body,
                },
            )
            assert denied.is_error
            assert not calls and not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())


@pytest.mark.parametrize("method", [GET, LIST])
def test_store_only_network_reads_do_not_infer_base_app_authority(loopback, monkeypatch, method):
    _, token = connect(loopback, caps=CAPS & APPSTORE_SCOPES, store_only=True)
    state = fixture_case(method)
    if method == LIST:
        state.result["recentUpdateEvents"][0]["playAppPackageName"] = "com.example.outsideallpairs"
    calls = google(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            capabilities = await successful(mcp, "get_connection_capabilities", {})
            assert capabilities["capability_packages"]["play:read"] == []
            response = await successful(
                mcp, "query_appstore", {"method": method, "parameters": state.parameters}
            )
            assert response["data"] == state.result
            count = len(calls)
            assert (await mcp.call_tool("list_reviews", {"package": APP})).is_error
            assert len(calls) == count

    asyncio.run(exercise())


def test_lost_put_response_status_prevents_retry_and_preserves_claim(loopback, monkeypatch):
    _, token = connect(loopback)
    state = fixture_case(P + "internalappsharingartifacts.uploadapk")
    calls = google(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            transfer = await reserve(mcp, state)
            handle = transfer["transfer_id"]
            path = loopback.settings.issuer + f"/transfers/{handle}/content"
            # Discard the result as a client would after losing the acknowledgement.
            await http.put(path, content=DATA, headers={"Content-Type": state.mime})
            assert (await successful(mcp, "get_transfer_status", {"transfer_id": handle}))[
                "state"
            ] == "READY"
            loopback.events.clear()
            assert (
                await http.put(path, content=DATA, headers={"Content-Type": state.mime})
            ).status_code == 400
            assert not loopback.events and not calls
            operation = (await prepare(mcp, state, handle))["operation_id"]
            result = await successful(
                mcp, "apply_" + state.kind, {"operation_id": operation, "confirmation": operation}
            )
            assert result["mutation_succeeded"] and len(calls) == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["artifact", "sharing", "appstore"])
def test_network_unknown_mutation_outcome_is_consumed_without_retry(loopback, monkeypatch, kind):
    _, token = connect(loopback)
    state = fixture_case(
        {
            "artifact": P + "edits.apks.upload",
            "sharing": P + "internalappsharingartifacts.uploadapk",
            "appstore": POLICY,
        }[kind]
    )
    calls = google(monkeypatch, state)
    factory = __import__("urllib.request", fromlist=["build_opener"]).build_opener

    def fail(req, timeout):
        response = factory().open(req, timeout=timeout)
        if req.get_method() == "POST":
            response.close()
            raise urllib.error.URLError("PRIVATE-PROVIDER-FAILURE")
        return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_: Mock(handlers=[], open=fail))

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            handle = await upload(loopback, mcp, http, state)
            operation = (await prepare(mcp, state, handle))["operation_id"]
            arguments = {"operation_id": operation, "confirmation": operation}
            failed = await mcp.call_tool("apply_" + state.kind, arguments)
            assert failed.is_error
            assert "PRIVATE-PROVIDER" not in failed.model_dump_json()
            count = len(calls)
            assert (await mcp.call_tool("apply_" + state.kind, arguments)).is_error
            assert len(calls) == count and len([c for c in calls if c[0] == "POST"]) == 1
            assert not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())


def test_refresh_does_not_extend_original_transfer_deadline(loopback, monkeypatch):
    request, token = connect(loopback)
    state = fixture_case(POLICY)
    store = loopback.app.state.oauth.transfers

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            handle = (await reserve(mcp, state))["transfer_id"]
            original = store._records[handle].deadline
        async with httpx2.AsyncClient(verify=loopback.tls, trust_env=False) as http:
            refreshed = await http.post(
                loopback.settings.issuer + "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": request["client_id"],
                    "refresh_token": token["refresh_token"],
                    "resource": loopback.settings.resource,
                },
            )
            assert refreshed.status_code == 200
            fresh = refreshed.json()
            assert fresh["access_token"] != token["access_token"]
            assert store._records[handle].deadline == original
            monkeypatch.setattr(
                "pubship.hosted_transfers.time",
                SimpleNamespace(monotonic=lambda: original + 0.001),
            )
            loopback.events.clear()
            rejected = await http.put(
                loopback.settings.issuer + f"/transfers/{handle}/content",
                headers={
                    "Authorization": "Bearer " + fresh["access_token"],
                    "Content-Type": state.mime,
                },
                content=DATA,
            )
            assert rejected.status_code == 400 and not loopback.events

    asyncio.run(exercise())


def test_refreshed_google_token_cannot_replace_prepared_original_credential(loopback, monkeypatch):
    _, token = connect(loopback)
    state = fixture_case(POLICY)
    calls = google(monkeypatch, state)
    provider = loopback.app.state.oauth

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            handle = await upload(loopback, mcp, http, state)
            operation = (await prepare(mcp, state, handle))["operation_id"]
            principal = await provider.load_access_token(token["access_token"])
            grant = provider.vault.get("grants", principal.subject)
            grant["access_token"] = "SYNTHETIC-NEW-GOOGLE-CREDENTIAL"
            provider.vault.put("grants", principal.subject, grant, 3600)
            result = await successful(
                mcp, "apply_" + state.kind, {"operation_id": operation, "confirmation": operation}
            )
            assert result["mutation_succeeded"]
            # google() checks the actual Authorization header equals the original token.
            assert len(calls) == 1 and not provider.transfers._records

    asyncio.run(exercise())


def test_claimed_upload_stays_charged_until_discard_and_cannot_then_apply(loopback, monkeypatch):
    _, token = connect(loopback)
    state = fixture_case(POLICY)
    calls = google(monkeypatch, state)

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            handle = await upload(loopback, mcp, http, state)
            operation = (await prepare(mcp, state, handle))["operation_id"]
            handles = [handle] + [(await reserve(mcp, state))["transfer_id"] for _ in range(2)]
            denied = await mcp.call_tool(
                "begin_upload_transfer",
                {
                    "method": state.method,
                    "parameters": state.parameters,
                    "mime_type": state.mime,
                    "size_bytes": len(DATA),
                    "sha256": hashlib.sha256(DATA).hexdigest(),
                    "body": state.body,
                },
            )
            assert denied.is_error and not calls
            await successful(mcp, "discard_transfer", {"transfer_id": handle})
            assert (
                await mcp.call_tool(
                    "apply_" + state.kind, {"operation_id": operation, "confirmation": operation}
                )
            ).is_error
            assert not calls
            handles.append((await reserve(mcp, state))["transfer_id"])
            for remaining in handles[1:]:
                await successful(mcp, "discard_transfer", {"transfer_id": remaining})
            assert not loopback.app.state.oauth.transfers._records

    asyncio.run(exercise())


def test_real_socket_disconnect_mid_upload_releases_reservation(loopback):
    _, token = connect(loopback)
    state = fixture_case(POLICY)

    async def exercise():
        async with client(loopback, token) as (mcp, _):
            handle = (await reserve(mcp, state))["transfer_id"]
            store = loopback.app.state.oauth.transfers
            closing, allow_close = threading.Event(), threading.Event()
            stream = store._records[handle].stream
            reserved_charge = store._records[handle].charge

            class GatedClose:
                def __getattr__(self, name):
                    return getattr(stream, name)

                def close(self):
                    closing.set()
                    assert allow_close.wait(10), "Test did not release physical cleanup"
                    stream.close()

            store._records[handle].stream = GatedClose()
            port = int(loopback.settings.issuer.rsplit(":", 1)[1])
            reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=loopback.tls)
            headers = (
                f"PUT /transfers/{handle}/content HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\nAuthorization: Bearer {token['access_token']}\r\n"
                f"Content-Type: {state.mime}\r\nContent-Length: {len(DATA)}\r\n\r\n"
            )
            writer.write(headers.encode() + DATA[:4096])
            await writer.drain()
            for _ in range(100):
                if loopback.events:
                    break
                await asyncio.sleep(0.01)
            assert loopback.events
            writer.close()
            await writer.wait_closed()
            try:
                async with asyncio.timeout(5):
                    while True:
                        status = await mcp.call_tool("get_transfer_status", {"transfer_id": handle})
                        if status.is_error and closing.is_set():
                            break
                        await asyncio.sleep(0.01)
                assert status.is_error
                # Logical rejection precedes physical disposal; it is still charged.
                with store._lock:
                    record = store._records[handle]
                    assert record.invalid and record.charge == reserved_charge
            finally:
                allow_close.set()
            async with asyncio.timeout(5):
                while True:
                    with store._lock:
                        disposed = not store._records
                    if disposed:
                        break
                    await asyncio.sleep(0.01)
            assert stream.closed and not store._records
            # Capacity is reusable after the interrupted network stream is closed.
            next_handle = (await reserve(mcp, state))["transfer_id"]
            await successful(mcp, "discard_transfer", {"transfer_id": next_handle})

    asyncio.run(exercise())
