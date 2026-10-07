"""Cancellation of actual SDK tool workers, using synthetic provider responses."""

import asyncio
import json
import threading
from functools import partial

import anyio
import pytest
from mcp import Client
from mcp_types import CancelledNotification, CancelledNotificationParams
from test_hosted_020_loopback import client
from test_hosted_020_loopback import loopback as loopback  # noqa: F401
from test_hosted_021_loopback import (
    P,
    account_case,
    account_provider,
    apply_args,
    connect,
    prepare_account,
)
from test_hosted_021_registry import context as context  # noqa: F401
from test_hosted_021_registry import prepare, scenario
from test_hosted_grants import provider as provider  # noqa: F401

from pubship import account_access_http
from pubship.hosted import RequestLimits
from pubship.hosted_cancellation import RequestCancellation
from pubship.hosted_tools import build_hosted_tools


@pytest.mark.parametrize("phase", ["prepare", "apply"])
def test_cancelled_sdk_worker_stops_after_preflight_and_releases_preparation(
    context, monkeypatch, phase
):
    state = scenario(monkeypatch, "androidpublisher.users.patch")
    prepared = prepare(context, state) if phase == "apply" else None
    entered, release = threading.Event(), threading.Event()
    original = account_access_http.observe

    def hold(*args, **kwargs):
        entered.set()
        assert release.wait(5), "Synthetic preflight was not released"
        return original(*args, **kwargs)

    monkeypatch.setattr(account_access_http, "observe", hold)
    name = phase + "_account_access"
    tool = next(
        tool
        for tool in build_hosted_tools(
            context.provider, context.registry, lambda: context.principal
        )
        if tool.name == name
    )
    arguments = (
        dict(operation_id=prepared["operation_id"], confirmation=prepared["operation_id"])
        if prepared
        else dict(
            method=state.method,
            parameters=state.parameters,
            body=state.body,
            acknowledge_access_effects=True,
        )
    )

    async def exercise():
        async with anyio.create_task_group() as group:
            scope = anyio.CancelScope()

            async def call():
                with scope:
                    await tool.run(arguments, context=None)

            group.start_soon(call)
            try:
                assert await anyio.to_thread.run_sync(entered.wait, 2)
                scope.cancel()
            finally:
                release.set()
        assert scope.cancelled_caught

    anyio.run(exercise)
    assert not state.mutated
    assert not context.registry._records
    assert all(request.method == "GET" for request in state.calls)


@pytest.mark.parametrize("phase", ["prepare", "apply", "mutation"])
@pytest.mark.parametrize("mode", ["auto", "legacy", "legacy-notify"])
def test_real_http_disconnect_stops_preflight_and_consumes_dispatched_operations(
    loopback, monkeypatch, phase, mode
):
    monkeypatch.setattr(
        "test_hosted_020_loopback.Client",
        partial(Client, mode="legacy" if mode.startswith("legacy") else mode),
    )
    _, token = connect(loopback)
    state = account_provider(monkeypatch, account_case(P + "users.patch"))
    entered, release, disconnected, disposed = (threading.Event() for _ in range(4))
    registry = loopback.app.state.hosted_registry
    original_dispatch = RequestLimits.__call__
    original_dispose = registry._drain_disposals
    original_cancel = RequestCancellation.cancel
    target_name = ("prepare" if phase == "prepare" else "apply") + "_account_access"

    async def observe_disconnect(middleware, scope, receive, send):
        body, targeted = bytearray(), False

        async def observed_receive():
            nonlocal targeted
            event = await receive()
            if scope.get("path") == "/mcp" and event["type"] == "http.request":
                body.extend(event.get("body", b""))
                if not event.get("more_body", False):
                    message = json.loads(body)
                    targeted = message.get("params", {}).get("name") == target_name
            if targeted and event["type"] == "http.disconnect":
                # Let the server's watcher process the event and cancel the
                # scope before the client thread releases the provider barrier.
                asyncio.get_running_loop().call_soon(disconnected.set)
            return event

        await original_dispatch(middleware, scope, observed_receive, send)

    def drain():
        original_dispose()
        if entered.is_set() and not registry._records:
            disposed.set()

    def cancel(middleware, context):
        matched = original_cancel(middleware, context)
        if matched and mode == "legacy-notify":
            disconnected.set()
        return matched

    monkeypatch.setattr(RequestLimits, "__call__", observe_disconnect)
    monkeypatch.setattr(registry, "_drain_disposals", drain)
    monkeypatch.setattr(RequestCancellation, "cancel", cancel)

    async def exercise():
        async with client(loopback, token) as (mcp, http):
            prepared = await prepare_account(mcp, state) if phase != "prepare" else None

            def hold(_):
                if not entered.is_set():
                    entered.set()
                    assert release.wait(5), "Synthetic provider read was not released"

            if phase == "mutation":
                state.on_write = lambda: hold(None)
            else:
                state.on_read = hold
            arguments = (
                apply_args(prepared)
                if prepared
                else dict(
                    method=state.method,
                    parameters=state.parameters,
                    body=state.body,
                    acknowledge_access_effects=True,
                )
            )
            name = ("apply" if prepared else "prepare") + "_account_access"
            request_ids = []

            async def capture(request):
                if request.method == "POST":
                    payload = json.loads(await request.aread())
                    if payload.get("params", {}).get("name") == name:
                        request_ids.append(payload["id"])

            http.event_hooks["request"].append(capture)
            # Exercise legacy physical disconnection separately from its
            # authenticated cancellation notification protocol.
            call = (
                http.post(
                    loopback.settings.resource,
                    headers={
                        "MCP-Protocol-Version": mcp.protocol_version,
                        "Accept": "application/json, text/event-stream",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 987654,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                )
                if mode == "legacy"
                else mcp.call_tool(name, arguments)
            )
            task = asyncio.create_task(call)
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                if mode == "legacy-notify":
                    await mcp.session.send_notification(
                        CancelledNotification(
                            params=CancelledNotificationParams(request_id=request_ids[0])
                        )
                    )
                    assert await asyncio.to_thread(disconnected.wait, 2)
                if mode != "legacy-notify":
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                    assert await asyncio.to_thread(disconnected.wait, 2)
            finally:
                release.set()
                if mode == "legacy-notify":
                    # Observe the completed error response after the worker stops.
                    # Client task cancellation is a separate disconnect case.
                    result = await asyncio.wait_for(task, 3)
                    if phase == "mutation":
                        assert "reconcile before preparing again" in result.content[0].text
                    else:
                        assert result.is_error
                elif not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            assert await asyncio.to_thread(disposed.wait, 2)
            assert sum(call[0] != "GET" for call in state.calls) == (phase == "mutation")
            assert not registry._records
            if prepared:
                count = len(state.calls)
                assert (await mcp.call_tool("apply_account_access", arguments)).is_error
                assert len(state.calls) == count

    asyncio.run(exercise())
