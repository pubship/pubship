"""Explicit connection capabilities; no environment or credential discovery."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from .account_access_config import (
    DEVELOPER,
    READ_METHOD,
    WRITE_METHODS,
    permission_values,
    valid_app,
    valid_email,
)
from .auth import SCOPES
from .config import PACKAGE
from .errors import PlayError
from .signing import KMS
from .signing import METHODS as SIGNING_METHODS
from .signing import _app as signing_app

READ_SCOPE = "play:read"
CAPABILITIES = MappingProxyType(
    {
        "play:review-replies": "Publish replies to customer reviews and read the related review conversation",
        "play:app-declarations": "Submit your app's Data safety declaration to Google",
        "play:app-operations": "Create device configurations and system APK variants; create, target, deploy or cancel app recovery actions",
        "play:tester-audience": "Read tester Google Group addresses and replace tester groups in an existing edit",
        "play:customer-data": "Read customer purchase and order data, potentially including personal information, addresses and purchase tokens, into this MCP client",
        "play:purchase-actions": "Acknowledge or consume purchases; cancel, defer or revoke subscriptions; request refunds or refund reviews",
        "play:external-transactions": "Report external payments and refunds to Google (does not transfer funds)",
        "play:subscriber-price-migration": "Migrate existing subscribers to new prices, which can trigger Google's customer notifications",
        "play:edit-resources": "Stage store configuration, resource replacement and deletion in an existing edit",
        "play:commerce-query": "Read regional price estimates and catalog offers without changing prices",
        "play:subscription-catalog": "Change the live subscription catalog, including activation, deactivation and deletion",
        "play:onetime-catalog": "Change the live one-time product catalog, including activation, cancellation and deletion",
        "play:legacy-product-catalog": "Change or delete live legacy managed products",
        "play:publisher-metadata": "Read Publisher catalog and configuration metadata",
        "play:edit-listings": "Stage listing text in an existing edit",
        "play:edit-tracks": "Replace an existing edit's complete track releases",
        "play:edit-create": "Create an edit (can invalidate the same user's other edit)",
        "play:edit-validate": "Validate an entire edit with Google",
        "play:edit-discard": "Discard an entire edit, including changes made elsewhere",
        "play:edit-commit": "Commit an entire edit and potentially submit other Console changes for review",
    }
)
# This immutable ceiling is the persisted v1 contract, not the current tool list.
V1_SCOPES = frozenset({READ_SCOPE, *CAPABILITIES})
APPSTORE_PAIR_SCOPES = frozenset(
    {
        "play:appstore-catalog",
        "play:appstore-management",
        "play:appstore-media",
    }
)
APPSTORE_EVENTS = "play:appstore-events"
APPSTORE_SCOPES = APPSTORE_PAIR_SCOPES | {APPSTORE_EVENTS}
CAPABILITIES = MappingProxyType(
    {
        **CAPABILITIES,
        "play:edit-artifacts": "Upload artifacts and stage external APK declarations in an existing edit",
        "play:artifact-downloads": "Download generated and system APK binaries",
        "play:internal-sharing": "Upload binaries to internal sharing and return sensitive sharing links",
        "play:appstore-catalog": "Read exact store/app views, including delivery tokens, into this MCP client",
        "play:appstore-events": "Read a store-wide event feed, including apps outside individual app selections",
        "play:appstore-management": "Submit complete app details and change publication status for exact store/app pairs",
        "play:appstore-media": "Upload APKs, images and policy documents for exact store/app pairs",
    }
)
V2_SCOPES = frozenset({READ_SCOPE, *CAPABILITIES})
ACCOUNT_DIRECTORY = "play:account-directory"
ACCOUNT_USERS = "play:account-users"
ACCOUNT_GRANTS = "play:account-grants"
SIGNING_ENROLL = "play:signing-enroll"
SIGNING_ROTATE = "play:signing-rotate"
ACCOUNT_SCOPES = frozenset({ACCOUNT_DIRECTORY, ACCOUNT_USERS, ACCOUNT_GRANTS})
SIGNING_SCOPES = frozenset({SIGNING_ENROLL, SIGNING_ROTATE})
ADMIN_SCOPES = ACCOUNT_SCOPES | SIGNING_SCOPES
ADMIN_FIELDS = frozenset(
    {
        "account_directory_developers",
        "account_user_targets",
        "account_grant_targets",
        "signing_targets",
    }
)
USER_METHODS = frozenset(m for m in WRITE_METHODS if ".users." in m)
GRANT_METHODS = WRITE_METHODS - USER_METHODS
ACCOUNT_METHOD_CAPABILITIES = {
    READ_METHOD: ACCOUNT_DIRECTORY,
    **dict.fromkeys(USER_METHODS, ACCOUNT_USERS),
    **dict.fromkeys(GRANT_METHODS, ACCOUNT_GRANTS),
}
SIGNING_METHOD_CAPABILITIES = {
    method: SIGNING_ENROLL if method.endswith(".enrollApp") else SIGNING_ROTATE
    for method in SIGNING_METHODS
}
CAPABILITIES = MappingProxyType(
    {
        **CAPABILITIES,
        ACCOUNT_DIRECTORY: "Read an account-wide directory page, including emails, access states and permissions, into this MCP client",
        ACCOUNT_USERS: "Change exact account users within selected methods and permission ceilings; inspect their complete target-user access; developer permissions can affect the whole account",
        ACCOUNT_GRANTS: "Change exact user/app grants within selected methods and permission ceilings; inspect the target user's access",
        SIGNING_ENROLL: "Enroll exact app/KMS-version pairs in enterprise self-hosted signing",
        SIGNING_ROTATE: "Rotate signing keys for exact enterprise app/KMS-version pairs",
    }
)
ALL_SCOPES = frozenset({READ_SCOPE, *CAPABILITIES})
ORDINARY_SCOPES = V2_SCOPES - APPSTORE_SCOPES
INVALID_GRANT = "Customer authorization is invalid, expired or revoked. Reconnect."


@dataclass(frozen=True)
class UserAuthority:
    methods: frozenset[str]
    developer_permissions: frozenset[str]
    affected_apps: Mapping[str, frozenset[str]]
    allow_expiration_change: bool

    def __post_init__(self):
        object.__setattr__(self, "methods", frozenset(self.methods))
        object.__setattr__(self, "developer_permissions", frozenset(self.developer_permissions))
        object.__setattr__(
            self,
            "affected_apps",
            MappingProxyType(
                {key: frozenset(values) for key, values in self.affected_apps.items()}
            ),
        )


@dataclass(frozen=True)
class GrantTargetAuthority:
    methods: frozenset[str]
    app_permissions: frozenset[str]

    def __post_init__(self):
        object.__setattr__(self, "methods", frozenset(self.methods))
        object.__setattr__(self, "app_permissions", frozenset(self.app_permissions))


def _freeze_admin(value):
    object.__setattr__(
        value, "account_directory_developers", frozenset(value.account_directory_developers)
    )
    for name in ("account_user_targets", "account_grant_targets"):
        object.__setattr__(value, name, MappingProxyType(dict(getattr(value, name))))
    object.__setattr__(
        value,
        "signing_targets",
        MappingProxyType({key: frozenset(pairs) for key, pairs in value.signing_targets.items()}),
    )


@dataclass(frozen=True)
class ConnectionAuthorization:
    subject: str
    client_id: str
    capability_packages: Mapping[str, frozenset[str]]
    expires: float
    scopes: frozenset[str]
    google_services: frozenset[str]
    bucket_configured: bool
    appstore_targets: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)
    appstore_event_stores: frozenset[str] = frozenset()
    authorization_schema_version: int = 1
    binary_transfers_enabled: bool = False
    account_directory_developers: frozenset[str] = frozenset()
    account_user_targets: Mapping[tuple[str, str], UserAuthority] = field(default_factory=dict)
    account_grant_targets: Mapping[tuple[str, str, str], GrantTargetAuthority] = field(
        default_factory=dict
    )
    signing_targets: Mapping[str, frozenset[tuple[str, str]]] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "capability_packages",
            MappingProxyType({k: frozenset(v) for k, v in self.capability_packages.items()}),
        )
        object.__setattr__(self, "scopes", frozenset(self.scopes))
        object.__setattr__(self, "google_services", frozenset(self.google_services))
        object.__setattr__(self, "appstore_targets", _freeze_targets(self.appstore_targets))
        object.__setattr__(self, "appstore_event_stores", frozenset(self.appstore_event_stores))
        _freeze_admin(self)


def _freeze_targets(targets):
    return MappingProxyType(
        {
            capability: MappingProxyType({store: frozenset(apps) for store, apps in stores.items()})
            for capability, stores in targets.items()
        }
    )


@dataclass(frozen=True)
class GrantAuthority:
    version: int
    capability_packages: Mapping[str, frozenset[str]]
    appstore_targets: Mapping[str, Mapping[str, frozenset[str]]] = field(default_factory=dict)
    appstore_event_stores: frozenset[str] = frozenset()
    account_directory_developers: frozenset[str] = frozenset()
    account_user_targets: Mapping[tuple[str, str], UserAuthority] = field(default_factory=dict)
    account_grant_targets: Mapping[tuple[str, str, str], GrantTargetAuthority] = field(
        default_factory=dict
    )
    signing_targets: Mapping[str, frozenset[tuple[str, str]]] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(
            self,
            "capability_packages",
            MappingProxyType(
                {key: frozenset(values) for key, values in self.capability_packages.items()}
            ),
        )
        object.__setattr__(self, "appstore_targets", _freeze_targets(self.appstore_targets))
        object.__setattr__(self, "appstore_event_stores", frozenset(self.appstore_event_stores))
        _freeze_admin(self)

    @property
    def scopes(self):
        return (
            frozenset(self.capability_packages)
            | frozenset(self.appstore_targets)
            | ({APPSTORE_EVENTS} if self.appstore_event_stores else frozenset())
            | ({ACCOUNT_DIRECTORY} if self.account_directory_developers else frozenset())
            | ({ACCOUNT_USERS} if self.account_user_targets else frozenset())
            | ({ACCOUNT_GRANTS} if self.account_grant_targets else frozenset())
            | frozenset(self.signing_targets)
        )


def scope_set(values):
    if (
        not isinstance(values, (list, tuple, set, frozenset))
        or any(not isinstance(v, str) for v in values)
        or len(values) != len(set(values))
        or READ_SCOPE not in values
        or not set(values) <= ALL_SCOPES
    ):
        raise PlayError("Select supported MCP capabilities, including play:read.")
    return frozenset(values)


def package_set(values, *, allow_empty=False):
    if (
        not isinstance(values, list)
        or not (0 if allow_empty else 1) <= len(values) <= 100
        or any(not isinstance(v, str) or len(v) > 255 or not PACKAGE.fullmatch(v) for v in values)
        or len(values) != len(set(values))
    ):
        raise PlayError("Select up to 100 distinct exact Android package names.")
    return frozenset(values)


def grant_authority(grant):
    """Legacy connections keep original reads. Partial/new malformed records fail closed."""
    try:
        if (
            not isinstance(grant, dict)
            or not isinstance(grant["client_id"], str)
            or not grant["client_id"]
        ):
            raise ValueError()
        version = grant.get("capability_version", 0)
        if type(version) is not int or version not in (0, 1, 2, 3):
            raise ValueError()
        packages = package_set(grant["packages"], allow_empty=version >= 2)
        google_scopes = grant["google_scopes"]
        if (
            not isinstance(google_scopes, list)
            or not google_scopes
            or any(not isinstance(s, str) or s not in SCOPES.values() for s in google_scopes)
            or len(google_scopes) != len(set(google_scopes))
            or not isinstance(grant["bucket"], str)
            or (
                grant["bucket"]
                and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", grant["bucket"])
            )
            or type(grant["expires"]) not in (int, float)
            or not math.isfinite(grant["expires"])
        ):
            raise ValueError()
        extensions = {"appstore_targets", "appstore_event_stores"}
        if version < 3 and ADMIN_FIELDS & grant.keys():
            raise ValueError()
        if version < 2 and extensions & grant.keys():
            raise ValueError()
        if "capability_version" not in grant and "capability_packages" not in grant:
            return GrantAuthority(0, {READ_SCOPE: packages})
        if version not in (1, 2, 3):
            raise ValueError()
        raw = grant["capability_packages"]
        allowed = V1_SCOPES if version == 1 else ORDINARY_SCOPES
        if not isinstance(raw, dict) or not set(raw) <= allowed or READ_SCOPE not in raw:
            raise ValueError()
        result = {
            name: package_set(values, allow_empty=version >= 2 and name == READ_SCOPE)
            for name, values in raw.items()
        }
        if result[READ_SCOPE] != packages or any(
            not values <= packages for values in result.values()
        ):
            raise ValueError()
        if set(result) - {READ_SCOPE} and SCOPES["publisher"] not in google_scopes:
            raise ValueError()
        targets, events = {}, frozenset()
        if version >= 2:
            targets, events = _store_dimensions(
                grant["appstore_targets"], grant["appstore_event_stores"]
            )
            if (targets or events) and SCOPES["publisher"] not in google_scopes:
                raise ValueError()
        admin = (
            _admin_dimensions({key: grant[key] for key in ADMIN_FIELDS})
            if version == 3
            else (frozenset(), {}, {}, {})
        )
        if any(admin) and SCOPES["publisher"] not in google_scopes:
            raise ValueError()
        if version >= 2 and not packages and not (targets or events or any(admin)):
            raise ValueError()
        return GrantAuthority(version, result, targets, events, *admin)
    except (KeyError, TypeError, ValueError, PlayError):
        raise PlayError(INVALID_GRANT) from None


def _store_dimensions(raw, stores):
    if not isinstance(raw, dict) or not set(raw) <= APPSTORE_PAIR_SCOPES:
        raise ValueError()
    targets, pairs = {}, set()
    for capability, mapping in raw.items():
        if not isinstance(mapping, dict) or not mapping or len(mapping) > 100:
            raise ValueError()
        validated = {}
        for store, apps in mapping.items():
            package_set([store])
            validated[store] = package_set(apps)
            pairs.update((store, app) for app in validated[store])
            if len(pairs) > 100:
                raise ValueError()
        targets[capability] = validated
    return targets, package_set(stores, allow_empty=True)


def grant_capabilities(grant):
    """Compatibility accessor: ordinary app permissions only; never flatten stores."""
    return dict(grant_authority(grant).capability_packages)


def consent_capabilities(
    requested, selected, package_fields, packages, services, *, _allow_empty=False
):
    """Intersect explicit browser choices with the client's requested ceiling."""
    ceiling = scope_set(requested)
    allowed_packages = package_set(packages, allow_empty=_allow_empty)
    if (
        not isinstance(selected, list)
        or any(not isinstance(s, str) or s not in ORDINARY_SCOPES - {READ_SCOPE} for s in selected)
        or len(selected) != len(set(selected))
        or not set(selected) <= ceiling
        or (selected and "publisher" not in services)
        or not isinstance(package_fields, dict)
        or not set(package_fields) <= (ORDINARY_SCOPES - {READ_SCOPE}) & ceiling
    ):
        raise PlayError(
            "Capabilities must be explicitly requested and authorized with Publisher access."
        )
    result = {READ_SCOPE: sorted(allowed_packages)}
    for capability, text in package_fields.items():
        if not isinstance(text, str) or len(text) > 4000:
            raise PlayError("Capability package choices must be bounded text.")
        if capability not in selected and text.strip():
            raise PlayError("Select the capability before granting its packages.")
    for capability in selected:
        text = package_fields.get(capability, "")
        values = package_set(sorted({v.strip() for v in text.split(",") if v.strip()}))
        if not values <= allowed_packages:
            raise PlayError("Capability packages must be within this connection's selected apps.")
        result[capability] = sorted(values)
    return result


def consent_authority(
    requested, selected, package_fields, packages, services, store_fields, *, admin_fields=None
):
    """New consent has independent exact store/app pairs and explicitly store-wide events."""
    ceiling = scope_set(requested)
    if (
        not isinstance(selected, list)
        or any(not isinstance(s, str) or s not in CAPABILITIES for s in selected)
        or len(selected) != len(set(selected))
        or not set(selected) <= ceiling
        or (selected and "publisher" not in services)
        or not isinstance(store_fields, dict)
        or not set(store_fields) <= APPSTORE_SCOPES & ceiling
    ):
        raise PlayError("Select only requested capabilities with Publisher access.")
    ordinary = consent_capabilities(
        requested,
        [s for s in selected if s in ORDINARY_SCOPES],
        package_fields,
        packages,
        services,
        _allow_empty=True,
    )
    targets, events = {}, []
    for capability in APPSTORE_SCOPES & ceiling:
        text = store_fields.get(capability, "")
        if not isinstance(text, str) or len(text) > 52000:
            raise PlayError("Store selections must be bounded text.")
        if capability not in selected:
            if text.strip():
                raise PlayError("Select the capability before granting its stores.")
            continue
        entries = [value.strip() for value in text.split(",") if value.strip()]
        if not entries or len(entries) > 100 or len(entries) != len(set(entries)):
            raise PlayError("Select up to 100 distinct exact stores or store/app pairs.")
        if capability == APPSTORE_EVENTS:
            events = entries
        else:
            mapping = {}
            for entry in entries:
                parts = entry.split("/")
                if len(parts) != 2:
                    raise PlayError("Use an exact store/app pair for each selection.")
                store, app = (part.strip() for part in parts)
                if app in mapping.setdefault(store, []):
                    raise PlayError("Duplicate store/app pair.")
                mapping[store].append(app)
            targets[capability] = mapping
    try:
        validated, event_set = _store_dimensions(targets, events)
    except (TypeError, ValueError, PlayError):
        raise PlayError("Select valid exact store/app pairs or store-wide event stores.") from None
    try:
        if admin_fields is None:
            if set(selected) & ADMIN_SCOPES:
                raise ValueError()
            admin = (frozenset(), {}, {}, {})
        else:
            admin = _admin_dimensions(admin_fields)
            dimensions = GrantAuthority(
                3,
                {},
                account_directory_developers=admin[0],
                account_user_targets=admin[1],
                account_grant_targets=admin[2],
                signing_targets=admin[3],
            )
            if dimensions.scopes != set(selected) & ADMIN_SCOPES:
                raise ValueError()
    except (TypeError, ValueError, PlayError):
        raise PlayError(
            "Select only explicitly requested exact account and signing authority."
        ) from None
    if not packages and not (validated or event_set or any(admin)):
        raise PlayError(
            "Select at least one app or an explicit store, account or signing capability."
        )
    result = {
        "capability_version": 2 if admin_fields is None else 3,
        "capability_packages": ordinary,
        "appstore_targets": targets,
        "appstore_event_stores": events,
    }
    if admin_fields is not None:
        result.update(administrative_dimensions(dimensions))
    return result


def _distinct(values, valid, maximum=100, *, nonempty=False):
    if (
        not isinstance(values, list)
        or not (1 if nonempty else 0) <= len(values) <= maximum
        or any(not isinstance(value, str) or not valid(value) for value in values)
        or len(values) != len(set(values))
    ):
        raise ValueError()
    return frozenset(values)


def _admin_dimensions(raw):
    if not isinstance(raw, dict) or set(raw) != ADMIN_FIELDS:
        raise ValueError()
    developers = _distinct(
        raw["account_directory_developers"], lambda value: DEVELOPER.fullmatch(value)
    )
    user_rows, grant_rows = raw["account_user_targets"], raw["account_grant_targets"]
    if (
        not isinstance(user_rows, list)
        or not isinstance(grant_rows, list)
        or len(user_rows) + len(grant_rows) > 100
    ):
        raise ValueError()
    developer_enums = permission_values("User", "developerAccountPermissions")
    app_enums = permission_values("Grant", "appLevelPermissions")
    users, grants, affected_count = {}, {}, 0
    for row in user_rows:
        if not isinstance(row, dict) or set(row) != {
            "developer",
            "user",
            "methods",
            "developer_permissions",
            "affected_apps",
            "allow_expiration_change",
        }:
            raise ValueError()
        developer, user = row["developer"], row["user"]
        if (
            not isinstance(developer, str)
            or not DEVELOPER.fullmatch(developer)
            or not valid_email(user)
            or (developer, user) in users
            or type(row["allow_expiration_change"]) is not bool
            or not isinstance(row["affected_apps"], dict)
        ):
            raise ValueError()
        affected_count += len(row["affected_apps"])
        if affected_count > 100 or any(not valid_app(app) for app in row["affected_apps"]):
            raise ValueError()
        methods = _distinct(
            row["methods"], lambda value: value in USER_METHODS, len(USER_METHODS), nonempty=True
        )
        permissions = _distinct(
            row["developer_permissions"],
            lambda value: value in developer_enums,
            len(developer_enums),
        )
        affected = {
            app: _distinct(values, lambda value: value in app_enums, len(app_enums))
            for app, values in row["affected_apps"].items()
        }
        users[developer, user] = UserAuthority(
            methods, permissions, affected, row["allow_expiration_change"]
        )
    for row in grant_rows:
        if not isinstance(row, dict) or set(row) != {
            "developer",
            "user",
            "app",
            "methods",
            "app_permissions",
        }:
            raise ValueError()
        developer, user, app = row["developer"], row["user"], row["app"]
        if (
            not isinstance(developer, str)
            or not DEVELOPER.fullmatch(developer)
            or not valid_email(user)
            or not valid_app(app)
            or (developer, user, app) in grants
        ):
            raise ValueError()
        methods = _distinct(
            row["methods"], lambda value: value in GRANT_METHODS, len(GRANT_METHODS), nonempty=True
        )
        permissions = _distinct(
            row["app_permissions"], lambda value: value in app_enums, len(app_enums)
        )
        grants[developer, user, app] = GrantTargetAuthority(methods, permissions)
    signing, pair_count = {}, 0
    if (
        not isinstance(raw["signing_targets"], dict)
        or not set(raw["signing_targets"]) <= SIGNING_SCOPES
    ):
        raise ValueError()
    for capability, rows in raw["signing_targets"].items():
        if not isinstance(rows, list) or not rows:
            raise ValueError()
        pair_count += len(rows)
        if pair_count > 100:
            raise ValueError()
        pairs = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"app", "kms_version"}:
                raise ValueError()
            app, kms = row["app"], row["kms_version"]
            if (
                not signing_app(app)
                or len(app) > 255
                or not isinstance(kms, str)
                or not KMS.fullmatch(kms)
                or (app, kms) in pairs
            ):
                raise ValueError()
            pairs.add((app, kms))
        signing[capability] = frozenset(pairs)
    return developers, users, grants, signing


def administrative_dimensions(authority):
    """JSON-safe exact dimensions, without tokens or provider account responses."""
    return {
        "account_directory_developers": sorted(authority.account_directory_developers),
        "account_user_targets": [
            {
                "developer": developer,
                "user": user,
                "methods": sorted(row.methods),
                "developer_permissions": sorted(row.developer_permissions),
                "affected_apps": {
                    app: sorted(values) for app, values in sorted(row.affected_apps.items())
                },
                "allow_expiration_change": row.allow_expiration_change,
            }
            for (developer, user), row in sorted(authority.account_user_targets.items())
        ],
        "account_grant_targets": [
            {
                "developer": developer,
                "user": user,
                "app": app,
                "methods": sorted(row.methods),
                "app_permissions": sorted(row.app_permissions),
            }
            for (developer, user, app), row in sorted(authority.account_grant_targets.items())
        ],
        "signing_targets": {
            capability: [{"app": app, "kms_version": kms} for app, kms in sorted(pairs)]
            for capability, pairs in sorted(authority.signing_targets.items())
        },
    }
