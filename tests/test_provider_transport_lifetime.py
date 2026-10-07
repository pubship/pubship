"""Real urllib lifetime, response, redirect and proxy regressions; no Google traffic."""

import base64
import gc
import ssl
import threading
import urllib.error
import urllib.request
import weakref
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_hosted_020_loopback import certificate

from pubship.errors import PlayError
from pubship.http import NoRedirect
from pubship.provider_transport import bounded_opener, dispatch_check


@contextmanager
def without_cyclic_collection():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def references(opener):
    https = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
    result = [weakref.ref(opener), weakref.ref(https)]
    # Python 3.14 constructs the default context in HTTPSHandler; older supported
    # versions defer it until connection creation. Check the context when present.
    if https._context is not None:
        result.append(weakref.ref(https._context))
    return result


@pytest.mark.parametrize("reject_before_open", [False, True])
def test_unused_opener_does_not_require_cyclic_collection(reject_before_open):
    refs = []

    def prepare():
        opener = bounded_opener(NoRedirect(), timeout=1)
        refs.extend(references(opener))
        if reject_before_open:
            try:
                dispatch_check(0, None)
            except PlayError:
                return  # Do not retain a traceback that itself owns this frame.

    with without_cyclic_collection():
        prepare()
        assert all(reference() is None for reference in refs)


@contextmanager
def upstream(*, context=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            if self.path.startswith("/redirect/"):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                self.send_header("Location", "/target")
                content = b"redirect"
            elif self.path.startswith("/error/"):
                self.send_response(int(self.path.rsplit("/", 1)[1]))
                content = b"synthetic error"
            else:
                self.send_response(200)
                content = b"abcdefgh"
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            try:
                self.wfile.write(content)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Rejected redirect/certificate paths may close early.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield f"{'https' if context else 'http'}://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("secure", [False, True])
def test_response_outlives_temporary_opener_and_remains_readable(tmp_path, secure):
    if secure:
        key, cert, _ = certificate(tmp_path)
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(cert, key)
    else:
        server_context = None
    refs = []
    context_refs = []

    def temporary_opener():
        opener = bounded_opener(
            NoRedirect(), timeout=2, proxy_handler=urllib.request.ProxyHandler({})
        )
        if secure:
            # A fresh verified context is owned only by this handler, then the
            # actual SSLSocket. No shared fixture context can hide lost ownership.
            https = next(h for h in opener.handlers if isinstance(h, urllib.request.HTTPSHandler))
            https._context = ssl.create_default_context(cafile=cert)
            context_refs.append(weakref.ref(https._context))
        refs.append(weakref.ref(opener))
        return opener

    with upstream(context=server_context) as (url, requests), without_cyclic_collection():
        with temporary_opener().open(url, timeout=2) as response:
            assert refs[0]() is None
            if secure:
                assert context_refs[0]() is not None
            assert response.read(3) == b"abc"
            assert response.read() == b"defgh"
        del response
        assert all(reference() is None for reference in context_refs)
        assert len(requests) == 1


@pytest.mark.parametrize("status", [302, 307])
def test_real_redirect_rejection_never_dispatches_target(status):
    with upstream() as (url, requests):
        request = urllib.request.Request(
            url + f"/redirect/{status}", headers={"Authorization": "Bearer synthetic-A"}
        )
        with pytest.raises(PlayError, match="redirect blocked"):
            bounded_opener(
                NoRedirect(), timeout=2, proxy_handler=urllib.request.ProxyHandler({})
            ).open(request, timeout=2)
        assert [path for path, _ in requests] == [f"/redirect/{status}"]


@pytest.mark.parametrize("status", [404, 500])
def test_real_http_error_retains_closeable_body_without_opener(status):
    refs = []

    def temporary_opener():
        opener = bounded_opener(
            NoRedirect(), timeout=2, proxy_handler=urllib.request.ProxyHandler({})
        )
        refs.append(weakref.ref(opener))
        return opener

    def error_response(url):
        try:
            temporary_opener().open(url, timeout=2)
        except urllib.error.HTTPError as error:
            # A saved exception traceback intentionally retains its open frame;
            # detach that diagnostic chain to test the response's own ownership.
            error.__traceback__ = None
            return error
        raise AssertionError("Expected HTTPError")

    with upstream() as (url, requests), without_cyclic_collection():
        error = error_response(url + f"/error/{status}")
        assert refs[0]() is None
        assert error.code == status and error.read(4) == b"synt"
        assert error.read() == b"hetic error"
        error.close()
        assert error.closed
        assert len(requests) == 1


def test_environment_proxy_and_explicit_bypass_keep_separate_authorization(monkeypatch):
    with upstream() as (proxy, proxy_requests), upstream() as (origin, origin_requests):
        monkeypatch.setenv("http_proxy", proxy.replace("http://", "http://synthetic:proxy@"))
        monkeypatch.setenv("no_proxy", "")
        for credential in ("synthetic-A", "synthetic-B"):
            request = urllib.request.Request(
                "http://provider.invalid/read", headers={"Authorization": "Bearer " + credential}
            )
            with bounded_opener(NoRedirect(), timeout=2).open(request, timeout=2) as response:
                assert response.read() == b"abcdefgh"
        request = urllib.request.Request(origin, headers={"Authorization": "Bearer synthetic-C"})
        with bounded_opener(
            NoRedirect(), timeout=2, proxy_handler=urllib.request.ProxyHandler({})
        ).open(request, timeout=2) as response:
            assert response.read() == b"abcdefgh"
        assert [path for path, _ in proxy_requests] == ["http://provider.invalid/read"] * 2
        assert [headers["Authorization"] for _, headers in proxy_requests] == [
            "Bearer synthetic-A",
            "Bearer synthetic-B",
        ]
        proxy_auth = "Basic " + base64.b64encode(b"synthetic:proxy").decode()
        assert all(headers["Proxy-Authorization"] == proxy_auth for _, headers in proxy_requests)
        assert len(origin_requests) == 1
        assert origin_requests[0][1]["Authorization"] == "Bearer synthetic-C"
        assert "Proxy-Authorization" not in origin_requests[0][1]
