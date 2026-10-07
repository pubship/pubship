from dataclasses import replace

import pytest

from pubship.config import Settings
from pubship.coverage import (
    EXTERNAL_TRANSACTION_METHODS,
    MONETIZATION_WRITE_METHODS,
    PRICE_MIGRATION_METHODS,
    PURCHASE_ACTION_METHODS,
    SUPPORT_METHODS,
)
from pubship.errors import PlayError

PACKAGE = "com.example.app"
PACKAGES = frozenset({PACKAGE})
FAMILIES = (
    ("purchase", PURCHASE_ACTION_METHODS),
    ("external_transaction", EXTERNAL_TRANSACTION_METHODS),
    ("price_migration", PRICE_MIGRATION_METHODS),
)


@pytest.mark.parametrize("family,methods", FAMILIES)
def test_family_grants_are_exact_independent_and_scoped(family, methods):
    method = sorted(methods)[0]
    base = Settings(
        packages=PACKAGES,
        sensitive_read_packages=PACKAGES,
        support_packages=PACKAGES,
        support_methods=SUPPORT_METHODS,
        monetization_packages=PACKAGES,
        monetization_methods=MONETIZATION_WRITE_METHODS,
    )
    with pytest.raises(PlayError):
        base.require_purchase_lifecycle(PACKAGE, method)
    ready = replace(base, **{family + "_packages": PACKAGES, family + "_methods": methods})
    ready.require_purchase_lifecycle(PACKAGE, method)
    for target in ("com.other.app", "*"):
        with pytest.raises(PlayError):
            ready.require_purchase_lifecycle(target, method)
    with pytest.raises(PlayError):
        replace(ready, **{family + "_methods": frozenset()}).require_purchase_lifecycle(
            PACKAGE, method
        )
    with pytest.raises(PlayError):
        replace(ready, **{family + "_packages": frozenset()}).require_purchase_lifecycle(
            PACKAGE, method
        )
    for other, other_methods in FAMILIES:
        if family != other:
            with pytest.raises(PlayError):
                ready.require_purchase_lifecycle(PACKAGE, sorted(other_methods)[0])
            with pytest.raises(PlayError):
                replace(ready, **{family + "_methods": other_methods})


@pytest.mark.parametrize("family,methods", FAMILIES)
def test_sensitive_read_scope_required_for_customer_actions_only(family, methods):
    settings = Settings(
        packages=PACKAGES, **{family + "_packages": PACKAGES, family + "_methods": methods}
    )
    method = sorted(methods)[0]
    if family == "price_migration":
        settings.require_purchase_lifecycle(PACKAGE, method)
    else:
        with pytest.raises(PlayError, match="SENSITIVE_READ"):
            settings.require_purchase_lifecycle(PACKAGE, method)
        replace(settings, sensitive_read_packages=PACKAGES).require_purchase_lifecycle(
            PACKAGE, method
        )


@pytest.mark.parametrize("family,methods", FAMILIES)
@pytest.mark.parametrize(
    "bad",
    [
        frozenset({"*"}),
        frozenset({"com.other.app"}),
        {PACKAGE},
        "com.example.app",
        None,
        frozenset({1}),
    ],
)
def test_invalid_family_packages_rejected(family, methods, bad):
    with pytest.raises(PlayError):
        Settings(packages=PACKAGES, **{family + "_packages": bad, family + "_methods": methods})


@pytest.mark.parametrize("family,methods", FAMILIES)
@pytest.mark.parametrize(
    "bad", [frozenset({"*"}), frozenset({"androidpublisher.orders.unknown"}), set(), "method", None]
)
def test_invalid_family_methods_rejected(family, methods, bad):
    with pytest.raises(PlayError):
        Settings(packages=PACKAGES, **{family + "_packages": PACKAGES, family + "_methods": bad})


@pytest.mark.parametrize("family,methods", FAMILIES)
def test_env_mapping_is_complete(monkeypatch, family, methods):
    for key in list(__import__("os").environ):
        if key.startswith("GOOGLE_PLAY_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_SENSITIVE_READ_PACKAGES", PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_" + family.upper() + "_PACKAGES", " " + PACKAGE + ",")
    monkeypatch.setenv("GOOGLE_PLAY_" + family.upper() + "_METHODS", ",".join(sorted(methods)))
    settings = Settings.from_env()
    assert getattr(settings, family + "_packages") == PACKAGES
    assert getattr(settings, family + "_methods") == methods
    for method in methods:
        settings.require_purchase_lifecycle(PACKAGE, method)
