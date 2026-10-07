"""Customer Google consent behind the SDK's MCP OAuth endpoints.

Opaque MCP tokens reference an encrypted connection. They are never Google
tokens, and no credential may be selected from a tool argument or local ADC.
"""

import math
import secrets
import threading
import time
import weakref
from datetime import UTC
from urllib.parse import urlencode, urlsplit

import anyio
from google.oauth2.credentials import Credentials
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizeError,
    OAuthAuthorizationServerProvider,
    OAuthClientInformationFull,
    OAuthToken,
    RefreshToken,
    RegistrationError,
    TokenError,
)

from .auth import SCOPES, validate_token
from .config import Settings
from .errors import PlayError
from .hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_GRANTS,
    ACCOUNT_METHOD_CAPABILITIES,
    ACCOUNT_USERS,
    ADMIN_FIELDS,
    APPSTORE_EVENTS,
    APPSTORE_PAIR_SCOPES,
    INVALID_GRANT,
    READ_SCOPE,
    SIGNING_METHOD_CAPABILITIES,
    ConnectionAuthorization,
    grant_authority,
    scope_set,
)
from .hosted_token_transport import HostedTokenRequest
from .operator_identity import require_operator_grant, verify_google_identity

GRANT_TTL = 30 * 86400
ACCESS_TTL = 900
ENROLLMENT_CLOSED = "New customer enrollment is currently closed."


class PlayOAuth(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, settings, vault):
        self.settings, self.vault = settings, vault
        self.new_enrollment_enabled = settings.new_enrollment_enabled
        # Single-instance deployment. A refresh cannot overwrite another refresh
        # or resurrect a grant deleted concurrently in this process.
        self._grant_locks = weakref.WeakValueDictionary()
        self._lock_guard = threading.Lock()
        self._invalidators = []

    @property
    def new_enrollment_enabled(self):
        return self._new_enrollment_enabled

    @new_enrollment_enabled.setter
    def new_enrollment_enabled(self, enabled):
        # One process-local switch is shared by HTTP and provider boundaries.
        # Deployment configuration supplies its initial value on every restart.
        if type(enabled) is not bool:
            raise PlayError("Hosted new enrollment must be explicitly enabled or disabled.")
        self._new_enrollment_enabled = enabled

    def grant_lock(self, grant_id):
        # A slow refresh for one customer must not block another customer.
        with self._lock_guard:
            lock = self._grant_locks.get(grant_id)
            if lock is None:
                lock = threading.RLock()
                self._grant_locks[grant_id] = lock
        # Return the native context manager: generator context managers mutate
        # exception tracebacks, which is incompatible with the SDK's frozen
        # TokenError dataclass.
        return lock

    def register_invalidator(self, callback):
        if not callable(callback):
            raise TypeError("Connection invalidator must be callable.")
        self._invalidators.append(callback)

    def disconnect(self, grant_id):
        with self.grant_lock(grant_id):
            self.vault.delete_connection(grant_id)
            failed = False
            for callback in self._invalidators:
                try:
                    callback(grant_id)
                except Exception:
                    # The grant stays revoked even if private memory cleanup fails.
                    failed = True
            if failed:
                raise PlayError("Connection revoked; preparation cleanup could not be completed.")

    async def get_client(self, client_id):
        data = self.vault.get("clients", client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info):
        if not self.new_enrollment_enabled:
            raise RegistrationError(
                error="invalid_client_metadata", error_description=ENROLLMENT_CLOSED
            )
        for redirect in client_info.redirect_uris:
            parsed = urlsplit(str(redirect))
            loopback = parsed.scheme == "http" and parsed.hostname in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            if (
                not (parsed.scheme == "https" or loopback)
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
            ):
                raise RegistrationError(
                    error="invalid_redirect_uri",
                    error_description="Use HTTPS or an HTTP loopback redirect, without credentials or fragments.",
                )
        self.vault.put(
            "clients", client_info.client_id, client_info.model_dump(mode="json"), 90 * 86400
        )

    async def authorize(self, client, params):
        if not self.new_enrollment_enabled:
            raise AuthorizeError(error="access_denied", error_description=ENROLLMENT_CLOSED)
        if params.resource != self.settings.resource:
            raise AuthorizeError(
                error="invalid_target",
                error_description="The MCP resource must match this service.",
            )
        try:
            scope_set(params.scopes or [READ_SCOPE])
        except PlayError:
            raise AuthorizeError(
                error="invalid_scope",
                error_description="Request supported capabilities including play:read.",
            ) from None
        request_id = secrets.token_urlsafe(32)
        self.vault.put(
            "consent",
            request_id,
            {
                "client_id": client.client_id,
                "client_name": client.client_name or "MCP client",
                "params": params.model_dump(mode="json"),
                "consent_expires_at": time.time() + 600,
            },
            600,
        )
        return self.settings.issuer + "/connect?" + urlencode({"request": request_id})

    def complete_google(self, pending, google_token):
        """Called only after cookie-bound state and upstream PKCE exchange succeed."""
        if not self.new_enrollment_enabled:
            raise PlayError(ENROLLMENT_CLOSED)
        email, google_subject = verify_google_identity(self.settings, pending, google_token)
        requested = set(pending["google_scopes"])
        granted = set(google_token.get("scope", "").split())
        if not requested <= granted:
            raise PlayError(
                "Google did not grant the selected permissions. Reconnect and choose the required services."
            )
        token = validate_token(google_token.get("access_token"))
        try:
            lifetime = min(3600, int(google_token["expires_in"]))
        except (KeyError, ValueError, TypeError, OverflowError):
            raise PlayError("Google returned an invalid token lifetime.") from None
        if lifetime < 60 or google_token.get("token_type", "").lower() != "bearer":
            raise PlayError("Google returned an unusable grant.")
        refresh = google_token.get("refresh_token")
        if refresh:
            refresh = validate_token(refresh)
        grant_id, code = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        ttl = GRANT_TTL if refresh else lifetime
        grant = {
            "verified_email": email,
            "google_subject": google_subject,
            "client_id": pending["client_id"],
            "packages": pending["packages"],
            "bucket": pending["bucket"],
            "google_scopes": sorted(requested),
            "access_token": token,
            "refresh_token": refresh,
            "google_expires": time.time() + lifetime,
            "expires": time.time() + ttl,
        }
        # Revalidate the consent ceiling after the browser/Google round trip.
        # Old in-flight read-only consent records remain usable without widening.
        params = pending["params"]
        selected = pending.get("capability_packages", {READ_SCOPE: pending["packages"]})
        version = pending.get("capability_version", 1)
        grant.update(capability_version=version, capability_packages=selected)
        if version in (2, 3):
            grant.update(
                appstore_targets=pending.get("appstore_targets"),
                appstore_event_stores=pending.get("appstore_event_stores"),
            )
        elif "appstore_targets" in pending or "appstore_event_stores" in pending:
            raise PlayError(INVALID_GRANT)
        if version == 3:
            grant.update({field: pending.get(field) for field in ADMIN_FIELDS})
        elif ADMIN_FIELDS & pending.keys():
            raise PlayError(INVALID_GRANT)
        require_operator_grant(self.settings, grant)
        authority = grant_authority(grant)
        if not authority.scopes <= scope_set(params.get("scopes") or [READ_SCOPE]):
            raise PlayError("Consent exceeds the client's requested capabilities.")
        authorization = AuthorizationCode(
            code=code,
            client_id=pending["client_id"],
            code_challenge=params["code_challenge"],
            scopes=sorted(authority.scopes),
            expires_at=time.time() + 120,
            redirect_uri=params["redirect_uri"],
            redirect_uri_provided_explicitly=params["redirect_uri_provided_explicitly"],
            resource=self.settings.resource,
            subject=grant_id,
        )
        self.vault.put_many(
            [
                ("grants", grant_id, grant, 120),
                ("codes", code, authorization.model_dump(mode="json"), 120),
            ]
        )
        # The redirect URI was validated by the SDK against the registered client.
        redirect = params["redirect_uri"]
        fields = {"code": code, "iss": self.settings.issuer}
        if params.get("state") is not None:
            fields["state"] = params["state"]
        return redirect + ("&" if "?" in redirect else "?") + urlencode(fields)

    async def load_authorization_code(self, client, authorization_code):
        data = self.vault.get("codes", authorization_code)
        if data and data["client_id"] == client.client_id:
            return AuthorizationCode.model_validate(data)
        return None

    def issue_tokens(self, grant_id, client_id, scopes):
        with self.grant_lock(grant_id):
            try:
                return self._issue_tokens(grant_id, client_id, scopes)
            except PlayError:
                self.disconnect(grant_id)
                raise TokenError(
                    error="invalid_grant",
                    error_description="Authorization could not be stored. Reconnect the Google account.",
                ) from None

    def _issue_tokens(self, grant_id, client_id, scopes):
        records, token = self._token_records(grant_id, client_id, scopes)
        self.vault.put_many(records)
        return token

    def _token_records(self, grant_id, client_id, scopes):
        grant = self.vault.get("grants", grant_id)
        if not isinstance(grant, dict) or grant.get("client_id") != client_id:
            raise TokenError(
                error="invalid_grant", error_description="Reconnect the Google account."
            )
        require_operator_grant(self.settings, grant)
        authority = grant_authority(grant)
        try:
            granted_scopes = scope_set(scopes)
        except PlayError:
            raise TokenError(error="invalid_scope") from None
        if not granted_scopes <= authority.scopes:
            raise TokenError(error="invalid_scope")
        scopes = sorted(granted_scopes)
        remaining = int(grant["expires"] - time.time())
        if remaining < 1:
            raise TokenError(error="invalid_grant")
        ttl = min(ACCESS_TTL, remaining)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        common = {
            "client_id": client_id,
            "subject": grant_id,
            "scopes": scopes,
            "resource": self.settings.resource,
        }
        access_record = AccessToken(token=access, expires_at=int(time.time()) + ttl, **common)
        refresh_record = RefreshToken(
            token=refresh, expires_at=int(time.time()) + remaining, **common
        )
        records = [
            ("grants", grant_id, grant, remaining),
            ("access", access, access_record.model_dump(mode="json"), ttl),
            ("refresh", refresh, refresh_record.model_dump(mode="json"), remaining),
        ]
        return records, OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=ttl,
            refresh_token=refresh,
            scope=" ".join(scopes),
        )

    async def exchange_authorization_code(self, client, authorization_code):
        data = self.vault.get("codes", authorization_code.code, consume=True)
        if not data or data["client_id"] != client.client_id:
            raise TokenError(
                error="invalid_grant",
                error_description="Authorization code expired or already used.",
            )
        return await anyio.to_thread.run_sync(
            self.issue_tokens, data["subject"], client.client_id, data["scopes"]
        )

    async def load_refresh_token(self, client, refresh_token):
        data = self.vault.get("refresh", refresh_token)
        if data and data["client_id"] == client.client_id:
            return RefreshToken.model_validate(data)
        used = self.vault.get("used_refresh", refresh_token)
        if used and used["client_id"] == client.client_id:
            # Reuse means this connection's refresh family is no longer trusted.
            await anyio.to_thread.run_sync(self.disconnect, used["subject"])
        return None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        return await anyio.to_thread.run_sync(
            self._exchange_refresh_token, client, refresh_token, scopes
        )

    def _exchange_refresh_token(self, client, refresh_token, scopes):
        try:
            requested = scope_set(scopes)
        except PlayError:
            raise TokenError(error="invalid_scope") from None
        with self.grant_lock(refresh_token.subject):
            try:
                original = self.vault.get("refresh", refresh_token.token)
                if not original:
                    used = self.vault.get("used_refresh", refresh_token.token)
                    if used and used["client_id"] == client.client_id:
                        self.disconnect(used["subject"])
                    raise TokenError(error="invalid_grant")
                if original.get("client_id") != client.client_id:
                    raise TokenError(error="invalid_grant")
                if not requested <= scope_set(original["scopes"]):
                    raise TokenError(error="invalid_scope")
                if original != refresh_token.model_dump(mode="json"):
                    raise TokenError(error="invalid_grant")
                records, result = self._token_records(
                    original["subject"], client.client_id, sorted(requested)
                )
                records.append(
                    (
                        "used_refresh",
                        refresh_token.token,
                        {"client_id": client.client_id, "subject": original["subject"]},
                        max(1, original["expires_at"] - time.time()),
                    )
                )
                if not self.vault.replace_consumed(
                    "refresh", refresh_token.token, original, records
                ):
                    raise TokenError(error="invalid_grant")
                return result
            except PlayError:
                # The transaction keeps the original credential/family intact.
                # Capacity or unavailable storage must not revoke a customer.
                raise TokenError(
                    error="invalid_request",
                    error_description="Authorization storage is unavailable or this connection is full. Try again later or reconnect this account.",
                ) from None
            except (KeyError, TypeError, ValueError):
                raise TokenError(error="invalid_grant") from None

    async def load_access_token(self, token):
        data = self.vault.get("access", token)
        if not data:
            return None
        try:
            principal = AccessToken.model_validate(data)
            await anyio.to_thread.run_sync(self._authenticate_principal, principal)
            return principal
        except (PlayError, ValueError, TypeError):
            return None

    async def revoke_token(self, token):
        if token.subject:
            await anyio.to_thread.run_sync(self.disconnect, token.subject)

    def authorize_current(self, principal, capability=None, package=None):
        """Validate the actual access record and current connection under its lock.

        Callers performing writes hold the same reentrant lock through dispatch;
        a fresh call immediately before dispatch also catches elapsed expiry.
        """
        if not isinstance(principal, AccessToken) or not principal.subject:
            raise PlayError(INVALID_GRANT)
        with self.grant_lock(principal.subject):
            return self._authorize_current_snapshot(principal, capability, package)

    def _authenticate_principal(self, principal):
        # Middleware must not queue behind a slow mutation: cancellation requests
        # need authentication before the SDK can signal the running tool. Tools
        # independently reauthorize under the connection lock before any effect.
        with self.vault.lock:
            return self._authorize_current_snapshot(principal)

    def _authorize_current_snapshot(self, principal, capability=None, package=None):
        try:
            current = self.vault.get("access", principal.token)
            grant = self.vault.get("grants", principal.subject)
            now = time.time()
            if (
                not current
                or current != principal.model_dump(mode="json")
                or principal.resource != self.settings.resource
                or type(principal.expires_at) not in (int, float)
                or not math.isfinite(principal.expires_at)
                or principal.expires_at <= now
                or not isinstance(grant, dict)
                or grant.get("client_id") != principal.client_id
            ):
                raise PlayError(INVALID_GRANT)
            require_operator_grant(self.settings, grant)
            authority = grant_authority(grant)
            scopes = scope_set(principal.scopes)
            if not scopes <= authority.scopes or grant["expires"] <= now:
                raise PlayError(INVALID_GRANT)
            effective = {
                name: values
                for name, values in authority.capability_packages.items()
                if name in scopes
            }
            if capability is not None and capability not in scopes:
                raise PlayError(
                    "This connection has not authorized the required capability. Reconnect to grant it."
                )
            if package is not None and package not in effective.get(capability or READ_SCOPE, ()):
                raise PlayError("App is outside this connection's authorized capability packages.")
            return ConnectionAuthorization(
                principal.subject,
                principal.client_id,
                effective,
                min(principal.expires_at, grant["expires"]),
                scopes,
                frozenset(
                    name for name, scope in SCOPES.items() if scope in grant["google_scopes"]
                ),
                bool(grant["bucket"]),
                {
                    name: stores
                    for name, stores in authority.appstore_targets.items()
                    if name in scopes
                },
                authority.appstore_event_stores if APPSTORE_EVENTS in scopes else frozenset(),
                authority.version,
                bool(getattr(getattr(self, "transfers", None), "enabled", False)),
                authority.account_directory_developers
                if ACCOUNT_DIRECTORY in scopes
                else frozenset(),
                authority.account_user_targets if ACCOUNT_USERS in scopes else {},
                authority.account_grant_targets if ACCOUNT_GRANTS in scopes else {},
                {
                    capability: pairs
                    for capability, pairs in authority.signing_targets.items()
                    if capability in scopes
                },
            )
        except (KeyError, TypeError, ValueError):
            raise PlayError(INVALID_GRANT) from None

    def authorize_appstore_current(self, principal, capability, store, target=None):
        authorization = self.authorize_current(principal, capability)
        if capability == APPSTORE_EVENTS:
            allowed = target is None and store in authorization.appstore_event_stores
        elif capability in APPSTORE_PAIR_SCOPES:
            allowed = target in authorization.appstore_targets.get(capability, {}).get(store, ())
        else:
            allowed = False
        if not allowed:
            raise PlayError(
                "Store or app is outside this connection's authorized capability targets."
            )
        return authorization

    def authorize_account_current(self, principal, method, developer, user=None, app=None):
        if (
            not isinstance(method, str)
            or not isinstance(developer, str)
            or (user is not None and not isinstance(user, str))
            or (app is not None and not isinstance(app, str))
        ):
            raise PlayError("Invalid account authorization identity.")
        capability = ACCOUNT_METHOD_CAPABILITIES.get(method)
        if capability is None:
            raise PlayError("Unsupported account authorization method.")
        authorization = self.authorize_current(principal, capability)
        if capability == ACCOUNT_DIRECTORY:
            allowed = (
                user is None
                and app is None
                and developer in authorization.account_directory_developers
            )
        elif capability == ACCOUNT_USERS:
            row = authorization.account_user_targets.get((developer, user))
            allowed = app is None and row is not None and method in row.methods
        else:
            row = authorization.account_grant_targets.get((developer, user, app))
            allowed = row is not None and method in row.methods
        if not allowed:
            raise PlayError(
                "Account method or exact target is outside this connection's authority."
            )
        return authorization

    def authorize_signing_current(self, principal, method, app, kms_version):
        if not all(isinstance(value, str) for value in (method, app, kms_version)):
            raise PlayError("Invalid signing authorization identity.")
        capability = SIGNING_METHOD_CAPABILITIES.get(method)
        if capability is None:
            raise PlayError("Unsupported signing authorization method.")
        authorization = self.authorize_current(principal, capability)
        if (app, kms_version) not in authorization.signing_targets.get(capability, ()):
            raise PlayError(
                "Signing method or exact app/key-version pair is outside this connection's authority."
            )
        return authorization

    def services(self, principal):
        # No fallback path exists here, even if the machine has ADC or gcloud.
        authorization = self.authorize_current(principal)
        with self.grant_lock(authorization.subject):
            # Disconnect may have occurred between the two lock acquisitions.
            authorization = self.authorize_current(principal)
            grant = self.vault.get("grants", authorization.subject)
            settings = Settings(
                packages=authorization.capability_packages[READ_SCOPE], bucket=grant["bucket"]
            )
            return settings, CustomerAuth(self, authorization.subject)


class CustomerAuth:
    """One verified connection, with no global/default Google credential cache."""

    def __init__(self, provider, grant_id):
        self.provider, self.grant_id = provider, grant_id

    def token(self, service):
        with self.provider.grant_lock(self.grant_id):
            grant = self.provider.vault.get("grants", self.grant_id)
            require_operator_grant(self.provider.settings, grant)
            if not grant or SCOPES[service] not in grant["google_scopes"]:
                raise PlayError(
                    "This customer has not authorized the required Google service. Reconnect with that service selected."
                )
            if grant["google_expires"] <= time.time() + 60:
                if not grant["refresh_token"]:
                    raise PlayError("Google authorization expired. Reconnect your account.")
                try:
                    credentials = Credentials(
                        token=None,
                        refresh_token=grant["refresh_token"],
                        token_uri="https://oauth2.googleapis.com/token",
                        client_id=self.provider.settings.google_client_id,
                        client_secret=self.provider.settings.google_client_secret,
                        scopes=grant["google_scopes"],
                    )
                    credentials.refresh(HostedTokenRequest())
                    grant["access_token"] = validate_token(credentials.token)
                    expiry = credentials.expiry
                    if expiry is None:
                        raise PlayError("Google did not supply an access-token expiry.")
                    expiry = (
                        expiry.replace(tzinfo=UTC)
                        if expiry.tzinfo is None
                        else expiry.astimezone(UTC)
                    )
                    grant["google_expires"] = min(time.time() + 3600, expiry.timestamp())
                    if credentials.refresh_token:
                        grant["refresh_token"] = validate_token(credentials.refresh_token)
                    self.provider.vault.put(
                        "grants", self.grant_id, grant, max(1, grant["expires"] - time.time())
                    )
                except Exception:
                    raise PlayError(
                        "Customer Google authorization could not be refreshed. Reconnect if it was revoked."
                    ) from None
            return validate_token(grant["access_token"])
