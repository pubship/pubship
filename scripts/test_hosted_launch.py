"""Manual #88 launch acceptance on an isolated, pinned native Traefik stack.

No arbitrary target URL, host credentials, provider writes or production volumes.
The short CI fixture remains scripts/test_hosted_load.py. Maximum/soak/proxy and
capacity profiles are manual and must be scheduled before execution.
"""

import argparse
import errno
import hashlib
import http.client
import io
import json
import math
import os
import platform
import re
import secrets
import shutil
import ssl
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

GIB = 1024**3
MIB = 1024**2
MAX_SECONDS = 45 * 60
MAX_TRAFFIC = 32 * GIB
PROXY_IMAGE = "traefik@sha256:31267173a15b4944e797a76ffd9c419707c8d8b32fe5b610f80cd0cfa05f372d"
ISSUER = "https://launch-fixture.invalid"
APP = "com.example.synthetic"
CHUNK = bytes(range(256)) * 4096
CONFIG = Path("/qa-config")
DATA = Path("/qa-data")


class CheckFailure(Exception):
    """Messages contain only fixed fixture diagnostics, never response bodies."""


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


class Budget:
    def __init__(self, seconds=MAX_SECONDS, traffic=MAX_TRAFFIC, *, clock=time.monotonic):
        require(0 < seconds <= MAX_SECONDS, "Invalid elapsed-time budget")
        require(0 < traffic <= MAX_TRAFFIC, "Invalid traffic budget")
        self.clock, self.started = clock, clock()
        self.seconds, self.limit, self.used = seconds, traffic, 0
        self.lock = threading.Lock()

    def charge(self, amount=0):
        require(type(amount) is int and amount >= 0, "Invalid traffic accounting")
        with self.lock:
            require(self.clock() - self.started < self.seconds, "Wall-clock budget exhausted")
            require(self.used + amount <= self.limit, "Traffic budget exhausted")
            self.used += amount

    def remaining(self):
        self.charge()
        return self.seconds - (self.clock() - self.started)


@dataclass(frozen=True)
class Profile:
    name: str
    families: int = 32
    object_bytes: int = GIB
    warm_seconds: int = 300
    load_seconds: int = 1800
    drain_seconds: int = 300
    rate: int = 20

    def __post_init__(self):
        require(self.name in {"smoke", "maximum", "soak", "proxy", "capacity"}, "Unknown profile")
        require(type(self.families) is int and 1 <= self.families <= 32, "Invalid family count")
        require(self.object_bytes in {65536, GIB}, "Unsupported fixture object size")
        require(self.rate == 20, "Steady offered rate is fixed before execution")
        require(
            self.warm_seconds + self.load_seconds + self.drain_seconds <= 2400,
            "Profile exceeds run allowance",
        )


def validate_byte_rejection(before, after, attempted_bytes, message, families):
    require(
        message
        in {
            "Hosted authorization capacity reached; try again later.",
            "Connection authorization capacity reached; reconnect this account.",
        },
        "Byte probe encountered unrelated storage failure",
    )
    require(before == after, "Rejected byte probe changed vault state")
    require(
        before["owner_rows"] < 4096 and before["lifecycle_rows"] < families * 4096,
        "Row capacity confounds byte rejection",
    )
    require(
        before["owner_bytes"] <= 4 * MIB < before["owner_bytes"] + attempted_bytes,
        "Rejection occurred before family byte boundary",
    )
    require(
        before["lifecycle_bytes"] + attempted_bytes <= families * 4 * MIB,
        "Global actual bytes confound family byte rejection",
    )


def chunks(size):
    require(type(size) is int and 0 <= size <= GIB, "Invalid synthetic payload size")
    remaining = size
    while remaining:
        value = CHUNK[: min(remaining, len(CHUNK))]
        yield value
        remaining -= len(value)


def digest(size):
    result = hashlib.sha256()
    for chunk in chunks(size):
        result.update(chunk)
    return result.hexdigest()


def distribution(values):
    require(bool(values), "No latency samples")
    ordered = sorted(values)
    return {
        "requests": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
        "p99_ms": round(ordered[math.ceil(len(ordered) * 0.99) - 1], 3),
        "max_ms": round(ordered[-1], 3),
    }


def proc_sample():
    status = Path("/proc/self/status").read_text().splitlines()
    result = {
        "rss_kib": int(next(s.split()[1] for s in status if s.startswith("VmRSS:"))),
        "peak_rss_kib": int(next(s.split()[1] for s in status if s.startswith("VmHWM:"))),
        "fds": len(list(Path("/proc/self/fd").iterdir())),
        "pid": os.getpid(),
    }
    for name in ("memory.current", "memory.peak", "memory.max", "cpu.max"):
        path = Path("/sys/fs/cgroup") / name
        result[name] = path.read_text().strip() if path.exists() else "unavailable"
    for name in ("memory.events", "cpu.stat"):
        path = Path("/sys/fs/cgroup") / name
        result[name] = (
            dict(line.split() for line in path.read_text().splitlines()) if path.exists() else {}
        )
    return result


def proxy_configuration():
    return {
        "http": {
            "routers": {
                "fixture": {
                    "rule": "Host(`launch-fixture.invalid`) && !PathPrefix(`/_fixture/`)",
                    "entryPoints": ["websecure"],
                    "service": "fixture",
                    "tls": {"options": "pubship-h1"},
                    "middlewares": ["fixture-close", "fixture-rate", "fixture-inflight"],
                }
            },
            "services": {"fixture": {"loadBalancer": {"servers": [{"url": "http://app:8080"}]}}},
            "middlewares": {
                "fixture-close": {"headers": {"customResponseHeaders": {"Connection": "close"}}},
                "fixture-rate": {"rateLimit": {"average": 30, "burst": 60, "period": "1s"}},
                "fixture-inflight": {"inFlightReq": {"amount": 100}},
            },
        },
        "tls": {
            "options": {"pubship-h1": {"alpnProtocols": ["http/1.1"]}},
            "certificates": [{"certFile": "/qa-config/cert.pem", "keyFile": "/qa-config/key.pem"}],
        },
    }


def provision(families):
    from cryptography import x509
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    os.umask(0o077)
    for root in (CONFIG, DATA):
        root.chmod(0o700)
        os.chown(root, 10001, 10001)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic launch fixture")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("proxy"), x509.DNSName("launch-fixture.invalid")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    values = {
        "families": families,
        "control": secrets.token_urlsafe(32),
        "vault_key": Fernet.generate_key().decode(),
    }
    files = {
        "fixture.json": json.dumps(values).encode(),
        "dynamic.yaml": json.dumps(proxy_configuration()).encode(),
        "key.pem": key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        "cert.pem": cert.public_bytes(serialization.Encoding.PEM),
    }
    for name, content in files.items():
        target = CONFIG / name
        target.write_bytes(content)
        target.chmod(0o600)
        os.chown(target, 10001, 10001)


def provider_main():
    families = json.loads((CONFIG / "fixture.json").read_text())["families"]

    class Provider(BaseHTTPRequestHandler):
        def do_GET(self):
            matched = re.fullmatch(r"/reviews/(\d+)", self.path)
            if not matched or not 0 <= int(matched[1]) < families:
                self.send_error(400)
                return
            time.sleep(0.02)
            body = json.dumps({"reviews": [{"reviewId": matched[1]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    ThreadingHTTPServer(("0.0.0.0", 8082), Provider).serve_forever()


def application_main():
    import anyio
    import uvicorn
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from pubship import __version__, hosted
    from pubship.auth import SCOPES
    from pubship.errors import PlayError
    from pubship.hosted_config import HostedSettings, TransferLimits
    from pubship.hosted_transfers import TransferPurpose
    from pubship.vault import VaultLimits

    require(sys.platform == "linux" and os.getuid() == 10001, "Requires non-root Linux fixture")
    config = json.loads((CONFIG / "fixture.json").read_text())
    spool = DATA / "spools"
    spool.mkdir(mode=0o700, exist_ok=True)
    settings = HostedSettings(
        ISSUER,
        "synthetic-fixture",
        "synthetic-secret",
        DATA / "vault.db",
        config["vault_key"].encode(),
        allowed_emails=frozenset({"owner@example.test"}),
        transfer_directory=spool,
        transfer_limits=TransferLimits(free_disk_bytes=4 * GIB),
        vault_limits=VaultLimits(connections=config["families"]),
        new_enrollment_enabled=True,
    )
    app = hosted.create_hosted_app(settings)
    store, vault, oauth = app.state.hosted_transfers, app.state.vault, app.state.oauth
    accounts = []
    for number in range(config["families"]):
        owner = (f"synthetic-family-{number}", f"synthetic-client-{number}")
        # Normal persistence and issuance paths enforce the candidate's real limits.
        # These are seeded synthetic grants, not evidence of browser consent.
        existing = vault.get("grants", owner[0])
        if existing is None:
            vault.put(
                "grants",
                owner[0],
                {
                    "verified_email": "owner@example.test",
                    "google_subject": "synthetic-owner",
                    "client_id": owner[1],
                    "packages": [APP],
                    "bucket": "",
                    "google_scopes": [SCOPES["publisher"]],
                    "access_token": f"synthetic-google-{number}",
                    "refresh_token": None,
                    "google_expires": time.time() + 7200,
                    "expires": time.time() + 7200,
                },
                7200,
            )
            vault.put(
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
                7200,
            )
        token = oauth.issue_tokens(*owner, ["play:read"])
        accounts.append(
            {
                "owner": owner,
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
            }
        )
    purpose = TransferPurpose(
        "synthetic-byte-fixture", {}, "application/octet-stream", "play:read", package=APP
    )

    def snapshot():
        with store._lock:
            records = list(store._records.values())
            physical = all(
                os.fstat(r.stream.fileno()).st_nlink == 0
                and os.fstat(r.stream.fileno()).st_blocks * 512 >= r.charge
                for r in records
                if r.stream is not None and not r.invalid
            )
            result = {
                "records": len(records),
                "charged_bytes": sum(r.charge for r in records),
                "active_streams": sum(r.active for r in records),
                "anonymous_physical": physical,
            }
        with vault.lock:
            result["vault_records"] = vault.db.execute("SELECT count(*) FROM records").fetchone()[0]
            result["lifecycle_records"] = vault.db.execute(
                "SELECT count(*) FROM record_owners"
            ).fetchone()[0]
            result["vault_payload_bytes"] = vault.db.execute(
                "SELECT coalesce(sum(length(payload)),0) FROM records"
            ).fetchone()[0]
        result.update(proc_sample())
        result.update(
            disk_free=shutil.disk_usage(spool).free,
            named_spools=len(list(spool.iterdir())),
            version=__version__,
            families=len(accounts),
        )
        return result

    def control(body):
        command = body["command"]
        number = body.get("owner", 0)
        require(type(number) is int and 0 <= number < len(accounts), "Invalid fixture owner")
        owner = tuple(accounts[number]["owner"])
        if command == "accounts":
            return {"accounts": accounts}
        if command == "snapshot":
            return snapshot()
        if command == "reserve":
            return store.reserve_upload(
                owner, purpose, body["size"], body["sha256"], time.monotonic() + 600
            )
        if command == "download":
            with store.reserve_download(
                owner, purpose, body["size"], time.monotonic() + 600
            ) as lease:
                for chunk in chunks(body["size"]):
                    lease.write(chunk)
                return lease.publish()
        if command == "discard":
            return {"state": store.discard(owner, body["transfer_id"])}
        if command == "discard_all":
            with store._lock:
                records = [(key, record.owner) for key, record in store._records.items()]
            for key, record_owner in records:
                store.discard(record_owner, key)
            return snapshot()
        if command == "disk_failure":
            before = snapshot()["records"]

            def exhausted(*args):
                raise OSError(errno.ENOSPC, "Synthetic allocation failure")

            with patch.object(store, "_allocate", exhausted):
                try:
                    store.reserve_upload(
                        owner, purpose, 65536, digest(65536), time.monotonic() + 60
                    )
                except PlayError:
                    pass
                else:
                    raise CheckFailure("Injected disk allocation failure was accepted")
            require(snapshot()["records"] == before, "Failed allocation retained quota")
            return {"injected_enospc": "passed", "actual_filesystem_full": "not_exercised"}
        if command == "fill_rows":
            target = body["per_family"]
            require(
                type(target) is int and 3 <= target <= vault.CONNECTION_LIMIT,
                "Invalid lifecycle row target",
            )
            for account in accounts:
                grant_id = account["owner"][0]
                with vault.lock:
                    count = vault.db.execute(
                        "SELECT count(*) FROM record_owners WHERE owner=?",
                        (vault.digest(grant_id),),
                    ).fetchone()[0]
                for start in range(count, target, 512):
                    vault.put_many(
                        [
                            (
                                "used_refresh",
                                f"fixture-row-{grant_id}-{index}",
                                {"subject": grant_id},
                                7200,
                            )
                            for index in range(start, min(start + 512, target))
                        ]
                    )
            return snapshot()
        if command == "byte_pressure":
            keys = []
            prefix = uuid.uuid4().hex

            def state():
                with vault.lock:
                    fingerprint = hashlib.sha256()
                    for namespace, key, expiry, payload in vault.db.execute(
                        "SELECT namespace,key,expires,payload FROM records ORDER BY namespace,key"
                    ):
                        fingerprint.update(json.dumps([namespace, key, expiry]).encode())
                        fingerprint.update(payload)
                    for query in (
                        "SELECT namespace,key,owner FROM record_owners ORDER BY namespace,key",
                        "SELECT namespace,owner,records,payload_bytes,grants FROM record_usage ORDER BY namespace,owner",
                    ):
                        for row in vault.db.execute(query):
                            fingerprint.update(json.dumps(row).encode())
                    owner_rows, owner_bytes = vault.db.execute(
                        "SELECT count(*),coalesce(sum(length(r.payload)),0) FROM records r "
                        "JOIN record_owners o ON r.namespace=o.namespace AND r.key=o.key WHERE o.owner=?",
                        (vault.digest(owner[0]),),
                    ).fetchone()
                    lifecycle_rows, lifecycle_bytes = vault.db.execute(
                        "SELECT count(*),coalesce(sum(length(r.payload)),0) FROM records r "
                        "JOIN record_owners o ON r.namespace=o.namespace AND r.key=o.key"
                    ).fetchone()
                    return {
                        "fingerprint": fingerprint.hexdigest(),
                        "owner_rows": owner_rows,
                        "owner_bytes": owner_bytes,
                        "lifecycle_rows": lifecycle_rows,
                        "lifecycle_bytes": lifecycle_bytes,
                    }

            original_encrypt = vault.cipher.encrypt
            attempted = []

            def measure_encrypt(value):
                encrypted = original_encrypt(value)
                attempted.append(len(encrypted))
                return encrypted

            try:
                for index in range(100):
                    key = f"bytes-{prefix}-{index}"
                    before = state()
                    attempted.clear()
                    try:
                        with patch.object(vault.cipher, "encrypt", new=measure_encrypt):
                            vault.put(
                                "used_refresh",
                                key,
                                {"subject": owner[0], "padding": "x" * 60000},
                                7200,
                            )
                    except PlayError as error:
                        require(len(attempted) == 1, "Byte probe did not encrypt one record")
                        validate_byte_rejection(
                            before, state(), attempted[0], str(error), len(accounts)
                        )
                        return {
                            "admitted_large_rows": len(keys),
                            "ciphertext_bytes": before["owner_bytes"],
                            "rejected_ciphertext_bytes": attempted[0],
                            "byte_limit": vault.CONNECTION_BYTES,
                            "state_unchanged": True,
                            "bounded_rejection": True,
                        }
                    keys.append(key)
                raise CheckFailure("Family ciphertext byte limit was not enforced")
            finally:
                for key in keys:
                    vault.delete("used_refresh", key)
        if command == "family_exhaustion":
            admitted = 0
            prefix = uuid.uuid4().hex
            for index in range(vault.CONNECTION_LIMIT + 1):
                try:
                    vault.put(
                        "used_refresh",
                        f"exhaustion-{prefix}-{owner[0]}-{index}",
                        {"subject": owner[0]},
                        7200,
                    )
                    admitted += 1
                except PlayError:
                    return {"admitted_before_rejection": admitted, "bounded_rejection": True}
            raise CheckFailure("Per-family row limit was not enforced")
        raise CheckFailure("Unknown fixture control command")

    async def fixture(scope, receive, send):
        if scope.get("path") == "/_fixture/control":
            headers = dict(scope.get("headers", []))
            if headers.get(b"x-fixture-control") != config["control"].encode():
                return await JSONResponse({"error": "forbidden"}, status_code=403)(
                    scope, receive, send
                )
            request = Request(scope, receive)
            data = await request.body()
            require(len(data) <= 65536, "Oversized control request")
            try:
                result = await anyio.to_thread.run_sync(control, json.loads(data))
                response = JSONResponse(result)
            except PlayError:
                response = JSONResponse({"error": "fixture_operation_rejected"}, status_code=400)
            except Exception as error:
                response = JSONResponse(
                    {"fixture_exception": type(error).__name__}, status_code=500
                )
            await response(scope, receive, send)
            return
        await app(scope, receive, send)

    def upstream(_opener, request, *args, **kwargs):
        require(request.get_method() == "GET", "Unexpected provider mutation")
        require(
            request.full_url.startswith(
                f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{APP}/reviews"
            ),
            "Unexpected provider URL",
        )
        auth = request.get_header("Authorization", "")
        matched = re.fullmatch(r"Bearer synthetic-google-(\d+)", auth)
        require(
            matched is not None and int(matched[1]) < len(accounts), "Unexpected provider identity"
        )
        connection = http.client.HTTPConnection("provider", 8082, timeout=5)
        try:
            connection.request("GET", "/reviews/" + matched[1])
            response = connection.getresponse()
            require(response.status == 200, "Synthetic provider failed")
            data = response.read(65537)
            require(len(data) <= 65536, "Synthetic provider exceeded bound")
        finally:
            connection.close()
        result = io.BytesIO(data)
        result.status = 200
        return result

    with (
        patch("urllib.request.OpenerDirector.open", upstream),
        patch("pubship.server.Auth", side_effect=CheckFailure("Local credentials forbidden")),
    ):
        uvicorn.run(
            fixture,
            host="0.0.0.0",
            port=8080,
            access_log=False,
            log_level="critical",
            proxy_headers=False,
            limit_concurrency=100,
            timeout_keep_alive=5,
        )
    vault.close()


class Driver:
    def __init__(self, profile):
        self.profile = profile
        self.budget = Budget()
        self.config = json.loads((CONFIG / "fixture.json").read_text())
        self.tls = ssl.create_default_context(cafile=str(CONFIG / "cert.pem"))
        self.accounts = []
        self.samples = []
        self.account_lock = threading.Lock()
        self.report = {
            "profile": asdict(profile),
            "status": "running",
            "cases": {},
            "measurement": "Application process RSS/FDs; separate driver and synthetic-provider containers",
            "unexercised": [
                "production Google",
                "public-service pressure",
                "HTTP/2 multiplexing",
                "HTTP/3 traffic",
                "actual full filesystem",
                "desktop attachments",
                "onboarding exhaustion versus token/revoke budgets (separate short fixture/unit evidence)",
                "full 630-second incomplete-header deadline",
            ],
        }

    def connection(self, *, direct=False, timeout=15):
        remaining = self.budget.remaining()
        if getattr(self, "_probe_deadline", None) is not None:
            remaining = min(remaining, self._probe_deadline - self.budget.clock())
        require(remaining > 0, "Condition did not recover before deadline")
        timeout = min(timeout, remaining)
        if direct:
            return http.client.HTTPConnection("app", 8080, timeout=timeout)
        return http.client.HTTPSConnection(
            "launch-fixture.invalid", 8443, context=self.tls, timeout=timeout
        )

    def request(self, method, path, body=b"", headers=None, *, direct=False, timeout=15):
        if isinstance(body, str):
            body = body.encode()
        self.budget.charge(len(body) + 2048)
        connection = self.connection(direct=direct, timeout=timeout)
        try:
            connection.request(
                method, path, body, {"Host": "launch-fixture.invalid", **(headers or {})}
            )
            response = connection.getresponse()
            content = response.read(2 * MIB + 1)
            self.budget.charge(len(content))
            require(len(content) <= 2 * MIB, "Oversized control/JSON response")
            return response.status, dict(response.getheaders()), content
        finally:
            connection.close()

    def control(self, command, *, rejected=False, **values):
        status, _, data = self.request(
            "POST",
            "/_fixture/control",
            json.dumps({"command": command, **values}),
            {"X-Fixture-Control": self.config["control"], "Content-Type": "application/json"},
            direct=True,
            timeout=660,
        )
        require(
            status == (400 if rejected else 200),
            f"Fixture control {command} returned HTTP {status}: "
            + (
                json.loads(data).get("fixture_exception", "rejected")
                if status == 500
                else "unexpected status"
            ),
        )
        return json.loads(data)

    def wait(self, predicate, seconds=10, *, started=None):
        self.budget.charge()
        started = self.budget.clock() if started is None else started
        end = min(started + seconds, self.budget.started + self.budget.seconds)
        previous = getattr(self, "_probe_deadline", None)
        self._probe_deadline = end
        try:
            while True:
                require(self.budget.clock() < end, "Condition did not recover before deadline")
                recovered = predicate()
                now = self.budget.clock()
                require(now <= end, "Condition did not recover before deadline")
                if recovered:
                    return round(now - started, 3)
                time.sleep(min(0.05, end - now))
        finally:
            self._probe_deadline = previous

    def start(self):
        deadline = time.monotonic() + 45
        while True:
            try:
                if self.request("GET", "/healthz")[0] == 200:
                    break
            except (OSError, http.client.HTTPException):
                pass
            require(time.monotonic() < deadline, "Fixture stack failed to become healthy")
            time.sleep(0.25)
        self.accounts = self.control("accounts")["accounts"]
        require(
            len(self.accounts) == self.profile.families,
            "Target families were not genuinely admitted",
        )
        self.read(0)
        self.report["baseline"] = self.control("snapshot")
        # The test-only control surface must never be exposed by the proxy.
        require(
            self.request("POST", "/_fixture/control", "{}")[0] == 404,
            "Proxy exposed fixture controls",
        )

    def read(self, number, request_id=None):
        started = time.monotonic()
        with self.account_lock:
            token = self.accounts[number]["access_token"]
        status, _, data = self.request(
            "POST",
            "/mcp",
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id or uuid.uuid4().hex,
                    "method": "tools/call",
                    "params": {"name": "list_reviews", "arguments": {"package": APP}},
                }
            ),
            {
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        require(status == 200, "Unexpected read HTTP result")
        value = json.loads(data)["result"]
        require(not value.get("isError"), "Read returned tool error")
        require(
            value["structuredContent"]["data"]["reviews"] == [{"reviewId": str(number)}],
            "Tenant response mismatch",
        )
        return (time.monotonic() - started) * 1000

    def refresh(self, number):
        started = time.monotonic()
        with self.account_lock:
            account = dict(self.accounts[number])
        status, _, data = self.request(
            "POST",
            "/token",
            urlencode(
                {
                    "grant_type": "refresh_token",
                    "client_id": account["owner"][1],
                    "refresh_token": account["refresh_token"],
                    "resource": ISSUER + "/mcp",
                }
            ),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if status != 200:
            try:
                code = json.loads(data).get("error")
            except (ValueError, AttributeError):
                code = None
            if code not in {
                "invalid_grant",
                "invalid_client",
                "invalid_scope",
                "invalid_request",
                "server_error",
                "rate_limited",
            }:
                code = "unclassified"
            raise CheckFailure(
                f"Existing connection {number} refresh returned HTTP {status} ({code})"
            )
        replacement = json.loads(data)
        with self.account_lock:
            self.accounts[number].update(
                access_token=replacement["access_token"], refresh_token=replacement["refresh_token"]
            )
        return (time.monotonic() - started) * 1000

    def sample(self):
        value = self.control("snapshot")
        value["elapsed_seconds"] = round(time.monotonic() - self.budget.started, 3)
        require(
            value["peak_rss_kib"] < 768 * 1024,
            "Application peak RSS exceeded fixed acceptance ceiling",
        )
        require(int(value["memory.events"].get("oom", "0")) == 0, "Application cgroup reported OOM")
        require(value["charged_bytes"] <= 8 * GIB, "Transfer aggregate quota exceeded")
        require(value["anonymous_physical"], "Transfer allocation was named or sparse")
        self.samples.append(value)
        return value

    def reads(self, seconds, *, rotations=False):
        started = time.monotonic()
        latencies, pending = [], []
        issued, next_sample, rotation = 0, started, 0
        with ThreadPoolExecutor(max_workers=min(self.profile.families, 32)) as pool:
            while time.monotonic() - started < seconds:
                self.budget.charge()
                now = time.monotonic()
                if now >= next_sample:
                    self.sample()
                    next_sample = now + 5
                if rotations and now - started >= (rotation + 1) * 480 and rotation < 2:
                    # Keep OAuth bursts below the proxy's per-IP burst allowance.
                    for number in range(len(self.accounts)):
                        self.refresh(number)
                        time.sleep(0.04)
                    rotation += 1
                due = started + issued / self.profile.rate
                if now < due:
                    time.sleep(min(due - now, 0.05))
                    continue
                pending.append(
                    pool.submit(self.read, issued % len(self.accounts), f"read-{started}-{issued}")
                )
                issued += 1
                if len(pending) >= 32:
                    latencies.append(pending.pop(0).result())
            latencies.extend(future.result() for future in pending)
        elapsed = time.monotonic() - started
        require(
            issued / seconds >= self.profile.rate * 0.95,
            "Driver could not sustain fixed offered rate",
        )
        result = distribution(latencies)
        result.update(
            elapsed_seconds=round(elapsed, 3),
            offered_rate=self.profile.rate,
            completed_rate=round(issued / elapsed, 3),
            token_rotation_rounds=rotation,
            scheduled_minute_windows=[
                {
                    "minute": index // (self.profile.rate * 60),
                    **distribution(latencies[index : index + self.profile.rate * 60]),
                }
                for index in range(0, len(latencies), self.profile.rate * 60)
            ],
            unexpected_errors=0,
            tenant_mismatches=0,
        )
        require(
            result["p95_ms"] <= 2000 and result["p99_ms"] <= 5000, "Read latency threshold exceeded"
        )
        return result

    def upload(self, number, transfer_id, size, *, expected=200):
        self.budget.charge(size + 2048)
        connection = self.connection(timeout=610)
        try:
            connection.putrequest("PUT", f"/transfers/{transfer_id}/content", skip_host=True)
            for key, value in {
                "Host": "launch-fixture.invalid",
                "Authorization": "Bearer " + self.accounts[number]["access_token"],
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            }.items():
                connection.putheader(key, value)
            connection.endheaders()
            for chunk in chunks(size):
                self.budget.charge()
                connection.send(chunk)
            response = connection.getresponse()
            data = response.read(65537)
            self.budget.charge(len(data))
            require(len(data) <= 65536 and response.status == expected, "Upload result mismatch")
            if expected == 200:
                value = json.loads(data)
                require(value.get("state") == "READY", "Upload did not seal")
            return response.status
        finally:
            connection.close()

    def download(self, number, transfer_id, size):
        self.budget.charge(size + 2048)
        connection = self.connection(timeout=610)
        result, received = hashlib.sha256(), 0
        try:
            connection.request(
                "GET",
                f"/transfers/{transfer_id}/content",
                headers={
                    "Host": "launch-fixture.invalid",
                    "Authorization": "Bearer " + self.accounts[number]["access_token"],
                },
            )
            response = connection.getresponse()
            require(
                response.status == 200 and response.getheader("Content-Length") == str(size),
                "Download status/length mismatch",
            )
            while chunk := response.read(MIB):
                self.budget.charge()
                received += len(chunk)
                require(received <= size, "Download exceeded reservation")
                result.update(chunk)
            require(
                received == size and result.hexdigest() == digest(size),
                "Download integrity mismatch",
            )
        finally:
            connection.close()

    def reserve(self, number, size, *, rejected=False):
        return self.control(
            "reserve", owner=number, size=size, sha256=digest(min(size, GIB)), rejected=rejected
        )

    def roundtrip(self, number, size):
        started = time.monotonic()
        uploaded = self.reserve(number, size)
        self.upload(number, uploaded["transfer_id"], size)
        self.control("discard", owner=number, transfer_id=uploaded["transfer_id"])
        downloaded = self.control("download", owner=number, size=size)
        self.download(number, downloaded["transfer_id"], size)
        self.control("discard", owner=number, transfer_id=downloaded["transfer_id"])
        return {"bytes_each_direction": size, "seconds": round(time.monotonic() - started, 3)}

    def held(self, number, transfer_id):
        connection = self.connection(timeout=40)
        connection.putrequest("PUT", f"/transfers/{transfer_id}/content", skip_host=True)
        for key, value in {
            "Host": "launch-fixture.invalid",
            "Authorization": "Bearer " + self.accounts[number]["access_token"],
            "Content-Type": "application/octet-stream",
            "Content-Length": "65536",
        }.items():
            connection.putheader(key, value)
        connection.endheaders()
        connection.send(b"x")
        self.budget.charge(2049)
        return connection

    def stream_pressure(self):
        self.control("discard_all")
        first = self.reserve(0, 65536)
        same_owner = self.held(0, first["transfer_id"])
        try:
            self.wait(lambda: self.control("snapshot")["active_streams"] == 1)
            second = self.reserve(0, 65536)
            self.upload(0, second["transfer_id"], 65536, expected=400)
        finally:
            same_owner.close()
        self.wait(lambda: self.control("snapshot")["records"] == 1, seconds=5)
        self.upload(0, second["transfer_id"], 65536)
        self.control("discard_all")
        held = []
        try:
            for number in range(4):
                value = self.reserve(number, 65536)
                held.append(self.held(number, value["transfer_id"]))
            self.wait(lambda: self.control("snapshot")["active_streams"] == 4)
            extra = self.reserve(4, 65536)
            self.upload(4, extra["transfer_id"], 65536, expected=400)
        finally:
            for connection in held:
                connection.close()
        self.wait(lambda: self.control("snapshot")["records"] == 1, seconds=5)
        self.upload(4, extra["transfer_id"], 65536)
        self.control("discard_all")
        return {
            "per_owner_held_streams": 1,
            "per_owner_overflow_status": 400,
            "per_owner_recovery_status": 200,
            "held_streams": 4,
            "overflow_status": 400,
            "recovery_status": 200,
            "cleanup_seconds_max": 5,
        }

    def maximum(self):
        require(self.profile.families == 32, "Maximum profile requires 32 families")
        require(
            self.sample()["disk_free"] >= 13 * GIB,
            "Insufficient isolated disk headroom for maximum profile",
        )
        cases = self.report["cases"]
        cases["limit_minus_one"] = self.roundtrip(0, GIB - 1)
        cases["limit_exact"] = self.roundtrip(0, GIB)
        before = self.control("snapshot")
        self.reserve(0, GIB + 1, rejected=True)
        require(
            self.control("snapshot")["records"] == before["records"],
            "Oversize reservation consumed capacity",
        )
        cases["object_overflow"] = "rejected_without_allocation"
        with ThreadPoolExecutor(max_workers=4) as pool:
            cases["four_maximum_roundtrips"] = list(
                pool.map(lambda number: self.roundtrip(number, GIB), range(4))
            )
        for number in range(4):
            self.reserve(number, GIB)
            self.reserve(number, GIB)
            if number == 0:
                self.reserve(0, 65536, rejected=True)
                cases["connection_bytes"] = "2GiB admitted; overflow rejected"
        charged = self.sample()
        require(charged["charged_bytes"] == 8 * GIB, "Maximum aggregate reservation mismatch")
        self.reserve(4, 65536, rejected=True)
        self.control("discard_all")
        cases["global_bytes"] = {
            "charged_bytes": charged["charged_bytes"],
            "overflow_rejected": True,
        }
        for _ in range(4):
            self.reserve(0, 65536)
        self.reserve(0, 65536, rejected=True)
        for number in range(1, 29):
            self.reserve(number, 65536)
        require(self.sample()["records"] == 32, "Handle envelope mismatch")
        self.reserve(29, 65536, rejected=True)
        self.control("discard_all")
        cases["handles"] = {"connection": 4, "global": 32, "overflow_rejected": True}
        cases["stream_pressure"] = self.stream_pressure()
        cases["disk_failure"] = self.control("disk_failure")

    def soak(self):
        cases = self.report["cases"]
        cases["warmup"] = self.reads(self.profile.warm_seconds)
        warm_values = [s["rss_kib"] for s in self.samples[-12:]]
        warm = statistics.median(warm_values)
        self.report["warmup_application_samples"] = list(self.samples)
        cases["rss_growth"] = {"warm_median_kib": warm}
        self.samples.clear()
        cases["steady"] = self.reads(self.profile.load_seconds, rotations=True)
        require(cases["steady"]["token_rotation_rounds"] == 2, "Two rotation rounds not completed")
        last = statistics.median([s["rss_kib"] for s in self.samples[-60:]])
        cases["rss_growth"].update(
            final_load_median_kib=last, growth_kib=last - warm, maximum_growth_kib=32 * 1024
        )
        require(last <= warm + 32 * 1024, "Application RSS growth exceeded fixed threshold")
        end = time.monotonic() + self.profile.drain_seconds
        while time.monotonic() < end:
            self.sample()
            time.sleep(min(5, max(0, end - time.monotonic())))

    def slow_body(self, progress):
        connection = self.connection(timeout=40)
        connection.putrequest("POST", "/mcp", skip_host=True)
        connection.putheader("Host", "launch-fixture.invalid")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", "100000")
        connection.endheaders()
        connection.send(b"x")
        self.budget.charge(2049)
        done = threading.Event()

        def drip():
            while not done.wait(1):
                try:
                    self.budget.charge(1)
                    connection.send(b"x")
                except (OSError, CheckFailure):
                    return

        sender = threading.Thread(target=drip, daemon=True)
        if progress:
            sender.start()
        started = time.monotonic()
        try:
            response = connection.getresponse()
            self.budget.charge(len(response.read(65536)))
            elapsed = time.monotonic() - started
            expected = 30 if progress else 10
            require(
                response.status == 408 and expected - 0.5 <= elapsed <= expected + 5,
                "Proxy body deadline mismatch",
            )
            return {"status": response.status, "seconds": round(elapsed, 3)}
        finally:
            done.set()
            if progress:
                sender.join(2)
            connection.close()

    def proxy(self):
        cases = self.report["cases"]
        cases["idle_body"] = self.slow_body(False)
        cases["total_body"] = self.slow_body(True)
        require(
            self.request(
                "POST", "/mcp", b"x" * (256 * 1024 + 1), {"Content-Type": "application/json"}
            )[0]
            == 413,
            "Ordinary JSON body cap was not enforced",
        )
        cases["body_cap"] = {"bytes": 256 * 1024 + 1, "status": 413}

        burst_offers = []

        def health(_):
            burst_offers.append(self.budget.clock())
            return self.request(
                "GET",
                "/healthz",
                headers={
                    "X-Forwarded-For": f"198.51.100.{_ % 250 + 1}",
                    "X-Real-IP": f"198.51.100.{_ % 250 + 1}",
                },
            )[0]

        statuses = []
        # Finite offered burst; direct application monitoring cannot spend the
        # proxy's client-IP budget and does not masquerade as customer traffic.
        with ThreadPoolExecutor(max_workers=32) as pool:
            statuses = list(pool.map(health, range(180)))
        require(429 in statuses and set(statuses) <= {200, 429}, "Proxy rate ceiling not observed")
        burst_stopped = max(burst_offers)
        cases["spoofed_forwarding_header_pressure"] = {
            str(code): statuses.count(code) for code in sorted(set(statuses))
        }
        cases["burst_recovery_seconds"] = self.wait(
            lambda: self.request("GET", "/healthz")[0] == 200, seconds=10, started=burst_stopped
        )
        pressure_started = time.monotonic()
        pressure_latencies, pressure_statuses, pending = [], [], []
        issued, next_sample = 0, pressure_started

        def measured_health():
            started = time.monotonic()
            code = self.request("GET", "/healthz")[0]
            require(code in {200, 429}, "Unexpected sustained proxy pressure result")
            return code, (time.monotonic() - started) * 1000

        with ThreadPoolExecutor(max_workers=32) as pool:
            while time.monotonic() - pressure_started < 300:
                now = time.monotonic()
                self.budget.charge()
                if now >= next_sample:
                    self.sample()
                    next_sample = now + 5
                due = pressure_started + issued / 60
                if now < due:
                    time.sleep(min(due - now, 0.02))
                    continue
                pending.append(pool.submit(measured_health))
                issued += 1
                if len(pending) >= 32:
                    code, latency = pending.pop(0).result()
                    pressure_statuses.append(code)
                    pressure_latencies.append(latency)
            pressure_stopped = self.budget.clock()
            for future in pending:
                code, latency = future.result()
                pressure_statuses.append(code)
                pressure_latencies.append(latency)
        require(
            issued >= 17100 and 429 in pressure_statuses, "Sustained offered pressure not achieved"
        )
        cases["sustained_rate_pressure"] = {
            "seconds": round(time.monotonic() - pressure_started, 3),
            "offered_rate": 60,
            "latency": distribution(pressure_latencies),
            "statuses": {
                str(code): pressure_statuses.count(code) for code in sorted(set(pressure_statuses))
            },
        }
        cases["sustained_recovery_seconds"] = self.wait(
            lambda: self.request("GET", "/healthz")[0] == 200, seconds=10, started=pressure_stopped
        )
        cases["bounded_sockets"] = self.socket_pressure()
        cases["incomplete_header_deadline"] = "not_exercised; 630s configured, no expiry receipt"

    def socket_pressure(self):
        # Complete headers, incomplete bodies: slots reach the application and
        # must recover when clients close. Opening is paced below rate average.
        before = self.sample()
        connections = []
        try:
            for _ in range(128):
                connection = self.connection(timeout=15)
                connection.putrequest("POST", "/mcp", skip_host=True)
                connection.putheader("Host", "launch-fixture.invalid")
                connection.putheader("Content-Length", "100000")
                connection.endheaders()
                connection.send(b"x")
                connections.append(connection)
                self.budget.charge(2049)
                time.sleep(0.04)
        finally:
            sockets_stopped = self.budget.clock()
            for connection in connections:
                connection.close()
        socket_recovery = self.wait(
            lambda: self.request("GET", "/healthz")[0] == 200, seconds=10, started=sockets_stopped
        )
        after = self.sample()
        return {
            "attempted": 128,
            "before_fds": before["fds"],
            "after_fds": after["fds"],
            "retained_peak_rss_kib": after["peak_rss_kib"],
            "peak_fds": "not measured while app control pool is saturated",
            "recovery_seconds": socket_recovery,
            "recovery": "passed",
            "note": "socket attempts, not 128 admitted requests",
        }

    def capacity(self):
        cases = self.report["cases"]
        cases["small_vault"] = self.reads(10)
        cases["small_refresh_latency"] = distribution(
            [self.refresh(n) for n in range(len(self.accounts))]
        )
        cases["family_byte_pressure"] = self.control("byte_pressure", owner=0)
        self.read(1)
        self.control("fill_rows", per_family=4090)
        cases["near_full_snapshot"] = self.sample()
        cases["near_full_reads"] = self.reads(30)
        refresh_latencies = []
        for _ in range(2):
            for number in range(len(self.accounts)):
                refresh_latencies.append(self.refresh(number))
                time.sleep(0.04)
        cases["near_full_refresh_latency"] = distribution(refresh_latencies)
        require(
            cases["near_full_refresh_latency"]["p95_ms"] <= 2000
            and cases["near_full_refresh_latency"]["p99_ms"] <= 5000,
            "Loaded refresh latency threshold exceeded",
        )
        cases["near_full_rotations"] = 2
        exhausted = self.control("family_exhaustion", owner=0)
        require(exhausted["bounded_rejection"], "Abusive family did not reach bounded rejection")
        self.refresh(1)
        self.read(1)
        cases["neighbor_after_exhaustion"] = "read_and_refresh_passed"
        self.control("fill_rows", per_family=4096)
        cases["full_snapshot"] = self.sample()
        require(
            cases["full_snapshot"]["lifecycle_records"] == self.profile.families * 4096,
            "Full lifecycle occupancy mismatch",
        )
        cases["full_row_overflow"] = self.control("family_exhaustion", owner=0)
        require(
            cases["full_row_overflow"]["admitted_before_rejection"] == 0,
            "Full vault admitted extra lifecycle row",
        )
        cases["full_reads"] = self.reads(30)

    def run(self):
        try:
            self.start()
            if self.profile.name == "smoke":
                self.report["cases"]["reads"] = self.reads(5)
                for number in range(len(self.accounts)):
                    self.refresh(number)
                    time.sleep(0.04)
                self.report["cases"]["refresh_families"] = len(self.accounts)
                self.report["cases"]["roundtrip"] = self.roundtrip(0, 65536)
                if len(self.accounts) >= 5:
                    self.report["cases"]["stream_pressure"] = self.stream_pressure()
            else:
                getattr(self, self.profile.name)()
            self.control("discard_all")
            self.wait(
                lambda: self.control("snapshot")["fds"] <= self.report["baseline"]["fds"] + 4,
                seconds=30,
            )
            final = self.sample()
            require(final["records"] == 0 and final["named_spools"] == 0, "Transfer cleanup failed")
            require(
                final["fds"] <= self.report["baseline"]["fds"] + 4,
                "Application descriptors did not drain",
            )
            self.report.update(status="passed", final=final)
        except Exception as error:
            self.report.update(
                status="failed",
                failure=str(error) if isinstance(error, CheckFailure) else type(error).__name__,
            )
        self.report.update(
            elapsed_seconds=round(time.monotonic() - self.budget.started, 3),
            accounted_traffic_bytes=self.budget.used,
            application_samples=self.samples,
        )
        return self.report


def docker_arguments(name, image_id, *, network, mounts, cpus, memory, command):
    """Every runtime stays on one internal network without a published port."""
    return [
        "run",
        "--name",
        name,
        "--network",
        network,
        "--user",
        "10001:10001",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--cpus",
        str(cpus),
        "--memory",
        memory,
        "--pids-limit",
        "128",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
        "--env",
        "PUBSHIP_SERVER_LAUNCH_FIXTURE=1",
        *[part for mount in mounts for part in ("--mount", mount)],
        "--entrypoint",
        command[0],
        image_id,
        *command[1:],
    ]


def proxy_arguments():
    return [
        "/usr/local/bin/traefik",
        "--providers.file.filename=/qa-config/dynamic.yaml",
        "--entrypoints.web.address=:8081",
        "--entrypoints.web.transport.respondingtimeouts.readtimeout=30s",
        "--entrypoints.web.http.redirections.entrypoint.to=websecure",
        "--entrypoints.web.http.redirections.entrypoint.scheme=https",
        "--entrypoints.websecure.address=:8443",
        "--entrypoints.websecure.transport.respondingtimeouts.readtimeout=630s",
        "--entrypoints.websecure.http2.maxconcurrentstreams=250",
        "--accesslog=false",
        "--log.level=ERROR",
    ]


def run_isolated(image, proxy_image, profile):
    require(
        re.fullmatch(r"traefik@sha256:[a-f0-9]{64}", proxy_image) is not None,
        "Proxy image must be an explicit Traefik digest",
    )
    started = time.monotonic()
    name = "pubship-launch-" + uuid.uuid4().hex
    network = name + "-network"
    volumes, containers = [], []
    report, measurements = {}, []
    stop = threading.Event()
    watcher = None

    def docker(*arguments, check=True, timeout=60):
        remaining = MAX_SECONDS - (time.monotonic() - started)
        require(remaining > 0, "Outer wall-clock budget exhausted")
        result = subprocess.run(
            ["docker", *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=min(timeout, remaining),
        )
        if check and result.returncode:
            raise CheckFailure(
                f"Docker fixture {arguments[0]} operation failed (exit {result.returncode})"
            )
        return result

    def inspect(reference):
        result = docker("image", "inspect", reference)
        return json.loads(result.stdout)[0]

    app_image, proxy = inspect(image), inspect(proxy_image)
    require(
        app_image["Config"]["User"] == "10001:10001",
        "Candidate image must declare UID/GID 10001:10001",
    )
    source = Path(__file__).read_bytes()
    source_digest = hashlib.sha256(source).hexdigest()
    frozen = tempfile.TemporaryDirectory(prefix=".pubship-launch-script-", dir=Path.home())
    frozen_path = Path(frozen.name) / "launch.py"
    frozen_path.write_bytes(source)
    frozen_path.chmod(0o444)
    script_mount = f"type=bind,src={frozen_path},dst=/launch.py,readonly"
    try:
        docker("network", "create", "--internal", network)
        for label in ("config", "data"):
            volume = name + "-" + label
            volumes.append(volume)
            docker("volume", "create", volume)
        config_mount = f"type=volume,src={volumes[0]},dst=/qa-config"
        data_mount = f"type=volume,src={volumes[1]},dst=/qa-data"
        initializer = name + "-init"
        containers.append(initializer)
        docker(
            "run",
            "--name",
            initializer,
            "--network",
            "none",
            "--user",
            "0:0",
            "--read-only",
            "--env",
            "PUBSHIP_SERVER_LAUNCH_FIXTURE=1",
            "--mount",
            config_mount,
            "--mount",
            data_mount,
            "--mount",
            script_mount,
            "--entrypoint",
            "python",
            app_image["Id"],
            "/launch.py",
            "--mode",
            "provision",
            "--families",
            str(profile.families),
        )
        for role, entrypoint, cpus, memory in (
            ("provider", ["python", "/launch.py", "--mode", "provider"], 0.5, "128m"),
            ("app", ["python", "/launch.py", "--mode", "application"], 1.5, "1g"),
            ("proxy", proxy_arguments(), 1, "512m"),
        ):
            container = name + "-" + role
            containers.append(container)
            mounts = [config_mount + ",readonly", script_mount]
            if role == "app":
                mounts.append(data_mount)
            args = docker_arguments(
                container,
                proxy["Id"] if role == "proxy" else app_image["Id"],
                network=network,
                mounts=mounts,
                cpus=cpus,
                memory=memory,
                command=entrypoint,
            )
            args[1:1] = ["-d", "--network-alias", role]
            if role == "proxy":
                args[1:1] = ["--network-alias", "launch-fixture.invalid"]
            if role == "app":
                args[1:1] = [
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700",
                ]
            docker(*args)

        def monitor():
            while not stop.wait(5):
                try:
                    result = docker(
                        "stats",
                        "--no-stream",
                        "--format",
                        "{{json .}}",
                        name + "-app",
                        name + "-proxy",
                        name + "-provider",
                        timeout=15,
                    )
                    measurements.append(
                        {
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "containers": [json.loads(line) for line in result.stdout.splitlines()],
                        }
                    )
                except Exception:
                    measurements.append(
                        {
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                            "error": "sampling_unavailable",
                        }
                    )

        watcher = threading.Thread(target=monitor, daemon=True)
        watcher.start()
        container = name + "-driver"
        containers.append(container)
        args = docker_arguments(
            container,
            app_image["Id"],
            network=network,
            mounts=[config_mount + ",readonly", script_mount],
            cpus=1,
            memory="512m",
            command=[
                "python",
                "/launch.py",
                "--mode",
                "driver",
                "--profile",
                profile.name,
                "--families",
                str(profile.families),
            ],
        )
        result = docker(*args, check=False, timeout=MAX_SECONDS)
        try:
            report = json.loads(result.stdout)
        except (ValueError, TypeError):
            report = {
                "status": "failed",
                "failure": "Driver did not produce a bounded JSON receipt",
            }
        require(
            result.returncode == 0 and report.get("status") == "passed", "Driver acceptance failed"
        )
        runtime = json.loads(docker("inspect", name + "-app").stdout)[0]
        require(
            runtime["HostConfig"]["Memory"] == GIB
            and runtime["HostConfig"]["NanoCpus"] == 1500000000,
            "Application resource limits differ from acceptance profile",
        )
        require(not runtime["State"]["OOMKilled"], "Application was OOM-killed")
        report["runtime_limits"] = {
            "memory_bytes": runtime["HostConfig"]["Memory"],
            "nano_cpus": runtime["HostConfig"]["NanoCpus"],
            "application_limit_concurrency": 100,
            "sqlite_tmpfs": runtime["HostConfig"]["Tmpfs"],
            "proxy_memory_mib": 512,
            "proxy_cpus": 1,
            "driver_memory_mib": 512,
            "driver_cpus": 1,
        }
    except Exception as error:
        report.update(
            status="failed",
            orchestration_failure=str(error)
            if isinstance(error, CheckFailure)
            else type(error).__name__,
        )
    finally:
        stop.set()
        if watcher:
            watcher.join(20)
        cleanup = []
        # A hung resource must not prevent attempts to remove the remaining owned
        # resources. Teardown has a separate bounded allowance after workload stop.
        teardown = [
            *[["rm", "-f", container] for container in reversed(containers)],
            *[["volume", "rm", volume] for volume in reversed(volumes)],
            ["network", "rm", network],
        ]
        for arguments in teardown:
            try:
                result = subprocess.run(
                    ["docker", *arguments], capture_output=True, timeout=20, check=False
                )
                cleanup.append(result.returncode == 0)
            except (OSError, subprocess.TimeoutExpired):
                cleanup.append(False)
        try:
            frozen.cleanup()
        except OSError:
            cleanup.append(False)
        report["cleanup"] = "passed" if all(cleanup) else "failed"
        if not all(cleanup):
            report["status"] = "failed"
    report.update(
        harness_sha256=source_digest,
        application_image=app_image["Id"],
        proxy_image=proxy_image,
        proxy_image_id=proxy["Id"],
        proxy_configuration_sha256=hashlib.sha256(
            json.dumps(proxy_configuration(), sort_keys=True).encode()
        ).hexdigest(),
        proxy_arguments=proxy_arguments()[1:],
        container_samples=measurements,
        host_platform=platform.platform(),
        total_seconds=round(time.monotonic() - started, 3),
    )
    report["equivalence_limits"] = [
        "isolated private TLS certificate and ports",
        "separate fixed driver/provider resources",
        "proxy 1 CPU/512MiB fixture allocation; production proxy resource parity not attested",
        "private64MiB SQLite tmpfs inside application cgroup; deployment temp-storage parity reported separately",
        "no production ingress network",
        "service TLS ALPN is HTTP/1.1 only; HTTP/3 disabled",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", help="Already-built candidate application image")
    parser.add_argument("--proxy-image", default=PROXY_IMAGE)
    parser.add_argument(
        "--profile", choices=("smoke", "maximum", "soak", "proxy", "capacity"), default="smoke"
    )
    parser.add_argument("--families", type=int, default=32)
    parser.add_argument(
        "--mode", choices=("provision", "provider", "application", "driver"), help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    profile = Profile(
        args.profile, families=args.families, object_bytes=65536 if args.profile == "smoke" else GIB
    )
    if args.mode:
        require(
            os.environ.get("PUBSHIP_SERVER_LAUNCH_FIXTURE") == "1" and Path("/.dockerenv").exists(),
            "Internal modes require the isolated fixture container",
        )
        if args.mode == "provision":
            provision(args.families)
            return
        if args.mode == "provider":
            provider_main()
            return
        if args.mode == "application":
            application_main()
            return
        result = Driver(profile).run()
    else:
        if not args.image:
            parser.error("--image is required; the harness never accepts a live target URL")
        try:
            result = run_isolated(args.image, args.proxy_image, profile)
        except Exception as error:
            result = {
                "status": "failed",
                "failure": str(error) if isinstance(error, CheckFailure) else type(error).__name__,
            }

    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
