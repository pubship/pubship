"""Fixed distribution adapters sharing the hosted registry's authority and lifecycle."""

import time

from .appstore import AppStoreOperations
from .appstore_config import AppStoreSettings
from .appstore_contracts import MEDIA, READ_METHODS, WRITE_METHODS
from .appstore_contracts import request_spec as appstore_spec
from .artifact import Artifacts
from .artifact_contracts import CAPS, DOWNLOAD_METHODS, UPLOAD_METHODS
from .artifact_contracts import request_spec as artifact_spec
from .config import Settings
from .errors import PlayError
from .hosted_catalog import (
    HOSTED_APPSTORE_CAPABILITIES,
    HOSTED_ARTIFACT_CAPABILITIES,
    HOSTED_SHARING_CAPABILITIES,
)
from .hosted_edits import MAX_REQUEST_BYTES, _ExecutionAuthorization, _size
from .hosted_transfers import TransferPurpose
from .sharing import CAPS as SHARING_CAPS
from .sharing import InternalSharing
from .sharing import _request_spec as sharing_spec


class HostedDistribution:
    def __init__(self, registry):
        self.registry = registry
        self.provider = registry.provider

    @property
    def transfers(self):
        store = self.registry.transfers
        if store is None or not store.enabled:
            raise PlayError("Hosted transfer storage is disabled or unavailable.")
        return store

    @staticmethod
    def _bounded(*values):
        if _size(values) > MAX_REQUEST_BYTES:
            raise PlayError("Hosted distribution request exceeds 256 KiB.")

    @staticmethod
    def _spec(kind, method, parameters, body=None, mime_type=None):
        if not isinstance(method, str):
            raise PlayError("An exact supported distribution method is required.")
        if kind == "artifact" and method in HOSTED_ARTIFACT_CAPABILITIES:
            spec = artifact_spec(
                method, parameters, mime_type=mime_type, body=body, _media_source=method in CAPS
            )
            return spec, HOSTED_ARTIFACT_CAPABILITIES[method], parameters["packageName"], None, None
        if kind == "sharing" and method in HOSTED_SHARING_CAPABILITIES:
            if (
                not isinstance(parameters, dict)
                or set(parameters) != {"packageName"}
                or body is not None
                or mime_type not in (None, "application/octet-stream")
            ):
                raise PlayError(
                    "Sharing requires an exact package and binary media, without a JSON body."
                )
            spec = sharing_spec(method, parameters["packageName"])
            return spec, HOSTED_SHARING_CAPABILITIES[method], parameters["packageName"], None, None
        if kind == "appstore" and method in HOSTED_APPSTORE_CAPABILITIES:
            spec = appstore_spec(method, parameters, body, mime_type)
            return (
                spec,
                HOSTED_APPSTORE_CAPABILITIES[method],
                None,
                spec["parameters"]["appStorePackageName"],
                spec["target_package"],
            )
        raise PlayError("This method is not enabled for this hosted distribution family.")

    def _authorize(self, principal, capability, package, store, target):
        if store is not None:
            return self.provider.authorize_appstore_current(principal, capability, store, target)
        return self.provider.authorize_current(principal, capability, package)

    @staticmethod
    def _purpose(spec, capability, package, store, target):
        method = spec["method"]
        cap = (
            CAPS.get(method)
            or SHARING_CAPS.get(method)
            or (MEDIA[method][1] if method in MEDIA else None)
        )
        return TransferPurpose(
            method,
            spec["parameters"],
            spec.get("mime_type") or "application/octet-stream",
            capability,
            package=package,
            store=store,
            target=target,
            max_bytes=cap,
        )

    def _settings(self, kind, method, package, store, target, *, media):
        limit = self.transfers.limits.object_bytes if media else 1024**3
        if kind == "appstore":
            return AppStoreSettings(
                stores=frozenset({store}),
                packages=frozenset({target}) if target else frozenset(),
                methods=frozenset({method}),
                max_bytes=min(limit, 10 * 1024**3),
            )
        return Settings(
            packages=frozenset({package}),
            artifact_packages=frozenset({package}),
            artifact_methods=frozenset({method}),
            artifact_max_bytes=min(limit, 50 * 1024**3),
        )

    def _engine(self, principal, record, spec, *, media):
        _, auth = self.provider.services(principal)
        hook = _ExecutionAuthorization(
            self.registry,
            record.capability,
            record.package,
            record.deadline,
            store=record.store,
            target=record.target,
        )
        hook.principal = principal
        hook()
        options = dict(
            _execution_authorization=hook,
            _apply_guard=record.guard,
            _max_snapshot_bytes=MAX_REQUEST_BYTES,
        )
        if media:
            purpose = self._purpose(
                spec, record.capability, record.package, record.store, record.target
            )
            transfers = self.transfers

            def snapshot_source(handle, cap):
                hook()
                lease = transfers.claim_upload(record.owner, purpose, handle, authorize=hook)
                # Engine validates the passed cap and disposes on failure.
                return lease

            options["_snapshot_source"] = snapshot_source
            if record.kind == "artifact":
                options["_destination_source"] = lambda cap: transfers.reserve_download(
                    record.owner, purpose, cap, record.deadline, authorize=hook
                )
        cls = {"artifact": Artifacts, "sharing": InternalSharing, "appstore": AppStoreOperations}[
            record.kind
        ]
        settings = self._settings(
            record.kind, record.method, record.package, record.store, record.target, media=media
        )
        engine = cls(settings, auth, **options)
        with self.registry._lock:
            record.engine = engine
        return engine

    def prepare(
        self,
        principal,
        kind,
        method,
        parameters,
        body,
        acknowledge_effects,
        upload_handle,
        mime_type,
    ):
        self._bounded(method, parameters, body, upload_handle, mime_type)
        spec, capability, package, store, target = self._spec(
            kind, method, parameters, body, mime_type
        )
        if (kind == "artifact" and method not in UPLOAD_METHODS) or (
            kind == "appstore" and method not in WRITE_METHODS
        ):
            raise PlayError("Use the dedicated distribution read/download tool for this method.")
        media = method in CAPS or method in SHARING_CAPS or method in MEDIA
        if media != (upload_handle is not None) or (
            media and (not isinstance(upload_handle, str) or not upload_handle)
        ):
            raise PlayError("Exactly binary preparations require an upload handle.")
        with self.provider.grant_lock(self.registry._subject(principal)):
            authorization = self._authorize(principal, capability, package, store, target)
            operation_id, record = self.registry._reserve(authorization, kind, capability, package)
            record.method, record.store, record.target = method, store, target
            engine = None
            try:
                engine = self._engine(principal, record, spec, media=media)
                if kind == "artifact":
                    result = engine.prepare(
                        method,
                        parameters,
                        mime_type=mime_type,
                        body=body,
                        acknowledge_effects=acknowledge_effects,
                        upload_handle=upload_handle,
                    )
                elif kind == "sharing":
                    result = engine.prepare(
                        method,
                        package,
                        acknowledge_sharing_effects=acknowledge_effects,
                        upload_handle=upload_handle,
                    )
                else:
                    result = engine.prepare(
                        method,
                        parameters,
                        body,
                        acknowledge_effects,
                        mime_type=mime_type,
                        upload_handle=upload_handle,
                    )
                return self.registry._complete_prepare(operation_id, record, engine, result)
            except BaseException:
                self.registry._failed_prepare(operation_id, record, engine)
                raise

    def _read(self, principal, kind, method, parameters):
        self._bounded(method, parameters)
        spec, capability, package, store, target = self._spec(kind, method, parameters)
        if (kind == "artifact" and method not in DOWNLOAD_METHODS) or (
            kind == "appstore" and method not in READ_METHODS
        ):
            raise PlayError("Only the dedicated fixed read/download methods are allowed.")
        with self.provider.grant_lock(self.registry._subject(principal)):
            authorization = self._authorize(principal, capability, package, store, target)
            operation_id, record = self.registry._reserve(authorization, kind, capability, package)
            record.method, record.store, record.target = method, store, target
            engine = None
            try:
                engine = self._engine(principal, record, spec, media=kind == "artifact")
                return (
                    engine.download(method, parameters)
                    if kind == "artifact"
                    else engine.query(method, parameters)
                )
            finally:
                self.registry._failed_prepare(operation_id, record, engine)

    def query_appstore(self, principal, method, parameters):
        return self._read(principal, "appstore", method, parameters)

    def download_artifact(self, principal, method, parameters):
        return self._read(principal, "artifact", method, parameters)

    def begin_upload_transfer(
        self, principal, method, parameters, mime_type, size_bytes, sha256, body=None
    ):
        self._bounded(method, parameters, mime_type, size_bytes, sha256, body)
        if not isinstance(method, str):
            raise PlayError("An exact supported binary method is required.")
        kind = (
            "artifact"
            if method in CAPS
            else "sharing"
            if method in SHARING_CAPS
            else "appstore"
            if method in MEDIA
            else None
        )
        if kind is None:
            raise PlayError("Only fixed binary upload methods accept transfer reservations.")
        spec, capability, package, store, target = self._spec(
            kind, method, parameters, body, mime_type
        )
        with self.provider.grant_lock(self.registry._subject(principal)):
            authorization = self._authorize(principal, capability, package, store, target)
            if self.registry._closed:
                raise PlayError("Hosted preparation service is shutting down.")
            owner = authorization.subject, authorization.client_id
            deadline = time.monotonic() + min(600, authorization.expires - time.time())
            return self.transfers.reserve_upload(
                owner,
                self._purpose(spec, capability, package, store, target),
                size_bytes,
                sha256,
                deadline,
            )

    def _transfer_action(self, principal, transfer_id, *, discard=False):
        with self.provider.grant_lock(self.registry._subject(principal)):
            authorization = self.provider.authorize_current(principal)
            owner = authorization.subject, authorization.client_id
            transfers = self.transfers
            purpose = transfers.purpose(owner, transfer_id)
            self._authorize(
                principal, purpose.capability, purpose.package, purpose.store, purpose.target
            )
            if self.registry._closed:
                raise PlayError("Hosted preparation service is shutting down.")
            if discard:
                return {
                    "transfer_id": transfer_id,
                    "disposal_status": transfers.discard(owner, transfer_id),
                }
            return transfers.status(owner, transfer_id)

    def get_transfer_status(self, principal, transfer_id):
        return self._transfer_action(principal, transfer_id)

    def discard_transfer(self, principal, transfer_id):
        return self._transfer_action(principal, transfer_id, discard=True)
