"""Reviewed operations, separate from the discovery inventory."""

PUBLISHER_EDIT_METHODS = frozenset(
    {
        "androidpublisher.edits.get",
        "androidpublisher.edits.details.get",
        "androidpublisher.edits.listings.get",
        "androidpublisher.edits.listings.list",
        "androidpublisher.edits.images.list",
        "androidpublisher.edits.tracks.get",
        "androidpublisher.edits.tracks.list",
        "androidpublisher.edits.countryavailability.get",
        "androidpublisher.edits.bundles.list",
        "androidpublisher.edits.apks.list",
    }
)

REPORTING_FAMILIES = (
    "anonrssandswapmemoryusage",
    "anrrate",
    "bitmapmemoryusage",
    "crashrate",
    "errors.counts",
    "excessivewakeuprate",
    "lmkrate",
    "slowrenderingrate",
    "slowstartrate",
    "stuckbackgroundwakelockrate",
)
REPORTING_METHODS = frozenset(
    {
        "playdeveloperreporting.anomalies.list",
        "playdeveloperreporting.apps.fetchReleaseFilterOptions",
        "playdeveloperreporting.apps.search",
        "playdeveloperreporting.vitals.errors.issues.search",
        "playdeveloperreporting.vitals.errors.reports.search",
    }
    | {
        f"playdeveloperreporting.vitals.{family}.{action}"
        for family in REPORTING_FAMILIES
        for action in ("get", "query")
    }
)


PUBLISHER_READ_METHODS = frozenset(
    {
        "androidpublisher.applications.deviceTierConfigs.get",
        "androidpublisher.applications.deviceTierConfigs.list",
        "androidpublisher.apprecovery.list",
        "androidpublisher.edits.expansionfiles.get",
        "androidpublisher.generatedapks.list",
        "androidpublisher.systemapks.variants.get",
        "androidpublisher.systemapks.variants.list",
        "androidpublisher.inappproducts.batchGet",
        "androidpublisher.inappproducts.get",
        "androidpublisher.inappproducts.list",
        "androidpublisher.monetization.onetimeproducts.batchGet",
        "androidpublisher.monetization.onetimeproducts.get",
        "androidpublisher.monetization.onetimeproducts.list",
        "androidpublisher.monetization.onetimeproducts.purchaseOptions.offers.list",
        "androidpublisher.monetization.subscriptions.batchGet",
        "androidpublisher.monetization.subscriptions.get",
        "androidpublisher.monetization.subscriptions.list",
        "androidpublisher.monetization.subscriptions.basePlans.offers.get",
        "androidpublisher.monetization.subscriptions.basePlans.offers.list",
    }
)


SENSITIVE_PUBLISHER_READ_METHODS = frozenset(
    {
        "androidpublisher.orders.get",
        "androidpublisher.orders.batchget",
        "androidpublisher.purchases.products.get",
        "androidpublisher.purchases.productsv2.getproductpurchasev2",
        "androidpublisher.purchases.subscriptionsv2.get",
        "androidpublisher.externaltransactions.getexternaltransaction",
        "androidpublisher.edits.testers.get",
    }
)


EDIT_WRITE_METHODS = frozenset(
    {
        "androidpublisher.edits.details.patch",
        "androidpublisher.edits.details.update",
        "androidpublisher.edits.expansionfiles.patch",
        "androidpublisher.edits.expansionfiles.update",
        "androidpublisher.edits.images.delete",
        "androidpublisher.edits.images.deleteall",
        "androidpublisher.edits.listings.delete",
        "androidpublisher.edits.listings.deleteall",
        "androidpublisher.edits.listings.update",
        "androidpublisher.edits.testers.patch",
        "androidpublisher.edits.testers.update",
        "androidpublisher.edits.tracks.create",
        "androidpublisher.edits.tracks.patch",
    }
)


ARTIFACT_METHODS = frozenset(
    {
        "androidpublisher.edits.apks.upload",
        "androidpublisher.edits.bundles.upload",
        "androidpublisher.edits.deobfuscationfiles.upload",
        "androidpublisher.edits.expansionfiles.upload",
        "androidpublisher.edits.images.upload",
        "androidpublisher.edits.apks.addexternallyhosted",
        "androidpublisher.generatedapks.download",
        "androidpublisher.systemapks.variants.download",
    }
)


MONETIZATION_QUERY_METHODS = frozenset(
    {
        "androidpublisher.monetization.convertRegionPrices",
        "androidpublisher.monetization.subscriptions.basePlans.offers.batchGet",
        "androidpublisher.monetization.onetimeproducts.purchaseOptions.offers.batchGet",
    }
)
MONETIZATION_WRITE_METHODS = frozenset(
    {
        "androidpublisher.monetization.subscriptions." + action
        for action in ("create", "patch", "batchUpdate", "delete")
    }
    | {
        "androidpublisher.monetization.subscriptions.basePlans." + action
        for action in ("activate", "deactivate", "delete", "batchUpdateStates")
    }
    | {
        "androidpublisher.monetization.subscriptions.basePlans.offers." + action
        for action in (
            "create",
            "patch",
            "delete",
            "activate",
            "deactivate",
            "batchUpdate",
            "batchUpdateStates",
        )
    }
)
PROVIDER_UNSUPPORTED_METHODS = frozenset({"androidpublisher.monetization.subscriptions.archive"})

ONETIME_WRITE_METHODS = frozenset(
    {
        "androidpublisher.monetization.onetimeproducts." + action
        for action in ("patch", "delete", "batchUpdate", "batchDelete")
    }
    | {
        "androidpublisher.monetization.onetimeproducts.purchaseOptions." + action
        for action in ("batchDelete", "batchUpdateStates")
    }
    | {
        "androidpublisher.monetization.onetimeproducts.purchaseOptions.offers." + action
        for action in (
            "activate",
            "deactivate",
            "cancel",
            "batchDelete",
            "batchUpdate",
            "batchUpdateStates",
        )
    }
)
MONETIZATION_WRITE_METHODS = MONETIZATION_WRITE_METHODS | ONETIME_WRITE_METHODS

LEGACY_PRODUCT_WRITE_METHODS = frozenset(
    "androidpublisher.inappproducts." + action
    for action in ("insert", "patch", "update", "delete", "batchUpdate", "batchDelete")
)
MONETIZATION_WRITE_METHODS = MONETIZATION_WRITE_METHODS | LEGACY_PRODUCT_WRITE_METHODS


SUPPORT_METHODS = frozenset(
    {
        "androidpublisher.reviews.reply",
        "androidpublisher.applications.dataSafety",
        "androidpublisher.applications.deviceTierConfigs.create",
        "androidpublisher.apprecovery.create",
        "androidpublisher.apprecovery.addTargeting",
        "androidpublisher.apprecovery.cancel",
        "androidpublisher.apprecovery.deploy",
        "androidpublisher.systemapks.variants.create",
    }
)
SHARING_METHODS = frozenset(
    {
        "androidpublisher.internalappsharingartifacts.uploadapk",
        "androidpublisher.internalappsharingartifacts.uploadbundle",
    }
)


# Independently authorized financial/customer actions, never inherited from catalog writes.
PURCHASE_ACTION_METHODS = frozenset(
    "androidpublisher." + name
    for name in (
        "purchases.products.acknowledge",
        "purchases.products.consume",
        "purchases.subscriptions.acknowledge",
        "purchases.subscriptions.cancel",
        "purchases.subscriptions.defer",
        "purchases.subscriptionsv2.cancel",
        "purchases.subscriptionsv2.defer",
        "purchases.subscriptionsv2.revoke",
        "orders.refund",
        "orders.reviewrefund",
    )
)
EXTERNAL_TRANSACTION_METHODS = frozenset(
    "androidpublisher.externaltransactions." + name
    for name in ("createexternaltransaction", "refundexternaltransaction")
)
PRICE_MIGRATION_METHODS = frozenset(
    "androidpublisher.monetization.subscriptions.basePlans." + name
    for name in ("migratePrices", "batchMigratePrices")
)
PURCHASE_LIFECYCLE_METHODS = (
    PURCHASE_ACTION_METHODS | EXTERNAL_TRANSACTION_METHODS | PRICE_MIGRATION_METHODS
)


SIGNING_METHODS = frozenset(
    "androidpublisher.appsigning." + action for action in ("enrollApp", "rotateAppSigningKey")
)
ACCOUNT_READ_METHODS = frozenset({"androidpublisher.users.list"})
ACCOUNT_WRITE_METHODS = frozenset(
    "androidpublisher." + resource + "." + action
    for resource in ("users", "grants")
    for action in ("create", "patch", "delete")
)
APPSTORE_READ_METHODS = frozenset(
    {
        "androidpublisher.appstorecatalog.recentappviews.get",
        "androidpublisher.appstorecatalog.recentupdateevents.list",
    }
)
APPSTORE_WRITE_METHODS = frozenset(
    "androidpublisher.appstoreappsreview." + action
    for action in (
        "createappstorehostedapp",
        "updateappstorehostedapp",
        "updateappstorehostedapppublishstatus",
        "uploadapk",
        "uploadappstoreapppolicydeclarationfile",
        "uploadimage",
    )
)

POLICY_DISABLED_METHODS = frozenset({"androidpublisher.purchases.voidedpurchases.list"})
