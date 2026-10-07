"""Independent opt-ins for local third-party store catalog and review operations."""

import os
from dataclasses import dataclass

from .appstore_contracts import APPSTORE_METHODS, package
from .errors import PlayError


@dataclass(frozen=True)
class AppStoreSettings:
    stores: frozenset[str] = frozenset()
    packages: frozenset[str] = frozenset()
    methods: frozenset[str] = frozenset()
    upload_root: str = ""
    max_bytes: int = 64 * 1024 * 1024

    def __post_init__(self):
        for values in (self.stores, self.packages):
            if not isinstance(values, frozenset):
                raise PlayError("App-store grants require exact frozen sets.")
            for value in values:
                package(value)
        if not isinstance(self.methods, frozenset) or not self.methods <= APPSTORE_METHODS:
            raise PlayError("GOOGLE_PLAY_APPSTORE_METHODS requires exact supported IDs.")
        if not isinstance(self.upload_root, str) or (
            self.upload_root and (not os.path.isabs(self.upload_root) or "\0" in self.upload_root)
        ):
            raise PlayError("GOOGLE_PLAY_APPSTORE_UPLOAD_ROOT must be an absolute directory.")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 10737418240:
            raise PlayError("GOOGLE_PLAY_APPSTORE_MAX_BYTES must be from 1 to 10737418240.")

    @classmethod
    def from_env(cls, base_settings):
        def values(name):
            return frozenset(
                v.strip()
                for v in os.getenv("GOOGLE_PLAY_APPSTORE_" + name, "").split(",")
                if v.strip()
            )

        try:
            cap = int(os.getenv("GOOGLE_PLAY_APPSTORE_MAX_BYTES", "67108864"))
        except ValueError:
            raise PlayError("GOOGLE_PLAY_APPSTORE_MAX_BYTES must be an integer.") from None
        result = cls(
            values("STORES"),
            values("PACKAGES"),
            values("METHODS"),
            os.getenv("GOOGLE_PLAY_APPSTORE_UPLOAD_ROOT", ""),
            cap,
        )
        if not (result.stores | result.packages) <= base_settings.packages:
            raise PlayError(
                "App-store store and target grants must be within GOOGLE_PLAY_PACKAGES."
            )
        return result

    def require(self, spec):
        if (
            spec["method"] not in self.methods
            or spec["parameters"]["appStorePackageName"] not in self.stores
        ):
            raise PlayError(
                "App-store operation requires exact APPSTORE_STORES and APPSTORE_METHODS grants."
            )
        if spec["target_package"] is not None and spec["target_package"] not in self.packages:
            raise PlayError("App-store target requires an exact APPSTORE_PACKAGES grant.")
