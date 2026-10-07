"""Bounded single-worker Linux acceptance with real HTTP and synthetic Google.

Run after building the candidate image:
    python3 scripts/test_hosted_load.py --image pubship:public-qa > /tmp/load.json

The runner creates a disposable disk volume and a network-disabled, read-only
container. It publishes no ports, accepts no target URL, and never reads host
credentials. This is a regression/load fixture, not a production capacity claim.
RSS/FD samples include the server, load driver and synthetic provider in one process.
"""

import argparse
import hashlib
import http.client
import io
import json
import math
import os
import platform
import socket
import statistics
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ISSUER = "https://load-fixture.invalid"
APP = "com.example.synthetic"
DATA = b"synthetic-load-fixture-" * 4096


def check(value, message):
    if not value:
        raise AssertionError(message)


def until(predicate, description, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        check(time.monotonic() < deadline, description)
        time.sleep(0.01)


def sample():
    status = Path("/proc/self/status").read_text().splitlines()
    return {
        "rss_kib": int(next(line.split()[1] for line in status if line.startswith("VmRSS:"))),
        "fd_count": len(list(Path("/proc/self/fd").iterdir())),
    }


def container_check(root):
    import uvicorn
    from cryptography.fernet import Fernet

    from pubship import hosted
    from pubship.auth import SCOPES
    from pubship.errors import PlayError
    from pubship.hosted_config import HostedSettings, TransferLimits
    from pubship.hosted_transfers import TransferPurpose

    check(sys.platform == "linux", "Physical storage acceptance requires Linux")
    check(os.getuid() == 10001, "Expected non-root container runtime")
    root.mkdir(mode=0o700, exist_ok=True)
    spool = root / "spool"
    spool.mkdir(mode=0o700)
    settings = HostedSettings(
        ISSUER,
        "synthetic-load-client",
        "synthetic-load-secret",
        root / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
        transfer_directory=spool,
        transfer_limits=TransferLimits(
            object_bytes=128 * 1024,
            connection_bytes=256 * 1024,
            total_bytes=1024 * 1024,
            connection_handles=2,
            total_handles=8,
            connection_streams=1,
            total_streams=2,
            idle_timeout_seconds=2,
            stream_timeout_seconds=10,
        ),
        new_enrollment_enabled=True,
    )
    app = hosted.create_hosted_app(settings)
    provider, store = app.state.oauth, app.state.hosted_transfers
    accounts = []
    for number in range(5):
        owner = (f"synthetic-grant-{number}", f"synthetic-client-{number}")
        grant = {
            "verified_email": "owner@example.test",
            "google_subject": "synthetic-owner",
            "client_id": owner[1],
            "packages": [APP],
            "bucket": "",
            "google_scopes": [SCOPES["publisher"]],
            "access_token": f"synthetic-google-{number}",
            "refresh_token": None,
            "google_expires": time.time() + 3600,
            "expires": time.time() + 3600,
        }
        app.state.vault.put("grants", owner[0], grant, 3600)
        issued = provider.issue_tokens(*owner, ["play:read"])
        app.state.vault.put(
            "clients",
            owner[1],
            {
                "client_id": owner[1],
                "redirect_uris": ["https://client.invalid/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": "play:read",
            },
            3600,
        )
        accounts.append((owner, issued.access_token, issued.refresh_token))

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, log_level="critical", proxy_headers=False)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    calls = 0
    calls_lock = threading.Lock()

    def synthetic_google(_opener, request, *args, **kwargs):
        nonlocal calls
        check(request.get_method() == "GET", "Synthetic provider received a write")
        check(
            request.full_url.startswith(
                f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{APP}/reviews"
            ),
            "Unexpected synthetic provider URL",
        )
        authorization = request.get_header("Authorization")
        check(authorization.startswith("Bearer synthetic-google-"), "Unexpected Google token")
        number = int(authorization.rsplit("-", 1)[1])
        check(0 <= number < 5, "Unexpected tenant")
        with calls_lock:
            calls += 1
        time.sleep(0.02)
        result = io.BytesIO(json.dumps({"reviews": [{"reviewId": str(number)}]}).encode())
        result.status = 200
        return result

    def request(method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request(
                method, path, body=body, headers={"Host": "load-fixture.invalid", **(headers or {})}
            )
            response = connection.getresponse()
            data = response.read(1024 * 1024)
            return response.status, dict(response.getheaders()), data
        finally:
            connection.close()

    def tool(number):
        request_number = number
        number %= len(accounts)
        started = time.monotonic()
        status, _, data = request(
            "POST",
            "/mcp",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_number + 1,
                    "method": "tools/call",
                    "params": {"name": "list_reviews", "arguments": {"package": APP}},
                }
            ),
            {
                "Authorization": "Bearer " + accounts[number][1],
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        check(status == 200, f"Read tool HTTP status {status}")
        result = json.loads(data)["result"]
        check(not result.get("isError"), "Read tool returned error")
        check(
            result["structuredContent"]["data"]["reviews"] == [{"reviewId": str(number)}],
            "Cross-tenant response or wrong synthetic result",
        )
        return (time.monotonic() - started) * 1000

    def partial(path="/mcp", token=None, length=100, method="POST"):
        connection = socket.create_connection(("127.0.0.1", port), timeout=40)
        headers = (
            f"{method} {path} HTTP/1.1\r\nHost: load-fixture.invalid\r\n"
            f"Content-Length: {length}\r\nContent-Type: application/"
            + ("octet-stream" if method == "PUT" else "json")
            + "\r\nConnection: close\r\n"
        )
        if token:
            headers += f"Authorization: Bearer {token}\r\n"
        connection.sendall(headers.encode() + b"\r\nx")
        return connection

    report = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "uid": os.getuid(),
        "conditions": {
            "network": "Docker --network none; loopback HTTP only; no TLS/proxy",
            "provider": "synthetic GET-only Google transport; fixed 20 ms delay; 5 seeded tenants; up to 20 concurrent clients",
            "allocation": "Linux real posix_fallocate and anonymous disk volume files",
            "measurement": "one process includes server, client driver and synthetic provider",
            "body_idle_seconds": hosted.BODY_IDLE_TIMEOUT,
            "body_total_seconds": hosted.BODY_TOTAL_TIMEOUT,
        },
        "reads": [],
    }
    with ExitStack() as stack:
        stack.enter_context(patch("urllib.request.OpenerDirector.open", synthetic_google))
        stack.enter_context(
            patch("pubship.server.Auth", side_effect=AssertionError("Local auth forbidden"))
        )
        thread.start()
        until(lambda: server.started, "Server did not start")
        try:
            tool(0)  # Warm imports and worker pool before baseline.
            report["baseline"] = sample()
            for concurrency in (1, 5, 20):
                barrier = threading.Barrier(concurrency)
                readings = []
                stop = threading.Event()

                def monitor(stop=stop, readings=readings):
                    while not stop.wait(0.01):
                        readings.append(sample())

                watcher = threading.Thread(target=monitor, daemon=True)
                watcher.start()

                def client(number, barrier=barrier):
                    barrier.wait(timeout=10)
                    return [tool(number) for _ in range(10)]

                started = time.monotonic()
                try:
                    with ThreadPoolExecutor(max_workers=concurrency) as pool:
                        latencies = [
                            latency
                            for row in pool.map(client, range(concurrency))
                            for latency in row
                        ]
                finally:
                    stop.set()
                    watcher.join(2)
                elapsed = time.monotonic() - started
                readings.append(sample())
                ordered = sorted(latencies)
                report["reads"].append(
                    {
                        "concurrency": concurrency,
                        "requests": len(latencies),
                        "elapsed_seconds": round(elapsed, 3),
                        "requests_per_second": round(len(latencies) / elapsed, 2),
                        "p50_ms": round(statistics.median(latencies), 2),
                        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
                        "max_ms": round(max(latencies), 2),
                        "peak_sampled_rss_kib": max(s["rss_kib"] for s in readings),
                        "peak_sampled_fd_count": max(s["fd_count"] for s in readings),
                    }
                )

            # Concurrent idle and progressing slow bodies exercise actual defaults.
            def slow(progress):
                started = time.monotonic()
                with partial() as connection:
                    finished = threading.Event()

                    def drip():
                        while not finished.wait(min(1, hosted.BODY_IDLE_TIMEOUT / 4)):
                            try:
                                connection.sendall(b"x")
                            except OSError:
                                return

                    sender = threading.Thread(target=drip, daemon=True)
                    if progress:
                        sender.start()
                    try:
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        response.read()
                        check(response.status == 408, "Slow JSON body not timed out")
                    finally:
                        finished.set()
                        if progress:
                            sender.join(2)
                return round(time.monotonic() - started, 3)

            with ThreadPoolExecutor(max_workers=2) as pool:
                idle = pool.submit(slow, False)
                total = pool.submit(slow, True)
                idle_seconds, total_seconds = idle.result(), total.result()
            check(
                hosted.BODY_IDLE_TIMEOUT - 0.2 <= idle_seconds < hosted.BODY_IDLE_TIMEOUT + 5,
                "Idle deadline drift",
            )
            check(
                hosted.BODY_TOTAL_TIMEOUT - 0.2 <= total_seconds < hosted.BODY_TOTAL_TIMEOUT + 5,
                "Total deadline drift",
            )
            report["slow_json"] = {
                "idle_seconds": idle_seconds,
                "progressing_total_seconds": total_seconds,
                "status": 408,
            }
            # Exercise exact quota under simultaneous reservation attempts. Byte
            # delivery uses HTTP; fixture seeding uses the real storage interface.
            purpose = TransferPurpose(
                "synthetic-read-fixture", {}, "application/octet-stream", "play:read", package=APP
            )
            barrier = threading.Barrier(20)

            def reserve(number):
                number %= len(accounts)
                barrier.wait(timeout=10)
                try:
                    value = store.reserve_upload(
                        accounts[number][0],
                        purpose,
                        len(DATA),
                        hashlib.sha256(DATA).hexdigest(),
                        time.monotonic() + 60,
                    )
                    return number, value["transfer_id"]
                except PlayError as error:
                    check(
                        "capacity" in str(error).lower() or "quota" in str(error).lower(),
                        "Unexpected reservation error",
                    )
                    return None

            with ThreadPoolExecutor(max_workers=20) as pool:
                reservations = [value for value in pool.map(reserve, range(20)) if value]
            check(len(reservations) == 8, "Concurrent global handle quota not enforced")
            # First held streams must belong to distinct connection owners.
            distinct = next(
                i for i, value in enumerate(reservations[1:], 1) if value[0] != reservations[0][0]
            )
            reservations[1], reservations[distinct] = reservations[distinct], reservations[1]
            for record in store._records.values():
                metadata = os.fstat(record.stream.fileno())
                check(
                    metadata.st_nlink == 0 and metadata.st_blocks * 512 >= record.charge,
                    "Unallocated or named spool",
                )
            held = []
            try:
                for number, handle in reservations[:2]:
                    held.append(
                        partial(
                            f"/transfers/{handle}/content", accounts[number][1], len(DATA), "PUT"
                        )
                    )
                until(
                    lambda: sum(r.active for r in store._records.values()) == 2,
                    "Streams did not acquire capacity",
                )
                number, handle = reservations[2]
                status, _, _ = request(
                    "PUT",
                    f"/transfers/{handle}/content",
                    DATA,
                    {
                        "Authorization": "Bearer " + accounts[number][1],
                        "Content-Type": "application/octet-stream",
                    },
                )
                check(status == 400, "Stream overload not rejected")
            finally:
                for connection in held:
                    connection.close()
            until(lambda: len(store._records) == 6, "Disconnected upload quotas not released")
            number, handle = reservations[2]
            status, _, _ = request(
                "PUT",
                f"/transfers/{handle}/content",
                DATA,
                {
                    "Authorization": "Bearer " + accounts[number][1],
                    "Content-Type": "application/octet-stream",
                },
            )
            check(status == 200, "Stream did not recover after overload")
            for number, handle in reservations[2:]:
                store.discard(accounts[number][0], handle)
            check(not store._records and not list(spool.iterdir()), "Transfer cleanup leaked")
            report["transfers"] = {
                "reservation_attempts": 20,
                "admitted": 8,
                "rejected": 12,
                "stream_limit": 2,
                "overload_status": 400,
                "recovery_status": 200,
                "remaining_records": 0,
                "named_files": 0,
            }
            # Invalid registrations consume onboarding budget without creating
            # clients; token/revoke budgets and authenticated reads stay usable.
            codes = [
                request("POST", "/register", "{}", {"Content-Type": "application/json"})[0]
                for _ in range(121)
            ]
            check(codes[-1] == 429 and codes.count(429) == 1, "Admission quota mismatch")
            status, _, _ = request(
                "POST",
                "/token",
                urlencode(
                    {
                        "grant_type": "refresh_token",
                        "client_id": accounts[0][0][1],
                        "refresh_token": accounts[0][2],
                        "resource": settings.resource,
                    }
                ),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            check(status == 200, "Existing customer refresh failed after onboarding overload")
            tool(0)
            check(request("GET", "/healthz")[0] == 200, "Health failed after overload")
            report["admission"] = {
                "attempts": 121,
                "limited": 1,
                "token_status_after_onboarding_limit": status,
                "authenticated_read_after_overload": "passed",
            }
            time.sleep(0.1)
            report["final"] = sample()
            report["synthetic_google_gets"] = calls
            check(
                report["final"]["fd_count"] <= report["baseline"]["fd_count"] + 2,
                "Descriptors leaked after workload",
            )
        finally:
            server.should_exit = True
            thread.join(10)
            sock.close()
            app.state.vault.close()
            check(not thread.is_alive(), "Server failed to shut down")
    return report


def docker_run(image):
    name = "pubship-load-" + uuid.uuid4().hex
    volume = name + "-data"
    script = Path(__file__).resolve()

    def docker(*args, **kwargs):
        return subprocess.run(
            ["docker", *args], check=True, text=True, capture_output=True, timeout=180, **kwargs
        )

    docker("volume", "create", volume)
    try:
        mount = f"type=volume,src={volume},dst=/qa-data"
        docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "0:0",
            "--mount",
            mount,
            "--entrypoint",
            "python",
            image,
            "-c",
            "import os; os.chown('/qa-data',10001,10001); os.chmod('/qa-data',0o700)",
        )
        result = docker(
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "1.5",
            "--memory",
            "1g",
            "--pids-limit",
            "128",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            mount,
            "--mount",
            f"type=bind,src={script},dst=/load.py,readonly",
            "--entrypoint",
            "python",
            image,
            "/load.py",
            "--in-container",
        )
        report = json.loads(result.stdout)
        report["image_id"] = docker("image", "inspect", "--format", "{{.Id}}", image).stdout.strip()
        report["conditions"].update(
            container_cpus=1.5, container_memory_mib=1024, container_pid_limit=128
        )
        return report
    except subprocess.CalledProcessError as error:
        # Fixtures contain synthetic secrets only; avoid accidental future leaks.
        raise RuntimeError(
            f"Isolated load container failed (exit {error.returncode}); diagnostics withheld. Reproduce with --in-container in an isolated test image."
        ) from None
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15, check=False)
        docker("volume", "rm", volume)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image")
    parser.add_argument("--in-container", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.in_container:
        result = container_check(Path("/qa-data"))
    elif args.image:
        result = docker_run(args.image)
    else:
        parser.error("--image is required")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
