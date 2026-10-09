"""Hosted MCP entry point: customer OAuth only, never machine Play credentials."""

import argparse
import base64
import binascii
import hashlib
import html
import json
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import anyio
from authlib.integrations.httpx_client import AsyncOAuth2Client
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver.tools import Tool
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, RedirectResponse

from .account_access_config import permission_values
from .auth import SCOPES
from .config import PACKAGE
from .errors import PlayError
from .hosted_cancellation import DISCONNECTED, RequestCancellation
from .hosted_config import HostedSettings
from .hosted_edits import HostedEditRegistry
from .hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_GRANTS,
    ACCOUNT_USERS,
    ADMIN_SCOPES,
    ALL_SCOPES,
    APPSTORE_EVENTS,
    APPSTORE_SCOPES,
    CAPABILITIES,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
    consent_authority,
    scope_set,
)
from .hosted_pages import SECURE_HEADERS, page
from .hosted_tools import build_hosted_tools
from .hosted_transfer_http import TransferHTTP, is_transfer_route
from .hosted_transfers import HostedTransferStore
from .oauth import READ_SCOPE, PlayOAuth
from .server import create_server, safe_tool
from .tool_metadata import enrich_tool
from .vault import Vault

COOKIE = "__Host-pubship-consent"
GOOGLE_EXCHANGE_TIMEOUT = 15
BODY_IDLE_TIMEOUT = 10
BODY_TOTAL_TIMEOUT = 30
ENROLLMENT_PATHS = frozenset({"/register", "/authorize", "/connect", "/google/callback"})


# The no-script browser form deliberately offers three rows per family. The
# persisted authority parser owns validation and supports its separate quota.
_ADMIN_ROWS = 3


def _admin_fields(capability):
    """Names and labels for structured, exact authority. No JSON editor."""
    if capability == ACCOUNT_DIRECTORY:
        return {"directory": "Exact developer IDs (comma-separated)"}
    fields = {}
    for row in range(_ADMIN_ROWS):
        prefix = capability.removeprefix("play:") + f"[{row}]"
        if capability in {SIGNING_ENROLL, SIGNING_ROTATE}:
            fields.update(
                {prefix + "[app]": "App ID", prefix + "[kms_version]": "Full Cloud KMS key version"}
            )
        else:
            fields.update(
                {prefix + "[developer]": "Developer ID", prefix + "[user]": "Exact user email"}
            )
            if capability == ACCOUNT_GRANTS:
                fields[prefix + "[app]"] = "App ID"
            fields[prefix + "[methods]"] = "Allowed methods"
            if capability == ACCOUNT_USERS:
                fields[prefix + "[developer_permissions]"] = "Developer permission ceiling"
                fields[prefix + "[allow_expiration_change]"] = (
                    "Allow changes to existing access expiry"
                )
                for app in range(_ADMIN_ROWS):
                    fields[prefix + f"[affected_apps][{app}][app]"] = f"Affected app {app + 1} ID"
                    fields[prefix + f"[affected_apps][{app}][permissions]"] = (
                        f"Affected app {app + 1} permission ceiling"
                    )
            else:
                fields[prefix + "[app_permissions]"] = "App permission ceiling"
    return fields


def _admin_form(capability, label):
    explanations = {
        ACCOUNT_DIRECTORY: "Read one page of account-wide email addresses, access states and permissions for each exact developer ID. This personal data is returned to your MCP client.",
        ACCOUNT_USERS: "Change only the developer/user pairs and methods entered below. Developer permissions apply across the developer account. Administrative permissions can grant broad control; affected-app selections do not limit the recipient's independent Console or API access. Deletion removes all of the user's access. Expiry changes require the separate checkbox and coverage of every affected app. Creating a user chooses initial expiry. Preparing changes processes a bounded account directory scan and returns the target user's access snapshot, including existing permissions.",
        ACCOUNT_GRANTS: "Change only the exact developer/user/app triples and methods entered below. Preparing changes processes a bounded account directory scan and returns the target user's existing access snapshot. This does not enable the raw account directory tool.",
        SIGNING_ENROLL: "Enterprise self-hosted Cloud KMS enrollment only, not ordinary Google-managed Play App Signing. Each exact app/key-version pair is independent. Google must grant eligibility and required KMS access. Preparation does not validate provider eligibility. This API has no readback or undo; effects may be irreversible.",
        SIGNING_ROTATE: "Enterprise self-hosted Cloud KMS rotation only. Each exact app/key-version pair is independent. Rotation may be irreversible through this API. There is no provider baseline, readback or undo. Google validates key and certificate relationships; preparation is not provider validation.",
    }
    result = (
        "<div class='capability-choice'><label><input type='checkbox' name='capabilities' value='"
        + capability
        + "'> "
        + html.escape(label)
        + "</label><p>"
        + explanations[capability]
        + "</p><details><summary>Exact authority for "
        + html.escape(label)
        + "</summary>"
        "<p>All fields are independent. Leave unused rows blank. Up to three rows per family are available in this form. "
        "Blank permission ceilings allow no permissions, not unrestricted access. Requires Releases and reviews (Publisher).</p>"
    )
    for name, field_label in _admin_fields(capability).items():
        ident = "admin-" + re.sub(r"[^a-zA-Z0-9_-]", "-", name)
        match = re.search(r"\[(\d+)\]", name)
        row_label = f"Row {int(match[1]) + 1}: " if match else ""
        label_text = row_label + field_label
        if name.endswith("[allow_expiration_change]"):
            result += (
                f"<label><input type='checkbox' name='{name}' value='yes' id='{ident}'>"
                + label_text
                + "</label>"
            )
        else:
            max_length = "4000" if name == "directory" or "permissions]" in name else "1000"
            result += (
                f"<label class='capability-app-label' for='{ident}'>"
                + label_text
                + f"</label><input type='text' name='{name}' id='{ident}' maxlength='{max_length}' autocomplete='off' spellcheck='false' autocapitalize='none'>"
            )
            if name.endswith("[methods]"):
                family = "users" if capability == ACCOUNT_USERS else "grants"
                result += (
                    "<small>Comma-separated exact methods: "
                    + ", ".join(
                        f"androidpublisher.{family}.{action}"
                        for action in ("create", "patch", "delete")
                    )
                    + ". No methods are selected by default.</small>"
                )
            elif "permissions]" in name:
                developer = name.endswith("[developer_permissions]")
                values = permission_values(
                    "User" if developer else "Grant",
                    "developerAccountPermissions" if developer else "appLevelPermissions",
                )
                result += (
                    "<details><summary>Allowed permission names (comma-separated)</summary><p>"
                    + html.escape(", ".join(sorted(values)))
                    + "</p></details>"
                )
    return result + "</details></div>"


def _admin_choices(form, requested):
    """Convert rows without merging duplicates or widening permission ceilings."""
    result = {
        "account_directory_developers": [],
        "account_user_targets": [],
        "account_grant_targets": [],
        "signing_targets": {},
    }

    def csv(value):
        value = str(value).strip()
        if not value:
            return []
        entries = [part.strip() for part in value.split(",")]
        if any(not part for part in entries):
            raise PlayError("Empty entries are not allowed in authority fields.")
        return entries

    for capability in requested & ADMIN_SCOPES:
        if capability == ACCOUNT_DIRECTORY:
            result["account_directory_developers"] = csv(form.get("directory", ""))
            continue
        for row in range(_ADMIN_ROWS):
            prefix = capability.removeprefix("play:") + f"[{row}]"
            fields = {
                name: str(form.get(name, "")).strip()
                for name in _admin_fields(capability)
                if name.startswith(prefix)
            }
            if not any(fields.values()):
                continue

            def value(key, fields=fields, prefix=prefix):
                return fields.get(prefix + "[" + key + "]", "")

            if capability in {SIGNING_ENROLL, SIGNING_ROTATE}:
                result["signing_targets"].setdefault(capability, []).append(
                    {"app": value("app"), "kms_version": value("kms_version")}
                )
                continue
            target = {
                "developer": value("developer"),
                "user": value("user"),
                "methods": csv(value("methods")),
            }
            if capability == ACCOUNT_USERS:
                flag = value("allow_expiration_change")
                if flag not in {"", "yes"}:
                    raise PlayError("Invalid expiry selection.")
                affected = {}
                for app_row in range(_ADMIN_ROWS):
                    app = value(f"affected_apps][{app_row}][app")
                    permissions = csv(value(f"affected_apps][{app_row}][permissions"))
                    if not app and not permissions:
                        continue
                    if app in affected or not app:
                        raise PlayError("Affected apps must be distinct exact identities.")
                    affected[app] = permissions
                target.update(
                    developer_permissions=csv(value("developer_permissions")),
                    affected_apps=affected,
                    allow_expiration_change=flag == "yes",
                )
                result["account_user_targets"].append(target)
            else:
                target.update(app=value("app"), app_permissions=csv(value("app_permissions")))
                result["account_grant_targets"].append(target)
    return result


def _authority_review(value):
    """Readable, escaped, exact validated values, including empty ceilings."""
    if isinstance(value, dict):
        if not value:
            return "<span>None (empty ceiling)</span>"
        labels = {
            "services": "Google services",
            "packages": "Android packages",
            "bucket": "Bulk-report bucket",
            "developer": "Exact developer ID",
            "user": "Exact user email",
            "app": "Exact app ID",
            "methods": "Allowed methods",
            "account_directory_developers": "Account directory developer IDs",
            "account_user_targets": "User authority",
            "account_grant_targets": "App-grant authority",
            "signing_targets": "Signing authority",
            "developer_permissions": "Developer permission ceiling",
            "app_permissions": "App permission ceiling",
            "affected_apps": "Affected apps and their permission ceilings",
            "allow_expiration_change": "Changes to existing expiry allowed",
            "kms_version": "Exact Cloud KMS key version",
            "capability_packages": "Ordinary app authority",
            "appstore_targets": "Exact store/app authority",
            "appstore_event_stores": "Store-wide event authority",
        }
        return (
            "<ul style='padding-left:18px;margin-block:8px'>"
            + "".join(
                "<li><strong>"
                + html.escape(labels.get(str(key), str(key)))
                + ":</strong> "
                + _authority_review(item)
                + "</li>"
                for key, item in value.items()
            )
            + "</ul>"
        )
    if isinstance(value, list):
        return (
            "<ul style='padding-left:18px;margin-block:8px'>"
            + "".join("<li>" + _authority_review(item) + "</li>" for item in value)
            + "</ul>"
            if value
            else "<span>None (empty ceiling)</span>"
        )
    display = ("Yes" if value else "No") if isinstance(value, bool) else str(value) or "None"
    return "<span style='overflow-wrap:anywhere'>" + html.escape(display) + "</span>"


class RequestLimits:
    """Bound bodies and OAuth admission without trusting forwarded client IPs.

    Ingress needs its own distributed rate limit. This in-process limiter is a
    backstop for a single worker, not a replacement for network controls.
    """

    def __init__(self, app, resource, provider=None):
        self.app = app
        self.resource = resource
        self.provider = provider
        self.windows = {name: OrderedDict() for name in ("onboarding", "token", "revoke")}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope["path"].rstrip("/")
        if path in ENROLLMENT_PATHS and self.provider is not None:
            if not self.provider.new_enrollment_enabled:
                return await JSONResponse(
                    {"error": "enrollment_closed"}, status_code=403, headers=SECURE_HEADERS
                )(scope, receive, send)
        admission = "onboarding" if path in ENROLLMENT_PATHS else path.removeprefix("/")
        if admission in self.windows:
            # Separate tables also stop onboarding churn evicting token/revoke
            # counters. The socket peer is authoritative; forwarded IPs are not.
            windows = self.windows[admission]
            key = (scope.get("client") or ("unknown", 0))[0]
            now = time.monotonic()
            since, count = windows.pop(key, (now, 0))
            if now - since >= 60:
                since, count = now, 0
            windows[key] = (since, count + 1)
            if len(windows) > 10000:
                windows.popitem(last=False)
            if count >= 120:
                return await JSONResponse(
                    {"error": "rate_limited"},
                    status_code=429,
                    headers={**SECURE_HEADERS, "Retry-After": "60"},
                )(scope, receive, send)
        if is_transfer_route(scope):
            # Exact byte routes authenticate themselves before consuming a body.
            return await self.app(scope, receive, send)
        body = bytearray()
        try:
            # Deadlines cover only request ingestion, never MCP execution or
            # response generation. Chunk progress resets idle, not total time.
            with anyio.fail_after(BODY_TOTAL_TIMEOUT):
                while True:
                    with anyio.fail_after(BODY_IDLE_TIMEOUT):
                        event = await receive()
                    if event["type"] == "http.disconnect":
                        return
                    body.extend(event.get("body", b""))
                    if len(body) > 256 * 1024 or not event.get("more_body", False):
                        break
        except TimeoutError:
            return await JSONResponse(
                {"error": "request_timeout"}, status_code=408, headers=SECURE_HEADERS
            )(scope, receive, send)
        if len(body) > 256 * 1024:
            return await JSONResponse(
                {"error": "request_too_large"}, status_code=413, headers=SECURE_HEADERS
            )(scope, receive, send)
        # MCP 2.3.0 parses but ignores token resource, and its revocation
        # schema incorrectly requires the optional client_secret field. Keep
        # compatibility handling at this bounded wire boundary; the SDK still
        # authenticates clients and validates grants/PKCE normally.
        if scope["method"] == "POST" and scope["path"] in {"/token", "/revoke"}:
            types = [v for k, v in scope.get("headers", []) if k.lower() == b"content-type"]
            authorizations = [
                v for k, v in scope.get("headers", []) if k.lower() == b"authorization"
            ]
            if len(types) != 1 or len(authorizations) > 1:
                return await JSONResponse(
                    {"error": "invalid_request"}, status_code=400, headers=SECURE_HEADERS
                )(scope, receive, send)
            content_type = types[0]
            if (
                content_type.split(b";", 1)[0].strip().lower()
                != b"application/x-www-form-urlencoded"
            ):
                return await JSONResponse(
                    {"error": "invalid_request"}, status_code=400, headers=SECURE_HEADERS
                )(scope, receive, send)
            try:
                form = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=64)
            except (UnicodeDecodeError, ValueError):
                return await JSONResponse(
                    {"error": "invalid_request"}, status_code=400, headers=SECURE_HEADERS
                )(scope, receive, send)
            if (
                scope["path"] == "/token"
                and "resource" in form
                and form["resource"] != [self.resource]
            ):
                return await JSONResponse(
                    {"error": "invalid_target"}, status_code=400, headers=SECURE_HEADERS
                )(scope, receive, send)
            changed = False
            if (
                "client_id" not in form
                and authorizations
                and authorizations[0].startswith(b"Basic ")
            ):
                # SDK 2.3 requires body client_id even for Basic. Derive only the
                # identifier; the SDK validates the original header and secret.
                try:
                    credentials = base64.b64decode(authorizations[0][6:], validate=True).decode(
                        "utf-8"
                    )
                    identifier, _ = credentials.split(":", 1)
                    form["client_id"] = [unquote(identifier)]
                    changed = True
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    return await JSONResponse(
                        {"error": "invalid_client"}, status_code=401, headers=SECURE_HEADERS
                    )(scope, receive, send)
            if scope["path"] == "/revoke" and "client_secret" not in form:
                form["client_secret"] = [""]
                changed = True
            if changed:
                body = bytearray(urlencode(form, doseq=True).encode())
                scope = dict(scope)
                scope["headers"] = [
                    (key, value)
                    for key, value in scope.get("headers", [])
                    if key.lower() != b"content-length"
                ] + [(b"content-length", str(len(body)).encode())]
        delivered = False

        async def bounded_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        if scope["method"] == "POST" and scope["path"].rstrip("/") == urlsplit(
            self.resource
        ).path.rstrip("/"):
            # The SDK's JSON response path does not watch disconnection. Keep a
            # single reader of the real ASGI channel after buffering the body;
            # SDK consumers receive the buffered body and the same disconnect.
            disconnected = anyio.Event()
            scope = dict(scope)
            scope[DISCONNECTED] = request_disconnected = threading.Event()
            response_complete = False

            async def mcp_send(event):
                nonlocal response_complete
                if event["type"] == "http.response.body" and not event.get("more_body", False):
                    response_complete = True
                await send(event)

            async def mcp_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}

            async with anyio.create_task_group() as group:

                async def watch_disconnect():
                    while (await receive())["type"] != "http.disconnect":
                        await anyio.lowlevel.checkpoint()
                    disconnected.set()
                    # ASGI also reports disconnect after a completed response.
                    # Legacy notification processing follows its 202 response;
                    # do not cancel that legitimate remaining server work.
                    if not response_complete:
                        request_disconnected.set()
                        group.cancel_scope.cancel()

                group.start_soon(watch_disconnect)
                try:
                    await self.app(scope, mcp_receive, mcp_send)
                finally:
                    group.cancel_scope.cancel()
        else:
            await self.app(scope, bounded_receive, send)


def create_hosted_app(settings, vault=None):
    # The SDK configures logging. HTTP client INFO logs can include callback
    # query strings when the app is embedded or tested, so suppress them here.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "authlib"):
        logging.getLogger(name).setLevel(logging.WARNING)
    vault = vault or Vault(settings.database, settings.encryption_key, limits=settings.vault_limits)
    provider = PlayOAuth(settings, vault)
    transfers = HostedTransferStore(settings.transfer_directory, settings.transfer_limits)
    provider.transfers = transfers
    provider.register_invalidator(transfers.invalidate)
    registry = HostedEditRegistry(provider, transfers=transfers)
    server = create_server(
        service_factory=lambda: provider.services(get_access_token()),
        hosted_tools=build_hosted_tools(provider, registry, get_access_token),
        hosted_catalog_context=lambda: provider.authorize_current(get_access_token()),
        middleware=[RequestCancellation(get_access_token)],
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=settings.issuer,
            resource_server_url=settings.resource,
            validate_token_resource=True,
            required_scopes=[READ_SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=sorted(ALL_SCOPES), default_scopes=[READ_SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    transfer_http = TransferHTTP(provider, transfers, settings.issuer)

    @server.custom_route("/transfers/{transfer_id}/content", methods=["GET", "PUT"])
    async def transfer_content(request):
        return await transfer_http.content(request)

    @safe_tool
    async def disconnect_google_account() -> dict[str, str]:
        """Disconnect this authenticated customer connection and delete its Google grant.
        Existing MCP access/refresh tokens for this connection stop working. Other
        connections are independent. Revoke the OAuth app in Google to remove all
        Google-side consent as well.
        """
        principal = get_access_token()
        await anyio.to_thread.run_sync(provider.services, principal)
        await provider.revoke_token(principal)
        return {"status": "disconnected"}

    disconnect_tool = Tool.from_function(
        disconnect_google_account,
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    enrich_tool(disconnect_tool, hosted=True)
    server.add_tool(
        disconnect_google_account,
        description=disconnect_tool.description,
        annotations=disconnect_tool.annotations,
    )

    @server.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"status": "ok"}, headers=SECURE_HEADERS)

    @server.custom_route("/connect", methods=["GET", "POST"])
    async def connect(request):
        if not provider.new_enrollment_enabled:
            return JSONResponse(
                {"error": "enrollment_closed"}, status_code=403, headers=SECURE_HEADERS
            )
        request_id = request.query_params.get("request", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", request_id):
            return page("<h1>Connection expired</h1><p>Start again from your MCP client.</p>", 400)
        pending = vault.get("consent", request_id)
        if not pending:
            return page("<h1>Connection expired</h1><p>Start again from your MCP client.</p>", 400)
        try:
            requested_capabilities = scope_set(pending["params"].get("scopes") or [READ_SCOPE])
        except (KeyError, PlayError):
            return page(
                "<h1>Invalid connection request</h1><p>Start again from your MCP client.</p>", 400
            )
        consent_expires = pending.get("consent_expires_at")
        if (consent_expires is None and requested_capabilities & ADMIN_SCOPES) or (
            consent_expires is not None and consent_expires <= time.time()
        ):
            return page("<h1>Connection expired</h1><p>Start again from your MCP client.</p>", 400)
        if request.method == "GET":
            capability_form = ""
            if requested_capabilities - {READ_SCOPE}:
                capability_form = (
                    "<fieldset><legend>Optional Publisher capabilities</legend>"
                    "<p>Each capability is off until you select it and enter its exact apps. "
                    "Ordinary app capabilities must use Android package names above. "
                    "Store capabilities use independent store/app pairs; a store-wide event grant covers all apps in that store's feed. "
                    "Listing and track changes are staged in edits. Commit and discard affect the entire edit, "
                    "including changes made elsewhere; commit success does not prove publication. "
                    "Commerce, support and purchase actions can affect live data without an edit commit. "
                    "Customer and tester reads send authorized provider data to this MCP client.</p>"
                )
                for capability, label in CAPABILITIES.items():
                    if capability in requested_capabilities:
                        field_id = "apps-" + capability.removeprefix("play:")
                        help_id = field_id + "-help"
                        if capability in ADMIN_SCOPES:
                            capability_form += _admin_form(capability, label)
                            continue
                        if capability in APPSTORE_SCOPES:
                            event = capability == APPSTORE_EVENTS
                            field_label = (
                                "Store package names" if event else "Exact store/app pairs"
                            )
                            example = (
                                "com.example.store"
                                if event
                                else "com.example.store/com.example.app"
                            )
                            explanation = (
                                "This permits the store-wide event feed, including apps outside any individual app selections."
                                if event
                                else "Use one store/app pair per entry. Each pair is independent; selecting two pairs does not authorize combinations between them."
                            )
                            capability_form += (
                                "<div class='capability-choice'><label><input type='checkbox' name='capabilities' value='"
                                + capability
                                + "' aria-describedby='"
                                + help_id
                                + "'> "
                                + html.escape(label)
                                + "</label><label class='capability-app-label' for='"
                                + field_id
                                + "'>"
                                + field_label
                                + "</label><input id='"
                                + field_id
                                + "' type='text' name='appstore_choices["
                                + capability
                                + "]' maxlength='52000' placeholder='"
                                + example
                                + "' autocomplete='off' spellcheck='false' autocapitalize='none' aria-describedby='"
                                + help_id
                                + "'><small id='"
                                + help_id
                                + "'>"
                                + explanation
                                + " Separate entries with commas. Requires Releases and reviews (Publisher) access. "
                                "These selections do not grant ordinary app reads and need not appear in Android package names above.</small></div>"
                            )
                            continue
                        capability_form += (
                            "<div class='capability-choice'><label><input type='checkbox' "
                            "name='capabilities' value='"
                            + capability
                            + "' aria-describedby='"
                            + help_id
                            + "'> "
                            + html.escape(label)
                            + "</label><label class='capability-app-label' for='"
                            + field_id
                            + "'>"
                            "Apps for this capability</label><input id='"
                            + field_id
                            + "' type='text' "
                            "name='capability_packages[" + capability + "]' maxlength='4000' "
                            "placeholder='com.example.app' autocomplete='off' spellcheck='false' "
                            "autocapitalize='none' aria-describedby='" + help_id + "'>"
                            "<small id='"
                            + help_id
                            + "'>When selected, enter exact app IDs from Android "
                            "package names above. Separate multiple apps with commas. Requires Releases "
                            "and reviews (Publisher) access.</small></div>"
                        )
                capability_form += "</fieldset>"
            csrf = secrets.token_urlsafe(32)
            with vault.lock:
                pending = vault.get("consent", request_id)
                if not pending:
                    return page(
                        "<h1>Connection expired</h1><p>Start again from your MCP client.</p>", 400
                    )
                pending["browser_hash"] = hashlib.sha256(csrf.encode()).hexdigest()
                pending.pop("authority_review_hash", None)
                vault.put(
                    "consent",
                    request_id,
                    pending,
                    max(0, consent_expires - time.time()) if consent_expires is not None else 600,
                )
            response = page(
                "<h1>Connect your Google Play account</h1>"
                "<p>Choose the apps and services for this connection. Your Google permissions still determine what is accessible.</p>"
                "<div class='request'><p><strong>"
                + html.escape(pending["client_name"])
                + "</strong> is asking to connect to Google Play through this independent, open-source MCP service.</p>"
                "<small>This client name is supplied by the requester and is not verified.</small></div>"
                "<p>Authorization will return to:<code class='destination'>"
                + html.escape(pending["params"]["redirect_uri"])
                + "</code><small>Continue only if you recognize and trust this destination.</small></p>"
                "<form method='post'><label for='packages'>Android package names</label>"
                "<input id='packages' type='text' name='packages' maxlength='4000' placeholder='com.example.app' autocomplete='off' spellcheck='false' autocapitalize='none' aria-describedby='packages-help'>"
                "<small id='packages-help'>Use the exact app IDs from Play Console. Separate multiple apps with commas. Leave blank only for a connection with explicit store, account or signing capabilities below.</small>"
                "<fieldset><legend>Services</legend><label><input type='checkbox' name='services' value='publisher' checked> Releases and reviews</label>"
                "<small>Google's Publisher permission permits reads and writes. Additional metadata, customer/tester reads and write operations require their separate capability consent below.</small>"
                "<label><input type='checkbox' name='services' value='reporting' checked> Android vitals and error reports</label>"
                "<label><input type='checkbox' name='services' value='storage'> Bulk reports</label></fieldset>"
                "<label for='bucket'>Bulk-report bucket <small>Only needed when Bulk reports is selected.</small></label>"
                "<input id='bucket' type='text' name='bucket' maxlength='222' autocomplete='off' spellcheck='false' autocapitalize='none' aria-describedby='bucket-help'>"
                "<small id='bucket-help'>Copy the exact bucket name from Play Console, without gs:// or a path.</small>"
                + capability_form
                + "<p class='privacy-note'>The service stores your Google grant encrypted until you disconnect or the connection expires, for at most 30 days. Tool results go to your MCP client. Prepared operations retain their request, observations and original credential in bounded process memory for at most ten minutes, sometimes less. Binary transfers use private temporary disk files for at most ten minutes or their shorter authorization lifetime. Restart loses preparations and transfers; cleanup does not guarantee secure erasure and removing them does not undo an accepted Google change. Authorized sharing links and delivery tokens go to your MCP client. Your MCP client may retain inputs and results, including any sensitive data you authorize. You can disconnect using its disconnect_google_account tool and revoke consent in your Google account.</p>"
                "<button type='submit'>Continue to Google</button></form>"
            )
            response.set_cookie(
                COOKIE, csrf, max_age=600, secure=True, httponly=True, samesite="lax", path="/"
            )
            return response
        cookie = request.cookies.get(COOKIE, "")
        if (
            request.headers.get("origin") != settings.issuer
            or not cookie
            or not secrets.compare_digest(
                pending.get("browser_hash", ""), hashlib.sha256(cookie.encode()).hexdigest()
            )
        ):
            return page(
                "<h1>Connection could not be verified</h1><p>Start again from your MCP client.</p>",
                403,
            )
        form = await request.form()
        allowed_fields = (
            {"packages", "services", "bucket", "capabilities", "authority_confirmation"}
            | {
                "capability_packages[" + capability + "]"
                for capability in requested_capabilities
                - {READ_SCOPE}
                - APPSTORE_SCOPES
                - ADMIN_SCOPES
            }
            | {
                "appstore_choices[" + capability + "]"
                for capability in requested_capabilities & APPSTORE_SCOPES
            }
        )
        allowed_fields |= {
            name for cap in requested_capabilities & ADMIN_SCOPES for name in _admin_fields(cap)
        }
        if not set(form) <= allowed_fields or any(
            len(form.getlist(name)) != 1
            for name in form
            if name not in {"services", "capabilities"}
        ):
            return page(
                "<h1>Invalid consent choices</h1><p>Use the requested capabilities and exact app fields.</p>",
                400,
            )
        packages = sorted(
            {p.strip() for p in str(form.get("packages", "")).split(",") if p.strip()}
        )
        services = set(form.getlist("services"))
        bucket = str(form.get("bucket", "")).strip()
        if (
            len(packages) > 100
            or any(not PACKAGE.fullmatch(p) for p in packages)
            or not services
            or not services <= SCOPES.keys()
            or (
                "storage" in services
                and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket)
            )
        ):
            return page(
                "<h1>Check connection settings</h1><p>Provide valid Android packages, at least one service, and the exact bucket name when selecting bulk reports. Go back to correct the form.</p>",
                400,
            )
        try:
            choices = consent_authority(
                requested_capabilities,
                form.getlist("capabilities"),
                {
                    capability: form["capability_packages[" + capability + "]"]
                    for capability in requested_capabilities
                    - {READ_SCOPE}
                    - APPSTORE_SCOPES
                    - ADMIN_SCOPES
                    if "capability_packages[" + capability + "]" in form
                },
                packages,
                services,
                {
                    capability: form["appstore_choices[" + capability + "]"]
                    for capability in requested_capabilities & APPSTORE_SCOPES
                    if "appstore_choices[" + capability + "]" in form
                },
                admin_fields=_admin_choices(form, requested_capabilities),
            )
        except PlayError:
            return page(
                "<h1>Invalid consent choices</h1><p>Choose only requested capabilities with Publisher access. Ordinary apps must be within the connection's packages; store capabilities require exact store/app pairs or store-wide selections. Account and signing rows require exact identities and selected methods. Blank permission ceilings permit no permissions. Go back to correct your entries.</p>",
                400,
            )
        needs_review = bool(
            set(form.getlist("capabilities")) & ADMIN_SCOPES or pending.get("authority_review_hash")
        )
        if needs_review:
            reviewed = {
                "services": sorted(services),
                "packages": packages,
                "bucket": bucket,
                **choices,
            }
            digest = hashlib.sha256(json.dumps(reviewed, sort_keys=True).encode()).hexdigest()
            if (
                form.get("authority_confirmation") != digest
                or pending.get("authority_review_hash") != digest
            ):
                # Re-read under the vault lock so a stale review cannot recreate
                # a record another request has already consumed.
                with vault.lock:
                    current = vault.get("consent", request_id)
                    if not current or current.get("browser_hash") != pending.get("browser_hash"):
                        return page(
                            "<h1>Connection already used</h1><p>Start again from your MCP client.</p>",
                            400,
                        )
                    current["authority_review_hash"] = digest
                    vault.put("consent", request_id, current, max(0, consent_expires - time.time()))
                hidden = "".join(
                    "<input type='hidden' name='"
                    + html.escape(name, quote=True)
                    + "' value='"
                    + html.escape(str(value), quote=True)
                    + "'>"
                    for name, value in form.multi_items()
                    if name != "authority_confirmation"
                )
                warnings = ""
                if choices["account_user_targets"]:
                    warnings += "<p>Developer permissions apply throughout the account. Administrative access may give the recipient broad control outside this service. User deletion removes access; expiry changes can extend or remove its lifetime.</p>"
                if choices["signing_targets"]:
                    warnings += "<p>Signing changes can be irreversible and have no API undo or readback. Google, not this preparation, validates eligibility and key relationships.</p>"
                return page(
                    "<h1>Review exact connection authority</h1><p>Confirm the exact entries below before continuing to Google. No rows are combined and no unlisted combinations are authorized.</p>"
                    + warnings
                    + _authority_review(
                        {
                            key: value
                            for key, value in reviewed.items()
                            if key != "capability_version" and (value or key == "packages")
                        }
                    )
                    + "<p>Account snapshots and KMS resource names go to your selected MCP client and may remain in its transcript. To change these choices, go back to the form and review again.</p>"
                    + "<form method='post'>"
                    + hidden
                    + "<input type='hidden' name='authority_confirmation' value='"
                    + digest
                    + "'><button type='submit'>Confirm and continue to Google</button></form>"
                )
        pending = vault.get("consent", request_id, consume=True)
        if not pending:
            return page(
                "<h1>Connection already used</h1><p>Start again from your MCP client.</p>", 400
            )
        if needs_review and pending.get("authority_review_hash") != digest:
            return page(
                "<h1>Connection choices changed</h1><p>Start again from your MCP client and review your choices.</p>",
                400,
            )
        state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        pending.update(
            packages=packages,
            bucket=bucket if "storage" in services else "",
            google_scopes=sorted(SCOPES[s] for s in services),
            **choices,
            verifier=verifier,
            identity_nonce=secrets.token_urlsafe(32),
        )
        async with AsyncOAuth2Client(
            settings.google_client_id,
            settings.google_client_secret,
            redirect_uri=settings.callback,
            scope=[*pending["google_scopes"], "openid", "email"],
            code_challenge_method="S256",
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            url, _ = client.create_authorization_url(
                "https://accounts.google.com/o/oauth2/v2/auth",
                state=state,
                code_verifier=verifier,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
                nonce=pending["identity_nonce"],
            )
        if not provider.new_enrollment_enabled:
            return JSONResponse(
                {"error": "enrollment_closed"}, status_code=403, headers=SECURE_HEADERS
            )
        vault.put("google_state", state, pending, 600)
        return RedirectResponse(url, status_code=303, headers=SECURE_HEADERS)

    @server.custom_route("/google/callback", methods=["GET"])
    async def callback(request):
        if not provider.new_enrollment_enabled:
            return JSONResponse(
                {"error": "enrollment_closed"}, status_code=403, headers=SECURE_HEADERS
            )
        state = request.query_params.get("state", "")
        pending = (
            vault.get("google_state", state)
            if re.fullmatch(r"[A-Za-z0-9_-]{40,64}", state)
            else None
        )
        cookie = request.cookies.get(COOKIE, "")
        if (
            not pending
            or not cookie
            or not secrets.compare_digest(
                pending.get("browser_hash", ""), hashlib.sha256(cookie.encode()).hexdigest()
            )
        ):
            return page(
                "<h1>Connection could not be verified</h1><p>Start again from your MCP client.</p>",
                400,
            )
        pending = vault.get("google_state", state, consume=True)
        if not pending or request.query_params.get("error") or not request.query_params.get("code"):
            return page(
                "<h1>Google connection not completed</h1><p>No connection was granted. Start again from your MCP client when ready.</p>",
                400,
            )
        try:
            with anyio.fail_after(GOOGLE_EXCHANGE_TIMEOUT):
                async with AsyncOAuth2Client(
                    settings.google_client_id,
                    settings.google_client_secret,
                    redirect_uri=settings.callback,
                    token_endpoint_auth_method="client_secret_post",
                    timeout=15,
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    token = await client.fetch_token(
                        "https://oauth2.googleapis.com/token",
                        code=request.query_params["code"],
                        code_verifier=pending["verifier"],
                    )
            redirect = await anyio.to_thread.run_sync(provider.complete_google, pending, token)
        except Exception:
            # OAuth responses can contain codes, tokens and provider-specific detail.
            return page(
                "<h1>Google connection failed</h1><p>The service could not verify the requested grant. Start again from your MCP client.</p>",
                400,
            )
        response = RedirectResponse(redirect, status_code=303, headers=SECURE_HEADERS)
        response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
        return response

    host = urlsplit(settings.issuer).netloc
    app = server.streamable_http_app(
        stateless_http=True,
        json_response=True,
        max_request_body_size=256 * 1024,
        host=host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[host],
            allowed_origins=[settings.issuer],
        ),
    )
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original_lifespan(application):
                yield
        finally:
            await anyio.to_thread.run_sync(registry.close)
            await anyio.to_thread.run_sync(transfers.close)

    app.router.lifespan_context = lifespan
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.issuer).hostname])
    app.add_middleware(RequestLimits, resource=settings.resource, provider=provider)
    # Explicitly expose non-secret handles for controlled shutdown/testing.
    app.state.vault = vault
    app.state.oauth = provider
    app.state.hosted_registry = registry
    app.state.hosted_transfers = transfers
    return app


def main():
    parser = argparse.ArgumentParser(
        description="Operator-only PubShip self-host HTTP service. Requires its own OAuth application and private vault."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    try:
        import uvicorn

        app = create_hosted_app(HostedSettings.from_env())
        # One worker: SQLite and in-process grant refresh/revocation are a single
        # trust/consistency boundary. No proxy headers or access/query logging.
        uvicorn.run(
            app,
            host=args.host,
            port=args.port,
            workers=1,
            access_log=False,
            proxy_headers=False,
            limit_concurrency=100,
            timeout_keep_alive=5,
        )
    except PlayError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
