"""Independent local account administration scopes and permission ceilings."""

import os
import re
from dataclasses import dataclass

from .config import PACKAGE
from .contracts import definitions
from .errors import PlayError

PREFIX = "androidpublisher."
READ_METHOD = PREFIX + "users.list"
WRITE_METHODS = frozenset(
    PREFIX + family + "." + action
    for family in ("users", "grants")
    for action in ("create", "patch", "delete")
)
METHODS = WRITE_METHODS | {READ_METHOD}
DEVELOPER = re.compile(r"[0-9]{1,30}")
EMAIL = re.compile(r"[A-Za-z0-9.!#$&'*+=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?")
APP_ID = re.compile(r"[0-9]{1,30}")


def valid_email(value):
    return isinstance(value, str) and len(value) <= 254 and EMAIL.fullmatch(value) is not None


def valid_app(value):
    return (
        isinstance(value, str)
        and len(value) <= 255
        and (PACKAGE.fullmatch(value) is not None or APP_ID.fullmatch(value) is not None)
    )


def permission_values(resource, field):
    values = definitions()["androidpublisher"]["schemas"][resource]["properties"][field]["items"]
    return frozenset(
        value
        for value, deprecated in zip(values["enum"], values["enumDeprecated"], strict=True)
        if not deprecated and not value.endswith("_UNSPECIFIED")
    )


@dataclass(frozen=True)
class AccountAccessSettings:
    developers: frozenset[str] = frozenset()
    methods: frozenset[str] = frozenset()
    users: frozenset[str] = frozenset()
    apps: frozenset[str] = frozenset()
    developer_permissions: frozenset[str] = frozenset()
    app_permissions: frozenset[str] = frozenset()

    def __post_init__(self):
        checks = (
            (self.developers, lambda v: isinstance(v, str) and DEVELOPER.fullmatch(v)),
            (self.methods, lambda v: v in METHODS),
            (self.users, valid_email),
            (self.apps, valid_app),
            (
                self.developer_permissions,
                lambda v: v in permission_values("User", "developerAccountPermissions"),
            ),
            (
                self.app_permissions,
                lambda v: v in permission_values("Grant", "appLevelPermissions"),
            ),
        )
        for values, predicate in checks:
            if not isinstance(values, frozenset) or any(not predicate(v) for v in values):
                raise PlayError("Invalid exact account access scope or permission ceiling.")

    @classmethod
    def from_env(cls, base_settings=None):
        # Deliberately independent of package, publishing, ADC and hosted settings.
        def values(suffix):
            raw = os.environ.get("GOOGLE_PLAY_ACCOUNT_ACCESS_" + suffix, "")
            if not raw:
                return frozenset()
            parts = raw.split(",")
            if any(not part.strip() for part in parts):
                raise PlayError("Account access scopes require nonempty comma-separated values.")
            return frozenset(part.strip() for part in parts)

        return cls(**{name: values(name.upper()) for name in cls.__dataclass_fields__})

    def require(self, developer, method, user=None, app=None):
        if developer not in self.developers or method not in self.methods:
            raise PlayError("Account developer and exact method require explicit local grants.")
        if user is not None and user not in self.users:
            raise PlayError("Account target user requires an explicit local grant.")
        if app is not None and app not in self.apps:
            raise PlayError("Account target app requires an explicit local grant.")

    def permissions(self, developer_permissions=(), app_permissions=()):
        if not set(developer_permissions).issubset(self.developer_permissions) or not set(
            app_permissions
        ).issubset(self.app_permissions):
            raise PlayError("Account operation exceeds the configured permission ceiling.")
