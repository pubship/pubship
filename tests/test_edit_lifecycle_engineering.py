import json
import time
import urllib.error
from unittest.mock import Mock, patch

import pytest

from pubship import edit_lifecycle_http
from pubship.config import Settings
from pubship.edit_lifecycle import EditLifecycle
from pubship.errors import PlayError
from pubship.publishing import _APPLY_LOCK

PACKAGE = "com.example.app"
PARAMETERS = {"packageName": PACKAGE, "editId": "edit-1"}
SETTINGS = Settings(
    packages=frozenset({PACKAGE}),
    write_packages=frozenset({PACKAGE}),
    edit_packages=frozenset({PACKAGE}),
)


@pytest.fixture
def service():
    return EditLifecycle(SETTINGS, Mock(token=Mock(return_value="private-token")), local=True)


@pytest.fixture
def edit():
    return {"id": "edit-1", "expiryTimeSeconds": str(int(time.time()) + 3600)}


@pytest.mark.parametrize("action", ["create", "validate", "discard", "commit"])
@pytest.mark.parametrize("ack", [False, None, 1, "true"])
def test_effects_acknowledgement_precedes_credentials(service, action, ack):
    parameters = {"packageName": PACKAGE} if action == "create" else PARAMETERS
    with patch("pubship.edit_lifecycle.http.get") as get:
        with pytest.raises(PlayError, match="acknowledge_effects"):
            service.prepare(action, parameters, ack)
    service.auth.token.assert_not_called()
    get.assert_not_called()


def test_create_preparation_reads_no_provider_and_apply_creates_once(service, edit):
    with (
        patch("pubship.edit_lifecycle.http.get") as get,
        patch("pubship.edit_lifecycle_http.execute", return_value=edit) as write,
    ):
        proposal = service.prepare("create", {"packageName": PACKAGE}, True)
        assert "invalidate" in proposal["effects"]
        assert proposal["proposed_request"]["body"] == {}
        assert not proposal["edit_metadata_check_performed"]
        operation = proposal["operation_id"]
        result = service.apply(operation, operation)
        with pytest.raises(PlayError, match="consumed"):
            service.apply(operation, operation)
    get.assert_not_called()
    write.assert_called_once_with("create", {"packageName": PACKAGE}, "private-token")
    assert result["created"] and result["executed"]
    assert not result["publication_verified"]


def test_commit_preserves_parameters_token_and_reports_incomplete_visibility(service, edit):
    parameters = {**PARAMETERS, "changesNotSentForReview": True}
    with (
        patch("pubship.edit_lifecycle.http.get", return_value=edit) as get,
        patch("pubship.edit_lifecycle_http.execute", return_value=edit) as write,
    ):
        proposal = service.prepare("commit", parameters, True)
        assert proposal["scope"] == "entire_edit"
        parameters["changesNotSentForReview"] = False
        proposal["proposed_request"]["parameters"]["editId"] = "changed"
        service.auth.token.return_value = "different-principal"
        operation = proposal["operation_id"]
        result = service.apply(operation, operation)
    service.auth.token.assert_called_once_with("publisher")
    assert get.call_count == 2
    assert all(call.args[1] == "private-token" for call in get.call_args_list)
    write.assert_called_once_with(
        "commit",
        {
            **PARAMETERS,
            "changesNotSentForReview": True,
            "changesInReviewBehavior": "ERROR_IF_IN_REVIEW",
        },
        "private-token",
    )
    assert result["committed"] and result["edit_metadata_check_performed"]
    for flag in ("publication_verified", "content_drift_checked", "full_edit_contents_verified"):
        assert result[flag] is False and proposal[flag] is False
    assert "private-token" not in json.dumps([proposal, result])


def test_metadata_change_consumes_without_mutation(service, edit):
    with (
        patch("pubship.edit_lifecycle.http.get", side_effect=[edit, {**edit, "extra": True}]),
        patch("pubship.edit_lifecycle_http.execute") as write,
    ):
        operation = service.prepare("discard", PARAMETERS, True)["operation_id"]
        with pytest.raises(PlayError, match="metadata changed"):
            service.apply(operation, operation)
        with pytest.raises(PlayError, match="consumed"):
            service.apply(operation, operation)
    write.assert_not_called()


def test_shared_apply_lock_consumes_lifecycle_operation(service):
    proposal = service.prepare("create", {"packageName": PACKAGE}, True)
    with _APPLY_LOCK, patch("pubship.edit_lifecycle_http.execute") as write:
        with pytest.raises(PlayError, match="in progress"):
            service.apply(proposal["operation_id"], proposal["operation_id"])
    write.assert_not_called()
    assert not service._preparations


@pytest.mark.parametrize(
    ("action", "parameters", "http_method", "suffix", "body"),
    [
        ("create", {"packageName": PACKAGE}, "POST", "/edits", b"{}"),
        ("validate", PARAMETERS, "POST", "/edits/edit-1:validate", None),
        ("discard", PARAMETERS, "DELETE", "/edits/edit-1", None),
        (
            "commit",
            PARAMETERS,
            "POST",
            "/edits/edit-1:commit?changesNotSentForReview=false&changesInReviewBehavior=ERROR_IF_IN_REVIEW",
            None,
        ),
        (
            "commit",
            {**PARAMETERS, "changesNotSentForReview": True},
            "POST",
            "/edits/edit-1:commit?changesNotSentForReview=true&changesInReviewBehavior=ERROR_IF_IN_REVIEW",
            None,
        ),
    ],
)
def test_exact_provider_wire(action, parameters, http_method, suffix, body, edit):
    response = Mock()
    response.read.return_value = json.dumps({} if action == "discard" else edit).encode()
    opener = Mock(handlers=[])
    opener.open.return_value.__enter__ = Mock(return_value=response)
    opener.open.return_value.__exit__ = Mock(return_value=False)
    with patch("urllib.request.build_opener", return_value=opener) as build:
        edit_lifecycle_http.execute(action, parameters, "private-token")
    request = opener.open.call_args.args[0]
    assert request.full_url.endswith(suffix)
    assert request.get_method() == http_method
    assert request.data == body
    assert request.get_header("Authorization") == "Bearer private-token"
    assert opener.open.call_count == 1
    assert response.read.call_args.args == (20 * 1024 * 1024 + 1,)
    assert isinstance(build.call_args.args[0], edit_lifecycle_http.NoRedirect)


@pytest.mark.parametrize("response", [b"", b"{}"])
def test_delete_accepts_documented_empty_result(response):
    opener = Mock(handlers=[])
    opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=response)))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    with patch("urllib.request.build_opener", return_value=opener):
        assert edit_lifecycle_http.execute("discard", PARAMETERS, "private-token") == {}


@pytest.mark.parametrize(
    ("action", "parameters"),
    [
        ("commit", {**PARAMETERS, "changesNotSentForReview": "true"}),
        ("commit", {**PARAMETERS, "changesNotSentForReview": 1}),
        ("commit", {**PARAMETERS, "changesInReviewBehavior": "CANCEL_IN_REVIEW_AND_SUBMIT"}),
        ("create", PARAMETERS),
        ("discard", {**PARAMETERS, "body": {}}),
        ("validate", {**PARAMETERS, "editId": "edit-1:commit"}),
        ("commit", {**PARAMETERS, "editId": "edit-1?flag=true"}),
        ("other", PARAMETERS),
    ],
)
def test_invalid_requests_never_dispatch(action, parameters):
    with patch("urllib.request.build_opener") as opener:
        with pytest.raises(PlayError):
            edit_lifecycle_http.execute(action, parameters, "private-token")
    opener.assert_not_called()


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("https://private.invalid", 403, "PRIVATE", {}, None),
        TimeoutError("PRIVATE"),
        PlayError("PRIVATE redirect"),
    ],
)
def test_dispatch_failures_are_redacted_uncertain_and_not_retried(failure):
    opener = Mock(handlers=[])
    opener.open.side_effect = failure
    with patch("urllib.request.build_opener", return_value=opener):
        with pytest.raises(PlayError, match="outcome unknown") as error:
            edit_lifecycle_http.execute("commit", PARAMETERS, "private-token")
    assert "PRIVATE" not in str(error.value)
    assert "No writes were attempted" not in str(error.value)
    assert opener.open.call_count == 1
    if isinstance(failure, urllib.error.HTTPError):
        assert "HTTP 403" in str(error.value)


@pytest.mark.parametrize(
    "content",
    [
        b"PRIVATE-not-json",
        b'{"id":"wrong", "expiryTimeSeconds":"9999999999"}',
        b'{"id":"edit-1", "expiryTimeSeconds":9999999999}',
        b'{"id":"edit-1", "expiryTimeSeconds":"9999999999", "extra":NaN}',
        b"[]",
        b"x" * (20 * 1024 * 1024 + 1),
    ],
)
def test_bad_provider_response_never_claims_commit_success(content):
    opener = Mock(handlers=[])
    opener.open.return_value.__enter__ = Mock(return_value=Mock(read=Mock(return_value=content)))
    opener.open.return_value.__exit__ = Mock(return_value=False)
    with patch("urllib.request.build_opener", return_value=opener):
        with pytest.raises(PlayError, match="outcome unknown") as error:
            edit_lifecycle_http.execute("commit", PARAMETERS, "private-token")
    assert "PRIVATE" not in str(error.value)
    assert opener.open.call_count == 1


def test_commit_response_expiry_need_not_remain_future():
    response = {"id": "edit-1", "expiryTimeSeconds": "1"}
    opener = Mock(handlers=[])
    opener.open.return_value.__enter__ = Mock(
        return_value=Mock(read=Mock(return_value=json.dumps(response).encode()))
    )
    opener.open.return_value.__exit__ = Mock(return_value=False)
    with patch("urllib.request.build_opener", return_value=opener):
        assert edit_lifecycle_http.execute("commit", PARAMETERS, "private-token") == response


def test_capacity_and_expiry_fail_before_additional_credentials_or_dispatch(service):
    with patch("pubship.edit_lifecycle.MAX_PREPARATIONS", 1):
        operation = service.prepare("create", {"packageName": PACKAGE}, True)["operation_id"]
        with pytest.raises(PlayError, match="capacity"):
            service.prepare("create", {"packageName": PACKAGE}, True)
    service.auth.token.assert_called_once()
    service._preparations[operation].deadline = time.monotonic() - 1
    with patch("pubship.edit_lifecycle_http.execute") as write:
        with pytest.raises(PlayError, match="expired"):
            service.apply(operation, operation)
    write.assert_not_called()
    assert not service._preparations
