"""Real browser consent navigation; every HTTP request is intercepted locally.

Run with ``uv run --frozen --all-extras --group browser pytest tests/browser``
after ``uv run --group browser playwright install chromium firefox webkit``.
The default environment skips this module when the optional group is absent.
An installed dependency with a missing browser is an error, including in CI.
"""

import base64
import hashlib
import html
import ssl
import sys
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from starlette.testclient import TestClient

from pubship.auth import SCOPES
from pubship.hosted import COOKIE, create_hosted_app
from pubship.hosted_config import HostedSettings
from pubship.hosted_grants import APPSTORE_EVENTS, APPSTORE_SCOPES
from pubship.hosted_grants import CAPABILITIES as CURRENT_CAPABILITIES
from pubship.hosted_grants import V2_SCOPES as ALL_SCOPES
from pubship.oauth import READ_SCOPE

# This matrix preserves the 0.20 ordinary/store controls. New administrative
# forms are exercised separately in the 0.21 native browser suite.
CAPABILITIES = {key: value for key, value in CURRENT_CAPABILITIES.items() if key in ALL_SCOPES}

playwright = pytest.importorskip("playwright.sync_api", reason="optional browser dependency group")

ISSUER = "https://mcp.example.test"
GOOGLE = "https://accounts.google.com"
CLIENT = "https://client.example/callback"
FOREIGN = "https://other.example.test"


@pytest.fixture(scope="module", params=["chromium", "firefox", "webkit"])
def browser(request):
    with playwright.sync_playwright() as engines:
        instance = getattr(engines, request.param).launch(
            headless=True, proxy={"server": "http://127.0.0.1:9"}
        )
        yield instance
        instance.close()


class BrowserBridge:
    def __init__(self, client, context):
        self.client = client
        self.context = context
        self.connect_url = ""
        self.posts = []
        self.callback_requests = []
        self.google_requests = []
        self.client_requests = []
        self.unexpected_requests = []
        self.page_headers = {}
        self.consent_headers = {}
        self.callback_state = None
        self.client_lock = threading.Lock()

    def begin(self, scopes=(READ_SCOPE,)):
        scope = " ".join(sorted(scopes))
        response = self.client.post(
            "/register",
            json={
                "client_name": "Synthetic browser client",
                "redirect_uris": [CLIENT],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "scope": scope,
            },
        )
        assert response.status_code == 201
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(b"v" * 64).digest()).decode().rstrip("=")
        )
        response = self.client.get(
            "/authorize",
            params={
                "client_id": response.json()["client_id"],
                "redirect_uri": CLIENT,
                "response_type": "code",
                "scope": scope,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "synthetic-client-state",
                "resource": ISSUER + "/mcp",
            },
        )
        assert response.status_code == 302
        self.connect_url = response.headers["location"]
        page = self.context.new_page()
        page.set_default_timeout(5000)
        page.goto(self.connect_url)
        page.get_by_label("Android package names", exact=True).fill("com.example.synthetic")
        return page

    def exchange(self, method, request_url, headers, body):
        url = urlsplit(request_url)
        origin = f"{url.scheme}://{url.netloc}"
        if origin == ISSUER:
            # The browser is the only cookie authority. TestClient's cookie jar
            # must never mask a missing, rejected or SameSite-blocked cookie.
            with self.client_lock:
                self.client.cookies.clear()
                response = self.client.request(method, request_url, headers=headers, content=body)
                self.client.cookies.clear()
            response_headers = dict(response.headers)
            for key in ("content-length", "content-encoding", "transfer-encoding"):
                response_headers.pop(key, None)
            if url.path == "/connect" and method == "GET":
                response_headers.update(self.page_headers)
                self.consent_headers = response_headers
            if url.path == "/connect" and method == "POST":
                self.posts.append(
                    {
                        "origin": headers.get("origin"),
                        "cookie": COOKIE + "=" in headers.get("cookie", ""),
                        "status": response.status_code,
                    }
                )
            if url.path == "/google/callback":
                self.callback_requests.append(
                    {
                        "cookie": COOKIE + "=" in headers.get("cookie", ""),
                        "status": response.status_code,
                    }
                )
            return response.status_code, response_headers, response.content
        elif origin == GOOGLE and url.path == "/o/oauth2/v2/auth":
            self.google_requests.append({"query": parse_qs(url.query), "headers": headers})
            state = self.callback_state or parse_qs(url.query)["state"][0]
            callback = (
                ISSUER + "/google/callback?" + urlencode({"state": state, "code": "synthetic-code"})
            )
            return (
                200,
                {"Content-Type": "text/html", "Referrer-Policy": "no-referrer"},
                (
                    f'<a href="{html.escape(callback, quote=True)}">Complete synthetic Google consent</a>'
                ).encode(),
            )
        elif origin == "https://client.example" and url.path == "/callback":
            self.client_requests.append(parse_qs(url.query))
            return 200, {"Content-Type": "text/html"}, b"<h1>Synthetic client callback</h1>"
        elif origin == FOREIGN and url.path == "/attack":
            # Same site, different origin: the Lax cookie remains present, so
            # this checks the Origin gate independently from missing cookies.
            action = html.escape(self.connect_url, quote=True)
            return (
                200,
                {"Content-Type": "text/html"},
                (
                    f'<form action="{action}" method="post"><input name="packages" value="com.example.synthetic"><input name="services" value="publisher"><button>Submit foreign form</button></form>'
                ).encode(),
            )
        elif url.path == "/favicon.ico":
            return 204, {}, b""
        self.unexpected_requests.append(origin + url.path)
        return 502, {}, b"Unexpected synthetic destination"


class LocalTLSProxy(BaseHTTPRequestHandler):
    """A closed HTTPS browser network: CONNECT never opens an upstream socket.

    Real TLS responses are necessary: browser route.fulfill(303) can bypass the
    next routing callback, and WebKit does not reliably retain fulfilled cookies.
    All hosts terminate here and then use ASGI or fixed synthetic responses.
    """

    def log_message(self, *_args):
        pass

    def do_CONNECT(self):
        allowed = {urlsplit(url).netloc for url in (ISSUER, GOOGLE, CLIENT, FOREIGN)}
        if self.path not in {host + ":443" for host in allowed}:
            self.server.bridge.unexpected_requests.append(self.path)
            self.send_error(502)
            return
        self.tunnel_host = self.path.removesuffix(":443")
        self.send_response(200, "Connection established")
        self.end_headers()
        try:
            self.connection = self.server.tls.wrap_socket(self.connection, server_side=True)
        except ssl.SSLError:
            # Browsers may probe then retry a generated test certificate.
            self.close_connection = True
            return
        self.rfile = self.connection.makefile("rb")
        self.wfile = self.connection.makefile("wb", buffering=0)
        self.handle_one_request()
        self.close_connection = True

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        headers = {key.lower(): value for key, value in self.headers.items()}
        if headers.get("host") != self.tunnel_host:
            self.send_error(400)
            return
        length = int(headers.get("content-length", "0"))
        if length > 256 * 1024:
            self.send_error(413)
            return
        status, response_headers, body = self.server.bridge.exchange(
            self.command,
            "https://" + self.tunnel_host + self.path,
            headers,
            self.rfile.read(length),
        )
        self.send_response(status)
        for key, value in response_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True


def synthetic_tls(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic browser network")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(urlsplit(url).hostname) for url in (ISSUER, GOOGLE, CLIENT, FOREIGN)]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "synthetic-key.pem", tmp_path / "synthetic-cert.pem"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.set_alpn_protocols(["http/1.1"])
    tls.load_cert_chain(cert_path, key_path)
    return tls


@pytest.fixture
def bridge(browser, tmp_path, synthetic_google_identity):
    tmp_path.chmod(0o700)
    settings = HostedSettings(
        ISSUER,
        "synthetic-browser-client",
        "synthetic-client-secret",
        tmp_path / "vault.db",
        Fernet.generate_key(),
        allowed_emails=frozenset({"owner@example.test"}),
        new_enrollment_enabled=True,
    )
    with patch("pubship.server.Auth", side_effect=AssertionError("Local auth forbidden")):
        app = create_hosted_app(settings)
    fetch = AsyncMock(side_effect=AssertionError("Real Google exchange forbidden"))
    with (
        TestClient(app, base_url=ISSUER, follow_redirects=False) as client,
        patch("pubship.hosted.AsyncOAuth2Client.fetch_token", new=fetch),
    ):
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), LocalTLSProxy)
        proxy.tls = synthetic_tls(tmp_path)
        context = browser.new_context(
            service_workers="block",
            # Only this closed loopback proxy uses a generated test certificate.
            # No production TLS connection or live provider is involved.
            ignore_https_errors=True,
            proxy={"server": f"http://127.0.0.1:{proxy.server_port}"},
        )
        fixture = BrowserBridge(client, context)
        proxy.bridge = fixture
        thread = threading.Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            yield fixture, fetch
            assert not fixture.unexpected_requests
        finally:
            context.close()
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=3)
            app.state.vault.close()


def continue_to_google(bridge):
    fixture, _ = bridge
    page = fixture.begin()
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(
        page.get_by_role("link", name="Complete synthetic Google consent")
    ).to_be_visible()
    assert fixture.posts == [{"origin": ISSUER, "cookie": True, "status": 303}]
    assert len(fixture.google_requests) == 1
    assert "referer" not in fixture.google_requests[0]["headers"]
    assert fixture.google_requests[0]["query"]["code_challenge_method"] == ["S256"]
    requested_scopes = set(fixture.google_requests[0]["query"]["scope"][0].split())
    assert requested_scopes == {SCOPES["publisher"], SCOPES["reporting"], "openid", "email"}
    assert fixture.google_requests[0]["query"]["nonce"][0]
    return page


def test_native_form_origin_cookie_and_google_redirect(bridge):
    fixture, fetch = bridge
    continue_to_google(bridge)
    directives = {
        parts[0]: parts[1:]
        for item in fixture.consent_headers["content-security-policy"].split(";")
        if (parts := item.strip().split())
    }
    assert directives["form-action"] == ["'self'", GOOGLE]
    assert directives["default-src"] == ["'none'"]
    assert directives["base-uri"] == ["'none'"]
    assert directives["frame-ancestors"] == ["'none'"]
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "selected",
    [
        (),
        ("play:edit-listings",),
        ("play:edit-listings", "play:publisher-metadata"),
        ("play:edit-resources",),
        ("play:commerce-query",),
        ("play:subscription-catalog",),
        ("play:onetime-catalog",),
        ("play:legacy-product-catalog",),
        *(
            (capability,)
            for capability in (
                "play:review-replies",
                "play:app-declarations",
                "play:app-operations",
                "play:tester-audience",
                "play:customer-data",
                "play:purchase-actions",
                "play:external-transactions",
                "play:subscriber-price-migration",
            )
        ),
        tuple(CAPABILITIES),
    ],
    ids=[
        "decline-all",
        "listing-only",
        "listing-and-metadata",
        "resource-only",
        "query-only",
        "subscriptions-only",
        "onetime-only",
        "legacy-only",
        "review-replies-only",
        "declarations-only",
        "app-operations-only",
        "testers-only",
        "customers-only",
        "purchases-only",
        "external-only",
        "migrations-only",
        "all-explicit",
    ],
)
def test_native_capability_form_grants_only_selected_exact_packages(bridge, selected):
    fixture, fetch = bridge
    page = fixture.begin(ALL_SCOPES)
    packages = ["com.example.synthetic", "com.example.other"]
    page.get_by_label("Android package names", exact=True).fill(", ".join(packages))
    playwright.expect(page.locator("input[name='capabilities']")).to_have_count(27)
    expected = {READ_SCOPE: sorted(packages)}
    expected_stores, expected_events = {}, []
    for capability, label in CAPABILITIES.items():
        checkbox = page.get_by_role("checkbox", name=label, exact=True)
        playwright.expect(checkbox).not_to_be_checked()
        row = page.locator(".capability-choice").filter(has=checkbox)
        apps = row.locator("input[type='text']")
        playwright.expect(apps).to_have_value("")
        if capability in selected:
            checkbox.check()
            package = packages[1] if capability == "play:publisher-metadata" else packages[0]
            if capability == APPSTORE_EVENTS:
                apps.fill("com.example.store")
                expected_events = ["com.example.store"]
            elif capability in APPSTORE_SCOPES:
                apps.fill("com.example.store/" + package)
                expected_stores[capability] = {"com.example.store": [package]}
            else:
                apps.fill(package)
                expected[capability] = [package]

    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(
        page.get_by_role("link", name="Complete synthetic Google consent")
    ).to_be_visible()
    assert fixture.posts == [{"origin": ISSUER, "cookie": True, "status": 303}]
    assert len(fixture.google_requests) == 1
    state = fixture.google_requests[0]["query"]["state"][0]
    pending = fixture.client.app.state.vault.get("google_state", state)
    assert pending["capability_packages"] == expected
    assert pending["capability_version"] == 3
    assert pending["appstore_targets"] == expected_stores
    assert pending["appstore_event_stores"] == expected_events
    assert pending["packages"] == sorted(packages)
    assert not fixture.callback_requests and not fixture.client_requests
    fetch.assert_not_called()


@pytest.mark.parametrize("apps", ["", "com.example.outside"], ids=["blank", "outside-connection"])
def test_native_capability_form_rejects_invalid_selected_packages(bridge, apps):
    fixture, fetch = bridge
    page = fixture.begin(ALL_SCOPES)
    checkbox = page.get_by_role("checkbox", name=CAPABILITIES["play:edit-listings"], exact=True)
    checkbox.check()
    row = page.locator(".capability-choice").filter(has=checkbox)
    row.get_by_label("Apps for this capability", exact=True).fill(apps)
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Invalid consent choices")).to_be_visible()
    assert fixture.posts == [{"origin": ISSUER, "cookie": True, "status": 400}]
    assert not fixture.google_requests and not fixture.callback_requests
    fetch.assert_not_called()


def test_all_capability_fields_fit_mobile_and_have_native_labels(bridge):
    fixture, fetch = bridge
    page = fixture.begin(ALL_SCOPES)
    playwright.expect(page.get_by_label("Apps for this capability", exact=True)).to_have_count(23)
    for width in (320, 380, 481, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        for capability, label in CAPABILITIES.items():
            checkbox = page.get_by_role("checkbox", name=label, exact=True)
            playwright.expect(checkbox).to_be_visible()
            row = page.locator(".capability-choice").filter(has=checkbox)
            apps = row.locator("input[type='text']")
            playwright.expect(apps).to_be_visible()
            box = apps.bounding_box()
            assert 0 <= box["x"] and box["x"] + box["width"] <= width, (width, capability)

    page.set_viewport_size({"width": 320, "height": 900})
    for label in CAPABILITIES.values():
        checkbox = page.get_by_role("checkbox", name=label, exact=True)
        checkbox.focus()
        page.keyboard.press("Space")
        playwright.expect(checkbox).to_be_checked()
        row = page.locator(".capability-choice").filter(has=checkbox)
        row.locator("label.capability-app-label").click()
        playwright.expect(row.locator("input[type='text']")).to_be_focused()
    assert not fixture.posts and not fixture.google_requests
    fetch.assert_not_called()


@pytest.mark.parametrize("failure", ["null_origin", "foreign_origin", "missing_cookie"])
def test_native_form_rejects_unverified_browser(bridge, failure):
    fixture, fetch = bridge
    if failure == "null_origin":
        fixture.page_headers["referrer-policy"] = "no-referrer"
    page = fixture.begin()
    if failure == "foreign_origin":
        page.goto(FOREIGN + "/attack")
        button = page.get_by_role("button", name="Submit foreign form")
    else:
        if failure == "missing_cookie":
            fixture.context.clear_cookies()
        button = page.get_by_role("button", name="Continue to Google", exact=True)
    button.click()
    playwright.expect(
        page.get_by_role("heading", name="Connection could not be verified")
    ).to_be_visible()
    expected_origin = {"null_origin": "null", "foreign_origin": FOREIGN, "missing_cookie": ISSUER}[
        failure
    ]
    assert fixture.posts == [
        {"origin": expected_origin, "cookie": failure != "missing_cookie", "status": 403}
    ]
    assert not fixture.google_requests
    fetch.assert_not_called()


def test_callback_lax_cookie_and_bound_state(bridge):
    fixture, fetch = bridge
    fetch.side_effect = None
    fetch.return_value = {
        "access_token": "synthetic-google-access",
        "refresh_token": "test-refresh",
        "scope": " ".join(SCOPES[s] for s in ("publisher", "reporting")),
        "expires_in": 3600,
        "token_type": "Bearer",
    }
    page = continue_to_google(bridge)
    page.get_by_role("link", name="Complete synthetic Google consent").click()
    playwright.expect(page.get_by_role("heading", name="Synthetic client callback")).to_be_visible()
    assert fixture.callback_requests == [{"cookie": True, "status": 303}]
    fetch.assert_awaited_once()
    assert fetch.call_args.kwargs["code"] == "synthetic-code"
    assert fetch.call_args.kwargs["code_verifier"]
    assert fixture.client_requests[0]["state"] == ["synthetic-client-state"]
    assert fixture.client_requests[0]["iss"] == [ISSUER]
    assert fixture.client_requests[0]["code"]
    assert "synthetic-google" not in page.url
    assert not any(cookie["name"] == COOKIE for cookie in fixture.context.cookies(ISSUER))


@pytest.mark.parametrize("failure", ["missing_cookie", "unknown_state"])
def test_callback_rejects_unbound_browser_without_exchange(bridge, failure):
    fixture, fetch = bridge
    if failure == "unknown_state":
        fixture.callback_state = "x" * 43
    page = continue_to_google(bridge)
    if failure == "missing_cookie":
        fixture.context.clear_cookies()
    page.get_by_role("link", name="Complete synthetic Google consent").click()
    playwright.expect(
        page.get_by_role("heading", name="Connection could not be verified")
    ).to_be_visible()
    assert fixture.callback_requests == [{"cookie": failure != "missing_cookie", "status": 400}]
    assert not fixture.client_requests
    fetch.assert_not_called()


def test_consent_layout_keyboard_focus_and_reduced_motion(bridge):
    fixture, _ = bridge
    page = fixture.begin()
    for width in (320, 380, 481, 520, 768, 1440):
        page.set_viewport_size({"width": width, "height": 900})
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), width
        playwright.expect(
            page.get_by_role("button", name="Continue to Google", exact=True)
        ).to_be_visible()
    page.reload()
    # macOS WebKit's native preference tabs through controls by default;
    # Option-Tab includes links, matching Safari's keyboard navigation.
    full_tab = (
        "Alt+Tab"
        if fixture.context.browser.browser_type.name == "webkit" and sys.platform == "darwin"
        else "Tab"
    )
    page.keyboard.press(full_tab)
    skip = page.get_by_role("link", name="Skip to connection", exact=True)
    playwright.expect(skip).to_be_focused()
    assert skip.bounding_box()["y"] >= 0
    page.keyboard.press("Enter")
    playwright.expect(page.get_by_role("main")).to_be_focused()
    page.keyboard.press("Tab")
    packages = page.get_by_label("Android package names", exact=True)
    playwright.expect(packages).to_be_focused()
    assert packages.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
    assert packages.evaluate("el => parseFloat(getComputedStyle(el).outlineWidth)") >= 3
    page.emulate_media(reduced_motion="reduce")
    button = page.get_by_role("button", name="Continue to Google", exact=True)
    assert button.evaluate("el => getComputedStyle(el).transitionDuration") == "0s"


@pytest.mark.parametrize("capability", sorted(APPSTORE_SCOPES))
def test_native_store_only_selection_does_not_need_base_apps(bridge, capability):
    fixture, fetch = bridge
    page = fixture.begin([READ_SCOPE, capability])
    page.get_by_label("Android package names", exact=True).fill("")
    checkbox = page.get_by_role("checkbox", name=CAPABILITIES[capability], exact=True)
    playwright.expect(checkbox).not_to_be_checked()
    checkbox.check()
    row = page.locator(".capability-choice").filter(has=checkbox)
    entry = (
        "com.example.store"
        if capability == APPSTORE_EVENTS
        else "com.example.store/com.example.app"
    )
    row.locator("input[type='text']").fill(entry)
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(
        page.get_by_role("link", name="Complete synthetic Google consent")
    ).to_be_visible()
    state = fixture.google_requests[0]["query"]["state"][0]
    pending = fixture.client.app.state.vault.get("google_state", state)
    assert pending["capability_version"] == 3
    assert pending["packages"] == []
    assert pending["capability_packages"] == {READ_SCOPE: []}
    if capability == APPSTORE_EVENTS:
        assert pending["appstore_event_stores"] == ["com.example.store"]
        assert pending["appstore_targets"] == {}
    else:
        assert pending["appstore_targets"] == {
            capability: {"com.example.store": ["com.example.app"]}
        }
        assert pending["appstore_event_stores"] == []
    fetch.assert_not_called()
