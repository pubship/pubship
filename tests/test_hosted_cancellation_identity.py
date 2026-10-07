"""Legacy notifications cannot cancel another authenticated owner's worker."""

from types import SimpleNamespace

import anyio
import pytest
from mcp.shared.exceptions import MCPError

from pubship.errors import PlayError
from pubship.hosted_cancellation import RequestCancellation, check_cancelled


def test_notification_owner_client_session_and_typed_id_are_exact():
    async def exercise():
        principal = SimpleNamespace(subject="owner", client_id="client")
        middleware = RequestCancellation(lambda: principal)
        request = SimpleNamespace(headers={"mcp-session-id": "session"}, scope={})
        context = SimpleNamespace(method="tools/call", request=request, request_id=1)

        async def running(_):
            notice = SimpleNamespace(request=request, params={"requestId": 1})
            for attribute, wrong in [("subject", "other"), ("client_id", "other")]:
                original = getattr(principal, attribute)
                setattr(principal, attribute, wrong)
                assert not middleware.cancel(notice)
                setattr(principal, attribute, original)
            request.headers["mcp-session-id"] = "other"
            assert not middleware.cancel(notice)
            request.headers["mcp-session-id"] = "session"
            notice.params["requestId"] = "1"
            assert not middleware.cancel(notice)
            notice.params["requestId"] = 1
            check_cancelled()
            assert middleware.cancel(notice)
            with pytest.raises(PlayError, match="cancelled"):
                check_cancelled()

        await middleware(context, running)
        assert not middleware._active
        assert not middleware.cancel(SimpleNamespace(request=request, params={"requestId": 1}))

    anyio.run(exercise)


def test_duplicate_and_capacity_rejection_release_active_identity():
    async def exercise():
        principal = SimpleNamespace(subject="owner", client_id="client")
        middleware = RequestCancellation(lambda: principal, max_total=2, max_per_owner=1)
        request = SimpleNamespace(headers={}, scope={})
        context = SimpleNamespace(method="tools/call", request=request, request_id=1)

        async def unreachable(_):
            raise AssertionError("Rejected request must not execute")

        async def running(_):
            with pytest.raises(MCPError, match="already uses"):
                await middleware(context, unreachable)
            other = SimpleNamespace(method="tools/call", request=request, request_id=2)
            with pytest.raises(MCPError, match="capacity"):
                await middleware(other, unreachable)
            raise ValueError("synthetic handler failure")

        with pytest.raises(ValueError, match="synthetic"):
            await middleware(context, running)
        assert not middleware._active
        check_cancelled()
        principal.subject = None
        with pytest.raises(MCPError, match="authenticated"):
            await middleware(context, unreachable)
        assert not middleware.cancel(SimpleNamespace(request=request, params={"requestId": 1}))

    anyio.run(exercise)
