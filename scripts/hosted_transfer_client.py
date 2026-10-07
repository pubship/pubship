"""Interactive reference client for hosted media using one in-memory OAuth session.

Run with uv run --frozen --all-extras python scripts/hosted_transfer_client.py
--server https://selfhost.example.test --scope play:edit-artifacts. Enter one JSON object
per command: {"command":"upload","source":"/private/app.apk","method":"...",
"parameters":{...},"mime_type":"application/vnd.android.package-archive"},
{"command":"download","transfer_id":"...","destination":"/private/new.apk"},
{"command":"tool","name":"prepare_artifact_upload","arguments":{...}}, or
{"command":"quit"}. Explicit apply commands perform provider writes. No automatic
apply, retry, redirect or token persistence. Review printed intent/effects first.
"""

import argparse
import asyncio
import contextlib
import json
import secrets
import webbrowser
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata

from pubship.hosted_transfer_client import (
    TransferClient,
    TransferClientError,
    redact_preview,
    safe_metadata,
    server_origin,
    tool,
)

TOOLS = {
    "get_transfer_status",
    "discard_transfer",
    "prepare_artifact_upload",
    "apply_artifact_upload",
    "download_artifact",
    "prepare_internal_sharing_upload",
    "apply_internal_sharing_upload",
    "prepare_appstore_operation",
    "apply_appstore_operation",
    "disconnect_google_account",
}


class MemoryStorage:
    tokens = None
    client = None

    async def get_tokens(self):
        return self.tokens

    async def set_tokens(self, tokens):
        self.tokens = tokens

    async def get_client_info(self):
        return self.client

    async def set_client_info(self, client_info):
        self.client = client_info


class Loopback:
    def __init__(self, origin, port):
        self.origin, self.port = origin, port
        self.uri = f"http://127.0.0.1:{port}/callback"
        self.state = None
        self.future = asyncio.get_running_loop().create_future()

    async def handle(self, reader, writer):
        accepted = False
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            method, destination, _ = raw.split(b"\r\n", 1)[0].decode("ascii").split(" ")
            parsed = urlsplit(destination)
            query = parse_qs(parsed.query, strict_parsing=True)
            state, code, issuer = (
                query.get("state", []),
                query.get("code", []),
                query.get("iss", [None]),
            )
            if (
                method == "GET"
                and parsed.path == "/callback"
                and not parsed.netloc
                and not parsed.scheme
                and not parsed.fragment
                and len(state) == 1
                and self.state
                and secrets.compare_digest(state[0], self.state)
                and len(code) == 1
                and len(issuer) == 1
                and not self.future.done()
            ):
                self.future.set_result(
                    AuthorizationCodeResult(code=code[0], state=state[0], iss=issuer[0])
                )
                accepted = True
        except (
            ValueError,
            UnicodeError,
            TimeoutError,
            asyncio.LimitOverrunError,
            asyncio.IncompleteReadError,
        ):
            pass
        finally:
            message = (
                b"Authorization received. Return to the reference client."
                if accepted
                else b"Authorization could not be verified."
            )
            status = "200 OK" if accepted else "400 Bad Request"
            writer.write(
                (
                    f"HTTP/1.1 {status}\r\nContent-Type: text/plain\r\nCache-Control: no-store\r\nReferrer-Policy: no-referrer\r\nConnection: close\r\nContent-Length: {len(message)}\r\n\r\n"
                ).encode()
                + message
            )
            with contextlib.suppress(OSError):
                await writer.drain()
            writer.close()

    async def redirect(self, url):
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        if (
            parsed.scheme + "://" + parsed.netloc != self.origin
            or parsed.path != "/authorize"
            or query.get("redirect_uri") != [self.uri]
            or query.get("code_challenge_method") != ["S256"]
            or len(query.get("state", [])) != 1
        ):
            raise TransferClientError("OAuth authorization destination did not match this server.")
        self.state = query["state"][0]
        self.future = asyncio.get_running_loop().create_future()
        if not webbrowser.open(url):
            raise TransferClientError(
                "Could not open browser; restart after configuring a browser."
            )

    async def callback(self):
        return await asyncio.wait_for(self.future, 600)


async def run(args):
    origin, storage = server_origin(args.server), MemoryStorage()
    callback = Loopback(origin, args.callback_port)
    listener = await asyncio.start_server(
        callback.handle, "127.0.0.1", args.callback_port, limit=8192
    )
    try:
        metadata = OAuthClientMetadata(
            redirect_uris=[callback.uri],
            client_name="PubShip reference transfer client",
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            scope=" ".join(dict.fromkeys(["play:read", *args.scope])),
            application_type="native",
        )
        auth = OAuthClientProvider(
            origin + "/mcp",
            metadata,
            storage,
            redirect_handler=callback.redirect,
            callback_handler=callback.callback,
        )
        async with httpx2.AsyncClient(
            auth=auth, trust_env=False, follow_redirects=False, timeout=httpx2.Timeout(30, read=660)
        ) as client:
            async with streamable_http_client(origin + "/mcp", http_client=client) as streams:
                async with ClientSession(*streams, read_timeout_seconds=660) as session:
                    await session.initialize()
                    transfers = TransferClient(origin, session, storage)
                    print(
                        "Connected. Enter JSON commands. Explicit apply commands change Google data."
                    )
                    while True:
                        try:
                            line = await asyncio.to_thread(input, "pubship> ")
                            if len(line) > 65536:
                                raise TransferClientError("Command exceeds 64 KiB.")
                            command = json.loads(line)
                            kind = command.pop("command")
                            if kind == "quit":
                                break
                            if kind in {"upload", "download"}:
                                result = await getattr(transfers, kind)(**command)
                            elif kind == "tool" and command.get("name") in TOOLS:
                                name, arguments = command["name"], command.get("arguments", {})
                                if name.startswith("prepare_"):
                                    print(
                                        json.dumps(
                                            {"operator_supplied_intent": redact_preview(arguments)}
                                        )
                                    )
                                result = await tool(session, name, arguments)
                                result = safe_metadata(result)
                            else:
                                raise TransferClientError("Unsupported reference command.")
                            print(json.dumps(result, sort_keys=True))
                        except EOFError:
                            break
                        except TransferClientError as error:
                            print(str(error))
                        except (ValueError, TypeError, KeyError, OSError):
                            print(
                                "Command could not complete. Inspect transfer status before retrying; details withheld."
                            )
    finally:
        listener.close()
        await listener.wait_closed()
        storage.tokens = storage.client = None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="https://selfhost.example.test")
    parser.add_argument("--scope", action="append", default=[])
    parser.add_argument("--callback-port", type=int, default=8765)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (KeyboardInterrupt, Exception):
        raise SystemExit(
            "Reference session ended. No automatic retry was made; credentials were not saved."
        ) from None


if __name__ == "__main__":
    main()
