import json
import time
from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from pubship import edit_writes_http
from pubship.config import Settings
from pubship.coverage import EDIT_WRITE_METHODS
from pubship.edit_writes import EditWrites
from pubship.edit_writes_contracts import (
    baseline_spec,
    request_spec,
    validate_baseline,
    validate_response,
)
from pubship.errors import PlayError

PREFIX = "androidpublisher.edits."
PACKAGE = "com.example.app"
PARAMETERS = {"packageName": PACKAGE, "editId": "synthetic-edit"}
DETAILS = {
    "defaultLanguage": "en-US",
    "contactEmail": "support@example.invalid",
    "contactPhone": "",
    "contactWebsite": "",
}
LISTING = {
    "language": "en-US",
    "title": "",
    "shortDescription": "  新\r\n",
    "fullDescription": "e\u0301 🐈",
}
CASES = {
    "details.patch": ({}, {"contactEmail": "new@example.invalid"}, DETAILS),
    "details.update": ({}, DETAILS, DETAILS),
    "expansionfiles.patch": (
        {"apkVersionCode": 10, "expansionFileType": "main"},
        {"referencesVersion": 9},
        {"fileSize": "123"},
    ),
    "expansionfiles.update": (
        {"apkVersionCode": 10, "expansionFileType": "patch"},
        {"referencesVersion": 9},
        {},
    ),
    "images.delete": (
        {"language": "en-US", "imageType": "icon", "imageId": "image1"},
        None,
        {"images": [{"id": "image1", "url": "https://untrusted.invalid"}]},
    ),
    "images.deleteall": ({"language": "en-US", "imageType": "icon"}, None, {"images": []}),
    "listings.delete": ({"language": "en-US"}, None, {"listings": [LISTING]}),
    "listings.deleteall": ({}, None, {"listings": []}),
    "listings.update": ({"language": "en-US"}, LISTING, {"listings": []}),
    "testers.patch": (
        {"track": "wear:closed-test"},
        {"googleGroups": []},
        {"googleGroups": ["group@example.invalid"]},
    ),
    "testers.update": ({"track": "closed-test"}, {"googleGroups": ["group@example.invalid"]}, {}),
    "tracks.create": (
        {},
        {"track": "wear:closed-test", "type": "CLOSED_TESTING", "formFactor": "WEAR"},
        {"tracks": []},
    ),
    "tracks.patch": (
        {"track": "production"},
        {"track": "production", "releases": [{"status": "halted", "versionCodes": ["123"]}]},
        {"track": "production", "releases": [{"status": "completed", "versionCodes": ["123"]}]},
    ),
}


@pytest.fixture
def service():
    settings = Settings(
        packages=frozenset({PACKAGE}),
        edit_write_packages=frozenset({PACKAGE}),
        edit_write_methods=EDIT_WRITE_METHODS,
    )
    return EditWrites(settings, Mock(token=Mock(return_value="synthetic-token")), local=True)


@pytest.fixture
def edit():
    return {"id": "synthetic-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}


@pytest.mark.parametrize("name", CASES)
def test_each_supported_contract_has_exact_caller_body_and_bounded_get_baseline(name):
    extra, body, current = CASES[name]
    parameters = {**PARAMETERS, **extra}
    spec = request_spec(PREFIX + name, parameters, body)
    assert spec["body"] == body
    assert spec["parameters"] == parameters
    assert (
        spec["http_method"]
        == {
            "patch": "PATCH",
            "update": "PUT",
            "create": "POST",
            "delete": "DELETE",
            "deleteall": "DELETE",
        }[name.split(".")[-1]]
    )
    baseline = baseline_spec(PREFIX + name, parameters)
    assert baseline["method"].endswith((".get", ".list"))
    assert "{" not in baseline["url"]
    validate_baseline(PREFIX + name, parameters, current)
    assert EDIT_WRITE_METHODS == {PREFIX + item for item in CASES}


@pytest.mark.parametrize(
    "name,extra,body",
    [
        ("details.patch", {}, {}),
        ("details.patch", {}, {"contactEmail": None}),
        ("details.patch", {}, {"contactPhone": "\ud800"}),
        ("details.patch", {}, {"defaultLanguage": ""}),
        ("details.patch", {}, {"defaultLanguage": "../PRIVATE"}),
        ("details.patch", {}, {"unknown": "PRIVATE"}),
        ("details.update", {}, {"defaultLanguage": "en-US"}),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "main"},
            {"fileSize": "1"},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "main"},
            {"referencesVersion": True},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "main"},
            {"referencesVersion": 1.0},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "main"},
            {"referencesVersion": 0},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "main"},
            {"referencesVersion": 2**31},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": True, "expansionFileType": "main"},
            {"referencesVersion": 9},
        ),
        (
            "expansionfiles.patch",
            {"apkVersionCode": 10, "expansionFileType": "expansionFileTypeUnspecified"},
            {"referencesVersion": 9},
        ),
        ("images.deleteall", {"language": "en-US", "imageType": "appImageTypeUnspecified"}, None),
        ("images.deleteall", {"language": "en-US", "imageType": "icon"}, {}),
        ("listings.update", {"language": "en-US"}, {"language": "en-US", "title": "name"}),
        ("listings.update", {"language": "de-DE"}, LISTING),
        ("testers.patch", {"track": "closed-test"}, {}),
        ("testers.patch", {"track": "closed-test"}, {"googleGroups": None}),
        ("testers.patch", {"track": "closed-test"}, {"googleGroups": [1]}),
        (
            "testers.patch",
            {"track": "closed-test"},
            {"googleGroups": ["group@example.invalid"] * 2},
        ),
        ("testers.patch", {"track": "closed-test"}, {"googleGroups": ["not-an-address"]}),
        (
            "testers.patch",
            {"track": "closed-test"},
            {"googleGroups": ["group\u0085@example.invalid"]},
        ),
        ("testers.patch", {"track": "closed-test"}, {"googleGroups": [], "emails": ["PRIVATE"]}),
        ("tracks.patch", {"track": "production"}, {"track": "production"}),
        ("tracks.patch", {"track": "production"}, {"track": "other", "releases": []}),
        (
            "tracks.patch",
            {"track": "production"},
            {"track": "production", "releases": [{"status": "inProgress", "versionCodes": ["1"]}]},
        ),
        (
            "tracks.create",
            {},
            {"track": "closed", "type": "TRACK_TYPE_UNSPECIFIED", "formFactor": "DEFAULT"},
        ),
        ("tracks.create", {}, {"track": "closed", "type": "CLOSED_TESTING", "formFactor": "WEAR"}),
        (
            "tracks.create",
            {},
            {"track": "wear:closed", "type": "CLOSED_TESTING", "formFactor": "DEFAULT"},
        ),
        ("tracks.create", {}, {"track": "wear:", "type": "CLOSED_TESTING", "formFactor": "WEAR"}),
        (
            "tracks.create",
            {},
            {"track": "automotive:a:b", "type": "CLOSED_TESTING", "formFactor": "AUTOMOTIVE"},
        ),
        ("tracks.create", {}, {"track": "closed", "type": "CLOSED_TESTING"}),
    ],
)
def test_request_failures_precede_credentials(service, name, extra, body):
    with patch("pubship.http.get") as get, pytest.raises(PlayError) as error:
        service.prepare(PREFIX + name, {**PARAMETERS, **extra}, body, True)
    service.auth.token.assert_not_called()
    get.assert_not_called()
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "track,form_factor",
    [("closed", "DEFAULT"), ("wear:closed", "WEAR"), ("automotive:closed", "AUTOMOTIVE")],
)
def test_closed_track_creation_supported_prefixes(track, form_factor):
    body = {"track": track, "formFactor": form_factor, "type": "CLOSED_TESTING"}
    assert request_spec(PREFIX + "tracks.create", PARAMETERS, body)["body"] == body


def test_prepare_preserves_exact_unicode_and_missing_video(service, edit):
    body = deepcopy(LISTING)
    with patch("pubship.http.get", side_effect=[edit, {"listings": []}]):
        prepared = service.prepare(
            PREFIX + "listings.update", {**PARAMETERS, "language": "en-US"}, body, True
        )
    assert prepared["proposed_request"]["body"] == body
    assert "video" not in prepared["proposed_request"]["body"]
    assert prepared["before"] == {"listings": []}
    assert not prepared["executed"] and not prepared["provider_validation_performed"]


def test_creation_requires_absence_in_validated_collection(service, edit):
    body = CASES["tracks.create"][1]
    for current in ({}, {"tracks": None}, {"tracks": [{}]}, {"tracks": [{"track": body["track"]}]}):
        with (
            patch("pubship.http.get", side_effect=[edit, current]),
            pytest.raises(PlayError),
        ):
            service.prepare(PREFIX + "tracks.create", PARAMETERS, body, True)
    assert not service._preparations


def test_actual_observation_reports_raced_deletion_honestly(service, edit):
    extra, _, current = CASES["images.delete"]
    with (
        patch("pubship.http.get", side_effect=[edit, current, edit, current, current]),
        patch("pubship.edit_writes_http.execute", return_value={}) as execute,
    ):
        prepared = service.prepare(PREFIX + "images.delete", {**PARAMETERS, **extra}, None, True)
        result = service.apply(prepared["operation_id"], prepared["operation_id"])
    execute.assert_called_once()
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert result["deletion_observed"] is False and result["after"] == current
    assert not result["committed"] and not result["publication_verified"]


def test_retained_input_token_and_public_output_are_isolated(service, edit):
    body = {"googleGroups": ["new@example.invalid"]}
    parameters = {**PARAMETERS, "track": "wear:closed"}
    before = {"googleGroups": ["old@example.invalid"]}
    after = {"googleGroups": ["provider@example.invalid"]}
    with (
        patch("pubship.http.get", side_effect=[edit, before, edit, before, after]) as get,
        patch("pubship.edit_writes_http.execute", return_value={"googleGroups": []}) as execute,
    ):
        prepared = service.prepare(PREFIX + "testers.patch", parameters, body, True)
        body["googleGroups"].append("other@example.invalid")
        prepared["proposed_request"]["body"]["googleGroups"].clear()
        service.auth.token.return_value = "changed-principal"
        result = service.apply(prepared["operation_id"], prepared["operation_id"])
    execute.assert_called_once_with(
        PREFIX + "testers.patch",
        parameters,
        {"googleGroups": ["new@example.invalid"]},
        "synthetic-token",
    )
    assert all(call.args[1] == "synthetic-token" for call in get.call_args_list)
    assert result["mutation_response"] == {"googleGroups": []} and result["after"] == after
    service.auth.token.assert_called_once_with("publisher")
    assert "synthetic-token" not in json.dumps(prepared)


def test_expiry_capacity_and_retained_snapshot_bounds(service, edit):
    edit["expiryTimeSeconds"] = str(int(time.time()) + 60)
    with (
        patch("pubship.edit_writes.MAX_PREPARATIONS", 1),
        patch("pubship.http.get", side_effect=[edit, DETAILS]) as get,
    ):
        prepared = service.prepare(PREFIX + "details.patch", PARAMETERS, {"contactPhone": ""}, True)
        assert (
            datetime.fromisoformat(prepared["expires_at"]).timestamp()
            <= int(edit["expiryTimeSeconds"]) + 0.01
        )
        with pytest.raises(PlayError, match="capacity"):
            service.prepare(PREFIX + "details.patch", PARAMETERS, {"contactPhone": ""}, True)
        operation = prepared["operation_id"]
        service._preparations[operation].deadline = time.monotonic() - 1
        with pytest.raises(PlayError, match="expired"):
            service.apply(operation, operation)
        assert get.call_count == 2
    with (
        patch("pubship.http.get", side_effect=[edit, {**DETAILS, "unknown": "x" * 65536}]),
        pytest.raises(PlayError, match="64 KiB"),
    ):
        service.prepare(PREFIX + "details.patch", PARAMETERS, {"contactPhone": ""}, True)


@pytest.mark.parametrize(
    "method,parameters,body,data",
    [
        ("details.patch", PARAMETERS, {"contactPhone": ""}, {"contactEmail": None}),
        (
            "expansionfiles.update",
            {**PARAMETERS, "apkVersionCode": 2, "expansionFileType": "main"},
            {"referencesVersion": 1},
            {"fileSize": "0", "referencesVersion": 1},
        ),
        (
            "images.delete",
            {**PARAMETERS, "language": "en-US", "imageType": "icon", "imageId": "id"},
            None,
            {"unexpected": "PRIVATE"},
        ),
        (
            "images.deleteall",
            {**PARAMETERS, "language": "en-US", "imageType": "icon"},
            None,
            {"deleted": [None]},
        ),
        ("listings.update", {**PARAMETERS, "language": "en-US"}, LISTING, {"language": "de-DE"}),
        (
            "testers.patch",
            {**PARAMETERS, "track": "closed"},
            {"googleGroups": []},
            {"googleGroups": [1]},
        ),
        ("tracks.create", PARAMETERS, CASES["tracks.create"][1], {"track": "wrong"}),
    ],
)
def test_malformed_mutation_responses_are_not_success(method, parameters, body, data):
    with pytest.raises(PlayError) as error:
        validate_response(PREFIX + method, parameters, body, data)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [b"", b"{}", b'{"deleted":[]}', b'{"deleted":[{"id":"id","url":"https://untrusted.invalid"}]}'],
)
def test_delete_all_wire_supports_documented_empty_or_deleted_images(payload):
    extra, _, _ = CASES["images.deleteall"]
    response = Mock(read=Mock(return_value=payload))
    context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        result = edit_writes_http.execute(
            PREFIX + "images.deleteall", {**PARAMETERS, **extra}, None, "synthetic-token"
        )
    request.assert_called_once()
    assert (
        request.call_args.args[0].get_method() == "DELETE"
        and request.call_args.args[0].data is None
    )
    assert result == (json.loads(payload) if payload else {})


def test_scope_configuration_is_independent_and_exact(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_EDIT_WRITE_PACKAGES", " " + PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_EDIT_WRITE_METHODS", PREFIX + "details.patch")
    for suffix in ("WRITE_PACKAGES", "EDIT_PACKAGES", "TRACK_PACKAGES", "SENSITIVE_READ_PACKAGES"):
        monkeypatch.delenv("GOOGLE_PLAY_" + suffix, raising=False)
    settings = Settings.from_env()
    assert settings.edit_write_packages == frozenset({PACKAGE})
    assert settings.edit_write_methods == frozenset({PREFIX + "details.patch"})
    assert (
        not settings.write_packages
        and not settings.edit_packages
        and not settings.track_packages
        and not settings.sensitive_read_packages
    )
    for value in (
        frozenset({"*"}),
        frozenset({PREFIX}),
        frozenset({PREFIX + "commit"}),
        {PREFIX + "details.patch"},
    ):
        with pytest.raises(PlayError, match="EDIT_WRITE_METHODS"):
            Settings(edit_write_methods=value)
    with pytest.raises(PlayError, match="EDIT_WRITE_PACKAGES"):
        Settings(edit_write_packages=frozenset({PACKAGE}))
