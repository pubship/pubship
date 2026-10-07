"""ADC or keyless gcloud impersonation. Access tokens live only in memory."""

import subprocess
import threading
import time
from datetime import UTC, datetime

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import Request

from .config import Settings
from .errors import PlayError


class BoundedRequest(Request):
    """Apply the same network timeout to credential refresh as provider reads."""

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = 15
        return super().__call__(*args, **kwargs)


SCOPES = {
    "publisher": "https://www.googleapis.com/auth/androidpublisher",
    "storage": "https://www.googleapis.com/auth/devstorage.read_only",
    "reporting": "https://www.googleapis.com/auth/playdeveloperreporting",
}


def validate_token(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16384
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
    ):
        raise PlayError("Authentication returned an invalid token.")
    return value


class Auth:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = {}
        self.lock = threading.Lock()

    def token(self, service):
        scope = SCOPES[service]
        with self.lock:
            cached = self.cache.get(service)
            if cached and time.monotonic() < cached[0]:
                return cached[1]
            if self.settings.auth_mode == "gcloud":
                command = [
                    self.settings.gcloud,
                    "auth",
                    "print-access-token",
                    "--impersonate-service-account=" + self.settings.impersonate,
                    "--scopes=" + scope,
                    "--quiet",
                ]
                if self.settings.account:
                    command.append("--account=" + self.settings.account)
                try:
                    result = subprocess.run(
                        command, capture_output=True, text=True, timeout=25, check=False
                    )
                except (OSError, subprocess.TimeoutExpired):
                    raise PlayError(
                        "gcloud unavailable or timed out; check GOOGLE_PLAY_GCLOUD and your login."
                    ) from None
                if result.returncode:
                    raise PlayError(
                        "Google authentication failed. Renew gcloud login and check Service Account Token Creator access."
                    )
                token = validate_token(result.stdout.strip())
                lifetime = 300  # Impersonated gcloud credentials have a one-hour lifetime.
            else:
                try:
                    source_scopes = (
                        ["https://www.googleapis.com/auth/cloud-platform"]
                        if self.settings.impersonate
                        else [scope]
                    )
                    credentials, _ = google.auth.default(scopes=source_scopes)
                    if self.settings.impersonate:
                        credentials = impersonated_credentials.Credentials(
                            source_credentials=credentials,
                            target_principal=self.settings.impersonate,
                            target_scopes=[scope],
                            lifetime=900,
                        )
                    credentials.refresh(BoundedRequest())
                    token = validate_token(credentials.token)
                    # ADC expiry is provider-dependent. Do not cache an unknown lifetime.
                    expiry = credentials.expiry
                    if expiry:
                        expiry = (
                            expiry.replace(tzinfo=UTC)
                            if expiry.tzinfo is None
                            else expiry.astimezone(UTC)
                        )
                    remaining = (expiry - datetime.now(UTC)).total_seconds() if expiry else 0
                    lifetime = max(0, min(300, remaining - 60))
                except PlayError:
                    raise
                except Exception:
                    raise PlayError(
                        "ADC authentication failed. Check Application Default Credentials, requested scopes and impersonation permissions."
                    ) from None
            self.cache[service] = (time.monotonic() + lifetime, token)
            return token
