"""Exercise a configured hosted image with disposable synthetic Docker volumes.

Uses only Python's standard library on the host. The image must already be built.
No host credential files, published ports, Google requests or real OAuth grants
are used. HTTP checks cross an actual TCP socket inside the network-isolated
container; this does not test a production TLS proxy or real customer consent.
"""

import argparse
import base64
import json
import secrets
import subprocess
import uuid

ISSUER = "https://mcp-smoke.invalid"
SECRET_DIR = "/run/pubship-smoke"
VAULT_DIR = "/var/lib/pubship"
TRANSFER_DIR = "/var/lib/pubship-transfers"

PROVISION = """
import json, os, pathlib, sys
values = json.load(sys.stdin)
os.umask(0o077)
for directory in ('/run/pubship-smoke', '/var/lib/pubship', '/var/lib/pubship-transfers'):
    path = pathlib.Path(directory)
    path.chmod(0o700)
    os.chown(path, 10001, 10001)
client = {'web': {
    'client_id': 'synthetic-container-smoke.apps.googleusercontent.com',
    'client_secret': values['client_secret'],
    'redirect_uris': ['https://mcp-smoke.invalid/google/callback'],
}}
for name, content in (
    ('google-web-client.json', json.dumps(client)),
    ('vault-key', values['key']),
):
    path = pathlib.Path('/run/pubship-smoke') / name
    path.write_text(content)
    path.chmod(0o600)
    os.chown(path, 10001, 10001)
"""

HTTP_CHECKS = """
import http.client, json, time
issuer = 'https://mcp-smoke.invalid'

def request(method, path, *, host='mcp-smoke.invalid', headers=None, body=None):
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=2)
    try:
        connection.request(method, path, body=body, headers={'Host': host, **(headers or {})})
        response = connection.getresponse()
        data = response.read(65537)
        assert len(data) <= 65536, 'Unexpectedly large smoke response'
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()

deadline = time.monotonic() + 30
while True:
    try:
        status, _, data = request('GET', '/healthz')
        if status == 200:
            break
    except (OSError, http.client.HTTPException):
        pass
    if time.monotonic() >= deadline:
        raise AssertionError('Configured container did not become healthy')
    time.sleep(0.2)
assert json.loads(data) == {'status': 'ok'}, 'Unexpected health response'
status, _, data = request('GET', '/.well-known/oauth-authorization-server')
assert status == 200, 'Authorization metadata unavailable'
metadata = json.loads(data)
assert metadata['issuer'] == issuer, 'Issuer mismatch'
assert metadata['authorization_endpoint'] == issuer + '/authorize'
assert metadata['token_endpoint'] == issuer + '/token'
assert 'S256' in metadata['code_challenge_methods_supported']
status, _, data = request('GET', '/.well-known/oauth-protected-resource/mcp')
assert status == 200, 'Protected-resource metadata unavailable'
resource = json.loads(data)
assert resource['resource'] == issuer + '/mcp', 'Resource mismatch'
assert resource['authorization_servers'] == [issuer], 'Authorization server mismatch'
for headers in ({}, {'Authorization': 'Bearer SYNTHETIC_NOT_AN_MCP_TOKEN'}):
    status, response_headers, _ = request(
        'POST', '/mcp', body='{}',
        headers={'Content-Type': 'application/json', **headers},
    )
    assert status == 401, 'Unauthenticated or foreign token was accepted'
    challenge = {k.lower(): v for k, v in response_headers.items()}.get('www-authenticate', '')
    assert issuer + '/.well-known/oauth-protected-resource/mcp' in challenge
for host in ('untrusted.invalid', '127.0.0.1:8080'):
    status, _, _ = request('GET', '/healthz', host=host, headers={'X-Forwarded-Host': 'mcp-smoke.invalid'})
    assert status == 400, 'Untrusted Host or forwarding-header substitution accepted'
"""

VAULT_CHECK = """
import errno, os, pathlib, stat, sys
from pubship.hosted_config import HostedSettings
from pubship.vault import Vault
assert os.getuid() == os.getgid() == 10001, 'Unexpected runtime identity'
settings = HostedSettings.from_env()
for name in ('google-web-client.json', 'vault-key'):
    metadata = pathlib.Path('/run/pubship-smoke', name).stat()
    assert metadata.st_uid == 10001 and stat.S_IMODE(metadata.st_mode) == 0o600
directory = settings.database.parent.stat()
assert directory.st_uid == 10001 and stat.S_IMODE(directory.st_mode) == 0o700
try:
    pathlib.Path('/run/pubship-smoke/should-not-be-writable').write_text('synthetic')
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES), 'Unexpected mount error'
else:
    raise AssertionError('Secret mount is writable')
vault = Vault(settings.database, settings.encryption_key)
try:
    expected = {'marker': 'SYNTHETIC_PERSISTENT_VAULT_SENTINEL'}
    if sys.argv[1] == 'write':
        vault.put('smoke', 'synthetic-record', expected, 600)
    assert vault.get('smoke', 'synthetic-record') == expected, 'Vault record lost across restart'
    assert vault.get('smoke', 'another-record') is None, 'Record identity mismatch'
finally:
    vault.close()
for path in settings.database.parent.glob('vault.db*'):
    assert b'SYNTHETIC_PERSISTENT_VAULT_SENTINEL' not in path.read_bytes(), 'Plaintext vault record'
"""


TRANSFER_CHECK = """
import hashlib, os, time
from pubship.hosted_config import HostedSettings
from pubship.hosted_transfers import HostedTransferStore, TransferPurpose
settings = HostedSettings.from_env()
store = HostedTransferStore(settings.transfer_directory, settings.transfer_limits)
try:
    purpose = TransferPurpose('synthetic', {}, 'application/octet-stream', 'synthetic')
    owner = ('synthetic-owner', 'synthetic-client')
    value = store.reserve_upload(owner, purpose, 65536, hashlib.sha256(b'x' * 65536).hexdigest(), time.monotonic() + 30)
    record = store._records[value['transfer_id']]
    metadata = os.fstat(record.stream.fileno())
    assert metadata.st_nlink == 0, 'Spool must be anonymous'
    assert metadata.st_blocks * 512 >= record.charge, 'Disk allocation was sparse'
    assert not list(settings.transfer_directory.iterdir()), 'Named transfer file leaked'
    assert store.discard(owner, value['transfer_id']) == 'disposed'
    assert not store._records, 'Quota was not released'
finally:
    store.close()
"""


def docker(*arguments, step, input_text=None, timeout=45, check=True):
    try:
        result = subprocess.run(
            ["docker", *arguments],
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f"{step}: Docker unavailable or timed out.") from None
    if check and result.returncode:
        # Do not echo runtime output or command input: these contain synthetic
        # secret values, and the same diagnostics must stay safe if code regresses.
        raise RuntimeError(f"{step}: Docker command failed ({result.returncode}); output withheld.")
    return result


def check_container(image):
    suffix = uuid.uuid4().hex
    name = "pubship-smoke-" + suffix
    initializer = name + "-init"
    secret_volume, vault_volume = name + "-secrets", name + "-vault"
    transfer_volume = name + "-transfers"
    volumes = []
    values = {
        "client_secret": "synthetic-" + secrets.token_urlsafe(24),
        "key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }
    inspect = docker("image", "inspect", image, step="Inspect image")
    if json.loads(inspect.stdout)[0]["Config"]["User"] != "10001:10001":
        raise RuntimeError("Hosted image must declare UID/GID 10001:10001.")
    try:
        for volume in (secret_volume, vault_volume, transfer_volume):
            docker("volume", "create", volume, step="Create temporary volume")
            volumes.append(volume)
        mounts = [
            "--mount",
            f"type=volume,src={secret_volume},dst={SECRET_DIR}",
            "--mount",
            f"type=volume,src={vault_volume},dst={VAULT_DIR}",
            "--mount",
            f"type=volume,src={transfer_volume},dst={TRANSFER_DIR}",
        ]
        docker(
            "run",
            "--rm",
            "-i",
            "--name",
            initializer,
            "--network",
            "none",
            "--read-only",
            "--user",
            "0:0",
            *mounts,
            "--entrypoint",
            "python",
            image,
            "-c",
            PROVISION,
            step="Provision synthetic private volumes",
            input_text=json.dumps(values),
        )
        docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,src={secret_volume},dst={SECRET_DIR},readonly",
            "--mount",
            f"type=volume,src={vault_volume},dst={VAULT_DIR}",
            "--mount",
            f"type=volume,src={transfer_volume},dst={TRANSFER_DIR}",
            "--env",
            "PUBSHIP_SERVER_ALLOWED_EMAILS=owner@example.test",
            "--env",
            f"PUBSHIP_SERVER_ISSUER={ISSUER}",
            "--env",
            f"PUBSHIP_SERVER_GOOGLE_CLIENT_FILE={SECRET_DIR}/google-web-client.json",
            "--env",
            f"PUBSHIP_SERVER_VAULT_KEY_FILE={SECRET_DIR}/vault-key",
            "--env",
            f"PUBSHIP_SERVER_VAULT_DATABASE={VAULT_DIR}/vault.db",
            "--env",
            f"PUBSHIP_SERVER_TRANSFER_DIRECTORY={TRANSFER_DIR}",
            image,
            step="Start configured container",
        )
        docker("exec", name, "python", "-c", HTTP_CHECKS, step="HTTP/authentication checks")
        docker("exec", name, "python", "-c", VAULT_CHECK, "write", step="Private vault checks")
        docker(
            "exec",
            name,
            "python",
            "-c",
            TRANSFER_CHECK,
            step="Physical transfer reservation checks",
        )
        docker("restart", "--time", "10", name, step="Restart configured container")
        docker("exec", name, "python", "-c", HTTP_CHECKS, step="HTTP checks after restart")
        docker("exec", name, "python", "-c", VAULT_CHECK, "read", step="Persistent vault checks")
        logs = docker("logs", name, step="Inspect synthetic runtime logs")
        combined = logs.stdout + logs.stderr
        for marker in (
            *values.values(),
            "SYNTHETIC_PERSISTENT_VAULT_SENTINEL",
            "SYNTHETIC_NOT_AN_MCP_TOKEN",
        ):
            if marker in combined:
                raise RuntimeError("Container logs exposed a synthetic secret; output withheld.")
    finally:
        # Only names allocated by this run are removed. Never prune other Docker
        # objects or mount a developer's credential directories.
        for container in (name, initializer):
            docker("rm", "-f", container, step="Remove smoke container", check=False)
        for volume in volumes:
            docker("volume", "rm", volume, step="Remove synthetic private volume")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="pubship:ci", help="Already-built hosted image")
    args = parser.parse_args()
    try:
        check_container(args.image)
    except (RuntimeError, ValueError, KeyError) as error:
        raise SystemExit(str(error)) from None
    print(
        "Configured container passed: non-root private mounts, TCP health/OAuth metadata, "
        "401 and Host rejection, encrypted restart persistence, redacted logs and cleanup. "
        "Synthetic configuration only; no Google auth or production TLS acceptance."
    )


if __name__ == "__main__":
    main()
