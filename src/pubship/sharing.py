"""Local, confirmed, single-attempt internal app sharing uploads."""

import atexit
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import artifact_http
from .artifact_files import (
    FileRoots,
    basename,
    dispose_snapshot,
    media_deadline,
    require_snapshot,
    retained_metadata,
)
from .auth import validate_token
from .config import PACKAGE
from .contracts import methods, validate_request
from .errors import PlayError
from .publishing import _APPLY_LOCK

PREFIX = "androidpublisher.internalappsharingartifacts."
CAPS = {PREFIX + "uploadapk": 1073741824, PREFIX + "uploadbundle": 10737418240}
PREPARATION_TTL_SECONDS = 600
MAX_PREPARATIONS = 4
UNKNOWN_OUTCOME = (
    "Internal app sharing upload outcome unknown; inspect internal app sharing in Play Console "
    "before preparing again. Do not automatically retry. This operation ID is consumed."
)
EFFECTS = (
    "Uploads directly to internal app sharing and creates an installation link for authorized "
    "testers. This is an immediate sharing action, without an edit or production release. "
    "No automatic retry or rollback."
)
NOTE = (
    "Provider download URLs are returned as untrusted data and are never opened or downloaded. "
    "Google signs sharing artifacts; provider artifact hashes and certificate fingerprints "
    "are separate from the frozen source file hashes. Production publication is not verified."
)


def _request_spec(method, package_name):
    if not isinstance(method, str) or method not in CAPS:
        raise PlayError("Method is not a supported internal app sharing upload.")
    if (
        not isinstance(package_name, str)
        or len(package_name) > 255
        or not PACKAGE.fullmatch(package_name)
    ):
        raise PlayError("Invalid Android package name.")
    parameters = {"packageName": package_name}
    validate_request(method, parameters)
    definition = methods()[method]
    path = definition["mediaUpload"]["protocols"]["simple"]["path"]
    return {
        "method": method,
        "http_method": "POST",
        "url": "https://androidpublisher.googleapis.com"
        + path.replace("{packageName}", package_name)
        + "?uploadType=media",
        "parameters": parameters,
        "mime_type": "application/octet-stream",
    }


def _validate_response(data):
    if not isinstance(data, dict):
        raise PlayError(UNKNOWN_OUTCOME)
    url = data.get("downloadUrl")
    if (
        not isinstance(url, str)
        or not 1 <= len(url) <= 8192
        or any(not c.isprintable() or c.isspace() for c in url)
        or "\\" in url
    ):
        raise PlayError(UNKNOWN_OUTCOME)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise PlayError(UNKNOWN_OUTCOME)
    if "sha256" in data and (
        not isinstance(data["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", data["sha256"])
    ):
        raise PlayError(UNKNOWN_OUTCOME)
    if "certificateFingerprint" in data and (
        not isinstance(data["certificateFingerprint"], str)
        or not 1 <= len(data["certificateFingerprint"]) <= 1024
        or not data["certificateFingerprint"].isprintable()
    ):
        raise PlayError(UNKNOWN_OUTCOME)


@dataclass(repr=False)
class _Preparation:
    method: str
    package_name: str
    spec: dict
    snapshot: object
    token: str = field(repr=False)
    deadline: float

    def close(self):
        return dispose_snapshot(self.snapshot)


class InternalSharing:
    def __init__(
        self,
        settings,
        auth,
        *,
        local=False,
        _execution_authorization=None,
        _apply_guard=None,
        _max_snapshot_bytes=None,
        _snapshot_source=None,
    ):
        self.settings, self.auth, self._local = settings, auth, local
        if _max_snapshot_bytes is not None and (
            type(_max_snapshot_bytes) is not int or not 0 < _max_snapshot_bytes <= 2 * 1024 * 1024
        ):
            raise PlayError("Snapshot limit must be a positive integer within the local limit.")
        self._execution_authorization = _execution_authorization
        self._apply_guard = _APPLY_LOCK if _apply_guard is None else _apply_guard
        self._snapshot_source = _snapshot_source
        self._max_snapshot_bytes = _max_snapshot_bytes
        self.files = FileRoots(settings.artifact_upload_root)
        self._preparations = {}
        self._cleanup_pending = []
        self._retained_bytes = 0
        self._retained_count = 0
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        atexit.register(self.close)

    def _enabled(self):
        if self._execution_authorization is not None:
            self._execution_authorization()
            return
        if (
            self._local is not True
            or not self.settings.artifact_packages
            or not self.settings.artifact_methods
        ):
            raise PlayError("Internal app sharing requires explicit local artifact scopes.")

    def _dispose_locked(self, preparation):
        preparation.token = ""
        try:
            status = preparation.close()
        except Exception:
            status = "cleanup-pending"
        if status != "disposed":
            self._cleanup_pending.append(preparation)
            return status
        self._retained_count -= 1
        self._retained_bytes -= preparation.snapshot.size
        return status

    def _authorize(self, deadline):
        self._enabled()
        if time.monotonic() >= deadline:
            raise PlayError("Sharing preparation expired; no further request attempted.")

    def _retained_metadata(self, operation_id):
        with self._lock:
            return retained_metadata(self._preparations[operation_id])

    def _dispose(self, operation_id):
        with self._prepare_lock, self._lock:
            self._purge()
            preparation = self._preparations.pop(operation_id, None)
            status = self._dispose_locked(preparation) if preparation is not None else "disposed"
            return (
                status
                if status != "disposed"
                else ("cleanup-pending" if self._cleanup_pending else "disposed")
            )

    def _purge(self):
        pending, self._cleanup_pending = self._cleanup_pending, []
        for preparation in pending:
            self._dispose_locked(preparation)
        for key, preparation in list(self._preparations.items()):
            if time.monotonic() >= preparation.deadline:
                self._dispose_locked(self._preparations.pop(key))

    def close(self):
        with self._prepare_lock, self._lock:
            pending, self._cleanup_pending = self._cleanup_pending, []
            for preparation in [*self._preparations.values(), *pending]:
                self._dispose_locked(preparation)
            self._preparations.clear()
            self.files.close()
        if not self._cleanup_pending:
            atexit.unregister(self.close)

    def prepare(
        self,
        method,
        package_name,
        file_path=None,
        acknowledge_sharing_effects=False,
        *,
        upload_handle=None,
    ):
        self._enabled()
        spec = _request_spec(method, package_name)
        self.settings.require_artifact(package_name, method)
        hosted = self._execution_authorization is not None
        if hosted:
            if (
                self._snapshot_source is None
                or not isinstance(upload_handle, str)
                or not upload_handle
                or file_path is not None
            ):
                raise PlayError(
                    "Hosted sharing requires an upload handle and authorized snapshot source, never a filename."
                )
        else:
            if upload_handle is not None:
                raise PlayError("Upload handles require hosted authorization.")
            basename(file_path)
            if not self.settings.artifact_upload_root:
                raise PlayError("Set GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT for sharing preparations.")
        if acknowledge_sharing_effects is not True:
            raise PlayError("acknowledge_sharing_effects must be true. " + EFFECTS)
        with self._prepare_lock:
            with self._lock:
                self._purge()
                if self._retained_count >= MAX_PREPARATIONS:
                    raise PlayError(
                        "Sharing preparation capacity reached; apply or wait for expiry."
                    )
                remaining = 2 * self.settings.artifact_max_bytes - self._retained_bytes
            deadline = media_deadline(
                time.monotonic() + PREPARATION_TTL_SECONDS, self._execution_authorization
            )
            cap = min(CAPS[method], self.settings.artifact_max_bytes, remaining)
            snapshot = (
                self._snapshot_source(upload_handle, cap)
                if hosted
                else self.files.snapshot(file_path, cap)
            )
            try:
                require_snapshot(snapshot, cap)
                if not hosted:
                    deadline = time.monotonic() + PREPARATION_TTL_SECONDS
                deadline = media_deadline(deadline, snapshot)
                self._authorize(deadline)
                token = validate_token(self.auth.token("publisher"))
                self._authorize(deadline)
                preparation = _Preparation(method, package_name, spec, snapshot, token, deadline)
                metadata = snapshot.metadata()
                with self._lock:
                    operation_id = "sharing_" + secrets.token_urlsafe(32)
                    while operation_id in self._preparations:
                        operation_id = "sharing_" + secrets.token_urlsafe(32)
                    self._preparations[operation_id] = preparation
                    self._retained_count += 1
                    self._retained_bytes += snapshot.size
                snapshot = None
            finally:
                if snapshot is not None:
                    with self._lock:
                        orphan = _Preparation(method, package_name, spec, snapshot, "", 0)
                        self._retained_count += 1
                        self._retained_bytes += snapshot.size
                        self._dispose_locked(orphan)
        return {
            "operation_id": operation_id,
            "expires_at": datetime.fromtimestamp(
                time.time() + deadline - time.monotonic(), UTC
            ).isoformat(),
            "method": method,
            "package_name": package_name,
            "proposed_request": deepcopy(spec),
            "source_file": metadata,
            "effects": EFFECTS,
            "executed": False,
            "provider_validation_performed": False,
            "data_scope": "internal_app_sharing",
            "note": "Repeat operation_id as confirmation. Single-use, process-local, at most ten minutes. Original credentials and private file snapshot are retained. Expired snapshots are purged on later preparations or shutdown; no timed secure-erasure promise. "
            + NOTE,
        }

    def apply(self, operation_id, confirmation):
        self._enabled()
        if (
            not isinstance(operation_id, str)
            or not re.fullmatch(r"sharing_[A-Za-z0-9_-]{43}", operation_id)
            or not isinstance(confirmation, str)
            or confirmation != operation_id
        ):
            raise PlayError("Confirmation must exactly repeat the sharing operation_id.")
        with self._lock:
            preparation = self._preparations.pop(operation_id, None)
            self._purge()
        if preparation is None:
            raise PlayError("Unknown, consumed or lost sharing preparation.")
        acquired = False
        result = None
        try:
            acquired = self._apply_guard.acquire(blocking=False)
            if not acquired:
                raise PlayError(
                    "Another publishing apply is in progress; this preparation is consumed."
                )
            self.settings.require_artifact(preparation.package_name, preparation.method)
            self._authorize(preparation.deadline)
            try:
                mutation = artifact_http.upload(
                    preparation.spec,
                    preparation.token,
                    preparation.snapshot,
                    **(
                        {
                            "deadline": preparation.deadline,
                            "_execution_authorization": lambda: self._authorize(
                                preparation.deadline
                            ),
                        }
                        if self._execution_authorization is not None
                        else {}
                    ),
                )
                _validate_response(mutation)
            except Exception:
                # A write can have happened even when its response is invalid or lost.
                raise PlayError(UNKNOWN_OUTCOME) from None
            result = {
                "operation_id": operation_id,
                "method": preparation.method,
                "package_name": preparation.package_name,
                "executed": True,
                "mutation_succeeded": True,
                "mutation_response": mutation,
                "source_file": preparation.snapshot.metadata(),
                "publication_verified": False,
                "data_scope": "internal_app_sharing",
                "note": EFFECTS + " " + NOTE,
            }
            return result
        finally:
            try:
                with self._lock:
                    cleaned = self._dispose_locked(preparation)
                if cleaned != "disposed" and result is not None:
                    result["cleanup_warning"] = (
                        "Upload succeeded, but private snapshot cleanup failed."
                    )
            finally:
                if acquired:
                    self._apply_guard.release()
