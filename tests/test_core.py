import gzip
import io
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest

from pubship import http
from pubship.auth import SCOPES, Auth, validate_token
from pubship.config import Settings
from pubship.errors import PlayError
from pubship.play import Play
from pubship.reports import Reports, parse_csv

PACKAGE = "com.example.app"
CSV = (
    "Date,Package name,Country,Daily User Installs\n"
    "2026-09-05,com.example.app,US,2\n"
    "2026-09-07,com.example.app,CA,1\n"
)


@pytest.fixture
def settings():
    return Settings(
        frozenset({PACKAGE}), "example-reports", "gcloud", "reader@example.iam.gserviceaccount.com"
    )


@pytest.fixture
def auth():
    value = Mock()
    value.token.return_value = "synthetic"
    return value


def test_catalog_can_start_without_google_configuration(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("GOOGLE_PLAY_"):
            monkeypatch.delenv(key)
    settings = Settings.from_env()
    assert settings.packages == frozenset()
    with pytest.raises(PlayError):
        settings.require_package(PACKAGE)


@pytest.mark.parametrize(
    "variable,value",
    [
        ("GOOGLE_PLAY_PACKAGES", "../other"),
        ("GOOGLE_PLAY_REPORT_BUCKET", "gs://bucket/path"),
        ("GOOGLE_PLAY_AUTH_MODE", "magic"),
        ("GOOGLE_PLAY_IMPERSONATE_SERVICE_ACCOUNT", "not-an-email"),
    ],
)
def test_configuration_validation(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(PlayError):
        Settings.from_env()


def test_app_boundary_precedes_auth_and_network(settings, auth):
    with patch.object(http, "get") as get:
        with pytest.raises(PlayError):
            Play(settings, auth).releases("com.example.other")
        with pytest.raises(PlayError):
            Reports(settings, auth).files("com.example.other", "installs", "202609")
    auth.token.assert_not_called()
    get.assert_not_called()


def test_release_state_is_preserved_and_custom_tracks_supported(settings, auth):
    data = {
        "releases": [
            {"releaseName": "1.2.0", "releaseLifecycleState": "RELEASE_LIFECYCLE_STATE_IN_REVIEW"}
        ]
    }
    with patch.object(http, "get", return_value=data) as get:
        result = Play(settings, auth).releases(PACKAGE, "closed-test")
    assert result["data"] == data
    assert "/tracks/closed-test/releases" in get.call_args.args[0]


def test_reviews_pass_pagination_and_translation(settings, auth):
    with patch.object(
        http, "get", return_value={"tokenPagination": {"nextPageToken": "next"}}
    ) as get:
        result = Play(settings, auth).reviews(PACKAGE, "token with spaces", 20, "en-US")
    assert result["data"]["tokenPagination"]["nextPageToken"] == "next"
    assert "token=token+with+spaces" in get.call_args.args[0]
    assert "translationLanguage=en-US" in get.call_args.args[0]


def test_review_id_is_encoded_not_interpolated(settings, auth):
    with patch.object(http, "get", return_value={"reviewId": "a/b?x=y"}) as get:
        Play(settings, auth).review(PACKAGE, "a/b?x=y")
    assert get.call_args.args[0].endswith("/reviews/a%2Fb%3Fx%3Dy")


@pytest.mark.parametrize(
    "data", [CSV.encode(), CSV.encode("utf-16"), gzip.compress(CSV.encode("utf-16"))]
)
def test_supported_csv_encodings(data):
    columns, rows = parse_csv(data, PACKAGE)
    assert "Daily User Installs" in columns
    assert rows[1]["Country"] == "CA"


@pytest.mark.parametrize(
    "text",
    [
        CSV.replace(PACKAGE, "com.example.other"),
        CSV + "2026-09-08\n",
        CSV.replace("2026-09-05", "not-a-date"),
        "Date,Date\n2026-09-05,2026-09-05\n",
    ],
)
def test_rejects_malformed_or_wrong_app_csv(text):
    with pytest.raises(PlayError):
        parse_csv(text.encode(), PACKAGE)


def test_decompression_limit(monkeypatch):
    monkeypatch.setattr(http, "MAX_BYTES", 100)
    with pytest.raises(PlayError, match="Expanded"):
        parse_csv(gzip.compress(b"x" * 101), PACKAGE)


def test_csv_empty_is_distinct_from_zero():
    _, rows = parse_csv(CSV.splitlines()[0].encode(), PACKAGE)
    assert rows == []


def test_report_generation_and_missing_dates(settings, auth):
    with patch.object(
        http, "get", side_effect=[{"generation": "123", "updated": "2026-09-09"}, CSV.encode()]
    ) as get:
        result = Reports(settings, auth).read(
            PACKAGE, "installs", "202609", "country", "2026-09-05", "2026-09-08", limit=1
        )
    assert result["dates_without_rows"] == ["2026-09-06", "2026-09-08"]
    assert result["report_last_date"] == "2026-09-07"
    assert result["matching_rows"] == 2
    assert result["next_offset"] == 1
    assert len(result["rows"]) == 1
    assert "generation=123" in get.call_args_list[1].args[0]


def test_report_file_scope_and_next_page(settings, auth):
    with patch.object(http, "get", return_value={"nextPageToken": "next"}) as get:
        result = Reports(settings, auth).files(PACKAGE, "ratings", "202609", "previous")
    assert result["data"]["nextPageToken"] == "next"
    assert "ratings_com.example.app_202609_" in get.call_args.args[0]
    assert "pageToken=previous" in get.call_args.args[0]


@pytest.mark.parametrize(
    "family,month,dimension,start,end",
    [
        ("../sales", "202609", "country", "2026-09-01", "2026-09-20"),
        ("installs", "202613", "country", "2026-09-01", "2026-09-20"),
        ("installs", "202609", "../../secret", "2026-09-01", "2026-09-20"),
        ("installs", "202609", "country", "2026-09-01", "2026-10-02"),
        ("installs", "202609", "country", "2026-09-20", "2026-09-01"),
    ],
)
def test_invalid_report_inputs_do_not_call_google(
    settings, auth, family, month, dimension, start, end
):
    with patch.object(http, "get") as get:
        with pytest.raises(PlayError):
            Reports(settings, auth).read(PACKAGE, family, month, dimension, start, end)
    get.assert_not_called()
    auth.token.assert_not_called()


def test_report_metadata_requires_generation(settings, auth):
    with patch.object(http, "get", return_value={}) as get:
        with pytest.raises(PlayError, match="generation"):
            Reports(settings, auth).read(
                PACKAGE, "ratings", "202609", "overview", "2026-09-01", "2026-09-02"
            )
    assert get.call_count == 1


def test_auth_errors_redact_subprocess_outputs(settings):
    with patch(
        "subprocess.run", return_value=subprocess.CompletedProcess([], 1, "SECRET", "SECRET")
    ):
        with pytest.raises(PlayError) as error:
            Auth(settings).token("storage")
    assert "SECRET" not in str(error.value)


def test_gcloud_tokens_are_scoped_cached_and_refreshed(settings):
    auth = Auth(settings)
    with patch(
        "subprocess.run", return_value=subprocess.CompletedProcess([], 0, "synthetic", "")
    ) as run:
        auth.token("storage")
        auth.token("storage")
        assert run.call_count == 1
        auth.token("publisher")
        assert run.call_count == 2
        auth.cache["storage"] = (0, "expired")
        auth.token("storage")
        assert run.call_count == 3
        assert "--scopes=" + SCOPES["storage"] in run.call_args.args[0]


def test_adc_refresh_and_unknown_expiry_no_cache():
    credentials = SimpleNamespace(token="synthetic", expiry=None, refresh=Mock())
    with patch("google.auth.default", return_value=(credentials, "example")) as default:
        auth = Auth(Settings())
        assert auth.token("publisher") == "synthetic"
        auth.token("publisher")
    assert default.call_count == 2
    credentials.refresh.assert_called()


def test_adc_impersonation_requests_target_scope():
    source = Mock()
    credentials = SimpleNamespace(
        token="synthetic", expiry=datetime.now(UTC) + timedelta(minutes=15), refresh=Mock()
    )
    settings = Settings(impersonate="reader@example.iam.gserviceaccount.com")
    with (
        patch("google.auth.default", return_value=(source, "example")),
        patch(
            "pubship.auth.impersonated_credentials.Credentials", return_value=credentials
        ) as impersonate,
    ):
        assert Auth(settings).token("storage") == "synthetic"
    assert impersonate.call_args.kwargs["target_scopes"] == [SCOPES["storage"]]


@pytest.mark.parametrize("value", ["", "a\nb", "x" * 20000, None])
def test_invalid_tokens(value):
    with pytest.raises(PlayError):
        validate_token(value)


@pytest.mark.parametrize(
    "url",
    [
        "http://storage.googleapis.com/x",
        "https://evil.example/",
        "https://storage.googleapis.com@evil.example/x",
    ],
)
def test_host_boundary(url):
    with pytest.raises(PlayError):
        http.get(url, "synthetic")


def test_redirects_block_credentials():
    with pytest.raises(PlayError):
        http.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example")


@pytest.mark.parametrize("code", [401, 403, 404, 429, 500])
def test_provider_errors_are_safe(code):
    error = HTTPError("https://example/SECRET", code, "SECRET", {}, io.BytesIO(b"SECRET"))
    with (
        patch("urllib.request.OpenerDirector.open", side_effect=error),
        pytest.raises(PlayError) as raised,
    ):
        http.get("https://storage.googleapis.com/test", "synthetic")
    assert str(code) in str(raised.value)
    assert "SECRET" not in str(raised.value)


def test_get_request_is_read_only_and_bounded():
    response = Mock()
    response.read.return_value = b'{"ok": true}'
    context = Mock()
    context.__enter__ = Mock(return_value=response)
    context.__exit__ = Mock(return_value=False)
    with patch("urllib.request.OpenerDirector.open", return_value=context) as open_request:
        assert http.get("https://storage.googleapis.com/test", "synthetic") == {"ok": True}
    assert open_request.call_args.args[0].get_method() == "GET"
    response.read.assert_called_once_with(http.MAX_BYTES + 1)


def test_adc_failure_does_not_leak_provider_error():
    with patch("google.auth.default", side_effect=RuntimeError("SECRET")):
        with pytest.raises(PlayError) as error:
            Auth(Settings()).token("publisher")
    assert "SECRET" not in str(error.value)


def test_adc_aware_expiry_retains_timezone():
    expiry = datetime.now(timezone(timedelta(hours=-8))) + timedelta(minutes=15)
    credentials = SimpleNamespace(token="synthetic", expiry=expiry, refresh=Mock())
    with patch("google.auth.default", return_value=(credentials, "example")) as default:
        auth = Auth(Settings())
        auth.token("storage")
        auth.token("storage")
    assert default.call_count == 1


def test_refresh_requests_have_a_timeout():
    from pubship.auth import BoundedRequest

    with patch("google.auth.transport.requests.Request.__call__", return_value=None) as call:
        BoundedRequest()(url="https://oauth2.googleapis.com/token", method="POST")
    assert call.call_args.kwargs["timeout"] == 15


def test_runtime_version_matches_installed_distribution():
    from importlib.metadata import version

    from pubship import __version__

    assert __version__ == version("pubship")
