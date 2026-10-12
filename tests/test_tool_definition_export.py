"""Exercise the discovery exporter, its isolation boundary and CLI failure paths."""

import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import google.auth
import pytest

from pubship.auth import Auth

ROOT = Path(__file__).resolve().parents[1]
EXPORTER = ROOT / "scripts/export_tool_definitions.py"


def exporter():
    return runpy.run_path(str(EXPORTER))


def test_exporter_uses_real_sdk_and_restores_operator_environment(monkeypatch):
    # All values are deliberately synthetic. No authentication store is read.
    sentinels = {
        "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/operator-key.json",
        "GOOGLE_PLAY_PACKAGES": "com.operator.private",
        "PUBSHIP_SERVER_ALLOWED_EMAILS": "operator@example.test",
        "PUBSHIP_EXPORT_TEST": "keep",
    }
    for key, value in sentinels.items():
        monkeypatch.setenv(key, value)
    module = exporter()
    real_list = module["_list_tools"]
    calls = []

    async def inspect(server):
        assert "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ
        assert "GOOGLE_PLAY_PACKAGES" not in os.environ
        assert "PUBSHIP_SERVER_ALLOWED_EMAILS" not in os.environ
        assert os.environ["PUBSHIP_EXPORT_TEST"] == "keep"
        with pytest.raises(AssertionError, match="ADC access prohibited"):
            google.auth.default()
        with pytest.raises(AssertionError, match="Credential access prohibited"):
            Auth.token(None)
        with socket.socket() as connection:
            for method in (connection.connect, connection.connect_ex):
                with pytest.raises(AssertionError, match="Network access prohibited"):
                    method(("127.0.0.1", 1))
        with pytest.raises(AssertionError, match="Network access prohibited"):
            socket.getaddrinfo("example.test", 443)
        calls.append(server)
        return await real_list(server)

    module["export_definitions"].__globals__["_list_tools"] = inspect
    profiles = asyncio.run(module["export_definitions"]())
    assert {name: len(value["tools"]) for name, value in profiles.items()} == {
        "local-default": 13,
        "local-all-opt-ins": 41,
        "self-host": 46,
    }
    assert len(calls) == 3
    for key, value in sentinels.items():
        assert os.environ[key] == value


def test_self_host_discovery_failure_closes_resources_and_restores_environment(monkeypatch):
    module = exporter()
    namespace = module["export_definitions"].__globals__
    real_create = namespace["create_hosted_app"]
    real_list = namespace["_list_tools"]
    closes = []
    calls = 0
    monkeypatch.setenv("GOOGLE_PLAY_PACKAGES", "com.operator.private")

    def tracked_create(settings):
        app = real_create(settings)
        for resource in (app.state.hosted_registry, app.state.hosted_transfers, app.state.vault):
            close = Mock(wraps=resource.close)
            monkeypatch.setattr(resource, "close", close)
            closes.append(close)
        return app

    async def failing_list(server):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("synthetic discovery failure")
        return await real_list(server)

    namespace["create_hosted_app"] = tracked_create
    namespace["_list_tools"] = failing_list
    with pytest.raises(RuntimeError, match="synthetic discovery failure"):
        asyncio.run(module["export_definitions"]())
    assert len(closes) == 3
    for close in closes:
        close.assert_called_once_with()
    assert os.environ["GOOGLE_PLAY_PACKAGES"] == "com.operator.private"


@pytest.mark.parametrize("lint", [False, True])
def test_export_cli_writes_profiles_and_runs_lint_only_when_requested(tmp_path, monkeypatch, lint):
    module = exporter()
    profiles = {name: {"tools": [{"name": "synthetic"}]} for name in ("a", "b", "c")}

    async def fake_export():
        return profiles

    namespace = module["main"].__globals__
    namespace["export_definitions"] = fake_export
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        sys, "argv", [str(EXPORTER), "--output-dir", str(tmp_path)] + (["--lint"] if lint else [])
    )
    module["main"]()
    assert run.call_count == (3 if lint else 0)
    for name, payload in profiles.items():
        assert json.loads((tmp_path / f"{name}.json").read_text()) == payload
    if lint:
        for (args, kwargs), name in zip(run.call_args_list, profiles, strict=True):
            assert args[0] == [
                sys.executable,
                "-m",
                "tdqs",
                "lint",
                "--file",
                str(tmp_path / f"{name}.json"),
                "--format",
                "json",
                "--fail-on",
                "never",
                "--output",
                str(tmp_path / f"{name}-lint.json"),
            ]
            assert kwargs == {"check": True}


def test_export_cli_propagates_lint_failure(tmp_path, monkeypatch):
    module = exporter()

    async def fake_export():
        return {"local-default": {"tools": []}}

    module["main"].__globals__["export_definitions"] = fake_export
    monkeypatch.setattr(sys, "argv", [str(EXPORTER), "--output-dir", str(tmp_path), "--lint"])
    monkeypatch.setattr(
        subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(7, "tdqs"))
    )
    with pytest.raises(subprocess.CalledProcessError) as error:
        module["main"]()
    assert error.value.returncode == 7


def test_exporter_script_entrypoint_requires_output_dir(monkeypatch):
    monkeypatch.setattr(sys, "argv", [str(EXPORTER)])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(str(EXPORTER), run_name="__main__")
    assert error.value.code == 2
