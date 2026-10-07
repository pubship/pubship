"""Real Chromium, Firefox and WebKit authority review through closed TLS proxy."""

import pytest
from test_hosted_consent_browser import (
    ISSUER,
    playwright,
)
from test_hosted_consent_browser import (
    bridge as bridge,
)
from test_hosted_consent_browser import (
    browser as browser,
)

from pubship.hosted_grants import (
    ACCOUNT_DIRECTORY,
    ACCOUNT_GRANTS,
    ACCOUNT_USERS,
    ADMIN_SCOPES,
    READ_SCOPE,
    SIGNING_ENROLL,
    SIGNING_ROTATE,
)

DEV = "123456789"
APP = "com.example.app"
KEY = "projects/synthetic/locations/global/keyRings/example/cryptoKeys/example/cryptoKeyVersions/1"


def open_capability(fixture, capability):
    page = fixture.begin([READ_SCOPE, capability])
    page.get_by_label("Android package names", exact=True).fill("")
    checkbox = page.locator(f"input[name='capabilities'][value='{capability}']")
    assert not checkbox.is_checked()
    checkbox.check()
    page.get_by_text("Exact authority for ", exact=False).click()
    if capability == ACCOUNT_DIRECTORY:
        page.get_by_label("Exact developer IDs (comma-separated)").fill(DEV)
    elif capability in {SIGNING_ENROLL, SIGNING_ROTATE}:
        page.get_by_label("Row 1: App ID", exact=True).fill(APP)
        page.get_by_label("Row 1: Full Cloud KMS key version", exact=True).fill(KEY)
    else:
        page.get_by_label("Row 1: Developer ID", exact=True).fill(DEV)
        page.get_by_label("Row 1: Exact user email", exact=True).fill("native@example.com")
        family = "users" if capability == ACCOUNT_USERS else "grants"
        page.get_by_label("Row 1: Allowed methods", exact=True).fill(
            f"androidpublisher.{family}.delete"
        )
        if capability == ACCOUNT_GRANTS:
            page.get_by_label("Row 1: App ID", exact=True).fill(APP)
        else:
            page.get_by_label("Row 1: Affected app 1 ID", exact=True).fill(APP)
    return page


@pytest.mark.parametrize("capability", sorted(ADMIN_SCOPES))
def test_native_review_and_explicit_confirmation_before_google(bridge, capability):
    fixture, fetch = bridge
    page = open_capability(fixture, capability)
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(
        page.get_by_role("heading", name="Review exact connection authority")
    ).to_be_visible()
    assert not fixture.google_requests
    assert page.locator("input[type=hidden][name=authority_confirmation]").input_value()
    assert "None (empty ceiling)" in page.locator("main").inner_text()
    page.get_by_role("button", name="Confirm and continue to Google").focus()
    page.keyboard.press("Enter")
    playwright.expect(
        page.get_by_role("link", name="Complete synthetic Google consent")
    ).to_be_visible()
    assert [item["status"] for item in fixture.posts] == [200, 303]
    assert all(item["origin"] == ISSUER and item["cookie"] for item in fixture.posts)
    assert len(fixture.google_requests) == 1
    fetch.assert_not_called()


@pytest.mark.parametrize("width", [390, 1440])
def test_native_narrow_layout_labels_keyboard_and_escaped_review(bridge, width):
    fixture, _ = bridge
    page = open_capability(fixture, ACCOUNT_USERS)
    page.set_viewport_size({"width": width, "height": 900})
    page.get_by_label("Row 1: Exact user email", exact=True).fill("native&'quote@example.com")
    page.get_by_label("Row 1: Allow changes to existing access expiry", exact=True).focus()
    page.keyboard.press("Space")
    assert page.get_by_label(
        "Row 1: Allow changes to existing access expiry", exact=True
    ).is_checked()
    assert page.locator("input[type=text]").evaluate_all(
        "items => items.every(item => item.labels.length > 0)"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(
        page.get_by_role("heading", name="Review exact connection authority")
    ).to_be_visible()
    assert "native&'quote@example.com" in page.locator("main").inner_text()
    assert "Changes to existing expiry allowed" in page.locator("main").inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not fixture.google_requests


def test_native_invalid_permissions_show_recoverable_error_before_google(bridge):
    fixture, fetch = bridge
    page = open_capability(fixture, ACCOUNT_USERS)
    page.get_by_label("Row 1: Developer permission ceiling", exact=True).fill("UNBOUNDED_ADMIN")
    page.get_by_role("button", name="Continue to Google", exact=True).click()
    playwright.expect(page.get_by_role("heading", name="Invalid consent choices")).to_be_visible()
    assert "Go back to correct your entries" in page.locator("main").inner_text()
    assert not fixture.google_requests
    fetch.assert_not_called()
    page.go_back()
    playwright.expect(
        page.get_by_role("heading", name="Connect your Google Play account")
    ).to_be_visible()
