"""Explicit local enterprise Cloud KMS signing, with reviewed one-shot intent."""

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from copy import deepcopy
from dataclasses import dataclass, field
from http.client import HTTPException
from urllib.parse import quote

from cryptography import x509
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.serialization import Encoding, load_der_private_key

from .artifact_files import media_deadline
from .auth import validate_token
from .config import PACKAGE
from .contracts import validate_request
from .errors import PlayError
from .http import NoRedirect, finite_float
from .provider_transport import bounded_opener
from .publishing import _APPLY_LOCK

PREFIX = "androidpublisher.appsigning."
METHODS = frozenset(PREFIX + action for action in ("enrollApp", "rotateAppSigningKey"))
KMS = re.compile(
    r"projects/[A-Za-z0-9][A-Za-z0-9._:-]{0,127}/locations/[a-z0-9-]{1,63}/keyRings/[A-Za-z0-9_-]{1,63}/cryptoKeys/[A-Za-z0-9_-]{1,63}/cryptoKeyVersions/[1-9][0-9]{0,18}"
)
APP_ID = re.compile(r"[1-9][0-9]{0,19}")
TTL = 600
MAX_PREPARATIONS = 20
NOTE = (
    "Enterprise self-hosted Cloud KMS only. Standard Google-managed signing uses Play Console. "
    "No provider baseline, atomic precondition, readback or rollback API exists here. "
    "Google verifies KMS permissions, key/certificate association, lineage and app eligibility. "
    "Provider acceptance does not verify propagation or successful installation. "
    "No automatic retry. Client transcripts may retain supplied public certificates and identifiers."
)
UNKNOWN = "Signing outcome unknown; operation ID consumed. Reconcile in Play Console before any new operation; do not automatically retry."


def _app(value):
    return isinstance(value, str) and bool(PACKAGE.fullmatch(value) or APP_ID.fullmatch(value))


@dataclass(frozen=True)
class SigningSettings:
    apps: frozenset[str] = frozenset()
    methods: frozenset[str] = frozenset()
    kms_versions: frozenset[str] = frozenset()

    def __post_init__(self):
        for values, valid in (
            (self.apps, _app),
            (self.methods, lambda v: v in METHODS),
            (self.kms_versions, lambda v: isinstance(v, str) and KMS.fullmatch(v)),
        ):
            if not isinstance(values, frozenset) or any(not valid(v) for v in values):
                raise PlayError(
                    "Signing configuration requires exact app IDs, supported methods and versioned KMS resources; no wildcards."
                )

    @classmethod
    def from_env(cls):
        return cls(
            **{
                key: frozenset(
                    v.strip()
                    for v in os.environ.get("GOOGLE_PLAY_SIGNING_" + key.upper(), "").split(",")
                    if v.strip()
                )
                for key in ("apps", "methods", "kms_versions")
            }
        )

    def require(self, spec):
        if (
            spec["app"] not in self.apps
            or spec["method"] not in self.methods
            or spec["kms"] not in self.kms_versions
        ):
            raise PlayError(
                "Signing requires independent exact SIGNING_APPS, SIGNING_METHODS and SIGNING_KMS_VERSIONS grants."
            )


def _bytes(value, limit):
    if not isinstance(value, str) or not value or len(value) > limit * 2:
        raise PlayError("Signing bytes must be bounded nonempty standard base64.")
    try:
        raw = base64.b64decode(value, validate=True)
        if not raw or len(raw) > limit or base64.b64encode(raw).decode() != value:
            raise ValueError()
        return raw
    except (ValueError, binascii.Error):
        raise PlayError("Signing bytes must be bounded canonical standard base64.") from None


def _certificate(value):
    raw = _bytes(value, 16384)
    # Accept exactly one public certificate; never accept PEM key material or bundles.
    if not re.fullmatch(
        rb"\s*-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\s*", raw
    ):
        raise PlayError(
            "Supply one base64-encoded PEM public certificate, never private key material."
        )
    try:
        der = x509.load_pem_x509_certificate(raw).public_bytes(Encoding.DER)
    except ValueError:
        raise PlayError("Invalid X.509 public certificate.") from None
    return {
        "certificateHash" + key: hashlib.new(name, der, usedforsecurity=False).hexdigest().upper()
        for key, name in (("Md5", "md5"), ("Sha1", "sha1"), ("Sha256", "sha256"))
    }


def request_spec(method, parameters, body):
    if not isinstance(method, str) or method not in METHODS:
        raise PlayError("Unsupported signing method.")
    validate_request(method, parameters, body)
    if set(parameters) != {"name"} or not _app(parameters["name"]):
        raise PlayError("Signing name must be an exact package or decimal app ID.")
    certificates = {}
    if method == PREFIX + "enrollApp":
        modes = set(body) & {"enrollNewApp", "enrollExistingApp"}
        if len(modes) != 1:
            raise PlayError("Choose exactly one signing enrollment mode.")
        mode = next(iter(modes))
        value = body[mode]
        if mode == "enrollNewApp":
            pair = value.get("cloudKmsKeyAndCert", {})
            key = pair.get("cloudKmsKey", {})
            certificates["signingCertificate"] = _certificate(pair.get("pemCertificate"))
        else:
            key = value.get("cloudKmsKey", {})
        if "pemUploadCertificate" in body:
            certificates["uploadCertificate"] = _certificate(body["pemUploadCertificate"])
    else:
        mode = "rotateAppSigningKey"
        if body.get("keyRotationReason") in (None, "KEY_ROTATION_REASON_UNSPECIFIED"):
            raise PlayError("Signing rotation requires an explicit reason.")
        value = body.get("rotatedCloudKmsKey", {})
        pair = value.get("cloudKmsKeyAndCert", {})
        key = pair.get("cloudKmsKey", {})
        certificates["rotatedKeyCertificate"] = _certificate(pair.get("pemCertificate"))
        lineage = _bytes(value.get("signingCertificateLineage"), 16384)
        if b"PRIVATE KEY" in lineage or b"-----BEGIN" in lineage:
            raise PlayError(
                "Lineage must be binary proof of rotation, never PEM or private key material."
            )
        try:
            load_der_private_key(lineage, password=None)
        except ValueError:
            pass  # Opaque provider-validated lineage, not a recognized DER key.
        except (TypeError, UnsupportedAlgorithm):
            raise PlayError("Private or encrypted key material is not a signing lineage.") from None
        else:
            raise PlayError("Private key material is not a signing lineage.")

    kms = key.get("cryptoKeyVersionResource")
    if not isinstance(kms, str) or not KMS.fullmatch(kms):
        raise PlayError("Signing requires an exact Cloud KMS cryptoKeyVersions resource.")
    return {
        "method": method,
        "app": parameters["name"],
        "kms": kms,
        "mode": mode,
        "certificates": certificates,
        "parameters": deepcopy(parameters),
        "body": deepcopy(body),
    }


def validate_response(spec, data):
    expected = (
        {"rotatedKeyCertificate"}
        if spec["mode"] == "rotateAppSigningKey"
        else {"signingCertificate"}
        | ({"uploadCertificate"} if "pemUploadCertificate" in spec["body"] else set())
    )
    if not isinstance(data, dict) or set(data) != expected:
        raise PlayError(UNKNOWN)
    normalized = {}
    for field_name in expected:
        value = data[field_name]
        if (
            not isinstance(value, dict)
            or "certificateHashSha256" not in value
            or not set(value)
            <= {"certificateHashMd5", "certificateHashSha1", "certificateHashSha256"}
        ):
            raise PlayError(UNKNOWN)
        normalized[field_name] = {}
        for algorithm, digest in value.items():
            size = {
                "certificateHashMd5": 16,
                "certificateHashSha1": 20,
                "certificateHashSha256": 32,
            }[algorithm]
            if not isinstance(digest, str) or not (
                re.fullmatch(r"[a-fA-F0-9]{" + str(size * 2) + "}", digest)
                or re.fullmatch(r"(?:[a-fA-F0-9]{2}:){" + str(size - 1) + "}[a-fA-F0-9]{2}", digest)
            ):
                raise PlayError(UNKNOWN)
            clean = digest.replace(":", "").upper()
            if (
                field_name in spec["certificates"]
                and clean != spec["certificates"][field_name][algorithm]
            ):
                raise PlayError(UNKNOWN)
            normalized[field_name][algorithm] = clean
    return normalized


def execute(spec, token, *, deadline=None, _execution_authorization=None):
    # Revalidate the full private intent before constructing a fixed URL.
    checked = request_spec(spec["method"], spec["parameters"], spec["body"])
    url = (
        "https://androidpublisher.googleapis.com/androidpublisher/v3/applications/"
        + quote(checked["app"], safe="")
        + "/appSigning:"
        + checked["method"].removeprefix(PREFIX)
    )
    request = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(checked["body"], allow_nan=False).encode(),
        headers={
            "Authorization": "Bearer " + validate_token(token),
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = bounded_opener(NoRedirect(), timeout=15, deadline=deadline)
    if _execution_authorization is not None:
        _execution_authorization()
    if deadline is not None and time.monotonic() >= deadline:
        raise PlayError("Signing preparation expired before dispatch.")
    try:
        with opener.open(request, timeout=15) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError()
        data = json.loads(raw, parse_float=finite_float, parse_constant=finite_float)
        return validate_response(checked, data)
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
        raise PlayError(f"Google API HTTP {status}. " + UNKNOWN) from None
    except (PlayError, OSError, ValueError, HTTPException, RecursionError):
        raise PlayError(UNKNOWN) from None


@dataclass(repr=False)
class Preparation:
    spec: dict
    token: str = field(repr=False)
    deadline: float


class Signing:
    def __init__(
        self,
        settings,
        auth,
        *,
        local=False,
        _execution_authorization=None,
        _apply_guard=None,
        _max_snapshot_bytes=None,
        _target_authorization=None,
    ):
        self.settings, self.auth, self.local = settings, auth, local
        if _max_snapshot_bytes is not None and (
            type(_max_snapshot_bytes) is not int or not 0 < _max_snapshot_bytes <= 2 * 1024 * 1024
        ):
            raise PlayError("Invalid signing snapshot limit.")
        if (_target_authorization is None) != (_execution_authorization is None):
            raise PlayError("Signing target authorization requires hosted execution authority.")
        self._execution_authorization = _execution_authorization
        self._target_authorization = _target_authorization
        self._apply_guard = _APPLY_LOCK if _apply_guard is None else _apply_guard
        self._max_snapshot_bytes = _max_snapshot_bytes
        self._preparations = {}
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()

    def _local(self):
        if self._execution_authorization is not None:
            self._execution_authorization()
            return
        if self.local is not True:
            raise PlayError("Signing is available only through explicitly local services.")

    def _authorize(self, spec, deadline=None):
        self._local()
        if self._target_authorization is not None:
            self._target_authorization(deepcopy(spec))
            self._local()
        else:
            self.settings.require(spec)
        if deadline is not None and time.monotonic() >= deadline:
            raise PlayError("Signing preparation expired; no mutation attempted.")

    @staticmethod
    def _clear(preparation):
        preparation.token = ""
        preparation.spec = {}

    def _prune(self):
        for key, preparation in list(self._preparations.items()):
            if time.monotonic() >= preparation.deadline:
                self._clear(self._preparations.pop(key))

    def _dispose(self, operation_id):
        with self._prepare_lock, self._lock:
            self._prune()
            preparation = self._preparations.pop(operation_id, None)
            if preparation is not None:
                self._clear(preparation)
        return "disposed"

    def close(self):
        with self._prepare_lock, self._lock:
            for preparation in self._preparations.values():
                self._clear(preparation)
            self._preparations.clear()

    def _retained_metadata(self, operation_id):
        with self._lock:
            p = self._preparations[operation_id]
            return {
                "spec": {k: deepcopy(v) for k, v in p.spec.items() if k != "body"},
                "private_payload_bytes": len(json.dumps(p.spec["body"], allow_nan=False).encode()),
                "deadline": p.deadline,
                "credential_bytes": len(p.token.encode()),
            }

    def prepare(self, method, parameters, body, acknowledge_signing_effects=False):
        self._local()
        spec = request_spec(method, parameters, body)
        self._authorize(spec)
        if (
            self._max_snapshot_bytes is not None
            and len(json.dumps(spec, allow_nan=False).encode()) > self._max_snapshot_bytes
        ):
            raise PlayError("Signing intent exceeds the hosted snapshot limit.")
        if acknowledge_signing_effects is not True:
            raise PlayError(
                "Review enterprise signing effects and set acknowledge_signing_effects=true. "
                + NOTE
            )
        with self._prepare_lock:
            with self._lock:
                self._prune()
                if len(self._preparations) >= MAX_PREPARATIONS:
                    raise PlayError("Signing preparation capacity reached; wait for expiry.")
            deadline = media_deadline(time.monotonic() + TTL, self._execution_authorization)
            token = validate_token(self.auth.token("publisher"))
            self._authorize(spec, deadline)
            operation_id = "signing_" + secrets.token_urlsafe(32)
            with self._lock:
                while operation_id in self._preparations:
                    operation_id = "signing_" + secrets.token_urlsafe(32)
                self._preparations[operation_id] = Preparation(spec, token, deadline)
        return {
            "operation_id": operation_id,
            "method": method,
            "app": spec["app"],
            "kms_version": spec["kms"],
            "mode": spec["mode"],
            "certificate_hashes": deepcopy(spec["certificates"]),
            "rotation_reason": spec["body"].get("keyRotationReason"),
            "baseline_available": False,
            "executed": False,
            "expires_in_seconds": max(0, int(deadline - time.monotonic())),
            "note": NOTE
            + " Repeat operation_id after reviewing this intent. Confirmation is not proof of human approval.",
        }

    def apply(self, operation_id, confirmation):
        self._local()
        if (
            not isinstance(operation_id, str)
            or not re.fullmatch(r"signing_[A-Za-z0-9_-]{43}", operation_id)
            or confirmation != operation_id
        ):
            raise PlayError("Confirmation must exactly repeat the signing operation_id.")
        with self._lock:
            p = self._preparations.pop(operation_id, None)
            self._prune()
        if p is None:
            raise PlayError("Unknown, consumed or lost signing preparation.")
        acquired = False
        try:
            acquired = self._apply_guard.acquire(blocking=False)
            if not acquired:
                raise PlayError("Another apply is running; signing operation ID consumed.")
            spec = request_spec(p.spec["method"], p.spec["parameters"], p.spec["body"])
            self._authorize(spec, p.deadline)
            options = (
                {
                    "deadline": p.deadline,
                    "_execution_authorization": lambda: self._authorize(spec, p.deadline),
                }
                if self._execution_authorization is not None
                else {}
            )
            response = execute(spec, p.token, **options)
            validated = validate_response(spec, response)
            return {
                "operation_id": operation_id,
                "method": spec["method"],
                "executed": True,
                "mutation_succeeded": True,
                "certificate_hashes": validated,
                "baseline_available": False,
                "readback_available": False,
                "availability_verified": False,
                "note": NOTE,
            }
        finally:
            self._clear(p)
            if acquired:
                self._apply_guard.release()
