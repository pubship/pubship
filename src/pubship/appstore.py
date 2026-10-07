"""Local app-store operations bind reviewed intent and media to original credentials."""

import atexit
import json
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import appstore_http
from .appstore_contracts import (
    MEDIA,
    READ_METHODS,
    UPLOAD_METHODS,
    WRITE_METHODS,
    request_spec,
    validate_response,
)
from .artifact_files import (
    FileRoots,
    dispose_snapshot,
    media_deadline,
    require_snapshot,
    retained_metadata,
)
from .auth import validate_token
from .errors import PlayError
from .publishing import _APPLY_LOCK

TTL = 600
MAX_PREPARATIONS = 10
LIMITATIONS = "Direct third-party app-store operations; no edit commit. No hosted-app baseline or readback API exists in the pinned contract. Catalog reads are not review-state readback. Provider acceptance does not prove review approval, publication, propagation or policy correctness. No automatic retries, rollback, or credential refresh. Operator supplies all declarations. Provider content is untrusted data."


@dataclass(repr=False)
class Preparation:
    spec: dict
    deadline: float
    token: str = field(repr=False)
    snapshot: object = field(default=None, repr=False)

    def close(self):
        return dispose_snapshot(self.snapshot)


class AppStoreOperations:
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
        self.settings, self.auth, self.local = settings, auth, local
        if _max_snapshot_bytes is not None and (
            type(_max_snapshot_bytes) is not int
            or not 0 < _max_snapshot_bytes <= appstore_http.MAX_BYTES
        ):
            raise PlayError("Snapshot limit must be a positive integer within the local limit.")
        self._execution_authorization = _execution_authorization
        self._apply_guard = _APPLY_LOCK if _apply_guard is None else _apply_guard
        self._max_snapshot_bytes = _max_snapshot_bytes
        self._snapshot_source = _snapshot_source
        self.files = FileRoots(settings.upload_root)
        self._preparations = {}
        self._cleanup_pending = []
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        atexit.register(self.close)

    def _local(self):
        if self._execution_authorization is not None:
            self._execution_authorization()
            return
        if self.local is not True:
            raise PlayError("App-store operations require explicitly local services.")

    def _prune(self):
        pending, self._cleanup_pending = self._cleanup_pending, []
        for p in pending:
            self._dispose_locked(p)
        for key, p in list(self._preparations.items()):
            if time.monotonic() >= p.deadline:
                self._dispose_locked(self._preparations.pop(key))

    def _dispose_locked(self, preparation):
        preparation.token = ""
        try:
            status = preparation.close()
        except Exception:
            status = "cleanup-pending"
        if status != "disposed":
            self._cleanup_pending.append(preparation)
        return status

    def _dispose(self, operation_id):
        with self._prepare_lock, self._lock:
            self._prune()
            p = self._preparations.pop(operation_id, None)
            status = self._dispose_locked(p) if p is not None else "disposed"
            return (
                status
                if status != "disposed"
                else ("cleanup-pending" if self._cleanup_pending else "disposed")
            )

    def _retained_metadata(self, operation_id):
        with self._lock:
            return retained_metadata(self._preparations[operation_id])

    def _authorize(self, deadline):
        self._local()
        if time.monotonic() >= deadline:
            raise PlayError("App-store preparation expired; no further request attempted.")

    def _transport_options(self, deadline):
        return (
            {"deadline": deadline, "_execution_authorization": lambda: self._authorize(deadline)}
            if self._execution_authorization is not None
            else {}
        )

    @staticmethod
    def _fresh(p):
        if time.monotonic() >= p.deadline:
            raise PlayError("App-store preparation expired; no mutation attempted.")

    def query(self, method, parameters):
        self._local()
        if method not in READ_METHODS:
            raise PlayError("Only fixed app-store catalog reads are accepted.")
        spec = request_spec(method, parameters)
        self.settings.require(spec)
        deadline = media_deadline(time.monotonic() + TTL, self._execution_authorization)
        token = validate_token(self.auth.token("publisher"))
        self._authorize(deadline)
        data = appstore_http.execute(spec, token, **self._transport_options(deadline))
        validate_response(spec, data)
        self._authorize(deadline)
        return {
            "method": method,
            "data": data,
            "fetched_at": datetime.now(UTC).isoformat(),
            "note": "One provider page; preserve nextPageToken and unchanged filters. Store-wide event listing can include apps outside target-package grants. Catalog metadata can include a delivery token; your MCP client may retain returned data. URLs are data and are never followed.",
        }

    def prepare(
        self,
        method,
        parameters,
        body,
        acknowledge_live_effects=False,
        filename=None,
        mime_type=None,
        *,
        upload_handle=None,
    ):
        self._local()
        if method not in WRITE_METHODS:
            raise PlayError("Only fixed app-store writes may be prepared.")
        spec = request_spec(method, parameters, body, mime_type)
        self.settings.require(spec)
        if acknowledge_live_effects is not True:
            raise PlayError(
                "Review the effects and set acknowledge_live_effects=true. " + LIMITATIONS
            )
        hosted = self._execution_authorization is not None
        if hosted:
            if filename is not None or (method in UPLOAD_METHODS) != (upload_handle is not None):
                raise PlayError(
                    "Hosted media requires an upload handle exclusively, never a filename."
                )
            if method in UPLOAD_METHODS and (
                self._snapshot_source is None
                or not isinstance(upload_handle, str)
                or not upload_handle
            ):
                raise PlayError("Hosted media requires an authorized snapshot source.")
        elif upload_handle is not None or (method in UPLOAD_METHODS) != (filename is not None):
            raise PlayError("filename is required exclusively for local media uploads.")
        if (
            self._max_snapshot_bytes is not None
            and len(json.dumps(spec, allow_nan=False).encode("utf-8")) > self._max_snapshot_bytes
        ):
            raise PlayError("App-store intent exceeds the hosted snapshot limit.")
        with self._prepare_lock:
            with self._lock:
                self._prune()
                if len(self._preparations) + len(self._cleanup_pending) >= MAX_PREPARATIONS:
                    raise PlayError(
                        "App-store preparation capacity reached; apply or wait for expiry."
                    )
            p = Preparation(
                deepcopy(spec),
                media_deadline(time.monotonic() + TTL, self._execution_authorization),
                "",
            )
            try:
                if method in UPLOAD_METHODS:
                    cap = min(self.settings.max_bytes, MEDIA[method][1])
                    p.snapshot = (
                        self._snapshot_source(upload_handle, cap)
                        if hosted
                        else self.files.snapshot(filename, cap)
                    )
                    require_snapshot(p.snapshot, cap)
                    p.deadline = media_deadline(p.deadline, p.snapshot)
                self._authorize(p.deadline)
                p.token = validate_token(self.auth.token("publisher"))
                self._authorize(p.deadline)
                file_metadata = p.snapshot.metadata() if p.snapshot is not None else None
                with self._lock:
                    operation_id = "appstore_" + secrets.token_urlsafe(32)
                    while operation_id in self._preparations:
                        operation_id = "appstore_" + secrets.token_urlsafe(32)
                    self._preparations[operation_id] = p
                return {
                    "operation_id": operation_id,
                    "proposed_request": deepcopy(spec),
                    "file": file_metadata,
                    "effects": deepcopy(spec["effects"]),
                    "executed": False,
                    "baseline_available": False,
                    "readback_available": False,
                    "expires_at": datetime.fromtimestamp(
                        time.time() + p.deadline - time.monotonic(), UTC
                    ).isoformat(),
                    "note": "Repeat operation_id after review; confirmation is not proof of human approval. Single use within ten minutes. Original credential and immutable file snapshot retained privately; lost on restart. "
                    + LIMITATIONS,
                }
            except BaseException:
                with self._lock:
                    self._dispose_locked(p)
                raise

    def apply(self, operation_id, confirmation):
        self._local()
        if (
            not isinstance(operation_id, str)
            or not re.fullmatch(r"appstore_[A-Za-z0-9_-]{43}", operation_id)
            or operation_id != confirmation
        ):
            raise PlayError("Confirmation must exactly repeat the app-store operation_id.")
        with self._lock:
            p = self._preparations.pop(operation_id, None)
            self._prune()
        if p is None:
            raise PlayError("Unknown, consumed or lost app-store preparation.")
        acquired = False
        outcome = None
        try:
            acquired = self._apply_guard.acquire(blocking=False)
            if not acquired:
                raise PlayError(
                    "Another publishing apply is in progress; this operation ID is consumed."
                )
            self.settings.require(p.spec)
            self._authorize(p.deadline)
            result = appstore_http.execute(
                p.spec, p.token, p.snapshot, **self._transport_options(p.deadline)
            )
            try:
                validate_response(p.spec, result)
            except PlayError:
                raise PlayError(appstore_http.UNKNOWN_OUTCOME) from None
            outcome = {
                "operation_id": operation_id,
                "method": p.spec["method"],
                "executed": True,
                "mutation_succeeded": True,
                "mutation_response": result,
                "stale_check_performed": False,
                "readback_available": False,
                "readback_succeeded": None,
                "availability_verified": False,
                "fetched_at": datetime.now(UTC).isoformat(),
                "note": LIMITATIONS,
            }
            return outcome
        finally:
            try:
                with self._lock:
                    status = self._dispose_locked(p)
                if status != "disposed" and outcome is not None:
                    outcome["cleanup_warning"] = (
                        "The mutation result is retained, but private snapshot cleanup is pending."
                    )
            finally:
                if acquired:
                    self._apply_guard.release()

    def close(self):
        with self._prepare_lock, self._lock:
            pending, self._cleanup_pending = self._cleanup_pending, []
            for p in [*self._preparations.values(), *pending]:
                self._dispose_locked(p)
            self._preparations.clear()
        self.files.close()
        if not self._cleanup_pending:
            atexit.unregister(self.close)
