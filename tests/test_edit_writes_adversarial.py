"""Independent edit-resource-write acceptance; synthetic Google transport only."""

import asyncio
import io
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from unittest.mock import Mock, patch
from urllib.error import HTTPError

import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from pubship.config import Settings
from pubship.edit_lifecycle import EditLifecycle
from pubship.edit_writes import EditWrites
from pubship.errors import PlayError
from pubship.server import create_server

APP = "com.example.app"
PREFIX = "androidpublisher.edits."
PARAMS = {"packageName": APP, "editId": "owned-edit"}
BASE = f"https://androidpublisher.googleapis.com/androidpublisher/v3/applications/{APP}/edits/owned-edit"
DETAILS = {
    "contactEmail": "support@example.test",
    "contactPhone": "+123",
    "contactWebsite": "https://example.test",
    "defaultLanguage": "en-US",
}
LISTING = {
    "language": "en-US",
    "title": "Title",
    "shortDescription": "Short",
    "fullDescription": "Full",
    "video": "",
}
TRACK = "wear:closed-qa"
IMAGE = {"id": "image-1", "url": "https://untrusted.example.test/image", "sha256": "synthetic"}

# verb, mutation suffix, extra parameters, exact body, baseline suffix,
# baseline response, mutation response, fresh readback response. This table is
# independent of the implementation's method-to-resource mapping.
CASES = {
    "details.patch": (
        "PATCH",
        "/details",
        {},
        {"contactEmail": "new@example.test"},
        "/details",
        DETAILS,
        {**DETAILS, "contactEmail": "new@example.test"},
        {**DETAILS, "contactEmail": "new@example.test"},
    ),
    "details.update": (
        "PUT",
        "/details",
        {},
        DETAILS,
        "/details",
        {**DETAILS, "contactPhone": "old"},
        DETAILS,
        DETAILS,
    ),
    "expansionfiles.patch": (
        "PATCH",
        "/apks/22/expansionFiles/main",
        {"apkVersionCode": 22, "expansionFileType": "main"},
        {"referencesVersion": 21},
        "/apks/22/expansionFiles/main",
        {"fileSize": "9223372036854775807"},
        {"referencesVersion": 21},
        {"referencesVersion": 21},
    ),
    "expansionfiles.update": (
        "PUT",
        "/apks/22/expansionFiles/patch",
        {"apkVersionCode": 22, "expansionFileType": "patch"},
        {"referencesVersion": 21},
        "/apks/22/expansionFiles/patch",
        {"referencesVersion": 20},
        {"referencesVersion": 21},
        {"referencesVersion": 21},
    ),
    "images.delete": (
        "DELETE",
        "/listings/en-US/phoneScreenshots/image-1",
        {"language": "en-US", "imageType": "phoneScreenshots", "imageId": "image-1"},
        None,
        "/listings/en-US/phoneScreenshots",
        {"images": [IMAGE]},
        None,
        {"images": []},
    ),
    "images.deleteall": (
        "DELETE",
        "/listings/en-US/phoneScreenshots",
        {"language": "en-US", "imageType": "phoneScreenshots"},
        None,
        "/listings/en-US/phoneScreenshots",
        {"images": [IMAGE]},
        {"deleted": [IMAGE]},
        {"images": []},
    ),
    "listings.delete": (
        "DELETE",
        "/listings/en-US",
        {"language": "en-US"},
        None,
        "/listings",
        {"listings": [LISTING]},
        None,
        {"listings": []},
    ),
    "listings.deleteall": (
        "DELETE",
        "/listings",
        {},
        None,
        "/listings",
        {"listings": [LISTING]},
        None,
        {"listings": []},
    ),
    "listings.update": (
        "PUT",
        "/listings/en-US",
        {"language": "en-US"},
        LISTING,
        "/listings",
        {"listings": []},
        LISTING,
        {"listings": [LISTING]},
    ),
    "testers.patch": (
        "PATCH",
        "/testers/wear%3Aclosed-qa",
        {"track": TRACK},
        {"googleGroups": ["new-group@example.test"]},
        "/testers/wear%3Aclosed-qa",
        {"googleGroups": ["old-group@example.test"]},
        {"googleGroups": ["new-group@example.test"]},
        {"googleGroups": ["new-group@example.test"]},
    ),
    "testers.update": (
        "PUT",
        "/testers/wear%3Aclosed-qa",
        {"track": TRACK},
        {"googleGroups": []},
        "/testers/wear%3Aclosed-qa",
        {"googleGroups": ["old-group@example.test"]},
        {"googleGroups": []},
        {"googleGroups": []},
    ),
    "tracks.create": (
        "POST",
        "/tracks",
        {},
        {"track": TRACK, "formFactor": "WEAR", "type": "CLOSED_TESTING"},
        "/tracks",
        {"tracks": [{"track": "production", "releases": []}]},
        {"track": TRACK, "releases": []},
        {"tracks": [{"track": "production", "releases": []}, {"track": TRACK, "releases": []}]},
    ),
    "tracks.patch": (
        "PATCH",
        "/tracks/wear%3Aclosed-qa",
        {"track": TRACK},
        {"track": TRACK, "releases": [{"status": "draft", "versionCodes": ["22"]}]},
        "/tracks/wear%3Aclosed-qa",
        {"track": TRACK, "releases": [{"status": "completed", "versionCodes": ["21"]}]},
        {"track": TRACK, "releases": [{"status": "draft", "versionCodes": ["22"]}]},
        {"track": TRACK, "releases": [{"status": "draft", "versionCodes": ["22"]}]},
    ),
}
ALL_METHODS = frozenset(PREFIX + method for method in CASES)


def configured(*, methods=ALL_METHODS, packages=frozenset({APP})):
    return Settings(packages=packages, edit_write_packages=packages, edit_write_methods=methods)


def fixture_service():
    auth = Mock()
    auth.token.return_value = "PRIVATE-original-token"
    edit = {"id": "owned-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}
    return EditWrites(configured(), auth, local=True), auth, edit


def prepare(service, name):
    case = CASES[name]
    return service.prepare(PREFIX + name, {**PARAMS, **case[2]}, deepcopy(case[3]), True)


@pytest.mark.parametrize("name", CASES)
def test_all_thirteen_wire_contracts_original_identity_and_observed_readback(name):
    service, auth, edit = fixture_service()
    verb, path, _, body, readpath, before, response, after = CASES[name]
    calls, changed = [], False

    def send(request, timeout):
        nonlocal changed
        calls.append((request.get_method(), request.full_url, request.data))
        assert request.get_header("Authorization") == "Bearer PRIVATE-original-token"
        assert timeout == 15
        if request.get_method() == "GET":
            assert request.data is None
            if request.full_url == BASE:
                data = edit
            else:
                assert request.full_url == BASE + readpath
                data = after if changed else before
        else:
            assert not changed, "Mutation retried"
            assert request.get_method() == verb and request.full_url == BASE + path
            assert (None if request.data is None else json.loads(request.data)) == body
            changed = True
            data = response
        return io.BytesIO(b"" if data is None else json.dumps(data).encode())

    with patch("urllib.request.OpenerDirector.open", side_effect=send):
        prepared = prepare(service, name)
        assert prepared["before"] == before
        auth.token.return_value = "PRIVATE-different-account"
        result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert [call[0] for call in calls] == ["GET", "GET", "GET", "GET", verb, "GET"]
    auth.token.assert_called_once_with("publisher")
    assert result["mutation_succeeded"] is True
    assert result["readback_succeeded"] is True
    assert result["after"] == after
    assert result["committed"] is False and result["publication_verified"] is False
    assert "PRIVATE" not in json.dumps(prepared) + json.dumps(result)


@pytest.mark.parametrize("name", CASES)
def test_confirmed_mutation_with_failed_readback_remains_successful_and_cannot_replay(name):
    service, _, edit = fixture_service()
    case = CASES[name]
    with patch(
        "pubship.http.get",
        side_effect=[
            edit,
            deepcopy(case[5]),
            edit,
            deepcopy(case[5]),
            PlayError("PRIVATE No writes were attempted. Retry later."),
        ],
    ):
        prepared = prepare(service, name)
        with patch(
            "pubship.edit_writes_http.execute", return_value=deepcopy(case[6] or {})
        ) as mutate:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_called_once()
    assert result["mutation_succeeded"] is True
    assert result["readback_succeeded"] is False
    assert "after" not in result
    assert result["observation_error"]
    assert "PRIVATE" not in json.dumps(result)
    assert "No writes were attempted" not in json.dumps(result)


@pytest.mark.parametrize("name", ["testers.patch", "testers.update", "tracks.patch"])
def test_array_operations_bind_exact_body_without_hidden_merge(name):
    service, _, edit = fixture_service()
    case = CASES[name]
    body = deepcopy(case[3])
    expected = deepcopy(body)
    with patch(
        "pubship.http.get",
        side_effect=[edit, deepcopy(case[5]), edit, deepcopy(case[5]), deepcopy(case[7])],
    ):
        prepared = service.prepare(PREFIX + name, {**PARAMS, **case[2]}, body, True)
        array = "googleGroups" if name.startswith("testers") else "releases"
        body[array].append("PRIVATE-unreviewed")
        prepared["proposed_request"]["body"][array].append("PRIVATE-output-mutation")
        with patch(
            "pubship.edit_writes_http.execute", return_value=deepcopy(case[6] or {})
        ) as mutate:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_called_once_with(
        PREFIX + name, {**PARAMS, **case[2]}, expected, "PRIVATE-original-token"
    )
    assert result["mutation_succeeded"]


@pytest.mark.parametrize("name", CASES)
def test_full_resource_or_collection_drift_prevents_mutation_and_consumes_id(name):
    service, _, edit = fixture_service()
    before = deepcopy(CASES[name][5])
    changed = {**before, "futureProviderField": "someone-else"}
    with patch("pubship.http.get", side_effect=[edit, before, edit, changed]):
        prepared = prepare(service, name)
        with patch("pubship.edit_writes_http.execute") as mutate:
            with pytest.raises(PlayError):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_not_called()


@pytest.mark.parametrize("name", ["tracks.create", "listings.update"])
@pytest.mark.parametrize(
    "failure",
    [
        PlayError("PRIVATE HTTP404"),
        PlayError("PRIVATE HTTP403"),
        PlayError("PRIVATE network timeout"),
    ],
)
def test_create_never_converts_failed_collection_read_to_absence(name, failure):
    service, _, edit = fixture_service()
    with (
        patch("pubship.http.get", side_effect=[edit, failure]) as read,
        patch("pubship.edit_writes_http.execute") as mutate,
    ):
        with pytest.raises(PlayError):
            prepare(service, name)
    assert read.call_count == 2
    mutate.assert_not_called()


@pytest.mark.parametrize(
    "before", [[], {"tracks": "bad"}, {"tracks": [None]}, {"tracks": [{"track": TRACK}]}]
)
def test_track_create_requires_valid_collection_without_existing_target(before):
    service, _, edit = fixture_service()
    with (
        patch("pubship.http.get", side_effect=[edit, before]),
        patch("pubship.edit_writes_http.execute") as mutate,
    ):
        with pytest.raises(PlayError):
            prepare(service, "tracks.create")
    mutate.assert_not_called()


@pytest.mark.parametrize("mode", ["default", "old_writes", "package_only", "method_only", "hosted"])
def test_both_new_optins_required_and_hosted_cannot_inherit_local_environment(mode, monkeypatch):
    for key in [
        "PACKAGES",
        "WRITE_PACKAGES",
        "EDIT_PACKAGES",
        "TRACK_PACKAGES",
        "EDIT_WRITE_PACKAGES",
    ]:
        monkeypatch.setenv("GOOGLE_PLAY_" + key, APP)
    monkeypatch.setenv("GOOGLE_PLAY_EDIT_WRITE_METHODS", ",".join(sorted(ALL_METHODS)))
    factory = Mock(side_effect=AssertionError("Discovery cannot resolve hosted credentials"))
    extra = {}
    if mode == "old_writes":
        extra = {
            "write_packages": frozenset({APP}),
            "edit_packages": frozenset({APP}),
            "track_packages": frozenset({APP}),
        }
    if mode == "package_only":
        extra = {"edit_write_packages": frozenset({APP})}
    if mode == "method_only":
        extra = {"edit_write_methods": ALL_METHODS}

    async def exercise():
        server = (
            create_server(service_factory=factory)
            if mode == "hosted"
            else create_server(Settings(packages=frozenset({APP}), **extra))
        )
        async with Client(server) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            assert "prepare_edit_write" not in names
            assert "apply_edit_write" not in names

    asyncio.run(exercise())
    factory.assert_not_called()


@pytest.mark.parametrize("name", CASES)
def test_app_and_method_scope_checked_before_credentials(name):
    auth = Mock()
    wrong_method = EditWrites(
        configured(
            methods=frozenset(
                {PREFIX + ("details.patch" if name != "details.patch" else "details.update")}
            )
        ),
        auth,
        local=True,
    )
    allowed = EditWrites(configured(), auth, local=True)
    with patch("pubship.http.get") as read:
        with pytest.raises(PlayError):
            prepare(wrong_method, name)
        with pytest.raises(PlayError):
            allowed.prepare(
                PREFIX + name,
                {**PARAMS, **CASES[name][2], "packageName": "com.other.app"},
                deepcopy(CASES[name][3]),
                True,
            )
    auth.token.assert_not_called()
    read.assert_not_called()


@pytest.mark.parametrize("code", [400, 401, 403, 409, 429, 500, 503])
def test_uncertain_mutation_is_not_retried_or_followed_by_readback(code):
    service, _, edit = fixture_service()
    before = deepcopy(CASES["details.patch"][5])
    failure = HTTPError(BASE + "/details", code, "PRIVATE-error", {}, io.BytesIO(b"PRIVATE-body"))
    with patch("pubship.http.get", side_effect=[edit, before, edit, before]) as read:
        prepared = prepare(service, "details.patch")
        with patch("urllib.request.OpenerDirector.open", side_effect=failure) as send:
            with pytest.raises(PlayError, match="outcome unknown") as caught:
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
    assert read.call_count == 4
    send.assert_called_once()
    assert "PRIVATE" not in str(caught.value)


def test_edit_write_cannot_race_lifecycle_and_same_preparation_cannot_replay():
    service, auth, edit = fixture_service()
    lifecycle = EditLifecycle(
        Settings(
            packages=frozenset({APP}),
            write_packages=frozenset({APP}),
            edit_packages=frozenset({APP}),
        ),
        auth,
        local=True,
    )
    before = deepcopy(CASES["details.patch"][5])
    with patch("pubship.http.get", side_effect=[edit, before, edit]):
        prepared = prepare(service, "details.patch")
        commit = lifecycle.prepare("commit", PARAMS, True)
    entered, release = threading.Event(), threading.Event()

    def slow_mutate(*args):
        entered.set()
        assert release.wait(5)
        return deepcopy(CASES["details.patch"][6])

    with (
        patch("pubship.http.get", side_effect=[edit, before, CASES["details.patch"][7]]) as read,
        patch("pubship.edit_writes_http.execute", side_effect=slow_mutate) as mutate,
        patch("pubship.edit_lifecycle_http.execute") as committing,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(service.apply, prepared["operation_id"], prepared["operation_id"])
        try:
            assert entered.wait(2)
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="in progress"):
                lifecycle.apply(commit["operation_id"], commit["operation_id"])
            committing.assert_not_called()
            assert read.call_count == 2
        finally:
            release.set()
        assert pending.result(timeout=2)["mutation_succeeded"]
        mutate.assert_called_once()


def test_real_stdio_prepare_apply_and_failed_readback_are_distinct_from_failed_mutation(tmp_path):
    launcher = tmp_path / "edit_writes_stdio.py"
    launcher.write_text("""
import io,json,time
from unittest.mock import patch
from pubship.auth import Auth
from pubship.server import main
from urllib.error import HTTPError
base="https://androidpublisher.googleapis.com/androidpublisher/v3/applications/com.example.app/edits/owned-edit"
edit={"id":"owned-edit","expiryTimeSeconds":str(int(time.time())+3600)}
state={"written":False}
def send(self,request,timeout):
    assert request.get_header("Authorization")=="Bearer synthetic-stdio" and timeout==15
    if request.full_url==base and request.get_method()=="GET":
        data=edit
    elif request.full_url==base+"/details":
        if request.get_method()=="PATCH":
            assert not state["written"] and json.loads(request.data)=={"contactEmail":"new@example.test"}
            state["written"]=True
            data={"contactEmail":"new@example.test"}
        else:
            assert request.get_method()=="GET" and request.data is None
            if state["written"]:
                raise HTTPError(request.full_url,503,"PRIVATE-readback",{},io.BytesIO(b"PRIVATE"))
            data={"contactEmail":"old@example.test"}
    else:
        raise AssertionError("Unexpected provider route or mutation")
    return io.BytesIO(json.dumps(data).encode())
with patch.object(Auth,"token",return_value="synthetic-stdio"),patch("urllib.request.OpenerDirector.open",send):
    main()
""")

    async def exercise():
        connection = StdioServerParameters(
            command=sys.executable,
            args=[str(launcher)],
            env={
                "GOOGLE_PLAY_PACKAGES": APP,
                "GOOGLE_PLAY_EDIT_WRITE_PACKAGES": APP,
                "GOOGLE_PLAY_EDIT_WRITE_METHODS": PREFIX + "details.patch",
                "GOOGLE_PLAY_AUTH_MODE": "adc",
                "GOOGLE_PLAY_WRITE_PACKAGES": "",
                "GOOGLE_PLAY_EDIT_PACKAGES": "",
                "GOOGLE_PLAY_TRACK_PACKAGES": "",
            },
        )
        async with Client(connection) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert not tools["prepare_edit_write"].annotations.read_only_hint
            assert not tools["apply_edit_write"].annotations.read_only_hint
            assert tools["apply_edit_write"].annotations.destructive_hint
            prepared = await client.call_tool(
                "prepare_edit_write",
                {
                    "method": PREFIX + "details.patch",
                    "parameters": PARAMS,
                    "body": {"contactEmail": "new@example.test"},
                    "acknowledge_effects": True,
                },
            )
            assert not prepared.is_error, prepared.content
            identifier = prepared.structured_content["operation_id"]
            malformed = await client.call_tool(
                "apply_edit_write",
                {
                    "operation_id": identifier,
                    "confirmation": identifier,
                    "body": {"PRIVATE": "unreviewed"},
                },
            )
            assert malformed.is_error and "PRIVATE" not in str(malformed.content)
            applied = await client.call_tool(
                "apply_edit_write",
                {"operation_id": identifier, "confirmation": identifier},
            )
            assert not applied.is_error, applied.content
            data = applied.structured_content
            assert data["mutation_succeeded"] is True and data["readback_succeeded"] is False
            assert "after" not in data and "PRIVATE" not in str(applied.content)
            assert "No writes were attempted" not in str(applied.content)
            replay = await client.call_tool(
                "apply_edit_write",
                {"operation_id": identifier, "confirmation": identifier},
            )
            assert replay.is_error

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "body",
    [
        {
            "googleGroups": ["PRIVATE-group@example.test"],
            "emailLists": ["PRIVATE-person@example.test"],
        },
        {"googleGroups": ["PRIVATE-group@example.test", None]},
        {"googleGroups": "PRIVATE-group@example.test"},
        {"googleGroups": [{"email": "PRIVATE-group@example.test"}]},
        None,
    ],
)
def test_tester_invalid_body_is_redacted_before_credentials(body, caplog):
    service, auth, _ = fixture_service()
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        service.prepare(PREFIX + "testers.update", {**PARAMS, "track": TRACK}, body, True)
    auth.token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in str(caught.value) + caplog.text


@pytest.mark.parametrize("ack", [False, None, 0, 1, "true"])
def test_tester_effects_acknowledgement_must_be_exact_boolean_on_mcp_wire(ack, caplog):
    async def exercise():
        async with Client(create_server(configured())) as client:
            assert "prepare_edit_write" in {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "prepare_edit_write",
                {
                    "method": PREFIX + "testers.update",
                    "parameters": {**PARAMS, "track": TRACK},
                    "body": {"googleGroups": ["PRIVATE-group@example.test"]},
                    "acknowledge_effects": ack,
                },
            )
            assert result.is_error
            assert "PRIVATE" not in str(result.content)

    with (
        patch("pubship.auth.Auth.token") as auth,
        patch("pubship.http.get") as read,
    ):
        asyncio.run(exercise())
    auth.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in caplog.text


@pytest.mark.parametrize(
    "name,before",
    [
        ("images.deleteall", {}),
        ("images.deleteall", {"images": [{"id": "same"}, {"id": "same"}]}),
        ("listings.update", {}),
        ("listings.update", {"listings": [{"title": "no-identity"}]}),
        ("listings.update", {"listings": [{"language": "en-US"}, {"language": "en-US"}]}),
        ("tracks.create", {"tracks": [{"track": "same"}, {"track": "same"}]}),
    ],
)
def test_collection_baseline_requires_explicit_array_and_unique_resource_identities(name, before):
    service, _, edit = fixture_service()
    with (
        patch("pubship.http.get", side_effect=[edit, before]),
        patch("pubship.edit_writes_http.execute") as mutate,
    ):
        with pytest.raises(PlayError):
            prepare(service, name)
    mutate.assert_not_called()


@pytest.mark.parametrize(
    "name", ["images.delete", "images.deleteall", "listings.delete", "listings.deleteall"]
)
@pytest.mark.parametrize("raw_response", [b"", b"{}"])
def test_delete_empty_success_bodies_are_valid_and_followed_by_observation(name, raw_response):
    service, _, edit = fixture_service()
    case = CASES[name]
    with patch("pubship.http.get", side_effect=[edit, case[5], edit, case[5], case[7]]) as read:
        prepared = prepare(service, name)
        with patch(
            "urllib.request.OpenerDirector.open", return_value=io.BytesIO(raw_response)
        ) as mutate:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_called_once()
    assert mutate.call_args.args[0].get_method() == "DELETE"
    assert mutate.call_args.args[0].data is None
    assert read.call_count == 5
    assert result["mutation_succeeded"] and result["readback_succeeded"]


@pytest.mark.parametrize(
    "name,after",
    [
        ("details.patch", {"contactEmail": []}),
        ("expansionfiles.update", {"referencesVersion": True}),
        ("images.deleteall", {"images": "malformed"}),
        ("listings.update", {"listings": [{"language": 17}]}),
        ("testers.update", {"googleGroups": [False]}),
        ("tracks.create", {"tracks": [{"track": None}]}),
    ],
)
def test_malformed_readback_does_not_erase_confirmed_mutation_success(name, after):
    service, _, edit = fixture_service()
    case = CASES[name]
    with patch("pubship.http.get", side_effect=[edit, case[5], edit, case[5], after]):
        prepared = prepare(service, name)
        with patch(
            "pubship.edit_writes_http.execute", return_value=deepcopy(case[6] or {})
        ) as mutate:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_called_once()
    assert result["mutation_succeeded"] is True and result["readback_succeeded"] is False
    assert "after" not in result


def test_edit_metadata_drift_consumes_operation_without_resource_get_or_mutation():
    service, _, edit = fixture_service()
    changed = {**edit, "expiryTimeSeconds": str(int(edit["expiryTimeSeconds"]) + 1)}
    with patch("pubship.http.get", side_effect=[edit, CASES["details.patch"][5], changed]) as read:
        prepared = prepare(service, "details.patch")
        with patch("pubship.edit_writes_http.execute") as mutate:
            with pytest.raises(PlayError):
                service.apply(prepared["operation_id"], prepared["operation_id"])
            with pytest.raises(PlayError, match="consumed"):
                service.apply(prepared["operation_id"], prepared["operation_id"])
    assert read.call_count == 3
    mutate.assert_not_called()


@pytest.mark.parametrize("name", CASES)
def test_unknown_parameter_is_rejected_before_credentials_for_every_method(name):
    service, auth, _ = fixture_service()
    case = CASES[name]
    with patch("pubship.http.get") as read, pytest.raises(PlayError) as caught:
        service.prepare(
            PREFIX + name, {**PARAMS, **case[2], "access_token": "PRIVATE"}, deepcopy(case[3]), True
        )
    auth.token.assert_not_called()
    read.assert_not_called()
    assert "PRIVATE" not in str(caught.value)


def test_shared_mutation_lock_remains_held_during_post_write_observation():
    service, auth, edit = fixture_service()
    lifecycle = EditLifecycle(
        Settings(
            packages=frozenset({APP}),
            write_packages=frozenset({APP}),
            edit_packages=frozenset({APP}),
        ),
        auth,
        local=True,
    )
    case = CASES["details.patch"]
    with patch("pubship.http.get", side_effect=[edit, case[5], edit]):
        prepared = prepare(service, "details.patch")
        commit = lifecycle.prepare("commit", PARAMS, True)
    entered, release = threading.Event(), threading.Event()
    read_count = 0

    def readback(url, token):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return edit
        if read_count == 2:
            return case[5]
        assert read_count == 3
        entered.set()
        assert release.wait(5)
        return case[7]

    with (
        patch("pubship.http.get", side_effect=readback),
        patch("pubship.edit_writes_http.execute", return_value=case[6]) as mutate,
        patch("pubship.edit_lifecycle_http.execute") as committing,
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        pending = pool.submit(service.apply, prepared["operation_id"], prepared["operation_id"])
        try:
            assert entered.wait(2)
            mutate.assert_called_once()
            with pytest.raises(PlayError, match="in progress"):
                lifecycle.apply(commit["operation_id"], commit["operation_id"])
            committing.assert_not_called()
        finally:
            release.set()
        assert pending.result(timeout=2)["readback_succeeded"]


def test_method_allowlist_is_exact_and_cannot_enable_other_mutation_families():
    for method in [
        "*",
        "androidpublisher.edits.*",
        PREFIX + "commit",
        PREFIX + "tracks.update",
        "androidpublisher.reviews.reply",
    ]:
        with pytest.raises(PlayError):
            configured(methods=frozenset({method}))


@pytest.mark.parametrize(
    "name,observation",
    [
        ("images.delete", "deletion_observed"),
        ("images.deleteall", "deletion_observed"),
        ("listings.delete", "deletion_observed"),
        ("listings.deleteall", "deletion_observed"),
        ("tracks.create", "creation_observed"),
    ],
)
def test_valid_readback_that_does_not_show_effect_is_returned_honestly(name, observation):
    service, _, edit = fixture_service()
    case = CASES[name]
    # The provider can acknowledge the mutation yet another observer or writer
    # leaves the subsequent collection different from the requested end state.
    # Preserve that observation; never synthesize an absent/deleted/created item.
    with patch("pubship.http.get", side_effect=[edit, case[5], edit, case[5], case[5]]):
        prepared = prepare(service, name)
        with patch(
            "pubship.edit_writes_http.execute", return_value=deepcopy(case[6] or {})
        ) as mutate:
            result = service.apply(prepared["operation_id"], prepared["operation_id"])
    mutate.assert_called_once()
    assert result["mutation_succeeded"] is True and result["readback_succeeded"] is True
    assert result["after"] == case[5]
    assert result[observation] is False
    assert result["committed"] is False and result["publication_verified"] is False
