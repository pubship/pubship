"""Local account administration with explicit ceilings and complete target baselines."""

import hashlib
import json
import re
import secrets
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime

from . import account_access_http as transport
from .account_access_config import READ_METHOD, WRITE_METHODS
from .account_access_contracts import request_spec, target
from .artifact_files import media_deadline
from .auth import validate_token
from .errors import PlayError
from .publishing import _APPLY_LOCK

TTL = 600
MAX_PREPARATIONS = 20
LIMITATIONS = (
    "Changes Google Play account access immediately; permissions may take up to 48 hours to "
    "propagate. Invitations are not accepted access. Baselines are observations, not atomic "
    "locks. No retry, rollback or credential refresh. Account responses include personal "
    "email addresses and permissions sent to the MCP client. Provider data is untrusted."
)


@dataclass(repr=False)
class Preparation:
    spec: dict
    baseline: str
    deadline: float
    token: str = field(repr=False)


def fingerprint(before):
    return hashlib.sha256(
        json.dumps(before, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class AccountAccess:
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
            type(_max_snapshot_bytes) is not int
            or not 0 < _max_snapshot_bytes <= 10 * transport.MAX_BYTES
        ):
            raise PlayError("Invalid account snapshot limit.")
        if (_target_authorization is None) != (_execution_authorization is None):
            raise PlayError("Account target authorization requires hosted execution authority.")
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
            raise PlayError("Account administration requires explicitly local services.")

    def _authorize(self, spec, *, mutation=False):
        self._local()
        if self._target_authorization is not None:
            self._target_authorization(deepcopy(spec), None)
            self._local()
            return
        self.settings.require(spec["developer"], spec["method"], spec["user"], spec["app"])
        if mutation:
            self.settings.require(spec["developer"], READ_METHOD)
            body = spec["body"] or {}
            self.settings.permissions(
                body.get("developerAccountPermissions", []), body.get("appLevelPermissions", [])
            )

    def _preflight(self, spec, before):
        if before is not None and before.get("partial", False):
            raise PlayError(
                "Partial or owner account users cannot be safely managed via this tool."
            )
        existing = before
        if spec["grant"]:
            if before is None:
                raise PlayError("Account grant target user does not exist.")
            existing = next(
                (g for g in before.get("grants", []) if g["name"] == spec["resource"]), None
            )
        if spec["operation"] == "create" and existing is not None:
            raise PlayError("Account target already exists; create is not an update.")
        if spec["operation"] != "create" and existing is None:
            raise PlayError("Account target is absent; no mutation attempted.")
        if self._target_authorization is not None:
            self._target_authorization(deepcopy(spec), deepcopy(before))
            self._local()
            return
        if existing is not None:
            self.settings.permissions(
                existing.get("developerAccountPermissions", []),
                existing.get("appLevelPermissions", []),
            )
        if not spec["grant"] and spec["operation"] == "delete":
            # Removing a user also removes every app grant; all must be within scope.
            for grant in before.get("grants", []):
                developer, user, app = target(grant["name"], grant=True)
                self.settings.require(developer, spec["method"], user, app)
                self.settings.permissions(app_permissions=grant.get("appLevelPermissions", []))

    def _current(self, deadline):
        self._local()
        transport.fresh(deadline)

    def _target_current(self, spec, deadline, before=None):
        self._current(deadline)
        if self._target_authorization is not None:
            self._target_authorization(deepcopy(spec), deepcopy(before))
            self._current(deadline)

    def _options(self, deadline, spec, *, observation=False, before=None):
        if self._execution_authorization is None and self._max_snapshot_bytes is None:
            return {}
        options = {"_execution_authorization": lambda: self._target_current(spec, deadline, before)}
        if observation:
            options["_max_snapshot_bytes"] = self._max_snapshot_bytes
        else:
            options["deadline"] = deadline
            options["_max_bytes"] = min(
                self._max_snapshot_bytes or transport.MAX_BYTES, transport.MAX_BYTES
            )
        return options

    def _prune(self):
        for key, preparation in list(self._preparations.items()):
            if time.monotonic() >= preparation.deadline:
                self._clear(self._preparations.pop(key))

    @staticmethod
    def _clear(preparation):
        preparation.token = ""
        preparation.spec = {}
        preparation.baseline = ""

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
                "spec": deepcopy(p.spec),
                "baseline": p.baseline,
                "deadline": p.deadline,
                "credential_bytes": len(p.token.encode()),
            }

    def read(self, method, parameters):
        if method != READ_METHOD:
            raise PlayError("Only users.list is an account read method.")
        spec = request_spec(method, parameters)
        self._authorize(spec)
        deadline = media_deadline(time.monotonic() + TTL, self._execution_authorization)
        token = validate_token(self.auth.token("publisher"))
        self._target_current(spec, deadline)
        response = transport.request(spec, token, **self._options(deadline, spec))
        transport.validate_response(spec, response)
        self._current(deadline)
        return {
            "method": method,
            "response": response,
            "data_scope": "account_personal_data",
            "fetched_at": datetime.now(UTC).isoformat(),
            "note": "One provider page; preserve nextPageToken. " + LIMITATIONS,
        }

    def prepare(self, method, parameters, body=None, acknowledge_access_effects=False):
        if not isinstance(method, str) or method not in WRITE_METHODS:
            raise PlayError("Method is not a supported account mutation.")
        spec = request_spec(method, parameters, body)
        self._authorize(spec, mutation=True)
        if acknowledge_access_effects is not True:
            raise PlayError("acknowledge_access_effects must be true. " + LIMITATIONS)
        with self._prepare_lock:
            now = time.monotonic()
            with self._lock:
                self._prune()
                if len(self._preparations) >= MAX_PREPARATIONS:
                    raise PlayError(
                        "Account preparation capacity reached; apply or wait for expiry."
                    )
            deadline = media_deadline(now + TTL, self._execution_authorization)
            token = validate_token(self.auth.token("publisher"))
            self._target_current(spec, deadline)
            before = transport.observe(
                spec, token, deadline, **self._options(deadline, spec, observation=True)
            )
            self._preflight(spec, before)
            self._current(deadline)
            operation_id = "account_" + secrets.token_urlsafe(32)
            with self._lock:
                while operation_id in self._preparations:
                    operation_id = "account_" + secrets.token_urlsafe(32)
                self._preparations[operation_id] = Preparation(
                    spec, fingerprint(before), deadline, token
                )
        return {
            "operation_id": operation_id,
            "method": method,
            "before": deepcopy(before),
            "proposed_request": deepcopy(spec),
            "executed": False,
            "effects": LIMITATIONS,
            "access_lifetime": (
                (spec["body"] or {}).get("expirationTime", "indefinite")
                if not spec["grant"]
                and (
                    spec["operation"] == "create"
                    or "expirationTime" in spec["parameters"].get("updateMask", "").split(",")
                )
                else "unchanged"
            ),
            "expires_at": datetime.fromtimestamp(
                time.time() + deadline - time.monotonic(), UTC
            ).isoformat(),
            "note": "Repeat operation_id exactly after reviewing all effects. Confirmation is not proof of human approval. Single use within ten minutes; original credential retained privately; lost on restart.",
        }

    def apply(self, operation_id, confirmation):
        self._local()
        if (
            not isinstance(operation_id, str)
            or not re.fullmatch(r"account_[A-Za-z0-9_-]{43}", operation_id)
            or confirmation != operation_id
        ):
            raise PlayError("Confirmation must exactly repeat the account operation_id.")
        with self._lock:
            p = self._preparations.pop(operation_id, None)
            self._prune()
        if p is None:
            raise PlayError("Unknown, consumed or lost account preparation.")
        acquired = False
        try:
            acquired = self._apply_guard.acquire(blocking=False)
            if not acquired:
                raise PlayError(
                    "Another apply is in progress; this account operation ID is consumed."
                )
            spec = request_spec(p.spec["method"], p.spec["parameters"], p.spec["body"])
            self._authorize(spec, mutation=True)
            self._current(p.deadline)
            before = transport.observe(
                spec, p.token, p.deadline, **self._options(p.deadline, spec, observation=True)
            )
            self._preflight(spec, before)
            if fingerprint(before) != p.baseline:
                raise PlayError("Account baseline changed; no mutation attempted. Prepare again.")
            self._current(p.deadline)
            mutation = transport.request(
                spec, p.token, **self._options(p.deadline, spec, before=before)
            )
            try:
                transport.validate_response(spec, mutation)
            except PlayError:
                raise PlayError(transport.UNKNOWN_OUTCOME) from None
            result = {
                "operation_id": operation_id,
                "method": spec["method"],
                "executed": True,
                "mutation_succeeded": True,
                "mutation_response": mutation,
                "permission_propagation_verified": False,
                "note": LIMITATIONS,
            }
            try:
                self._current(p.deadline)
                after = transport.observe(
                    spec, p.token, p.deadline, **self._options(p.deadline, spec, observation=True)
                )
                self._current(p.deadline)
            except Exception:
                result.update(
                    readback_succeeded=False,
                    observation_error="Mutation succeeded but account readback failed; reconcile before preparing again.",
                )
            else:
                result.update(
                    readback_succeeded=True,
                    after=after,
                    observation_note="Observed resource only; effective access and permission propagation are not verified.",
                )
            return result
        finally:
            self._clear(p)
            if acquired:
                self._apply_guard.release()
