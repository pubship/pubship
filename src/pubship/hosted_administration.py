"""Exact account/signing policies inside the shared hosted preparation lifecycle."""

import time

from .account_access import AccountAccess
from .account_access_config import READ_METHOD, AccountAccessSettings
from .account_access_contracts import request_spec as account_spec
from .account_access_contracts import target as account_target
from .errors import PlayError
from .hosted_cancellation import check_cancelled
from .hosted_edits import MAX_REQUEST_BYTES, _size
from .hosted_grants import ACCOUNT_METHOD_CAPABILITIES, SIGNING_METHOD_CAPABILITIES
from .signing import Signing, SigningSettings
from .signing import request_spec as signing_spec


def _identity(kind, spec):
    if kind == "account":
        return spec["method"], spec["developer"], spec["user"], spec["app"]
    return spec["method"], spec["app"], spec["kms"]


class _AdministrationAuthorization:
    def __init__(self, registry, kind, spec, deadline):
        self.registry, self.kind = registry, kind
        self.identity, self.deadline = _identity(kind, spec), deadline
        self.principal = None

    def authorize(self, principal):
        check_cancelled()
        if self.registry._closed or principal is None:
            raise PlayError("Hosted administration is inactive or shutting down.")
        function = (
            self.registry.provider.authorize_account_current
            if self.kind == "account"
            else self.registry.provider.authorize_signing_current
        )
        authorization = function(principal, *self.identity)
        check_cancelled()
        if self.registry._closed or time.monotonic() >= self.deadline:
            raise PlayError("Hosted administration preparation expired; prepare again.")
        return authorization

    def __call__(self):
        return self.authorize(self.principal)

    def target(self, spec, before=None):
        if _identity(self.kind, spec) != self.identity:
            raise PlayError("Administrative preparation target changed.")
        authorization = self()
        if self.kind == "account":
            _account_policy(authorization, spec, before)
        # Preserve the original deadline after policy work, even if a current
        # connection has been refreshed or narrowed since preparation.
        self()


def _within(values, ceiling):
    if (
        not isinstance(values, list)
        or any(not isinstance(value, str) or value not in ceiling for value in values)
        or len(values) != len(set(values))
    ):
        raise PlayError("Account operation exceeds its exact permission ceiling.")


def _account_policy(authorization, spec, before):
    """Existing affected access and requested replacements each need exact consent."""
    if spec["method"] == READ_METHOD:
        return
    developer, user, app = spec["developer"], spec["user"], spec["app"]
    body = spec["body"] or {}
    if before is not None:
        if (
            not isinstance(before, dict)
            or before.get("name") != f"developers/{developer}/users/{user}"
            or before.get("email") != user
        ):
            raise PlayError("Account observation does not match its exact target.")
    if spec["grant"]:
        row = authorization.account_grant_targets[developer, user, app]
        _within(body.get("appLevelPermissions", []), row.app_permissions)
        if before is not None:
            existing = next(
                (grant for grant in before.get("grants", []) if grant["name"] == spec["resource"]),
                None,
            )
            if existing is not None:
                _within(existing.get("appLevelPermissions", []), row.app_permissions)
        return
    row = authorization.account_user_targets[developer, user]
    _within(body.get("developerAccountPermissions", []), row.developer_permissions)
    expiry = spec["operation"] == "patch" and "expirationTime" in spec["parameters"].get(
        "updateMask", ""
    ).split(",")
    if expiry and not row.allow_expiration_change:
        raise PlayError("Changing account access lifetime requires separate explicit consent.")
    if before is not None:
        _within(before.get("developerAccountPermissions", []), row.developer_permissions)
        if spec["operation"] == "delete" or expiry:
            for grant in before.get("grants", []):
                observed_developer, observed_user, affected_app = account_target(
                    grant["name"], grant=True
                )
                if (observed_developer, observed_user) != (
                    developer,
                    user,
                ) or affected_app not in row.affected_apps:
                    raise PlayError(
                        "Existing affected app access is outside this user's exact consent."
                    )
                _within(grant.get("appLevelPermissions", []), row.affected_apps[affected_app])


class HostedAdministration:
    def __init__(self, registry):
        self.registry, self.provider = registry, registry.provider

    @staticmethod
    def _spec(kind, method, parameters, body):
        if _size([method, parameters, body]) > MAX_REQUEST_BYTES:
            raise PlayError("Hosted administration request exceeds 256 KiB.")
        methods = ACCOUNT_METHOD_CAPABILITIES if kind == "account" else SIGNING_METHOD_CAPABILITIES
        if not isinstance(method, str) or method not in methods:
            raise PlayError("Unsupported exact hosted administration method.")
        spec = (
            account_spec(method, parameters, body)
            if kind == "account"
            else signing_spec(method, parameters, body)
        )
        return spec, methods[method]

    def _authorize(self, principal, kind, spec):
        function = (
            self.provider.authorize_account_current
            if kind == "account"
            else self.provider.authorize_signing_current
        )
        return function(principal, *_identity(kind, spec))

    def authorize_record(self, principal, record):
        hook = record.engine._execution_authorization
        if not isinstance(hook, _AdministrationAuthorization) or hook.kind != record.kind:
            raise PlayError("Administrative preparation authority is unavailable.")
        return hook.authorize(principal)

    def _engine(self, principal, kind, spec, record):
        _, auth = self.provider.services(principal)
        hook = _AdministrationAuthorization(self.registry, kind, spec, record.deadline)
        hook.principal = principal
        hook.target(spec)
        cls, settings = (
            (AccountAccess, AccountAccessSettings())
            if kind == "account"
            else (Signing, SigningSettings())
        )
        engine = cls(
            settings,
            auth,
            _execution_authorization=hook,
            _target_authorization=hook.target,
            _apply_guard=record.guard,
            _max_snapshot_bytes=MAX_REQUEST_BYTES,
        )
        with self.registry._lock:
            record.engine = engine
        return engine

    def prepare(self, principal, kind, method, parameters, body, acknowledge_effects):
        spec, capability = self._spec(kind, method, parameters, body)
        if kind == "account" and method == READ_METHOD:
            raise PlayError("Use read_account_access for directory reads.")
        self.registry.cleanup()
        operation_id, record, engine = None, None, None
        try:
            with self.provider.grant_lock(self.registry._subject(principal)):
                authorization = self._authorize(principal, kind, spec)
                operation_id, record = self.registry._reserve(
                    authorization, kind, capability, None, _cleanup=False
                )
                record.method = method
                try:
                    engine = self._engine(principal, kind, spec, record)
                    result = engine.prepare(method, parameters, body, acknowledge_effects)
                    return self.registry._complete_prepare(operation_id, record, engine, result)
                except BaseException:
                    self.registry._failed_prepare(operation_id, record, engine, drain=False)
                    raise
        finally:
            # Engine cleanup may take its own locks. Never retain the grant or
            # registry lock while draining disposal or releasing charged memory.
            self.registry._drain_disposals()

    def read(self, principal, method, parameters):
        spec, capability = self._spec("account", method, parameters, None)
        if method != READ_METHOD:
            raise PlayError("Only users.list is an account directory read.")
        self.registry.cleanup()
        record, operation_id, engine = None, None, None
        try:
            with self.provider.grant_lock(self.registry._subject(principal)):
                authorization = self._authorize(principal, "account", spec)
                operation_id, record = self.registry._reserve(
                    authorization, "account", capability, None, _cleanup=False
                )
                record.method = method
                try:
                    engine = self._engine(principal, "account", spec, record)
                    return engine.read(method, parameters)
                finally:
                    self.registry._failed_prepare(operation_id, record, engine, drain=False)
        finally:
            self.registry._drain_disposals()
