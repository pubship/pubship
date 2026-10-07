"""Hosted configuration has no local Google account or default credential path."""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import PlayError
from .vault import VaultLimits, private_file


@dataclass(frozen=True)
class TransferLimits:
    object_bytes: int = 1024**3
    connection_bytes: int = 2 * 1024**3
    total_bytes: int = 8 * 1024**3
    connection_handles: int = 4
    total_handles: int = 32
    connection_streams: int = 1
    total_streams: int = 4
    ttl_seconds: int = 600
    free_disk_bytes: int = 1024**3
    idle_timeout_seconds: int = 30
    stream_timeout_seconds: int = 600

    def __post_init__(self):
        if any(
            type(value) is not int or not 0 < value <= 2**63 - 1 for value in self.__dict__.values()
        ):
            raise PlayError("Hosted transfer limits must be positive bounded integers.")
        if (
            not self.object_bytes <= self.connection_bytes <= self.total_bytes
            or self.connection_handles > self.total_handles
            or self.connection_streams > self.total_streams
            or self.free_disk_bytes < 1024**3
            or self.ttl_seconds > 600
            or self.stream_timeout_seconds > self.ttl_seconds
            or self.idle_timeout_seconds > self.stream_timeout_seconds
        ):
            raise PlayError("Hosted transfer limits have inconsistent relationships.")

    @classmethod
    def from_env(cls):
        values = {}
        for name in cls.__dataclass_fields__:
            value = os.environ.get("PUBSHIP_SERVER_TRANSFER_" + name.upper())
            if value is not None:
                if not value.isascii() or not value.isdecimal() or len(value) > 19:
                    raise PlayError("Hosted transfer limits require positive decimal integers.")
                values[name] = int(value)
        return cls(**values)


def valid_operator_email(value):
    """Exact account identities only: no wildcards, aliases or domain matching."""
    return (
        isinstance(value, str)
        and len(value) <= 254
        and re.fullmatch(
            r"[A-Za-z0-9.!#$%&'+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}",
            value,
        )
        is not None
        and ".." not in value
    )


@dataclass(frozen=True)
class HostedSettings:
    issuer: str
    google_client_id: str
    google_client_secret: str = field(repr=False)
    database: Path
    encryption_key: bytes = field(repr=False)
    transfer_directory: Path | None = None
    transfer_limits: TransferLimits = field(default_factory=TransferLimits)
    new_enrollment_enabled: bool = False
    vault_limits: VaultLimits = field(default_factory=VaultLimits)
    allowed_emails: frozenset[str] = field(default_factory=frozenset, repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.allowed_emails, frozenset)
            or not self.allowed_emails
            or len(self.allowed_emails) > 100
            or any(not valid_operator_email(email) for email in self.allowed_emails)
        ):
            raise PlayError(
                "PUBSHIP_SERVER_ALLOWED_EMAILS requires exact operator Google account emails."
            )
        if type(self.new_enrollment_enabled) is not bool:
            raise PlayError("Hosted new enrollment must be explicitly enabled or disabled.")
        if self.transfer_directory is not None and (
            not isinstance(self.transfer_directory, Path)
            or not self.transfer_directory.is_absolute()
        ):
            raise PlayError("Hosted transfer storage requires an explicit absolute directory.")
        if not isinstance(self.transfer_limits, TransferLimits):
            raise PlayError("Hosted transfer limits are invalid.")
        if not isinstance(self.vault_limits, VaultLimits):
            raise PlayError("Vault limits are invalid.")
        parsed = urlsplit(self.issuer)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise PlayError(
                "Hosted issuer must be an HTTPS origin, without a path, query or credentials."
            )
        if not self.google_client_id or not self.google_client_secret:
            raise PlayError("Hosted Google OAuth client configuration is required.")
        object.__setattr__(self, "issuer", self.issuer.rstrip("/"))

    @property
    def resource(self):
        return self.issuer + "/mcp"

    @property
    def callback(self):
        return self.issuer + "/google/callback"

    @classmethod
    def from_env(cls):
        required = (
            "PUBSHIP_SERVER_ALLOWED_EMAILS",
            "PUBSHIP_SERVER_ISSUER",
            "PUBSHIP_SERVER_GOOGLE_CLIENT_FILE",
            "PUBSHIP_SERVER_VAULT_KEY_FILE",
            "PUBSHIP_SERVER_VAULT_DATABASE",
            "PUBSHIP_SERVER_TRANSFER_DIRECTORY",
        )
        if any(not os.environ.get(name) for name in required):
            raise PlayError(
                "Hosted setup requires PUBSHIP_SERVER_ALLOWED_EMAILS, PUBSHIP_SERVER_ISSUER, PUBSHIP_SERVER_GOOGLE_CLIENT_FILE, PUBSHIP_SERVER_VAULT_KEY_FILE, PUBSHIP_SERVER_VAULT_DATABASE and PUBSHIP_SERVER_TRANSFER_DIRECTORY. Local ADC and gcloud are never used."
            )
        enrollment = os.environ.get("PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED", "false")
        if enrollment not in {"true", "false"}:
            raise PlayError("PUBSHIP_SERVER_NEW_ENROLLMENT_ENABLED must be true or false.")
        try:
            client = json.loads(private_file(Path(os.environ["PUBSHIP_SERVER_GOOGLE_CLIENT_FILE"])))
            web = client["web"]
            settings = cls(
                os.environ["PUBSHIP_SERVER_ISSUER"],
                web["client_id"],
                web["client_secret"],
                Path(os.environ["PUBSHIP_SERVER_VAULT_DATABASE"]),
                private_file(Path(os.environ["PUBSHIP_SERVER_VAULT_KEY_FILE"])).strip(),
                allowed_emails=frozenset(
                    email.strip()
                    for email in os.environ["PUBSHIP_SERVER_ALLOWED_EMAILS"].split(",")
                ),
                new_enrollment_enabled=enrollment == "true",
                transfer_directory=Path(os.environ["PUBSHIP_SERVER_TRANSFER_DIRECTORY"]),
                transfer_limits=TransferLimits.from_env(),
                vault_limits=VaultLimits.from_env(),
            )
            if settings.callback not in web.get("redirect_uris", []):
                raise PlayError(
                    "The hosted callback must be listed in the Google web client's redirect_uris."
                )
            return settings
        except (ValueError, KeyError, TypeError):
            raise PlayError(
                "Hosted OAuth configuration must be a Google web-client JSON file."
            ) from None
