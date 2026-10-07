import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from test_hosted import begin, call_tool, callback, connect_account, exchange
from test_hosted import hosted as hosted

from pubship.auth import SCOPES
from pubship.errors import PlayError
from pubship.oauth import READ_SCOPE
from pubship.vault import Vault


@pytest.fixture
def vault(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    monkeypatch.setattr(Vault, "ADMISSION_LIMITS", {"clients": 2, "consent": 2, "google_state": 2})
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 3)
    instance = Vault(tmp_path / "vault.db", Fernet.generate_key())
    yield instance
    instance.close()


def test_public_admission_cannot_exhaust_lifecycle_capacity(vault):
    for namespace in vault.ADMISSION_LIMITS:
        for index in range(2):
            vault.put(namespace, str(index), {"synthetic": True}, 600)
        with pytest.raises(PlayError, match="capacity"):
            vault.put(namespace, "overflow", {}, 600)

    vault.put_many(
        [
            (namespace, "customer", {"synthetic": True}, 600)
            for namespace in ("grants", "access", "refresh")
        ]
    )
    assert all(vault.get(namespace, "customer") for namespace in ("grants", "access", "refresh"))


def test_replacements_work_at_capacity_without_allowing_new_records(vault):
    for index in range(3):
        vault.put("grants", str(index), {"generation": 1}, 600)
    vault.put("grants", "0", {"generation": 2}, 600)
    assert vault.get("grants", "0") == {"generation": 2}
    with pytest.raises(PlayError, match="capacity"):
        vault.put("access", "new", {}, 600)

    for index in range(2):
        vault.put("clients", str(index), {"generation": 1}, 600)
    vault.put("clients", "0", {"generation": 2}, 600)
    assert vault.get("clients", "0") == {"generation": 2}


def test_token_pair_capacity_failure_rolls_back_all_records(vault):
    vault.put_many([("grants", "customer", {"generation": 1}, 600), ("access", "old", {}, 600)])
    with pytest.raises(PlayError, match="capacity"):
        vault.put_many(
            [
                ("grants", "customer", {"generation": 2}, 600),
                ("access", "new", {}, 600),
                ("refresh", "new", {}, 600),
            ]
        )
    assert vault.get("grants", "customer") == {"generation": 1}
    assert vault.get("access", "new") is None
    assert vault.get("refresh", "new") is None


def test_capacity_is_atomic_under_concurrent_admission(vault):
    def admit(index):
        try:
            vault.put("clients", str(index), {"synthetic": True}, 600)
            return True
        except PlayError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        admitted = list(pool.map(admit, range(12)))
    assert sum(admitted) == 2
    assert sum(vault.get("clients", str(index)) is not None for index in range(12)) == 2


def test_expired_records_release_admission_capacity(vault):
    vault.put("clients", "expired", {}, -1)
    vault.put_many([("clients", "one", {}, 600), ("clients", "two", {}, 600)])
    assert vault.get("clients", "expired") is None
    assert vault.get("clients", "one") == {}
    assert vault.get("clients", "two") == {}


def test_batch_duplicate_keys_count_once(vault):
    vault.put_many(
        [("clients", "same", {"generation": generation}, 600) for generation in range(4)]
    )
    assert vault.get("clients", "same") == {"generation": 3}
    vault.put("clients", "second", {}, 600)


def test_public_batch_overflow_does_not_update_lifecycle_record(vault):
    vault.put("grants", "customer", {"generation": 1}, 600)
    with pytest.raises(PlayError, match="capacity"):
        vault.put_many(
            [("grants", "customer", {"generation": 2}, 600)]
            + [("clients", str(index), {}, 600) for index in range(3)]
        )
    assert vault.get("grants", "customer") == {"generation": 1}
    assert vault.get("clients", "0") is None


def test_google_completion_cannot_leave_an_orphan_grant(hosted, monkeypatch):
    _, app, _ = hosted
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 1)
    pending = {
        "client_id": "synthetic-client",
        "packages": ["com.example.app"],
        "bucket": "",
        "google_scopes": [SCOPES["publisher"]],
        "params": {
            "code_challenge": "a" * 43,
            "redirect_uri": "https://client.example/callback",
            "redirect_uri_provided_explicitly": True,
        },
    }
    token = {
        "access_token": "SYNTHETIC-GOOGLE-TOKEN",
        "refresh_token": "fake-refresh",
        "scope": SCOPES["publisher"],
        "token_type": "Bearer",
        "expires_in": 3600,
    }
    with pytest.raises(PlayError, match="capacity"):
        app.state.oauth.complete_google(pending, token)
    assert app.state.vault.db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0


def test_code_exchange_capacity_failure_revokes_grant_without_partial_tokens(hosted, monkeypatch):
    settings, app, client = hosted
    request = begin(client, settings)
    code = callback(client, request)
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 2)
    response = exchange(client, settings, request, code)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"
    assert "Reconnect" in response.json()["error_description"]
    assert (
        app.state.vault.db.execute(
            "SELECT COUNT(*) FROM records WHERE namespace IN ('grants', 'codes', 'access', 'refresh')"
        ).fetchone()[0]
        == 0
    )
    assert exchange(client, settings, request, code).status_code == 400


def test_refresh_capacity_failure_preserves_old_tokens_and_allows_retry(hosted, monkeypatch):
    settings, _, client = hosted
    request, tokens = connect_account(client, settings, "com.example.alice", "SYNTHETIC-ALICE")
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 3)
    response = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": request["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": settings.resource,
        },
    )
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert call_tool(client, tokens["access_token"], "list_api_methods", {}).status_code == 200
    monkeypatch.setattr(Vault, "LIFECYCLE_LIMIT", 30000)
    retried = client.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": request["client_id"],
            "refresh_token": tokens["refresh_token"],
            "resource": settings.resource,
        },
    )
    assert retried.status_code == 200
    assert (
        call_tool(client, retried.json()["access_token"], "list_api_methods", {}).status_code == 200
    )


@pytest.mark.parametrize("failed_write", ["replay_marker", "token_pair"])
def test_failed_refresh_persistence_is_redacted_and_preserves_other_customers(hosted, failed_write):
    settings, app, client = hosted
    request, alice = connect_account(client, settings, "com.example.alice", "SYNTHETIC-ALICE")
    _, bob = connect_account(client, settings, "com.example.bob", "SYNTHETIC-BOB")
    namespace = "used_refresh" if failed_write == "replay_marker" else "access"
    # Fail inside the actual transaction, including after the old token was deleted.
    app.state.vault.db.execute(
        "CREATE TRIGGER fail_refresh BEFORE INSERT ON records "
        f"WHEN NEW.namespace='{namespace}' BEGIN SELECT RAISE(ABORT,'PRIVATE-STORAGE-DETAIL'); END"
    )
    data = {
        "grant_type": "refresh_token",
        "client_id": request["client_id"],
        "refresh_token": alice["refresh_token"],
        "resource": settings.resource,
    }
    response = client.post("/token", data=data)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"
    assert "PRIVATE" not in response.text
    assert call_tool(client, alice["access_token"], "list_api_methods", {}).status_code == 200
    assert call_tool(client, bob["access_token"], "list_api_methods", {}).status_code == 200
    app.state.vault.db.execute("DROP TRIGGER fail_refresh")
    assert client.post("/token", data=data).status_code == 200


def basic_header(client_id, secret):
    credentials = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {"Authorization": "Basic " + credentials}


def basic_authorization(client, settings):
    registered = client.post(
        "/register",
        json={
            "redirect_uris": ["https://client.example/callback"],
            "token_endpoint_auth_method": "client_secret_basic",
            "grant_types": ["authorization_code", "refresh_token"],
            "scope": READ_SCOPE,
        },
    )
    assert registered.status_code == 201
    info = registered.json()
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    authorization = client.get(
        "/authorize",
        params={
            "client_id": info["client_id"],
            "redirect_uri": "https://client.example/callback",
            "response_type": "code",
            "scope": READ_SCOPE,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "resource": settings.resource,
        },
    )
    assert authorization.status_code == 302
    connect_url = authorization.headers["location"]
    assert client.get(connect_url).status_code == 200
    consent = client.post(
        connect_url,
        data={"packages": "com.example.alice", "services": ["publisher", "reporting"]},
        headers={"Origin": settings.issuer},
    )
    assert consent.status_code == 303
    state = parse_qs(urlsplit(consent.headers["location"]).query)["state"][0]
    code = callback(client, {"state": state})
    return info, {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": "https://client.example/callback",
        "code_verifier": verifier,
        "resource": settings.resource,
    }


def test_basic_authentication_without_body_credentials_exchanges_refreshes_and_revokes(hosted):
    settings, _, client = hosted
    info, data = basic_authorization(client, settings)
    headers = basic_header(info["client_id"], info["client_secret"])
    issued = client.post("/token", headers=headers, data=data)
    assert issued.status_code == 200, issued.text
    refreshed = client.post(
        "/token",
        headers=headers,
        data={
            "grant_type": "refresh_token",
            "refresh_token": issued.json()["refresh_token"],
            "resource": settings.resource,
        },
    )
    assert refreshed.status_code == 200, refreshed.text
    token = refreshed.json()["access_token"]
    assert call_tool(client, token, "list_api_methods", {"limit": 1}).status_code == 200
    revoked = client.post(
        "/revoke", headers=headers, data={"token": refreshed.json()["refresh_token"]}
    )
    assert revoked.status_code == 200, revoked.text
    assert call_tool(client, token, "list_api_methods", {}).status_code == 401


@pytest.mark.parametrize("failure", ["wrong_secret", "mismatched_client"])
def test_basic_authentication_failures_cannot_consume_code_or_revoke_grant(hosted, failure):
    settings, _, client = hosted
    info, data = basic_authorization(client, settings)
    valid_headers = basic_header(info["client_id"], info["client_secret"])
    invalid_headers = (
        basic_header(info["client_id"], "wrong-secret")
        if failure == "wrong_secret"
        else valid_headers
    )
    extra = {}
    if failure == "mismatched_client":
        other = client.post(
            "/register",
            json={
                "redirect_uris": ["https://other.example/callback"],
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )
        assert other.status_code == 201
        extra["client_id"] = other.json()["client_id"]
    rejected = client.post("/token", headers=invalid_headers, data={**data, **extra})
    assert rejected.status_code == 401, rejected.text
    issued = client.post("/token", headers=valid_headers, data=data)
    assert issued.status_code == 200, issued.text
    tokens = issued.json()
    rejected = client.post(
        "/revoke", headers=invalid_headers, data={"token": tokens["refresh_token"], **extra}
    )
    assert rejected.status_code == 401, rejected.text
    assert (
        call_tool(client, tokens["access_token"], "list_api_methods", {"limit": 1}).status_code
        == 200
    )
