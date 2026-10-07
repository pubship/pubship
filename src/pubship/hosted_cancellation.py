"""Cooperative cancellation at hosted worker-thread execution boundaries."""

from contextvars import ContextVar
from threading import Event, Lock

from anyio import from_thread
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST

from .errors import PlayError

DISCONNECTED = "pubship.request_disconnected"
_disconnected = ContextVar("hosted_request_disconnected", default=None)


class RequestCancellation:
    """Bounded active requests; legacy cancellation needs authenticated ownership."""

    def __init__(self, principal_factory, *, max_total=200, max_per_owner=20):
        self._principal = principal_factory
        self._max_total, self._max_per_owner = max_total, max_per_owner
        self._active, self._lock = {}, Lock()

    def _key(self, context, request_id):
        principal = self._principal()
        owner = (getattr(principal, "subject", None), getattr(principal, "client_id", None))
        request = context.request
        session = request.headers.get("mcp-session-id", "") if request is not None else ""
        if (
            not all(isinstance(value, str) and value for value in owner)
            or type(request_id) not in (str, int)
            or len(str(request_id)) > 256
            or len(session) > 256
        ):
            raise MCPError(INVALID_REQUEST, "A bounded authenticated request identity is required.")
        return *owner, session, type(request_id), request_id

    def cancel(self, context):
        try:
            key = self._key(context, (context.params or {}).get("requestId"))
        except MCPError:
            return False
        with self._lock:
            signal = self._active.get(key)
            if signal is not None:
                signal.set()
            return signal is not None

    async def __call__(self, context, call_next):
        if context.method == "notifications/cancelled":
            self.cancel(context)
            return await call_next(context)
        if context.method != "tools/call":
            return await call_next(context)
        key = self._key(context, context.request_id)
        request = context.request
        signal = request.scope.get(DISCONNECTED) if request is not None else None
        signal = signal if signal is not None else Event()
        with self._lock:
            if key in self._active:
                raise MCPError(INVALID_REQUEST, "An active request already uses this ID.")
            if (
                len(self._active) >= self._max_total
                or sum(active[:2] == key[:2] for active in self._active) >= self._max_per_owner
            ):
                raise MCPError(INVALID_REQUEST, "Hosted active request capacity reached.")
            self._active[key] = signal
        # Legacy HTTP handlers run outside the ASGI cancel scope. SDK context
        # middleware carries the same signal into their synchronous workers.
        token = _disconnected.set(signal)
        try:
            return await call_next(context)
        finally:
            _disconnected.reset(token)
            with self._lock:
                self._active.pop(key, None)


def check_cancelled():
    signal = _disconnected.get()
    if signal is not None and signal.is_set():
        raise PlayError("Hosted request was cancelled; no further provider request was sent.")
    # The MCP SDK shields synchronous tool workers until they finish. Checking
    # their host scope explicitly prevents a cancelled preflight or queued apply
    # from dispatching a later mutation. Direct synchronous callers have no scope.
    try:
        from_thread.check_cancelled()
    except RuntimeError:
        pass  # Not an AnyIO worker thread (including direct registry tests).
