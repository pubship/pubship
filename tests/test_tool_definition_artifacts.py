"""Artifact discovery must fail closed on installation, provenance or parity errors."""

import builtins
import json
import runpy
import socket
import subprocess
import sys
import sysconfig
from pathlib import Path
from types import ModuleType

import google.auth
import pytest

_SCRIPT = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/check_tool_definition_artifacts.py")
)
check_artifacts = _SCRIPT["check_artifacts"]
compare_definitions = _SCRIPT["compare_definitions"]


def write_definitions(directory, *, version="0.24.1"):
    directory.mkdir(exist_ok=True)
    for profile, count in (("local-default", 13), ("local-all-opt-ins", 41), ("self-host", 46)):
        (directory / f"{profile}.json").write_text(
            json.dumps(
                {
                    "tools": [
                        {"name": f"test_{index}", "description": "Synthetic fixture"}
                        for index in range(count)
                    ],
                }
            )
        )
    (directory / "provenance.json").write_text(json.dumps({"version": version}))


@pytest.fixture
def artifact_run(tmp_path, monkeypatch):
    root = tmp_path / "checkout"
    dist = root / "dist"
    dist.mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nversion = "0.24.1"\n')
    (dist / "pubship-0.24.1-py3-none-any.whl").write_bytes(b"synthetic")
    (dist / "pubship-0.24.1.tar.gz").write_bytes(b"synthetic")
    output = tmp_path / "evidence"
    calls = []
    state = {"fail_at": None, "missing_rebuild": False}

    def fake_run(command, *, cwd, environment):
        calls.append((command, cwd, environment))
        if state["fail_at"] == len(calls):
            raise subprocess.CalledProcessError(1, command)
        if command[:2] == ["uv", "export"]:
            Path(command[command.index("--output-file") + 1]).write_text("# synthetic\n")
        elif command[:2] == ["uv", "build"] and not state["missing_rebuild"]:
            rebuild = Path(command[command.index("--out-dir") + 1])
            rebuild.mkdir()
            (rebuild / "pubship-0.24.1-py3-none-any.whl").write_bytes(b"synthetic")
        elif "-c" in command:
            write_definitions(Path(command[-1]))

    monkeypatch.setitem(check_artifacts.__globals__, "run", fake_run)
    return root, dist, output, calls, state


def test_artifact_discovery_uses_isolated_installs_and_frozen_dependencies(
    artifact_run, monkeypatch
):
    root, dist, output, calls, _ = artifact_run
    for name in (
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_PLAY_PACKAGES",
        "PUBSHIP_SERVER_ALLOWED_EMAILS",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "synthetic-operator-setting")
    check_artifacts(root, dist, output)
    assert len(calls) == 11
    for command, cwd, environment in calls:
        assert not any(
            name.startswith(("GOOGLE_PLAY_", "PUBSHIP_SERVER_", "PYTHON"))
            or name == "GOOGLE_APPLICATION_CREDENTIALS"
            for name in environment
        )
        if command[:2] == ["uv", "export"]:
            assert cwd == root
            assert {"--frozen", "--all-extras", "--no-default-groups", "--no-emit-project"} <= set(
                command
            )
        else:
            assert not cwd.is_relative_to(root)
        if command[:3] == ["uv", "pip", "sync"]:
            assert "--require-hashes" in command
        if "-c" in command:
            assert command[1:3] == ["-I", "-c"]
            assert command[-3] == (str(root / "src") if command[-1].endswith("/source") else "-")
    assert (output / "source/local-default.json").is_file()
    assert (output / "wheel/local-all-opt-ins.json").is_file()
    assert (output / "sdist-wheel/self-host.json").is_file()


@pytest.mark.parametrize("failure", range(1, 12))
def test_artifact_discovery_stops_at_each_failed_subprocess(artifact_run, failure):
    root, dist, output, calls, state = artifact_run
    state["fail_at"] = failure
    with pytest.raises(subprocess.CalledProcessError):
        check_artifacts(root, dist, output)
    assert len(calls) == failure


def test_artifact_discovery_rejects_missing_rebuilt_wheel(artifact_run):
    root, dist, output, calls, state = artifact_run
    state["missing_rebuild"] = True
    with pytest.raises(ValueError, match="sdist build did not produce"):
        check_artifacts(root, dist, output)
    assert len(calls) == 9


@pytest.mark.parametrize("failure", ["inside_checkout", "missing_artifact", "stale_output"])
def test_artifact_discovery_rejects_invalid_inputs_before_commands(artifact_run, failure):
    root, dist, output, calls, _ = artifact_run
    if failure == "inside_checkout":
        output = root / "evidence"
    elif failure == "missing_artifact":
        dist = root / "missing"
    else:
        (output / "source").mkdir(parents=True)
    with pytest.raises((ValueError, FileExistsError)):
        check_artifacts(root, dist, output)
    assert not calls


@pytest.mark.parametrize("profile", _SCRIPT["PROFILES"])
@pytest.mark.parametrize("change", ["description", "annotation", "missing_file"])
def test_full_payload_comparison_rejects_each_profile_regression(tmp_path, profile, change):
    reference, candidate = tmp_path / "source", tmp_path / "wheel"
    write_definitions(reference)
    write_definitions(candidate)
    path = candidate / f"{profile}.json"
    if change == "missing_file":
        path.unlink()
    else:
        data = json.loads(path.read_text())
        data["tools"][0][change] = "changed"
        path.write_text(json.dumps(data))
    with pytest.raises((ValueError, FileNotFoundError)):
        compare_definitions(reference, candidate)


@pytest.mark.parametrize("profile", _SCRIPT["PROFILES"])
@pytest.mark.parametrize(
    "source_default,artifact_default",
    [(True, 1), (False, 0), (1, 1.0), (float("nan"), float("nan")), (float("inf"), float("inf"))],
)
def test_payload_parity_preserves_json_types_and_rejects_nonfinite_numbers(
    tmp_path, profile, source_default, artifact_default
):
    reference, candidate = tmp_path / "source", tmp_path / "wheel"
    for directory, default in ((reference, source_default), (candidate, artifact_default)):
        write_definitions(directory)
        path = directory / f"{profile}.json"
        payload = json.loads(path.read_text())
        payload["tools"][0]["inputSchema"] = {
            "properties": {"value": {"default": default}},
        }
        path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        compare_definitions(reference, candidate)


def test_full_payload_comparison_rejects_wrong_installed_version(tmp_path):
    reference, candidate = tmp_path / "source", tmp_path / "wheel"
    write_definitions(reference)
    write_definitions(candidate, version="0.0.0")
    with pytest.raises(ValueError, match="installed version"):
        compare_definitions(reference, candidate)


def test_subprocess_wrapper_enforces_timeout_and_check(monkeypatch, tmp_path):
    received = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: received.append((args, kwargs)))
    _SCRIPT["run"](["synthetic"], cwd=tmp_path, environment={})
    assert received == [
        (
            (["synthetic"],),
            {
                "cwd": tmp_path,
                "env": {},
                "check": True,
                "timeout": 300,
            },
        )
    ]


@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("failure", [None, "package_origin", "module_origin", "missing_origin"])
def test_discovery_bootstrap_enforces_import_provenance(monkeypatch, tmp_path, installed, failure):
    # Exercise the exact child bootstrap with synthetic modules. No real package,
    # credential file or provider connection is loaded by this orchestration test.
    source = "-" if installed else str(tmp_path / "src")
    package_root = (Path(sysconfig.get_path("purelib")) if installed else Path(source)) / "pubship"
    package = ModuleType("pubship")
    package.__file__ = str(package_root / "__init__.py")
    package.__version__ = "0.24.1"
    if failure == "package_origin":
        package.__file__ = str(tmp_path / "wrong/__init__.py")
    for name in tuple(sys.modules):
        if name == "pubship" or name.startswith("pubship."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "pubship", package)
    child = ModuleType("pubship.server")
    if failure != "missing_origin":
        child.__file__ = str(
            tmp_path / "outside/server.py"
            if failure == "module_origin"
            else package_root / "server.py"
        )
    monkeypatch.setitem(sys.modules, "pubship.server", child)
    output = tmp_path / "output"
    output.mkdir()
    monkeypatch.setattr(sys, "argv", ["bootstrap", source, "exporter.py", str(output)])
    monkeypatch.setattr(sys, "path", sys.path.copy())
    exports = []

    def assert_discovery_guards():
        with pytest.raises(AssertionError, match="ADC access prohibited"):
            google.auth.default()
        with pytest.raises(AssertionError, match="Network access prohibited"):
            socket.getaddrinfo("example.invalid", 443)
        with socket.socket() as connection:
            for connect in (connection.connect, connection.connect_ex):
                with pytest.raises(AssertionError, match="Network access prohibited"):
                    connect(("127.0.0.1", 443))

    original_import = builtins.__import__
    imports = []

    def guarded_import(name, *args, **kwargs):
        if name == "pubship":
            imports.append(name)
            assert_discovery_guards()
        return original_import(name, *args, **kwargs)

    def fake_export(*args, **kwargs):
        exports.append((args, kwargs))
        assert_discovery_guards()

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(runpy, "run_path", fake_export)
    if failure:
        with pytest.raises(RuntimeError, match="expected source or isolated install"):
            exec(_SCRIPT["_DISCOVER"], {})
        assert not (output / "provenance.json").exists()
        assert len(exports) == (0 if failure == "package_origin" else 1)
    else:
        exec(_SCRIPT["_DISCOVER"], {})
        provenance = json.loads((output / "provenance.json").read_text())
        assert provenance["package_root"] == str(package_root.resolve())
        assert provenance["version"] == "0.24.1"
        assert set(provenance["modules"]) == {"pubship", "pubship.server"}
        assert exports == [(("exporter.py",), {"run_name": "__main__"})]
    assert imports == ["pubship"]


def test_cli_passes_parsed_paths(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setitem(
        check_artifacts.__globals__, "check_artifacts", lambda *args: calls.append(args)
    )
    monkeypatch.setattr(sys, "argv", ["check", "--output-dir", str(tmp_path)])
    _SCRIPT["main"]()
    assert calls == [(Path(__file__).resolve().parents[1], Path("dist"), tmp_path)]


def test_script_entrypoint_rejects_missing_cli_arguments(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check"])
    with pytest.raises(SystemExit) as error:
        runpy.run_path(
            str(Path(__file__).parents[1] / "scripts/check_tool_definition_artifacts.py"),
            run_name="__main__",
        )
    assert error.value.code == 2
