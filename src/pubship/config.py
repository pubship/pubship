"""Explicit per-process app scope; no customer-specific defaults."""

import os
import re
from dataclasses import dataclass

from .coverage import (
    ARTIFACT_METHODS,
    EDIT_WRITE_METHODS,
    EXTERNAL_TRANSACTION_METHODS,
    MONETIZATION_WRITE_METHODS,
    PRICE_MIGRATION_METHODS,
    PURCHASE_ACTION_METHODS,
    SHARING_METHODS,
    SUPPORT_METHODS,
)
from .errors import PlayError

PACKAGE = re.compile(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z][A-Za-z0-9_]*)+")


@dataclass(frozen=True)
class Settings:
    packages: frozenset[str] = frozenset()
    bucket: str = ""
    auth_mode: str = "adc"
    impersonate: str = ""
    account: str = ""
    gcloud: str = "gcloud"
    write_packages: frozenset[str] = frozenset()
    edit_packages: frozenset[str] = frozenset()
    track_packages: frozenset[str] = frozenset()
    sensitive_read_packages: frozenset[str] = frozenset()
    edit_write_packages: frozenset[str] = frozenset()
    edit_write_methods: frozenset[str] = frozenset()

    artifact_packages: frozenset[str] = frozenset()
    artifact_methods: frozenset[str] = frozenset()
    artifact_upload_root: str = ""
    artifact_download_root: str = ""
    artifact_max_bytes: int = 1073741824

    monetization_packages: frozenset[str] = frozenset()
    monetization_methods: frozenset[str] = frozenset()

    support_packages: frozenset[str] = frozenset()
    support_methods: frozenset[str] = frozenset()

    purchase_packages: frozenset[str] = frozenset()
    purchase_methods: frozenset[str] = frozenset()
    external_transaction_packages: frozenset[str] = frozenset()
    external_transaction_methods: frozenset[str] = frozenset()
    price_migration_packages: frozenset[str] = frozenset()
    price_migration_methods: frozenset[str] = frozenset()

    def __post_init__(self):
        for family, allowed in (
            ("purchase", PURCHASE_ACTION_METHODS),
            ("external_transaction", EXTERNAL_TRANSACTION_METHODS),
            ("price_migration", PRICE_MIGRATION_METHODS),
        ):
            packages = getattr(self, family + "_packages")
            methods = getattr(self, family + "_methods")
            label = "GOOGLE_PLAY_" + family.upper()
            if (
                not isinstance(packages, frozenset)
                or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in packages)
                or not packages.issubset(self.packages)
            ):
                raise PlayError(
                    label + "_PACKAGES requires exact names within GOOGLE_PLAY_PACKAGES."
                )
            if not isinstance(methods, frozenset) or not methods.issubset(allowed):
                raise PlayError(label + "_METHODS requires exact supported IDs from that family.")

        if (
            not isinstance(self.support_packages, frozenset)
            or any(
                not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.support_packages
            )
            or not self.support_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_SUPPORT_PACKAGES must contain explicit names within GOOGLE_PLAY_PACKAGES."
            )
        if not isinstance(self.support_methods, frozenset) or not self.support_methods.issubset(
            SUPPORT_METHODS
        ):
            raise PlayError(
                "GOOGLE_PLAY_SUPPORT_METHODS requires exact supported method IDs; no wildcards."
            )

        if (
            not isinstance(self.monetization_packages, frozenset)
            or any(
                not isinstance(p, str) or not PACKAGE.fullmatch(p)
                for p in self.monetization_packages
            )
            or not self.monetization_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_MONETIZATION_PACKAGES must contain explicit names within GOOGLE_PLAY_PACKAGES."
            )
        if not isinstance(
            self.monetization_methods, frozenset
        ) or not self.monetization_methods.issubset(MONETIZATION_WRITE_METHODS):
            raise PlayError(
                "GOOGLE_PLAY_MONETIZATION_METHODS requires exact supported live-write IDs; no wildcards."
            )
        if (
            not isinstance(self.artifact_packages, frozenset)
            or any(
                not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.artifact_packages
            )
            or not self.artifact_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_ARTIFACT_PACKAGES must be explicit names within GOOGLE_PLAY_PACKAGES."
            )
        if not isinstance(self.artifact_methods, frozenset) or not self.artifact_methods.issubset(
            ARTIFACT_METHODS | SHARING_METHODS
        ):
            raise PlayError(
                "GOOGLE_PLAY_ARTIFACT_METHODS requires exact supported method IDs; no wildcards."
            )
        if (
            type(self.artifact_max_bytes) is not int
            or not 1 <= self.artifact_max_bytes <= 53687091200
        ):
            raise PlayError(
                "GOOGLE_PLAY_ARTIFACT_MAX_BYTES must be an integer from 1 to 53687091200."
            )
        for root in (self.artifact_upload_root, self.artifact_download_root):
            if not isinstance(root, str) or (root and (not os.path.isabs(root) or "\x00" in root)):
                raise PlayError("Artifact roots must be explicit absolute directory paths.")
        if (
            not isinstance(self.edit_write_packages, frozenset)
            or any(
                not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.edit_write_packages
            )
            or not self.edit_write_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_EDIT_WRITE_PACKAGES must be explicit package names within GOOGLE_PLAY_PACKAGES."
            )
        if not isinstance(
            self.edit_write_methods, frozenset
        ) or not self.edit_write_methods.issubset(EDIT_WRITE_METHODS):
            raise PlayError(
                "GOOGLE_PLAY_EDIT_WRITE_METHODS must contain exact supported method IDs; no wildcards."
            )
        if (
            not isinstance(self.write_packages, frozenset)
            or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.write_packages)
            or not self.write_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_WRITE_PACKAGES must be explicit package names within GOOGLE_PLAY_PACKAGES."
            )
        if (
            not isinstance(self.edit_packages, frozenset)
            or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.edit_packages)
            or not self.edit_packages.issubset(self.write_packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_EDIT_PACKAGES must be explicit package names within GOOGLE_PLAY_WRITE_PACKAGES."
            )
        if (
            not isinstance(self.track_packages, frozenset)
            or any(not isinstance(p, str) or not PACKAGE.fullmatch(p) for p in self.track_packages)
            or not self.track_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_TRACK_PACKAGES must be explicit package names within GOOGLE_PLAY_PACKAGES."
            )
        if (
            not isinstance(self.sensitive_read_packages, frozenset)
            or any(
                not isinstance(p, str) or not PACKAGE.fullmatch(p)
                for p in self.sensitive_read_packages
            )
            or not self.sensitive_read_packages.issubset(self.packages)
        ):
            raise PlayError(
                "GOOGLE_PLAY_SENSITIVE_READ_PACKAGES must be explicit package names within GOOGLE_PLAY_PACKAGES."
            )

    @classmethod
    def from_env(cls):
        packages = frozenset(
            p.strip() for p in os.environ.get("GOOGLE_PLAY_PACKAGES", "").split(",") if p.strip()
        )
        if any(not PACKAGE.fullmatch(p) for p in packages):
            raise PlayError(
                "GOOGLE_PLAY_PACKAGES must contain comma-separated Android package names."
            )
        write_packages = frozenset(
            p.strip()
            for p in os.environ.get("GOOGLE_PLAY_WRITE_PACKAGES", "").split(",")
            if p.strip()
        )
        edit_packages = frozenset(
            p.strip()
            for p in os.environ.get("GOOGLE_PLAY_EDIT_PACKAGES", "").split(",")
            if p.strip()
        )
        track_packages = frozenset(
            p.strip()
            for p in os.environ.get("GOOGLE_PLAY_TRACK_PACKAGES", "").split(",")
            if p.strip()
        )
        sensitive_read_packages = frozenset(
            p.strip()
            for p in os.environ.get("GOOGLE_PLAY_SENSITIVE_READ_PACKAGES", "").split(",")
            if p.strip()
        )
        edit_write_packages = frozenset(
            p.strip()
            for p in os.environ.get("GOOGLE_PLAY_EDIT_WRITE_PACKAGES", "").split(",")
            if p.strip()
        )
        edit_write_methods = frozenset(
            m.strip()
            for m in os.environ.get("GOOGLE_PLAY_EDIT_WRITE_METHODS", "").split(",")
            if m.strip()
        )
        bucket = os.environ.get("GOOGLE_PLAY_REPORT_BUCKET", "")
        if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,220}[a-z0-9]", bucket):
            raise PlayError(
                "GOOGLE_PLAY_REPORT_BUCKET must be a bucket name, without gs:// or a path."
            )
        mode = os.environ.get("GOOGLE_PLAY_AUTH_MODE", "adc")
        if mode not in {"adc", "gcloud"}:
            raise PlayError("GOOGLE_PLAY_AUTH_MODE must be adc or gcloud.")
        impersonate = os.environ.get("GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT", "")
        if impersonate and not re.fullmatch(
            r"[a-zA-Z0-9._-]+@[a-zA-Z0-9.-]+\.iam\.gserviceaccount\.com", impersonate
        ):
            raise PlayError("Invalid service-account email.")
        if mode == "gcloud" and not impersonate:
            raise PlayError("gcloud mode requires GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT.")
        try:
            artifact_max_bytes = int(os.environ.get("GOOGLE_PLAY_ARTIFACT_MAX_BYTES", "1073741824"))
        except ValueError:
            raise PlayError("GOOGLE_PLAY_ARTIFACT_MAX_BYTES must be an integer.") from None
        return cls(
            packages,
            bucket,
            mode,
            impersonate,
            os.environ.get("GOOGLE_PLAY_ACCOUNT", ""),
            os.environ.get("GOOGLE_PLAY_GCLOUD", "gcloud"),
            write_packages,
            edit_packages,
            track_packages,
            sensitive_read_packages,
            edit_write_packages,
            edit_write_methods,
            frozenset(
                p.strip()
                for p in os.environ.get("GOOGLE_PLAY_ARTIFACT_PACKAGES", "").split(",")
                if p.strip()
            ),
            frozenset(
                m.strip()
                for m in os.environ.get("GOOGLE_PLAY_ARTIFACT_METHODS", "").split(",")
                if m.strip()
            ),
            os.environ.get("GOOGLE_PLAY_ARTIFACT_UPLOAD_ROOT", ""),
            os.environ.get("GOOGLE_PLAY_ARTIFACT_DOWNLOAD_ROOT", ""),
            artifact_max_bytes,
            frozenset(
                p.strip()
                for p in os.environ.get("GOOGLE_PLAY_MONETIZATION_PACKAGES", "").split(",")
                if p.strip()
            ),
            frozenset(
                m.strip()
                for m in os.environ.get("GOOGLE_PLAY_MONETIZATION_METHODS", "").split(",")
                if m.strip()
            ),
            frozenset(
                p.strip()
                for p in os.environ.get("GOOGLE_PLAY_SUPPORT_PACKAGES", "").split(",")
                if p.strip()
            ),
            frozenset(
                m.strip()
                for m in os.environ.get("GOOGLE_PLAY_SUPPORT_METHODS", "").split(",")
                if m.strip()
            ),
            **{
                family + "_" + kind: frozenset(
                    value.strip()
                    for value in os.environ.get(
                        "GOOGLE_PLAY_" + family.upper() + "_" + kind.upper(), ""
                    ).split(",")
                    if value.strip()
                )
                for family in ("purchase", "external_transaction", "price_migration")
                for kind in ("packages", "methods")
            },
        )

    def require_package(self, package: str):
        if not PACKAGE.fullmatch(package) or package not in self.packages:
            raise PlayError(
                "App is outside GOOGLE_PLAY_PACKAGES. Configure its package explicitly first."
            )

    def require_write_package(self, package: str):
        self.require_package(package)
        if package not in self.write_packages:
            raise PlayError(
                "App is outside GOOGLE_PLAY_WRITE_PACKAGES; listing staging is disabled."
            )

    def require_bucket(self):
        if not self.bucket:
            raise PlayError(
                "Set GOOGLE_PLAY_REPORT_BUCKET to the exact bucket copied from Play Console."
            )

    def require_edit_package(self, package: str):
        self.require_write_package(package)
        if package not in self.edit_packages:
            raise PlayError(
                "App is outside GOOGLE_PLAY_EDIT_PACKAGES; edit lifecycle operations are disabled."
            )

    def require_track_package(self, package: str):
        self.require_package(package)
        if package not in self.track_packages:
            raise PlayError("App is outside GOOGLE_PLAY_TRACK_PACKAGES; track staging is disabled.")

    def require_sensitive_read_package(self, package: str):
        self.require_package(package)
        if package not in self.sensitive_read_packages:
            raise PlayError(
                "App is outside GOOGLE_PLAY_SENSITIVE_READ_PACKAGES; sensitive reads are disabled."
            )

    def require_edit_write(self, package: str, method: str):
        self.require_package(package)
        if package not in self.edit_write_packages or method not in self.edit_write_methods:
            raise PlayError(
                "Edit write requires both GOOGLE_PLAY_EDIT_WRITE_PACKAGES and GOOGLE_PLAY_EDIT_WRITE_METHODS authorization."
            )

    def require_artifact(self, package, method):
        self.require_package(package)
        if package not in self.artifact_packages or method not in self.artifact_methods:
            raise PlayError("Artifact package or method is outside the explicit local scopes.")

    def require_monetization(self, package: str, method: str):
        self.require_package(package)
        if package not in self.monetization_packages or method not in self.monetization_methods:
            raise PlayError(
                "Live commerce writes require independent GOOGLE_PLAY_MONETIZATION_PACKAGES and GOOGLE_PLAY_MONETIZATION_METHODS opt-ins."
            )

    def require_support(self, package: str, method: str):
        self.require_package(package)
        if package not in self.support_packages or method not in self.support_methods:
            raise PlayError(
                "Support writes require independent GOOGLE_PLAY_SUPPORT_PACKAGES and GOOGLE_PLAY_SUPPORT_METHODS opt-ins."
            )

    def require_purchase_lifecycle(self, package: str, method: str):
        self.require_package(package)
        for family, allowed in (
            ("purchase", PURCHASE_ACTION_METHODS),
            ("external_transaction", EXTERNAL_TRANSACTION_METHODS),
            ("price_migration", PRICE_MIGRATION_METHODS),
        ):
            if method in allowed:
                if package not in getattr(self, family + "_packages") or method not in getattr(
                    self, family + "_methods"
                ):
                    raise PlayError(
                        "Purchase lifecycle requires the matching family's exact package and method grants."
                    )
                if family != "price_migration":
                    self.require_sensitive_read_package(package)
                return
        raise PlayError("Unsupported purchase lifecycle method.")
