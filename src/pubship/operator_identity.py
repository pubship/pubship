"""Verify operator identity using Google's signed ID token, never a form email."""

import secrets
import urllib.request

from google.oauth2 import id_token

from .errors import PlayError
from .hosted_token_transport import MAX_RESPONSE_BYTES, TIMEOUT_SECONDS, _Response
from .http import NoRedirect
from .provider_transport import bounded_opener

CERTIFICATES_URL = "https://www.googleapis.com/oauth2/v1/certs"
IDENTITY_ERROR = "Google account identity is unverified or not allowed by this operator."


class GoogleCertificateRequest:
    """Fixed public certificate fetch; no credentials, proxy, redirects or retries."""

    def __call__(self, url, method="GET", **kwargs):
        if url != CERTIFICATES_URL or method != "GET" or kwargs:
            raise PlayError(IDENTITY_ERROR)
        try:
            opener = bounded_opener(
                NoRedirect(),
                timeout=TIMEOUT_SECONDS,
                proxy_handler=urllib.request.ProxyHandler({}),
            )
            with opener.open(
                urllib.request.Request(url, method="GET"), timeout=TIMEOUT_SECONDS
            ) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if response.status != 200 or len(data) > MAX_RESPONSE_BYTES:
                    raise PlayError(IDENTITY_ERROR)
                return _Response(response.status, data, dict(response.headers))
        except Exception:
            raise PlayError(IDENTITY_ERROR) from None


def verify_google_identity(settings, pending, token):
    """Signature, issuer, audience, lifetime, nonce and verified exact-email gate."""
    try:
        raw = token.get("id_token")
        nonce = pending.get("identity_nonce")
        if not isinstance(raw, str) or not 1 <= len(raw) <= 16384:
            raise ValueError()
        if not isinstance(nonce, str) or not 32 <= len(nonce) <= 128:
            raise ValueError()
        claims = id_token.verify_oauth2_token(
            raw,
            GoogleCertificateRequest(),
            audience=settings.google_client_id,
        )
        email, subject = claims.get("email"), claims.get("sub")
        claimed_nonce = claims.get("nonce")
        if (
            claims.get("email_verified") is not True
            or not isinstance(email, str)
            or email not in settings.allowed_emails
            or not isinstance(subject, str)
            or not 1 <= len(subject) <= 255
            or not isinstance(claimed_nonce, str)
            or not secrets.compare_digest(nonce, claimed_nonce)
        ):
            raise ValueError()
        return email, subject
    except Exception:
        # Neither tokens, rejected emails nor provider errors reach the client/log.
        raise PlayError(IDENTITY_ERROR) from None


def require_operator_grant(settings, grant):
    """Invalidate legacy grants and grants removed from the operator allowlist."""
    if (
        not isinstance(grant, dict)
        or not isinstance(grant.get("verified_email"), str)
        or grant["verified_email"] not in settings.allowed_emails
        or not isinstance(grant.get("google_subject"), str)
        or not 1 <= len(grant["google_subject"]) <= 255
    ):
        raise PlayError(IDENTITY_ERROR)
