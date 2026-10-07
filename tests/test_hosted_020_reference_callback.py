import asyncio
import importlib.util
from pathlib import Path
from urllib.parse import urlencode


def test_each_authorization_uses_a_fresh_callback_and_rejects_previous_state(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "transfer_reference",
        Path(__file__).resolve().parents[1] / "scripts/hosted_transfer_client.py",
    )
    reference = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reference)
    monkeypatch.setattr(reference.webbrowser, "open", lambda _: True)

    async def exercise():
        callback = reference.Loopback("https://mcp.example", 0)
        server = await asyncio.start_server(callback.handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        async def send(state, code):
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            query = urlencode({"state": state, "code": code, "iss": "https://mcp.example"})
            writer.write(f"GET /callback?{query} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            await writer.drain()
            response = await reader.read()
            writer.close()
            await writer.wait_closed()
            return response

        async with server:
            for number in (1, 2):
                state = f"state-{number}"
                await callback.redirect(
                    "https://mcp.example/authorize?"
                    + urlencode(
                        {
                            "redirect_uri": callback.uri,
                            "code_challenge_method": "S256",
                            "state": state,
                        }
                    )
                )
                assert not callback.future.done()
                if number == 2:
                    assert b"400 Bad Request" in await send("state-1", "old-code")
                    assert not callback.future.done()
                assert b"200 OK" in await send(state, f"code-{number}")
                result = await callback.callback()
                assert result.code == f"code-{number}"
                assert result.state == state

    asyncio.run(exercise())
