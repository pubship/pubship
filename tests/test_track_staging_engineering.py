import json
import time
from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from pubship import track_staging_http
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.track_staging import TrackStaging, track_preview

PACKAGE = "com.example.app"
PARAMETERS = {"packageName": PACKAGE, "editId": "synthetic-edit", "track": "wear:production"}
RELEASE = {"status": "completed", "versionCodes": ["99"]}


@pytest.fixture
def service():
    settings = Settings(packages=frozenset({PACKAGE}), track_packages=frozenset({PACKAGE}))
    return TrackStaging(settings, Mock(token=Mock(return_value="synthetic-token")), local=True)


@pytest.fixture
def edit():
    return {"id": "synthetic-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}


def test_full_pinned_release_fields_and_exact_unicode_are_preserved():
    release = {
        "name": "  发布 e\u0301 🐈\n",
        "status": "inProgress",
        "versionCodes": ["1", "9223372036854775807"],
        "userFraction": 0.25,
        "inAppUpdatePriority": 5,
        "releaseNotes": [
            {"language": "zh-Hant-TW", "text": "  新\r\n"},
            {"language": "en-US", "text": ""},
        ],
        "countryTargeting": {"countries": ["US", "PT"], "includeRestOfWorld": False},
    }
    current = {"track": "wear:production", "unknown": {"provider": "kept"}}
    preview = track_preview(
        {"data": current, "fetched_at": "synthetic-time"}, PARAMETERS, [release]
    )
    assert preview["before"] == current
    assert preview["proposed"] == {"track": "wear:production", "releases": [release]}
    assert preview["proposed_request"]["body"] == preview["proposed"]
    assert preview["proposed_request"]["url"].endswith("/tracks/wear%3Aproduction")
    assert preview["changed"] and preview["preview_only"]
    assert not preview["provider_validation_performed"]
    assert not preview["stale_check_performed"] and not preview["executed"]
    assert "not a predicted final track" in preview["note"]
    release["releaseNotes"][0]["text"] = "caller mutation"
    current["unknown"]["provider"] = "caller mutation"
    assert preview["before"]["unknown"]["provider"] == "kept"
    assert preview["proposed"]["releases"][0]["releaseNotes"][0]["text"] == "  新\r\n"


@pytest.mark.parametrize(
    "status,fraction",
    [("draft", None), ("completed", None), ("halted", None), ("halted", 0.1), ("inProgress", 0.2)],
)
def test_supported_statuses_do_not_invent_transition_restrictions(status, fraction):
    release = {**RELEASE, "status": status}
    if fraction is not None:
        release["userFraction"] = fraction
    track_staging_http.validate_track_request(PARAMETERS, [release])


@pytest.mark.parametrize(
    "release",
    [
        {},
        {"status": "completed"},
        {"versionCodes": ["1"]},
        {**RELEASE, "status": "statusUnspecified"},
        {**RELEASE, "status": "PRIVATE"},
        {**RELEASE, "versionCodes": []},
        {**RELEASE, "versionCodes": ["0"]},
        {**RELEASE, "versionCodes": ["01"]},
        {**RELEASE, "versionCodes": ["-1"]},
        {**RELEASE, "versionCodes": ["9223372036854775808"]},
        {**RELEASE, "versionCodes": ["1", "1"]},
        {**RELEASE, "versionCodes": [1]},
        {**RELEASE, "userFraction": 0.5},
        {**RELEASE, "status": "inProgress"},
        {**RELEASE, "status": "inProgress", "userFraction": True},
        {**RELEASE, "status": "inProgress", "userFraction": 0},
        {**RELEASE, "status": "inProgress", "userFraction": 1},
        {**RELEASE, "status": "inProgress", "userFraction": 10**400},
        {**RELEASE, "status": "inProgress", "userFraction": float("nan")},
        {**RELEASE, "status": "inProgress", "userFraction": float("inf")},
        {**RELEASE, "inAppUpdatePriority": True},
        {**RELEASE, "inAppUpdatePriority": 1.0},
        {**RELEASE, "inAppUpdatePriority": 6},
        {**RELEASE, "inAppUpdatePriority": -1},
        {**RELEASE, "name": "\ud800"},
        {**RELEASE, "name": None},
        {**RELEASE, "unknown": "PRIVATE"},
        {**RELEASE, "releaseNotes": [{"language": "en-US"}]},
        {**RELEASE, "releaseNotes": [{"language": "en-US", "text": None}]},
        {**RELEASE, "releaseNotes": [{"language": "../escape", "text": "PRIVATE"}]},
        {**RELEASE, "releaseNotes": [{"language": "en-US", "text": "a"}] * 2},
        {**RELEASE, "countryTargeting": {"countries": ["US"]}},
        {
            **RELEASE,
            "status": "inProgress",
            "userFraction": 0.1,
            "countryTargeting": {"countries": ["us"]},
        },
        {
            **RELEASE,
            "status": "inProgress",
            "userFraction": 0.1,
            "countryTargeting": {"countries": ["US", "US"]},
        },
        {
            **RELEASE,
            "status": "inProgress",
            "userFraction": 0.1,
            "countryTargeting": {"includeRestOfWorld": 1},
        },
        {
            **RELEASE,
            "status": "inProgress",
            "userFraction": 0.1,
            "countryTargeting": {"unknown": "PRIVATE"},
        },
    ],
)
def test_invalid_release_contract_fails_before_auth(service, release):
    with patch("pubship.track_staging.http.get") as get:
        with pytest.raises(PlayError) as error:
            service.prepare(PARAMETERS, [release], True)
    assert "PRIVATE" not in str(error.value)
    service.auth.token.assert_not_called()
    get.assert_not_called()


def test_country_targeting_requires_production_and_does_not_strip_fields():
    release = {
        **RELEASE,
        "status": "inProgress",
        "userFraction": 0.1,
        "countryTargeting": {"countries": ["US"]},
    }
    with pytest.raises(PlayError, match="production"):
        track_staging_http.validate_track_request({**PARAMETERS, "track": "wear:beta"}, [release])
    track_staging_http.validate_track_request(PARAMETERS, [release])


@pytest.mark.parametrize(
    "track", ["production", "custom-testing", "automotive:beta", "android_xr:custom-test"]
)
def test_track_names_remain_opaque_bounded_path_segments(track):
    spec = track_staging_http.request_spec({**PARAMETERS, "track": track}, [])
    assert spec["url"].endswith("/tracks/" + track.replace(":", "%3A"))


@pytest.mark.parametrize(
    "track", ["wear%3Aproduction", "../production", "x/y", "x?y", "x#y", ".", "..", "x" * 101]
)
def test_transport_rejects_path_escape_before_dispatch(track):
    with patch("urllib.request.OpenerDirector.open") as request, pytest.raises(PlayError):
        track_staging_http.execute({**PARAMETERS, "track": track}, [RELEASE], "synthetic")
    request.assert_not_called()


def test_snapshot_bound_and_input_size_are_enforced(service, edit):
    with pytest.raises(PlayError, match="64 KiB"):
        service.prepare(PARAMETERS, [{**RELEASE, "name": "x" * (64 * 1024)}], True)
    service.auth.token.assert_not_called()
    with patch(
        "pubship.track_staging.http.get",
        side_effect=[edit, {"track": "wear:production", "extra": "x" * (64 * 1024)}],
    ):
        with pytest.raises(PlayError, match="64 KiB"):
            service.prepare(PARAMETERS, [RELEASE], True)
    assert service._preparations == {}


def test_provider_shape_validation_preserves_future_fields_and_sparse_values():
    current = {
        "track": "wear:production",
        "future": {"field": 1},
        "releases": [{"status": "futureStatus", "future": True}],
    }
    track_staging_http.validate_response(current, PARAMETERS)
    track_staging_http.validate_response({"track": "wear:production"}, PARAMETERS)


@pytest.mark.parametrize(
    "current",
    [
        None,
        [],
        {},
        {"track": "production"},
        {"track": "wear:production", "releases": None},
        {"track": "wear:production", "releases": [None]},
        {"track": "wear:production", "releases": [{"userFraction": True}]},
        {"track": "wear:production", "releases": [{"countryTargeting": None}]},
        {"track": "wear:production", "extra": "\udfff"},
    ],
)
def test_malformed_provider_shape_is_rejected(current):
    with pytest.raises(PlayError):
        track_staging_http.validate_response(current, PARAMETERS)


def test_clear_is_explicit_and_noop_is_consumed_without_put(service, edit):
    current = {"track": "wear:production", "releases": []}
    with (
        patch("pubship.track_staging.http.get", side_effect=[edit, current] * 2),
        patch("pubship.track_staging_http.execute") as put,
    ):
        prepared = service.prepare(PARAMETERS, [], True)
        assert prepared["clears_desired_releases"] and not prepared["changed"]
        result = service.apply(prepared["operation_id"], prepared["operation_id"])
        assert result["no_op"] and not result["put_attempted"]
        assert result["stale_check_performed"] and not result["committed"]
        with pytest.raises(PlayError, match="consumed"):
            service.apply(prepared["operation_id"], prepared["operation_id"])
    put.assert_not_called()


def test_lifetime_is_capped_by_edit_expiry_and_capacity(service, edit):
    edit["expiryTimeSeconds"] = str(int(time.time()) + 60)
    current = {"track": "wear:production"}
    with (
        patch("pubship.track_staging.MAX_PREPARATIONS", 1),
        patch("pubship.track_staging.http.get", side_effect=[edit, current]) as get,
    ):
        prepared = service.prepare(PARAMETERS, [RELEASE], True)
        assert (
            datetime.fromisoformat(prepared["expires_at"]).timestamp()
            <= int(edit["expiryTimeSeconds"]) + 0.01
        )
        with pytest.raises(PlayError, match="capacity"):
            service.prepare(PARAMETERS, [RELEASE], True)
        operation = prepared["operation_id"]
        service._preparations[operation].deadline = time.monotonic() - 1
        with pytest.raises(PlayError, match="expired"):
            service.apply(operation, operation)
        assert get.call_count == 2


def test_wire_put_uses_encoded_track_exact_body_and_actual_provider_data():
    actual = {
        "track": "wear:production",
        "releases": [
            {**RELEASE, "name": "Google supplied"},
            {"status": "halted", "versionCodes": ["98"]},
        ],
    }
    response = Mock(read=Mock(return_value=json.dumps(actual).encode()))
    context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        assert track_staging_http.execute(PARAMETERS, [RELEASE], "synthetic-token") == actual
    request.assert_called_once()
    wire = request.call_args.args[0]
    assert wire.get_method() == "PUT"
    assert wire.full_url.endswith("/tracks/wear%3Aproduction")
    assert json.loads(wire.data) == {"track": "wear:production", "releases": [RELEASE]}
    assert wire.get_header("Authorization") == "Bearer synthetic-token"
    assert request.call_args.kwargs == {"timeout": 15}


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not JSON PRIVATE",
        b"null",
        b'{"track":"wear:production"}',
        b'{"track":"wear:production","releases":[{"userFraction":NaN}]}',
        b'{"track":"wear:production","releases":null}',
        b'{"track":"wear:production","releases":[{}]}',
        b'{"track":"wear:production","releases":[{"versionCodes":["not-an-int"]}]}',
        b'{"track":"wear:production","releases":[{"versionCodes":["9223372036854775808"]}]}',
        b'{"track":"wear:production","releases":[{"versionCodes":[]}]}',
    ],
)
def test_malformed_put_response_is_unknown_outcome(body):
    response = Mock(read=Mock(return_value=body))
    context = Mock(__enter__=Mock(return_value=response), __exit__=Mock(return_value=False))
    with patch("urllib.request.OpenerDirector.open", return_value=context) as request:
        with pytest.raises(PlayError, match="outcome unknown") as error:
            track_staging_http.execute(PARAMETERS, [RELEASE], "synthetic-token")
    assert "PRIVATE" not in str(error.value)
    request.assert_called_once()


def test_track_scope_is_parsed_independently_from_listing_and_lifecycle(monkeypatch):
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", PACKAGE)
    monkeypatch.setenv("GOOGLE_PLAY_TRACK_PACKAGES", " " + PACKAGE + " ")
    monkeypatch.delenv("GOOGLE_PLAY_WRITE_PACKAGES", raising=False)
    monkeypatch.delenv("GOOGLE_PLAY_EDIT_PACKAGES", raising=False)
    settings = Settings.from_env()
    assert settings.track_packages == settings.packages == frozenset({PACKAGE})
    assert settings.write_packages == settings.edit_packages == frozenset()
    for value in (frozenset({"com.other.app"}), frozenset({"INVALID"}), {PACKAGE}):
        with pytest.raises(PlayError, match="TRACK_PACKAGES"):
            Settings(packages=frozenset({PACKAGE}), track_packages=value)


def test_missing_releases_is_distinct_from_empty_in_preview():
    missing = track_preview(
        {"data": {"track": "wear:production"}, "fetched_at": "now"}, PARAMETERS, []
    )
    empty = track_preview(
        {"data": {"track": "wear:production", "releases": []}, "fetched_at": "now"}, PARAMETERS, []
    )
    assert missing["changed"] and "releases" not in missing["before"]
    assert not empty["changed"] and empty["before"]["releases"] == []


def test_returned_proposal_cannot_mutate_retained_release(service, edit):
    releases = [deepcopy(RELEASE)]
    current = {"track": "wear:production"}
    with (
        patch("pubship.track_staging.http.get", side_effect=[edit, current] * 2),
        patch(
            "pubship.track_staging_http.execute",
            return_value={"track": "wear:production", "releases": [RELEASE]},
        ) as put,
    ):
        prepared = service.prepare(PARAMETERS, releases, True)
        releases[0]["versionCodes"].append("100")
        prepared["proposed"]["releases"][0]["versionCodes"].append("101")
        prepared["proposed_request"]["body"]["releases"][0]["versionCodes"].append("102")
        service.apply(prepared["operation_id"], prepared["operation_id"])
    put.assert_called_once_with(PARAMETERS, [RELEASE], "synthetic-token")
