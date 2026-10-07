"""Symbol uploads must recognize bundle versions without losing drift checks."""

import time
from copy import deepcopy
from unittest.mock import Mock

import pytest

from pubship.artifact import Artifacts
from pubship.config import Settings
from pubship.errors import PlayError

METHOD = "androidpublisher.edits.deobfuscationfiles.upload"
PARAMETERS = {
    "packageName": "com.example.app",
    "editId": "saved-edit",
    "apkVersionCode": 7,
    "deobfuscationFileType": "proguard",
}


@pytest.fixture
def service(tmp_path):
    (tmp_path / "mapping.txt").write_text("com.example.Source -> a:\n")
    instance = Artifacts(
        Settings(
            packages=frozenset({"com.example.app"}),
            artifact_packages=frozenset({"com.example.app"}),
            artifact_methods=frozenset({METHOD}),
            artifact_upload_root=str(tmp_path),
        ),
        Mock(token=Mock(return_value="synthetic-token")),
        local=True,
    )
    yield instance
    instance.close()


def wire(monkeypatch, apks, bundles):
    state = {"apks": apks, "bundles": bundles}
    calls = []
    edit = {"id": "saved-edit", "expiryTimeSeconds": str(int(time.time()) + 3600)}

    def get(url, token):
        calls.append((url, token))
        if url.endswith("/apks"):
            return deepcopy(state["apks"])
        if url.endswith("/bundles"):
            return deepcopy(state["bundles"])
        assert url.endswith("/edits/saved-edit")
        return dict(edit)

    upload = Mock(return_value={"deobfuscationFile": {"symbolType": "proguard"}})
    monkeypatch.setattr("pubship.artifact.http.get", get)
    monkeypatch.setattr("pubship.artifact.artifact_http.upload", upload)
    return state, calls, upload


@pytest.mark.parametrize(
    "apks,bundles",
    [
        ({"apks": [{"versionCode": 7}]}, {"bundles": []}),
        ({"apks": []}, {"bundles": [{"versionCode": 7}]}),
        ({}, {"bundles": [{"versionCode": 7}]}),
    ],
)
def test_symbol_version_can_be_observed_in_apk_or_bundle(service, monkeypatch, apks, bundles):
    _, calls, upload = wire(monkeypatch, apks, bundles)
    prepared = service.prepare(
        METHOD,
        PARAMETERS,
        filename="mapping.txt",
        mime_type="application/octet-stream",
        acknowledge_effects=True,
    )
    assert prepared["additional_baselines"] == [bundles]
    result = service.apply(prepared["operation_id"], prepared["operation_id"])
    assert result["mutation_succeeded"] and result["readback_succeeded"]
    assert not result["artifact_observation_available"]
    assert not result["digest_verified"]
    assert result["additional_observations"] == [bundles]
    assert sum(url.endswith("/bundles") for url, _ in calls) == 3
    assert all(token == "synthetic-token" for _, token in calls)
    assert upload.call_count == 1
    assert "/apks/7/deobfuscationFiles/proguard?uploadType=media" in upload.call_args.args[0]["url"]


def test_bundle_metadata_drift_blocks_symbol_upload(service, monkeypatch):
    state, _, upload = wire(monkeypatch, {"apks": []}, {"bundles": [{"versionCode": 7}]})
    prepared = service.prepare(
        METHOD,
        PARAMETERS,
        filename="mapping.txt",
        mime_type="application/octet-stream",
        acknowledge_effects=True,
    )
    state["bundles"] = {"bundles": [{"versionCode": 7, "sha256": "changed"}]}
    with pytest.raises(PlayError, match="baseline changed"):
        service.apply(prepared["operation_id"], prepared["operation_id"])
    upload.assert_not_called()
    with pytest.raises(PlayError, match="consumed"):
        service.apply(prepared["operation_id"], prepared["operation_id"])


def test_missing_target_is_not_inferred_from_unknown_collections(service, monkeypatch):
    _, _, upload = wire(monkeypatch, {}, {})
    with pytest.raises(PlayError, match="observed existing APK or bundle"):
        service.prepare(
            METHOD,
            PARAMETERS,
            filename="mapping.txt",
            mime_type="application/octet-stream",
            acknowledge_effects=True,
        )
    upload.assert_not_called()
    assert service._retained_count == 0
