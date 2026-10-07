"""Real TCP cancellation stops authenticated download delivery and closes its lease."""

import socket
import threading
import time
from types import SimpleNamespace

import uvicorn

from pubship.hosted_transfer_http import _DownloadResponse


def test_real_socket_disconnect_stops_download_before_eof():
    closed = threading.Event()

    class Lease:
        size = 20 * 1024 * 1024
        deadline = time.monotonic() + 10
        reads = 0

        def read(self, amount):
            time.sleep(0.02)
            self.reads += 1
            return b"x" * amount if self.reads <= 20 else b""

        def validate(self):
            pass

        def close(self):
            closed.set()

    lease = Lease()

    async def app(scope, receive, send):
        response = _DownloadResponse(
            lease,
            SimpleNamespace(stream_timeout_seconds=10, idle_timeout_seconds=2),
            "application/octet-stream",
        )
        await response(scope, receive, send)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
    )
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            client.sendall(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert b"200 OK" in client.recv(1024)
            at_disconnect = lease.reads
        assert closed.wait(2), "Disconnected delivery must release its active lease"
        assert lease.reads <= at_disconnect + 2
        assert lease.reads < 20
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive()
