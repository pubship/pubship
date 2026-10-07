"""Single-attempt urllib transport with an absolute socket-I/O deadline.

The deadline applies below HTTP buffering, including status lines, headers,
chunk framing and individual upload writes. No worker or in-flight mutation is
abandoned on timeout. System DNS resolution is outside Python's socket timeout;
it can still delay connection establishment. TLS/connect calls use a bounded
socket timeout, not cancellation.
"""

import io
import time
import urllib.request
import weakref
from functools import partial
from http.client import HTTPConnection, HTTPResponse, HTTPSConnection

from .errors import PlayError


def dispatch_check(deadline, authorize):
    """Run after request construction, outside uncertain-outcome handling."""
    if authorize is not None:
        authorize()
    if deadline is not None and time.monotonic() >= deadline:
        raise PlayError("Provider request expired before dispatch; no request was sent.")


def _remaining(deadline, idle_timeout):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Provider transport deadline exceeded.")
    return min(remaining, idle_timeout)


class _DeadlineReader(io.RawIOBase):
    def __init__(self, sock, deadline, idle_timeout):
        self._sock, self._deadline, self._idle_timeout = sock, deadline, idle_timeout
        self._raw = sock.makefile("rb", buffering=0)

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(_remaining(self._deadline, self._idle_timeout))
        return self._raw.readinto(buffer)

    def close(self):
        try:
            self._raw.close()
        finally:
            super().close()


class _ResponseSocket:
    def __init__(self, sock, deadline, idle_timeout):
        self._sock, self._deadline, self._idle_timeout = sock, deadline, idle_timeout

    def makefile(self, mode):
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline, self._idle_timeout))


class _DeadlineResponse(HTTPResponse):
    def __init__(self, sock, *args, deadline, idle_timeout, **kwargs):
        super().__init__(_ResponseSocket(sock, deadline, idle_timeout), *args, **kwargs)


class _ConnectionDeadline:
    def connect(self):
        super().connect()
        # DNS cannot be interrupted by the socket timeout. If it returned late,
        # stop before sending the origin request rather than extending its life.
        self.sock.settimeout(_remaining(self._deadline, self._idle_timeout))

    def send(self, data):
        if self.sock is None and self.auto_open:
            self.connect()
        if self.sock is not None:
            # HTTPConnection._send_output calls send separately for headers and
            # every upload chunk. Earlier reads/writes may have used most of the
            # budget; a late sendall must not reuse the connection's old timeout.
            self.sock.settimeout(_remaining(self._deadline, self._idle_timeout))
        return super().send(data)


class _HTTPConnection(_ConnectionDeadline, HTTPConnection):
    pass


class _HTTPSConnection(_ConnectionDeadline, HTTPSConnection):
    pass


def _connection(connection_type, deadline, idle_timeout, host, **kwargs):
    kwargs["timeout"] = _remaining(deadline, idle_timeout)
    connection = connection_type(host, **kwargs)
    connection._deadline, connection._idle_timeout = deadline, idle_timeout
    connection.response_class = partial(
        _DeadlineResponse, deadline=deadline, idle_timeout=idle_timeout
    )
    return connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, deadline, idle_timeout):
        super().__init__()
        self._connection = partial(_connection, _HTTPConnection, deadline, idle_timeout)

    def http_open(self, request):
        return self.do_open(self._connection, request)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, deadline, idle_timeout):
        super().__init__()
        self._connection = partial(_connection, _HTTPSConnection, deadline, idle_timeout)

    def https_open(self, request):
        return self.do_open(self._connection, request, context=self._context)


def bounded_opener(
    redirect_handler, *, timeout, deadline=None, total_timeout=None, proxy_handler=None
):
    end = time.monotonic() + (timeout if total_timeout is None else total_timeout)
    if deadline is not None:
        end = min(end, deadline)
    handlers = [redirect_handler, _HTTPHandler(end, timeout), _HTTPSHandler(end, timeout)]
    if proxy_handler is not None:
        # An explicit empty ProxyHandler disables environment proxy discovery;
        # ordinary provider requests retain urllib's existing proxy behavior.
        handlers.append(proxy_handler)
    opener = urllib.request.build_opener(*handlers)
    # urllib's default strong handler.parent links create an opener cycle that
    # retains each request's TLS context/trust store until cyclic collection.
    # The active open() call owns the opener; returned responses own their socket
    # and TLS context independently. Keep only the reverse ownership links weak.
    parent = weakref.proxy(opener)
    for handler in opener.handlers:
        handler.add_parent(parent)
    return opener


def read_chunk(response, size):
    # HTTPResponse.read(n) waits for all n bytes. read1 returns available bytes,
    # allowing stream authorization to be rechecked between network reads.
    return response.read1(size) if isinstance(response, HTTPResponse) else response.read(size)
