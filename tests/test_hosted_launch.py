"""Fast launch-fixture safety tests. No Docker, Google, large data or soak runs."""

import importlib.util
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "hosted_launch_fixture", Path(__file__).resolve().parents[1] / "scripts/test_hosted_launch.py"
)
launch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = launch
SPEC.loader.exec_module(launch)


def test_concurrent_traffic_cannot_overspend_or_advance_after_rejection():
    budget = launch.Budget(seconds=10, traffic=100, clock=lambda: 0)
    barrier = threading.Barrier(20)

    def contender(_):
        barrier.wait(timeout=5)
        try:
            budget.charge(10)
            return True
        except launch.CheckFailure:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        accepted = list(pool.map(contender, range(20)))
    assert sum(accepted) == 10
    assert budget.used == 100
    with pytest.raises(launch.CheckFailure, match="Traffic"):
        budget.charge(1)
    assert budget.used == 100


def test_deadline_stops_progress_even_when_traffic_budget_remains():
    now = [100.0]
    budget = launch.Budget(seconds=10, traffic=100, clock=lambda: now[0])
    now[0] = 109.99
    budget.charge(1)
    now[0] = 110.0
    with pytest.raises(launch.CheckFailure, match="Wall-clock"):
        budget.charge(1)
    assert budget.used == 1


@pytest.mark.parametrize(
    "values", [{"seconds": 2701}, {"traffic": 32 * launch.GIB + 1}, {"seconds": 0}, {"traffic": 0}]
)
def test_operator_cannot_disable_outer_budget(values):
    with pytest.raises(launch.CheckFailure):
        launch.Budget(**values)


def test_wrong_tenant_response_fails_without_printing_customer_content():
    driver = object.__new__(launch.Driver)
    driver.account_lock = threading.Lock()
    driver.accounts = [{"access_token": "synthetic"}]
    driver.request = lambda *args, **kwargs: (
        200,
        {},
        json.dumps(
            {
                "result": {
                    "structuredContent": {
                        "data": {"reviews": [{"reviewId": "OTHER-TENANT-PRIVATE"}]}
                    }
                }
            }
        ).encode(),
    )
    with pytest.raises(launch.CheckFailure, match="Tenant response mismatch") as error:
        driver.read(0)
    assert "OTHER-TENANT-PRIVATE" not in str(error.value)


def test_driver_failure_removes_only_its_private_stack(monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        args = command[1:]
        output, result = "", 0
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "10001:10001"}}])
        if args[0] == "run" and "driver" in args:
            output, result = (
                json.dumps({"status": "failed", "failure": "Synthetic expected failure"}),
                1,
            )
        return subprocess.CompletedProcess(command, result, output, "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    report = launch.run_isolated("candidate", launch.PROXY_IMAGE, launch.Profile("smoke"))
    assert report["status"] == "failed" and report["cleanup"] == "passed"
    assert report["failure"] == "Synthetic expected failure"
    network_create = next(c for c in commands if c[1:3] == ["network", "create"])
    assert "--internal" in network_create
    removed = [c[-1] for c in commands if c[1:3] == ["rm", "-f"]]
    assert len(removed) == 5 and all(n.startswith("pubship-launch-") for n in removed)
    assert any(c[1:3] == ["network", "rm"] for c in commands)
    assert sum(c[1:3] == ["volume", "rm"] for c in commands) == 2
    tmpfs_runs = [c for c in commands if "--tmpfs" in c]
    assert len(tmpfs_runs) == 1
    app = tmpfs_runs[0]
    assert app[app.index("--network-alias") + 1] == "app"
    assert (
        app[app.index("--tmpfs") + 1]
        == "/tmp:rw,noexec,nosuid,nodev,size=64m,uid=10001,gid=10001,mode=0700"
    )
    assert app[app.index("--memory") + 1] == "1g"
    assert "--read-only" in app
    for command in commands:
        if command[0] != "docker":
            continue
        assert "--privileged" not in command
        assert "-p" not in command and "--publish" not in command
        assert "/var/run/docker.sock" not in " ".join(command)


def test_unpinned_proxy_rejected_before_docker_or_filesystem_changes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No Docker command should run")

    monkeypatch.setattr(launch.subprocess, "run", forbidden)
    with pytest.raises(launch.CheckFailure, match="explicit Traefik digest"):
        launch.run_isolated("candidate", "traefik:latest", launch.Profile("smoke"))


@pytest.mark.parametrize("failure", ["timeout", "oserror", "exit"])
def test_cleanup_failure_keeps_receipt_and_attempts_every_owned_resource(monkeypatch, failure):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        args = command[1:]
        output, code = "", 0
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "10001:10001"}}])
        elif args[0] == "run" and "driver" in args:
            output, code = json.dumps({"status": "failed", "failure": "Synthetic failure"}), 1
        elif args[:2] == ["rm", "-f"] and args[-1].endswith("-driver"):
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, 20)
            if failure == "oserror":
                raise OSError("Synthetic unavailable daemon")
            code = 1
        return subprocess.CompletedProcess(command, code, output, "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    report = launch.run_isolated("candidate", launch.PROXY_IMAGE, launch.Profile("smoke"))
    assert report["status"] == "failed" and report["cleanup"] == "failed"
    assert report["failure"] == "Synthetic failure"
    assert sum(c[1:3] == ["rm", "-f"] for c in commands) == 5
    assert sum(c[1:3] == ["volume", "rm"] for c in commands) == 2
    assert sum(c[1:3] == ["network", "rm"] for c in commands) == 1


def test_all_containers_use_one_frozen_harness_copy(monkeypatch, tmp_path):
    source = tmp_path / "source.py"
    source.write_bytes(b"original fixture source")
    monkeypatch.setattr(launch, "__file__", str(source))
    mounted = []

    def run(command, **kwargs):
        args = command[1:]
        output, code = "", 0
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "10001:10001"}}])
        elif args[0] == "run":
            mount = next(a for a in args if a.startswith("type=bind,src="))
            frozen = Path(mount.split("src=", 1)[1].split(",dst=", 1)[0])
            mounted.append(frozen)
            assert frozen != source
            assert frozen.read_bytes() == b"original fixture source"
            source.write_bytes(b"edited after container startup")
            if "driver" in args:
                output, code = json.dumps({"status": "failed"}), 1
        return subprocess.CompletedProcess(command, code, output, "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    report = launch.run_isolated("candidate", launch.PROXY_IMAGE, launch.Profile("smoke"))
    assert len(mounted) == 5 and len(set(mounted)) == 1
    assert report["harness_sha256"] == launch.hashlib.sha256(b"original fixture source").hexdigest()
    assert not mounted[0].exists()


def test_recovery_rejects_true_probe_completed_after_deadline():
    now = [0.0]
    driver = object.__new__(launch.Driver)
    driver.budget = launch.Budget(seconds=20, clock=lambda: now[0])

    def slow_true():
        now[0] = 6
        return True

    with pytest.raises(launch.CheckFailure, match="before deadline"):
        driver.wait(slow_true, seconds=5)
    assert driver._probe_deadline is None


def test_recovery_includes_time_before_first_probe():
    now = [0.0]
    driver = object.__new__(launch.Driver)
    driver.budget = launch.Budget(seconds=20, clock=lambda: now[0])
    now[0] = 11
    called = []
    with pytest.raises(launch.CheckFailure, match="before deadline"):
        driver.wait(lambda: called.append(True) or True, seconds=10, started=0)
    assert not called


def test_recovery_probe_socket_timeout_uses_remaining_window(monkeypatch):
    now = [0.0]
    driver = object.__new__(launch.Driver)
    driver.budget = launch.Budget(seconds=20, clock=lambda: now[0])
    now[0] = 7
    observed = []
    monkeypatch.setattr(
        launch.http.client, "HTTPConnection", lambda host, port, timeout: observed.append(timeout)
    )

    def probe():
        driver.connection(direct=True, timeout=660)
        now[0] = 8
        return True

    assert driver.wait(probe, seconds=10, started=0) == 8
    assert observed == [3]


def byte_state():
    return {
        "fingerprint": "unchanged",
        "owner_rows": 50,
        "owner_bytes": 4 * launch.MIB - 100,
        "lifecycle_rows": 150,
        "lifecycle_bytes": 5 * launch.MIB,
    }


@pytest.mark.parametrize(
    "message",
    [
        "Hosted authorization capacity reached; try again later.",
        "Connection authorization capacity reached; reconnect this account.",
    ],
)
def test_byte_rejection_accepts_actual_boundary_with_unchanged_state(message):
    before = byte_state()
    launch.validate_byte_rejection(before, dict(before), 200, message, 32)


@pytest.mark.parametrize("fault", ["early", "changed", "storage", "rows", "global_bytes"])
def test_byte_rejection_cannot_pass_on_early_or_unrelated_fault(fault):
    before = byte_state()
    message = "Hosted authorization capacity reached; try again later."
    if fault == "early":
        before["owner_bytes"] = 1000
    elif fault == "storage":
        message = "Credential storage is unavailable; reconnect later."
    elif fault == "rows":
        before["owner_rows"] = 4096
    elif fault == "global_bytes":
        before["lifecycle_bytes"] = 32 * 4 * launch.MIB
    after = dict(before)
    if fault == "changed":
        after["fingerprint"] = "modified"
    with pytest.raises(launch.CheckFailure):
        launch.validate_byte_rejection(before, after, 200, message, 32)


def test_volume_created_before_timeout_is_still_removed(monkeypatch):
    owned = set()
    attempts = []

    def run(command, **kwargs):
        args = command[1:]
        output = ""
        if args[:2] == ["image", "inspect"]:
            output = json.dumps([{"Id": "sha256:" + "a" * 64, "Config": {"User": "10001:10001"}}])
        elif args[:2] == ["volume", "create"]:
            owned.add(args[-1])
            raise subprocess.TimeoutExpired(command, 60)
        elif args[:2] == ["volume", "rm"]:
            attempts.append(args[-1])
            owned.remove(args[-1])
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(launch.subprocess, "run", run)
    report = launch.run_isolated("candidate", launch.PROXY_IMAGE, launch.Profile("smoke"))
    assert report["status"] == "failed" and report["cleanup"] == "passed"
    assert report["orchestration_failure"] == "TimeoutExpired"
    assert len(attempts) == 1 and attempts[0].startswith("pubship-launch-")
    assert not owned


def test_launch_proxy_uses_host_scoped_h1_and_closes_all_ordinary_responses():
    config = launch.proxy_configuration()
    router = config["http"]["routers"]["fixture"]
    assert router["tls"] == {"options": "pubship-h1"}
    assert config["tls"]["options"] == {"pubship-h1": {"alpnProtocols": ["http/1.1"]}}
    assert router["middlewares"] == ["fixture-close", "fixture-rate", "fixture-inflight"]
    assert config["http"]["middlewares"]["fixture-close"] == {
        "headers": {"customResponseHeaders": {"Connection": "close"}}
    }
    assert not any("http3" in argument.lower() for argument in launch.proxy_arguments())
    assert (
        "--entrypoints.websecure.transport.respondingtimeouts.readtimeout=630s"
        in launch.proxy_arguments()
    )


def test_tls_connection_uses_router_hostname_for_sni():
    driver = object.__new__(launch.Driver)
    driver.tls = launch.ssl.create_default_context()
    driver.budget = launch.Budget()
    connection = driver.connection()
    assert connection.host == "launch-fixture.invalid"
    assert connection.port == 8443


def test_socket_pressure_keeps_control_off_saturated_pool_and_counts_close_time(monkeypatch):
    driver = object.__new__(launch.Driver)
    now, active, sampled, starts = [0.0], [0], [], []
    driver.budget = launch.Budget(clock=lambda: now[0])

    class Connection:
        def putrequest(self, *args, **kwargs):
            active[0] += 1

        def putheader(self, *args):
            pass

        def endheaders(self):
            pass

        def send(self, value):
            pass

        def close(self):
            active[0] -= 1
            now[0] += 0.01

    def sample():
        assert active[0] == 0
        sampled.append(now[0])
        return {"fds": 14, "peak_rss_kib": 12345}

    def wait(predicate, *, seconds, started):
        assert active[0] == 0 and seconds == 10
        assert now[0] - started == pytest.approx(1.28)
        starts.append(started)
        assert predicate()
        return now[0] - started

    driver.connection = lambda **kwargs: Connection()
    driver.sample = sample
    driver.wait = wait
    driver.request = lambda *args: (200, {}, b"")
    monkeypatch.setattr(launch.time, "sleep", lambda seconds: None)
    result = driver.socket_pressure()
    assert len(sampled) == 2 and starts == [0]
    assert result["attempted"] == 128 and result["recovery_seconds"] == pytest.approx(1.28)
    assert result["retained_peak_rss_kib"] == 12345
    assert result["peak_fds"].startswith("not measured")
