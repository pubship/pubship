"""Local artifact preparation, one-shot uploads and explicitly scoped downloads."""

import atexit
import json
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import artifact_http, http
from .artifact_contracts import (
    CAPS,
    DOWNLOAD_METHODS,
    EFFECTS,
    EXTERNAL,
    UPLOAD_METHODS,
    baseline_specs,
    bounded_json,
    request_spec,
    require_target,
    validate_baseline,
    validate_response,
)
from .artifact_files import (
    FileRoots,
    basename,
    dispose_snapshot,
    media_deadline,
    require_snapshot,
    retained_metadata,
)
from .auth import validate_token
from .edit_lifecycle_http import edit_url
from .edit_writes import _edit_expiry
from .errors import PlayError
from .publishing import _APPLY_LOCK

PREPARATION_TTL_SECONDS = 600
MAX_PREPARATIONS = 4
OBSERVATION_ERROR = "The artifact mutation succeeded, but its subsequent metadata observation failed. Reconcile the edit; do not automatically retry. This operation ID is consumed."
NOTE = (
    "Existing edit staging only; no commit, publication or provider policy validation. "
    "Metadata checks are not atomic compare-and-swap and external changes can race. "
    "Artifact archives are never parsed, extracted or executed. Provider URLs are untrusted "
    "data, never additional download targets. Deobfuscation content cannot be read back. "
    "Missing collection fields do not prove emptiness or artifact absence."
)


@dataclass(repr=False)
class _Preparation:
    method: str
    parameters: dict
    body: dict | None
    spec: dict
    snapshot: object
    baseline: str
    edit_snapshot: str
    token: str = field(repr=False)
    deadline: float

    def close(self):
        return dispose_snapshot(self.snapshot)


class Artifacts:
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
        _destination_source=None,
    ):
        self.settings, self.auth, self._local = settings, auth, local
        if _max_snapshot_bytes is not None and (
            type(_max_snapshot_bytes) is not int or not 0 < _max_snapshot_bytes <= 2 * 1024 * 1024
        ):
            raise PlayError("Snapshot limit must be a positive integer within the local limit.")
        self._execution_authorization = _execution_authorization
        self._apply_guard = _APPLY_LOCK if _apply_guard is None else _apply_guard
        self._max_snapshot_bytes = _max_snapshot_bytes
        self._snapshot_source, self._destination_source = _snapshot_source, _destination_source
        self.files = FileRoots(settings.artifact_upload_root, settings.artifact_download_root)
        self._preparations = {}
        self._retained_bytes = 0
        self._retained_count = 0
        self._cleanup_pending = []
        self._lock = threading.Lock()
        self._prepare_lock = threading.Lock()
        atexit.register(self.close)

    def close(self):
        with self._prepare_lock, self._lock:
            pending, self._cleanup_pending = self._cleanup_pending, []
            for preparation in [*self._preparations.values(), *pending]:
                self._dispose_locked(preparation)
            self._preparations.clear()
            self.files.close()
        if not self._cleanup_pending:
            atexit.unregister(self.close)

    def _enabled(self):
        if self._execution_authorization is not None:
            self._execution_authorization()
            return
        if (
            self._local is not True
            or not self.settings.artifact_packages
            or not self.settings.artifact_methods
        ):
            raise PlayError("Artifacts require explicitly enabled local package and method scopes.")

    def _authorize(self, deadline):
        self._enabled()
        if time.monotonic() >= deadline:
            raise PlayError("Artifact preparation expired; no further request attempted.")

    def _bounded(self, value):
        try:
            result = json.dumps(value, sort_keys=True, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            raise PlayError("Invalid artifact observation data.") from None
        if (
            self._max_snapshot_bytes is not None
            and len(result.encode("utf-8")) > self._max_snapshot_bytes
        ):
            raise PlayError("Artifact observations exceed the hosted snapshot limit.")
        return result

    def _read(self, method, parameters, token, deadline, edit=None):
        snapshots, available = [], []
        for spec in baseline_specs(method, parameters):
            self._authorize(deadline)
            data = http.get(spec["url"], token)
            if self._max_snapshot_bytes is not None:
                self._bounded([edit, *snapshots, data])
            available.append(validate_baseline(spec, data))
            snapshots.append(data)
        bounded_json(snapshots)
        return snapshots, available

    def _purge(self):
        pending, self._cleanup_pending = self._cleanup_pending, []
        for preparation in pending:
            self._dispose_locked(preparation)
        for key, value in list(self._preparations.items()):
            if time.monotonic() >= value.deadline:
                self._dispose_locked(self._preparations.pop(key))

    def _dispose_locked(self, preparation):
        preparation.token = ""
        try:
            status = preparation.close()
        except Exception:
            status = "cleanup-pending"
        snapshot = preparation.snapshot
        if status != "disposed":
            # Failed cleanup still consumes disk. Keep the object and reservation
            # until a later local cleanup attempt actually closes the descriptor.
            self._cleanup_pending.append(preparation)
            return status
        self._retained_count -= 1
        if snapshot is not None:
            self._retained_bytes -= snapshot.size
        return status

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

    def prepare(
        self,
        method,
        parameters,
        filename=None,
        mime_type=None,
        body=None,
        acknowledge_effects=False,
        *,
        upload_handle=None,
    ):
        self._enabled()
        hosted = self._execution_authorization is not None
        if upload_handle is not None and (not hosted or self._snapshot_source is None):
            raise PlayError("Upload handles require an authorized hosted snapshot source.")
        if (
            hosted
            and method in CAPS
            and (not isinstance(upload_handle, str) or not upload_handle or filename is not None)
        ):
            raise PlayError(
                "Hosted binary preparations require an upload handle, never a filename."
            )
        spec = request_spec(
            method,
            parameters,
            filename=filename,
            mime_type=mime_type,
            body=body,
            _media_source=upload_handle is not None,
        )
        if method not in UPLOAD_METHODS:
            raise PlayError("Use download_artifact for artifact downloads.")
        self.settings.require_artifact(parameters["packageName"], method)
        if method in CAPS and not hosted:
            basename(filename)
            if not self.settings.artifact_upload_root:
                raise PlayError("Set GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT for binary preparations.")
        if acknowledge_effects is not True:
            raise PlayError("acknowledge_effects must be true. " + EFFECTS)
        parameters, body = deepcopy(parameters), deepcopy(body)
        with self._prepare_lock:
            with self._lock:
                self._purge()
                if self._retained_count >= MAX_PREPARATIONS:
                    raise PlayError(
                        "Artifact preparation capacity reached; apply or wait for expiry."
                    )
                retained = self._retained_bytes
            snapshot = None
            try:
                deadline = media_deadline(
                    time.monotonic() + PREPARATION_TTL_SECONDS, self._execution_authorization
                )
                if method in CAPS:
                    remaining = 2 * self.settings.artifact_max_bytes - retained
                    cap = min(CAPS[method], self.settings.artifact_max_bytes, remaining)
                    snapshot = (
                        self._snapshot_source(upload_handle, cap)
                        if hosted
                        else self.files.snapshot(filename, cap)
                    )
                    require_snapshot(snapshot, cap)
                    if not hosted:
                        deadline = time.monotonic() + PREPARATION_TTL_SECONDS
                    deadline = media_deadline(deadline, snapshot)
                self._authorize(deadline)
                token = validate_token(self.auth.token("publisher"))
                self._authorize(deadline)
                edit = http.get(edit_url(parameters), token)
                self._bounded(edit)
                expiry = _edit_expiry(edit, parameters)
                deadline = min(deadline, time.monotonic() + expiry - time.time())
                snapshots, available = self._read(method, parameters, token, deadline, edit)
                require_target(method, parameters, body, snapshots)
                self._authorize(deadline)
                operation_id = "artifact_" + secrets.token_urlsafe(32)
                preparation = _Preparation(
                    method,
                    parameters,
                    body,
                    spec,
                    snapshot,
                    bounded_json(snapshots),
                    bounded_json(edit),
                    token,
                    deadline,
                )
                file_metadata = snapshot.metadata() if snapshot is not None else None
                with self._lock:
                    self._preparations[operation_id] = preparation
                    self._retained_count += 1
                    self._retained_bytes += snapshot.size if snapshot is not None else 0
                snapshot = None
            finally:
                if snapshot is not None:
                    # Failed prepare still owns the lease until disposal succeeds.
                    with self._lock:
                        orphan = _Preparation(
                            method, parameters, body, spec, snapshot, "", "", "", 0
                        )
                        self._retained_count += 1
                        self._retained_bytes += snapshot.size
                        self._dispose_locked(orphan)
        proposal = deepcopy(spec)
        if body is not None:
            # The exact bytes remain retained. Avoid echoing multi-MiB icon/certificate blobs.
            apk = deepcopy(body["externallyHostedApk"])
            apk["iconBase64"] = {"encoded_length": len(apk["iconBase64"])}
            apk["certificateBase64s"] = [
                {"encoded_length": len(v)} for v in apk["certificateBase64s"]
            ]
            proposal["body_summary"] = {"externallyHostedApk": apk}
        result = {
            "operation_id": operation_id,
            "expires_at": datetime.fromtimestamp(
                time.time() + deadline - time.monotonic(), UTC
            ).isoformat(),
            "method": method,
            "before": deepcopy(snapshots[0]),
            "baseline_observation_available": all(available),
            "data_scope": "edit_snapshot",
            "proposed_request": proposal,
            "effects": EFFECTS,
            "executed": False,
            "provider_validation_performed": False,
            "stale_check_performed": False,
            "deobfuscation_content_baseline_available": False,
            "note": "Repeat operation_id as confirmation. Single-use, process-local, at most ten minutes and edit expiry. Original credentials and private file snapshot are retained. Expired snapshots are purged on subsequent preparations or shutdown; no timed secure-erasure promise. "
            + NOTE,
        }
        if file_metadata is not None:
            result["file"] = file_metadata
        if len(snapshots) > 1:
            result["additional_baselines"] = deepcopy(snapshots[1:])
        return result

    def apply(self, operation_id, confirmation):
        self._enabled()
        if (
            not isinstance(operation_id, str)
            or not re.fullmatch(r"artifact_[A-Za-z0-9_-]{43}", operation_id)
            or not isinstance(confirmation, str)
            or confirmation != operation_id
        ):
            raise PlayError("Confirmation must exactly repeat the artifact operation_id.")
        with self._lock:
            preparation = self._preparations.pop(operation_id, None)
        if preparation is None:
            raise PlayError("Unknown, consumed or lost artifact preparation.")
        acquired = False
        result = None
        try:
            acquired = self._apply_guard.acquire(blocking=False)
            if not acquired:
                raise PlayError(
                    "Another publishing apply is in progress; this preparation is consumed."
                )
            method, parameters = preparation.method, preparation.parameters
            self.settings.require_artifact(parameters["packageName"], method)
            self._authorize(preparation.deadline)
            edit = http.get(edit_url(parameters), preparation.token)
            self._bounded(edit)
            _edit_expiry(edit, parameters)
            if bounded_json(edit) != preparation.edit_snapshot:
                raise PlayError("Edit metadata changed; no artifact upload was attempted.")
            snapshots, _ = self._read(
                method, parameters, preparation.token, preparation.deadline, edit
            )
            if bounded_json(snapshots) != preparation.baseline:
                raise PlayError("Artifact metadata baseline changed; no upload was attempted.")
            require_target(method, parameters, preparation.body, snapshots)
            self._authorize(preparation.deadline)
            _edit_expiry(edit, parameters)
            mutation = artifact_http.upload(
                preparation.spec,
                preparation.token,
                preparation.snapshot,
                preparation.body,
                **(
                    {
                        "deadline": preparation.deadline,
                        "_execution_authorization": lambda: self._authorize(preparation.deadline),
                    }
                    if self._execution_authorization is not None
                    else {}
                ),
            )
            try:
                verified = validate_response(
                    method, parameters, preparation.body, preparation.snapshot, mutation
                )
            except Exception:
                raise PlayError(artifact_http.UNKNOWN_OUTCOME) from None
            result = {
                "operation_id": operation_id,
                "method": method,
                "executed": True,
                "mutation_succeeded": True,
                "mutation_response": mutation,
                "digest_verified": verified,
                "stale_check_performed": True,
                "committed": False,
                "publication_verified": False,
                "data_scope": "edit_snapshot",
                "note": NOTE,
            }
            try:
                after, available = self._read(
                    method, parameters, preparation.token, preparation.deadline, edit
                )
            except Exception:
                result.update(readback_succeeded=False, observation_error=OBSERVATION_ERROR)
            else:
                content_available = all(available) and not method.endswith(
                    "deobfuscationfiles.upload"
                )
                result.update(
                    readback_succeeded=True,
                    after=after[0],
                    artifact_observation_available=content_available,
                )
                if len(after) > 1:
                    result["additional_observations"] = after[1:]
                if content_available:
                    if method.endswith("images.upload"):
                        observed = any(
                            x["id"] == mutation["image"]["id"] for x in after[0]["images"]
                        )
                    elif method.endswith("expansionfiles.upload"):
                        observed = after[0] == mutation["expansionFile"]
                    else:
                        version = (
                            mutation["externallyHostedApk"]["versionCode"]
                            if method == EXTERNAL
                            else mutation["versionCode"]
                        )
                        key = "bundles" if method.endswith("bundles.upload") else "apks"
                        observed = any(x["versionCode"] == version for x in after[0][key])
                    result["artifact_observed"] = observed
            return result
        finally:
            try:
                with self._lock:
                    cleaned = self._dispose_locked(preparation)
                if cleaned != "disposed" and result is not None:
                    result["cleanup_warning"] = (
                        "The upload result is retained, but private snapshot cleanup failed."
                    )
            finally:
                if acquired:
                    self._apply_guard.release()

    @staticmethod
    def _fresh(preparation):
        if time.monotonic() >= preparation.deadline:
            raise PlayError("Artifact preparation expired; no upload was attempted.")

    def download(self, method, parameters):
        self._enabled()
        spec = request_spec(method, parameters)
        if method not in DOWNLOAD_METHODS:
            raise PlayError("Use prepare_artifact_upload for artifact uploads.")
        self.settings.require_artifact(parameters["packageName"], method)
        hosted = self._execution_authorization is not None
        if hosted and self._destination_source is None:
            raise PlayError("Hosted downloads require an authorized destination source.")
        if not hosted and not self.settings.artifact_download_root:
            raise PlayError("Set GOOGLE_PLAY_ARTIFACT_DOWNLOAD_ROOT for artifact downloads.")
        destination = (
            self._destination_source(self.settings.artifact_max_bytes)
            if hosted
            else self.files.destination(self.settings.artifact_max_bytes)
        )
        try:
            deadline = media_deadline(
                time.monotonic()
                + (PREPARATION_TTL_SECONDS if hosted else artifact_http.TRANSFER_SECONDS),
                self._execution_authorization,
                destination,
            )
            self._authorize(deadline)
            token = validate_token(self.auth.token("publisher"))
            self._authorize(deadline)
            result = artifact_http.download(
                spec,
                token,
                destination,
                **(
                    {
                        "deadline": deadline,
                        "_execution_authorization": lambda: self._authorize(deadline),
                    }
                    if hosted
                    else {}
                ),
            )
        except BaseException:
            if hosted:
                dispose_snapshot(destination)
            raise
        return {
            "method": method,
            **result,
            "downloaded": True,
            "google_mutation_performed": False,
            "note": (
                "Private download handle created. "
                if hosted
                else "Local file created without overwrite. "
            )
            + "Bytes are untrusted and were not parsed or executed. Generated APK availability follows Google's processing and committed state; no automatic commit or track write.",
        }
