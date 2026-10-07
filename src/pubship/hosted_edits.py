"""Bounded process-only preparations, isolated by authenticated connection and client."""

import json
import math
import secrets
import threading
import time
import weakref
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from .config import Settings
from .edit_lifecycle import EditLifecycle
from .edit_writes import EditWrites
from .errors import PlayError
from .hosted_cancellation import check_cancelled
from .hosted_catalog import (
    HOSTED_COMMERCE_CAPABILITIES,
    HOSTED_COMMERCE_QUERY_METHODS,
    HOSTED_EDIT_RESOURCE_METHODS,
    HOSTED_PURCHASE_CAPABILITIES,
    HOSTED_SUPPORT_CAPABILITIES,
    HOSTED_TESTER_WRITE_METHODS,
)
from .monetization import Monetization
from .publishing import Publishing
from .purchase import PurchaseLifecycle
from .purchase_contracts import package_for_spec
from .purchase_contracts import request_spec as purchase_request_spec
from .support import SupportOperations
from .track_staging import TrackStaging

CAPABILITIES = {
    "listing": "play:edit-listings",
    "track": "play:edit-tracks",
    "create": "play:edit-create",
    "validate": "play:edit-validate",
    "discard": "play:edit-discard",
    "commit": "play:edit-commit",
}
MAX_REQUEST_BYTES = 256 * 1024
# Reserve before provider I/O. Snapshots are independently limited by the engines.
MAX_OPERATION_BYTES = 512 * 1024


def _size(value):
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError, UnicodeError):
        raise PlayError("Preparation must contain bounded JSON data.") from None


@dataclass(repr=False)
class _Record:
    owner: tuple[str, str]
    kind: str
    capability: str
    package: str | None
    deadline: float
    retained_bytes: int = MAX_OPERATION_BYTES
    engine: object = None
    inner_id: str = ""
    guard: object = None
    applying: bool = False
    method: str | None = None
    store: str | None = None
    target: str | None = None
    preparing: bool = True
    disposal_pending: bool = False
    disposing: bool = False


class _ExecutionAuthorization:
    """Internal engine hook activated only under the connection's grant lock."""

    def __init__(self, registry, capability, package, deadline, *, store=None, target=None):
        self.registry, self.capability, self.package = registry, capability, package
        self.principal = None
        self.deadline = deadline
        self.store, self.target = store, target

    def __call__(self):
        check_cancelled()
        if self.registry._closed or self.principal is None:
            raise PlayError("Hosted preparation is inactive or the service is shutting down.")
        if self.store is not None:
            self.registry.provider.authorize_appstore_current(
                self.principal, self.capability, self.store, self.target
            )
        else:
            self.registry.provider.authorize_current(self.principal, self.capability, self.package)
        check_cancelled()
        # Current authority may outlive the original preparation after refresh.
        # Check after authorization, including create's final no-preflight hook.
        if self.registry._closed:
            raise PlayError("Hosted preparation service is shutting down.")
        if time.monotonic() >= self.deadline:
            raise PlayError("Preparation expired; prepare again. No mutation was attempted.")


class HostedEditRegistry:
    """One engine per preparation, no persistent payloads and no fallback credentials.

    The short state lock never spans network I/O. The provider grant lock orders
    a connection's calls against disconnect, independently of other connections.
    Byte limits measure serialized retained records, not Python allocator overhead.
    """

    def __init__(
        self,
        provider,
        *,
        transfers=None,
        cleanup_interval=30,
        max_per_connection=20,
        max_total=200,
        max_payload_bytes_per_connection=1024 * 1024,
        max_payload_bytes_total=8 * 1024 * 1024,
    ):
        if (
            type(cleanup_interval) not in (int, float)
            or not math.isfinite(cleanup_interval)
            or cleanup_interval <= 0
            or any(
                type(n) is not int or n < 1
                for n in (
                    max_per_connection,
                    max_total,
                    max_payload_bytes_per_connection,
                    max_payload_bytes_total,
                )
            )
        ):
            raise ValueError("Registry limits and cleanup interval must be positive.")
        self.provider = provider
        self.transfers = transfers
        self._limits = (
            max_per_connection,
            max_total,
            max_payload_bytes_per_connection,
            max_payload_bytes_total,
        )
        self._records = {}
        self._guards = weakref.WeakValueDictionary()
        self._lock = threading.Lock()
        self._closed = False
        self._stop = threading.Event()
        provider.register_invalidator(self.invalidate)
        self._worker = threading.Thread(
            target=self._periodic_cleanup,
            args=(cleanup_interval,),
            daemon=True,
            name="hosted-preparation-cleanup",
        )
        self._worker.start()
        from .hosted_distribution import HostedDistribution

        self.distribution = HostedDistribution(self)
        from .hosted_administration import HostedAdministration

        self.administration = HostedAdministration(self)

    def _periodic_cleanup(self, interval):
        while not self._stop.wait(interval):
            self.cleanup()

    @staticmethod
    def _erase(record):
        """No registry lock is held while disposing an engine or a file lease."""
        engine = record.engine
        if engine is None:
            return "disposed"
        engine._execution_authorization.principal = None
        dispose = getattr(engine, "_dispose", None)
        if dispose is not None:
            with engine._lock:
                ids = list(engine._preparations)
            statuses = [dispose(inner_id) for inner_id in ids or [record.inner_id]]
            if any(status != "disposed" for status in statuses):
                return "cleanup-pending"
        else:
            # Only the fixed pre-media engines may use this compatibility adapter.
            if record.kind not in {
                "listing",
                "track",
                "lifecycle",
                "edit_resource",
                "commerce",
                "support",
                "purchase",
            }:
                raise PlayError("Engine requires an explicit disposal adapter.")
            with engine._lock:
                engine._preparations.clear()
        close = getattr(engine, "close", None)
        if close is not None:
            close()
        return "disposed"

    def _remove(self, operation_id):
        """Mark under the state lock; retained quota is released after disposal."""
        record = self._records.get(operation_id)
        if record is not None:
            record.disposal_pending = True

    def _drain_disposals(self):
        with self._lock:
            pending = [
                (key, record)
                for key, record in self._records.items()
                if record.disposal_pending
                and not record.preparing
                and not record.applying
                and not record.disposing
            ]
            for _, record in pending:
                record.disposing = True
        for key, record in pending:
            try:
                status = self._erase(record)
            except Exception:
                status = "cleanup-pending"
            with self._lock:
                record.disposing = False
                if status == "disposed" and self._records.get(key) is record:
                    self._records.pop(key)
                    record.engine = None

    def cleanup(self):
        with self._lock:
            now = time.monotonic()
            for key, record in list(self._records.items()):
                if record.deadline <= now:
                    self._remove(key)
        self._drain_disposals()

    def invalidate(self, subject):
        with self._lock:
            for key, record in list(self._records.items()):
                if record.owner[0] == subject:
                    self._remove(key)
        self._drain_disposals()

    def close(self):
        self._stop.set()
        with self._lock:
            self._closed = True
            for key in list(self._records):
                self._remove(key)
        self._drain_disposals()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=1)

    @staticmethod
    def _subject(principal):
        subject = getattr(principal, "subject", None)
        if not isinstance(subject, str) or not subject:
            raise PlayError("Customer authorization is required.")
        return subject

    def _reserve(self, authorization, kind, capability, package, *, _cleanup=True):
        if _cleanup:
            self.cleanup()
        owner = (authorization.subject, authorization.client_id)
        with self._lock:
            if self._closed:
                raise PlayError("Hosted preparation service is shutting down.")
            owned = [r for r in self._records.values() if r.owner == owner]
            per_count, total_count, per_bytes, total_bytes = self._limits
            if (
                len(owned) >= per_count
                or len(self._records) >= total_count
                or sum(r.retained_bytes for r in owned) + MAX_OPERATION_BYTES > per_bytes
                or sum(r.retained_bytes for r in self._records.values()) + MAX_OPERATION_BYTES
                > total_bytes
            ):
                raise PlayError("Hosted preparation capacity reached; apply or wait for cleanup.")
            guard = self._guards.get(owner)
            if guard is None:
                guard = threading.Lock()
                self._guards[owner] = guard
            operation_id = "hosted_" + secrets.token_urlsafe(32)
            while operation_id in self._records:
                operation_id = "hosted_" + secrets.token_urlsafe(32)
            record = _Record(
                owner,
                kind,
                capability,
                package,
                time.monotonic() + min(600, authorization.expires - time.time()),
                guard=guard,
            )
            self._records[operation_id] = record
            return operation_id, record

    def _complete_prepare(self, operation_id, record, engine, result):
        inner_id = result["operation_id"]
        retained = engine._preparations[inner_id]
        hook = engine._execution_authorization
        effective_deadline = min(record.deadline, retained.deadline)
        retained.deadline = hook.deadline = effective_deadline
        metadata = getattr(engine, "_retained_metadata", None)
        if metadata is not None:
            values = metadata(inner_id)
            credential_bytes = values.get("credential_bytes", 0)
            private_payload_bytes = values.get("private_payload_bytes", 0)
            if any(
                type(count) is not int or count < 0
                for count in (credential_bytes, private_payload_bytes)
            ):
                raise PlayError("Invalid engine private-payload accounting.")
            retained_bytes = _size(values) + credential_bytes + private_payload_bytes
        elif record.kind in {
            "listing",
            "track",
            "lifecycle",
            "edit_resource",
            "commerce",
            "support",
            "purchase",
        }:
            retained_bytes = _size(asdict(retained))
        else:
            raise PlayError("Engine requires explicit retained metadata.")
        retained_bytes += _size(
            [
                record.owner,
                record.kind,
                record.capability,
                record.package,
                record.store,
                record.target,
                record.method,
            ]
        )
        if retained_bytes > MAX_OPERATION_BYTES:
            raise PlayError("Hosted preparation exceeds its retained-payload limit.")
        hook()
        hook.principal = None
        with self._lock:
            if (
                self._closed
                or self._records.get(operation_id) is not record
                or record.disposal_pending
            ):
                raise PlayError("Preparation expired or service stopped during preparation.")
            record.deadline = effective_deadline
            if record.deadline <= time.monotonic():
                raise PlayError("Preparation expired during provider reads; prepare again.")
            record.engine, record.inner_id = engine, inner_id
            record.retained_bytes = retained_bytes
            record.preparing = False
        result["operation_id"] = operation_id
        result["expires_at"] = datetime.fromtimestamp(
            time.time() + record.deadline - time.monotonic(), UTC
        ).isoformat()
        result["note"] += (
            " Hosted preparations are connection/client bound. Expiry, disconnect and shutdown invalidate them; cleanup retries retain charged reservations until released."
        )
        return result

    def _failed_prepare(self, operation_id, record, engine, *, drain=True):
        with self._lock:
            record.engine = engine
            record.preparing = False
            self._remove(operation_id)
        if drain:
            self._drain_disposals()

    def prepare(
        self,
        principal,
        kind,
        parameters,
        payload=None,
        acknowledge_effects=False,
        action=None,
        *,
        method=None,
        observation_version_code=None,
        upload_handle=None,
        mime_type=None,
    ):
        if kind in {"account", "signing"}:
            if any(
                value is not None
                for value in (action, observation_version_code, upload_handle, mime_type)
            ):
                raise PlayError(
                    "Account and signing operations do not accept media or edit inputs."
                )
            return self.administration.prepare(
                principal, kind, method, parameters, payload, acknowledge_effects
            )
        if kind in {"artifact", "sharing", "appstore"}:
            if action is not None or observation_version_code is not None:
                raise PlayError(
                    "Distribution preparations do not accept edit actions or observation versions."
                )
            return self.distribution.prepare(
                principal,
                kind,
                method,
                parameters,
                payload,
                acknowledge_effects,
                upload_handle,
                mime_type,
            )
        if upload_handle is not None or mime_type is not None:
            raise PlayError("Only distribution operations accept media inputs.")
        if kind not in {
            "listing",
            "track",
            "lifecycle",
            "edit_resource",
            "commerce",
            "support",
            "purchase",
        }:
            raise PlayError("Unsupported hosted preparation kind.")
        if observation_version_code is not None and kind != "support":
            raise PlayError("Only support operations accept observation_version_code.")
        if kind == "edit_resource":
            if not isinstance(method, str) or method not in (
                HOSTED_EDIT_RESOURCE_METHODS | HOSTED_TESTER_WRITE_METHODS
            ):
                raise PlayError("This edit resource method is not enabled for hosted execution.")
            capability = (
                "play:tester-audience"
                if method in HOSTED_TESTER_WRITE_METHODS
                else "play:edit-resources"
            )
        elif kind in {"support", "purchase"}:
            methods = (
                HOSTED_SUPPORT_CAPABILITIES if kind == "support" else HOSTED_PURCHASE_CAPABILITIES
            )
            if not isinstance(method, str) or method not in methods:
                raise PlayError("This method is not enabled for this hosted operation family.")
            capability = methods[method]
        elif kind == "commerce":
            if not isinstance(method, str) or method not in HOSTED_COMMERCE_CAPABILITIES:
                raise PlayError("This commerce method is not enabled for hosted execution.")
            capability = HOSTED_COMMERCE_CAPABILITIES[method]
        else:
            if method is not None:
                raise PlayError("This preparation kind does not accept a method argument.")
            capability = CAPABILITIES.get(action if kind == "lifecycle" else kind)
        if capability is None or (
            kind == "lifecycle" and action not in {"create", "validate", "discard", "commit"}
        ):
            raise PlayError("Unsupported hosted edit action.")
        if _size([parameters, payload, observation_version_code]) > MAX_REQUEST_BYTES:
            raise PlayError("Hosted preparation request exceeds 256 KiB.")
        if kind == "purchase":
            # External transactions identify apps through validated parent/name paths.
            # Never authorize an unrelated caller-supplied packageName instead.
            package = package_for_spec(purchase_request_spec(method, parameters, payload))
        else:
            if not isinstance(parameters, dict) or not isinstance(
                parameters.get("packageName"), str
            ):
                raise PlayError("An exact packageName is required.")
            package = parameters["packageName"]
        with self.provider.grant_lock(self._subject(principal)):
            authorization = self.provider.authorize_current(principal, capability, package)
            operation_id, record = self._reserve(authorization, kind, capability, package)
            record.method = method
            engine = None
            try:
                _, auth = self.provider.services(principal)
                packages = frozenset({package})
                settings = Settings(
                    packages=packages,
                    # Target observations needed for an authorized financial action
                    # do not grant the separate raw sensitive-read tool capability.
                    sensitive_read_packages=packages
                    if capability in {"play:purchase-actions", "play:external-transactions"}
                    else frozenset(),
                    support_packages=packages if kind == "support" else frozenset(),
                    support_methods=frozenset({method}) if kind == "support" else frozenset(),
                    purchase_packages=packages
                    if capability == "play:purchase-actions"
                    else frozenset(),
                    purchase_methods=frozenset({method})
                    if capability == "play:purchase-actions"
                    else frozenset(),
                    external_transaction_packages=packages
                    if capability == "play:external-transactions"
                    else frozenset(),
                    external_transaction_methods=frozenset({method})
                    if capability == "play:external-transactions"
                    else frozenset(),
                    price_migration_packages=packages
                    if capability == "play:subscriber-price-migration"
                    else frozenset(),
                    price_migration_methods=frozenset({method})
                    if capability == "play:subscriber-price-migration"
                    else frozenset(),
                    write_packages=packages if kind in {"listing", "lifecycle"} else frozenset(),
                    track_packages=packages if kind == "track" else frozenset(),
                    edit_packages=packages if kind == "lifecycle" else frozenset(),
                    edit_write_packages=packages if kind == "edit_resource" else frozenset(),
                    edit_write_methods=frozenset({method})
                    if kind == "edit_resource"
                    else frozenset(),
                    monetization_packages=packages if kind == "commerce" else frozenset(),
                    monetization_methods=frozenset(
                        m for m, cap in HOSTED_COMMERCE_CAPABILITIES.items() if cap == capability
                    )
                    if kind == "commerce"
                    else frozenset(),
                )
                hook = _ExecutionAuthorization(self, capability, package, record.deadline)
                hook.principal = principal
                engine_type = {
                    "listing": Publishing,
                    "track": TrackStaging,
                    "lifecycle": EditLifecycle,
                    "edit_resource": EditWrites,
                    "commerce": Monetization,
                    "support": SupportOperations,
                    "purchase": PurchaseLifecycle,
                }[kind]
                extra = (
                    {"_max_snapshot_bytes": MAX_REQUEST_BYTES}
                    if kind in {"edit_resource", "commerce", "support", "purchase"}
                    else {}
                )
                engine = engine_type(
                    settings,
                    auth,
                    _execution_authorization=hook,
                    _apply_guard=record.guard,
                    **extra,
                )
                with self._lock:
                    record.engine = engine
                if kind == "listing":
                    result = engine.prepare(parameters, payload)
                elif kind == "track":
                    result = engine.prepare(parameters, payload, acknowledge_effects)
                elif kind == "support":
                    result = engine.prepare(
                        record.method,
                        parameters,
                        payload,
                        acknowledge_effects,
                        observation_version_code,
                    )
                elif kind in {"edit_resource", "commerce", "purchase"}:
                    result = engine.prepare(record.method, parameters, payload, acknowledge_effects)
                else:
                    result = engine.prepare(action, parameters, acknowledge_effects)
                return self._complete_prepare(operation_id, record, engine, result)
            except BaseException:
                self._failed_prepare(operation_id, record, engine)
                raise

    def query_monetization(self, principal, method, parameters, body):
        if not isinstance(method, str) or method not in HOSTED_COMMERCE_QUERY_METHODS:
            raise PlayError("This commerce query is not enabled for hosted execution.")
        if not isinstance(parameters, dict) or not isinstance(parameters.get("packageName"), str):
            raise PlayError("An exact packageName is required.")
        if _size([parameters, body]) > MAX_REQUEST_BYTES:
            raise PlayError("Hosted commerce query exceeds 256 KiB.")
        package = parameters["packageName"]
        with self.provider.grant_lock(self._subject(principal)):
            authorization = self.provider.authorize_current(
                principal, "play:commerce-query", package
            )
            _, auth = self.provider.services(principal)
            hook = _ExecutionAuthorization(
                self,
                "play:commerce-query",
                package,
                time.monotonic() + min(600, authorization.expires - time.time()),
            )
            hook.principal = principal
            try:
                return Monetization(
                    Settings(packages=frozenset({package})), auth, _execution_authorization=hook
                ).query(method, parameters, body)
            finally:
                hook.principal = None

    def apply(self, principal, kind, operation_id, confirmation):
        result = None
        try:
            result = self._apply(principal, kind, operation_id, confirmation)
            return result
        finally:
            self._drain_disposals()
            with self._lock:
                pending = (
                    isinstance(operation_id, str)
                    and operation_id in self._records
                    and self._records[operation_id].disposal_pending
                )
            if result is not None and pending:
                result.setdefault(
                    "cleanup_warning",
                    "The operation result is retained; private resource cleanup is pending and remains charged.",
                )

    def _apply(self, principal, kind, operation_id, confirmation):
        if not isinstance(operation_id, str) or confirmation != operation_id:
            raise PlayError("Confirmation must exactly repeat operation_id.")
        with self.provider.grant_lock(self._subject(principal)):
            authorization = self.provider.authorize_current(principal)
            owner = (authorization.subject, authorization.client_id)
            with self._lock:
                record = self._records.get(operation_id)
                # Checking owner before consumption prevents ID theft from denying service.
                if (
                    record is None
                    or record.owner != owner
                    or record.kind != kind
                    or record.applying
                    or record.disposal_pending
                ):
                    raise PlayError("Unknown, consumed or lost preparation; prepare again.")
                if record.engine is None or record.preparing:
                    raise PlayError("Preparation has not completed.")
                if self._closed or record.deadline <= time.monotonic():
                    self._remove(operation_id)
                    raise PlayError("Preparation expired; prepare again.")
            if record.kind in {"account", "signing"}:
                self.administration.authorize_record(principal, record)
            elif record.store is not None:
                self.provider.authorize_appstore_current(
                    principal, record.capability, record.store, record.target
                )
            else:
                self.provider.authorize_current(principal, record.capability, record.package)
            with self._lock:
                if self._records.get(operation_id) is not record or record.disposal_pending:
                    raise PlayError("Preparation expired; prepare again.")
                if self._closed or record.deadline <= time.monotonic():
                    self._remove(operation_id)
                    raise PlayError("Preparation expired; prepare again.")
                record.applying = True
            engine = record.engine
            engine._execution_authorization.principal = principal
            try:
                result = engine.apply(record.inner_id, record.inner_id)
                result["operation_id"] = operation_id
                return result
            finally:
                with self._lock:
                    record.applying = False
                    self._remove(operation_id)

    def read_account_access(self, principal, method, parameters):
        return self.administration.read(principal, method, parameters)

    def query_appstore(self, principal, method, parameters):
        return self.distribution.query_appstore(principal, method, parameters)

    def download_artifact(self, principal, method, parameters):
        return self.distribution.download_artifact(principal, method, parameters)

    def begin_upload_transfer(
        self, principal, method, parameters, mime_type, size_bytes, sha256, body=None
    ):
        return self.distribution.begin_upload_transfer(
            principal, method, parameters, mime_type, size_bytes, sha256, body
        )

    def get_transfer_status(self, principal, transfer_id):
        return self.distribution.get_transfer_status(principal, transfer_id)

    def discard_transfer(self, principal, transfer_id):
        return self.distribution.discard_transfer(principal, transfer_id)
