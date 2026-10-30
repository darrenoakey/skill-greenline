# Real stopped-baseline maintenance lifecycle proofs on disposable repositories.
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.request
from dataclasses import dataclass

import pytest

from test_bootstrap import GREENLINE, Project, assert_success, cli, git
from test_bootstrap import interrupt as interrupt_first_bootstrap
from test_bootstrap import wait_for


@dataclass
class Maintenance:
    project: Project
    baseline: str
    deployed: bytes
    history: bytes
    calls: int


def candidate_command(project, *arguments):
    return subprocess.run([str(project.repo / "run"), *arguments], cwd=project.repo,
                          capture_output=True, text=True, timeout=10, check=False)


def maintenance_bootstrap(snapshot):
    project = snapshot.project
    return cli(project.repo, "bootstrap", "gl/candidate", "--stopped-baseline", snapshot.baseline)


def journal(project):
    path = project.state / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def assert_history_preserved(snapshot):
    assert (snapshot.project.state / "journal.jsonl").read_bytes().startswith(snapshot.history)


def assert_status(project, verdict):
    result = cli(project.repo, "status")
    assert_success(result)
    assert f"[{verdict}]" in result.stdout


def assert_stopped_status(project):
    assert_status(project, "PRODUCTION STOPPED; attestation historical")
    result = cli(project.repo, "doctor")
    assert result.returncode == 2
    assert "production not deliberately stopped" in result.stdout + result.stderr


def assert_restored(snapshot):
    project = snapshot.project
    assert git(project.repo, "rev-parse", "main") == snapshot.baseline
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == snapshot.baseline
    assert (project.state / "deployed").read_bytes() == snapshot.deployed
    assert not project.service.exists()
    assert not (project.state / "bootstrap.json").exists()
    assert not (project.common / "OLD_DEPLOY_DANGER").exists()
    assert_history_preserved(snapshot)
    stopped = candidate_command(project, "bootstrap-stopped")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr


@pytest.fixture
def bootstrap_project(tmp_path):
    value = Project(tmp_path)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def maintenance(bootstrap_project):
    project = bootstrap_project
    assert_success(project.bootstrap())
    baseline = git(project.repo, "rev-parse", "main")
    deployed = (project.state / "deployed").read_bytes()
    history = (project.state / "journal.jsonl").read_bytes()
    token = json.loads(project.service.read_text())["token"]
    cleanup = candidate_command(project, "bootstrap-cleanup", "--bootstrap-id", token)
    assert cleanup.returncode == 0, cleanup.stdout + cleanup.stderr
    assert not project.service.exists()
    stopped = candidate_command(project, "bootstrap-stopped")
    assert stopped.returncode == 0, stopped.stdout + stopped.stderr
    (project.common / "started").unlink()
    project.before = baseline
    project.green = baseline
    project.tip = baseline
    (project.candidate / "maintenance-change").write_text("second deployment\n")
    git(project.candidate, "add", "maintenance-change")
    git(project.candidate, "commit", "-qm", "maintenance candidate")
    project.tip = git(project.candidate, "rev-parse", "HEAD")
    return Maintenance(project, baseline, deployed, history, len(project.calls()))


def interrupt_maintenance(snapshot, signum):
    project = snapshot.project
    (project.flags / "hold").touch()
    log_path = project.common / f"maintenance-{signum}.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(GREENLINE), "bootstrap", "gl/candidate", "--stopped-baseline",
             snapshot.baseline, "--repo", str(project.repo)],
            cwd=project.repo, stdout=log, stderr=log,
        )
        try:
            wait_for(lambda: (project.common / "started").exists())
            record_path = project.state / "bootstrap.json"
            original = record_path.read_bytes()
            record = json.loads(original)
            project.owned_ids.add(record["bootstrap_id"])
            assert record["version"] == 2 and type(record["version"]) is int
            assert record["mode"] == "stopped-baseline"
            assert record["stopped_baseline"] == snapshot.baseline
            assert record["pre_deployed"] == snapshot.baseline
            assert record["pre_main"] == snapshot.baseline
            assert record["pre_green"] == snapshot.baseline
            assert record["candidate"] != snapshot.baseline
            os.kill(process.pid, signum)
            process.wait(timeout=12)
            return record, original
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_stopped_baseline_success_deploys_maintenance_and_preserves_history(maintenance):
    project = maintenance.project
    assert_success(maintenance_bootstrap(maintenance))
    candidate = git(project.repo, "rev-parse", "main")
    assert candidate != maintenance.baseline
    assert git(project.repo, "rev-parse", f"{candidate}^{{tree}}") == git(project.repo, "rev-parse", f"{project.tip}^{{tree}}")
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == candidate
    assert (project.state / "deployed").read_text() == candidate + "\n"
    assert (project.repo / "maintenance-change").read_text() == "second deployment\n"
    assert not (project.state / "bootstrap.json").exists()
    assert_history_preserved(maintenance)
    complete = [entry for entry in journal(project) if entry.get("event") == "bootstrap_complete"][-1]
    assert complete["merged"] == candidate
    assert complete["mode"] == "stopped-baseline"
    assert complete["stopped_baseline"] == maintenance.baseline
    assert complete["production"] == "healthy"
    assert_status(project, "OK")
    calls = project.calls()[maintenance.calls:]
    assert calls and all(item["head"] == candidate for item in calls)
    assert [item["command"] for item in calls].count("bootstrap-stopped") == 2
    assert not (project.common / "OLD_DEPLOY_DANGER").exists()
    data = json.loads(project.service.read_text())
    with urllib.request.urlopen(f'http://127.0.0.1:{data["port"]}/', timeout=1) as response:
        assert response.read().decode() == data["token"]


def test_default_bootstrap_refuses_prior_attestation(maintenance):
    project = maintenance.project
    result = project.bootstrap()
    assert result.returncode != 0
    assert len(project.calls()) == maintenance.calls
    assert_restored(maintenance)


def test_stopped_baseline_refuses_wrong_exact_baseline(maintenance):
    wrong = git(maintenance.project.repo, "rev-parse", f"{maintenance.baseline}^")
    result = maintenance_bootstrap(Maintenance(**{**maintenance.__dict__, "baseline": wrong}))
    assert result.returncode != 0
    assert len(maintenance.project.calls()) == maintenance.calls
    assert_restored(maintenance)


@pytest.mark.parametrize("baseline", ["main", "A" * 40, "f" * 39, "g" * 40])
def test_stopped_baseline_refuses_malformed_sha(maintenance, baseline):
    project = maintenance.project
    result = cli(project.repo, "bootstrap", "gl/candidate", "--stopped-baseline", baseline)
    assert result.returncode != 0
    assert len(project.calls()) == maintenance.calls
    assert_restored(maintenance)


@pytest.mark.parametrize("damage", ["missing-deployed", "pending", "incomplete-history"])
def test_stopped_baseline_refuses_unsafe_attestation_or_history(maintenance, damage):
    project = maintenance.project
    deployed = project.state / "deployed"
    if damage == "missing-deployed":
        deployed.unlink()
    elif damage == "pending":
        (project.state / "pending-deploy").write_text(maintenance.baseline + "\n")
    else:
        with (project.state / "journal.jsonl").open("a") as stream:
            stream.write(json.dumps({"event": "start", "branch": "gl/interrupted"}) + "\n")
    result = maintenance_bootstrap(maintenance)
    assert result.returncode != 0
    assert len(project.calls()) == maintenance.calls
    assert git(project.repo, "rev-parse", "main") == maintenance.baseline
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == maintenance.baseline
    if damage == "missing-deployed":
        assert not deployed.exists()
    else:
        assert deployed.read_bytes() == maintenance.deployed
    assert_history_preserved(maintenance)
    assert not (project.state / "bootstrap.json").exists()
    assert not project.service.exists()


def test_stopped_baseline_refuses_live_baseline_without_cleanup(maintenance):
    project = maintenance.project
    server = subprocess.Popen([sys.executable, str(project.repo / "run"), "serve", "revived-baseline"],
                              cwd=project.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(project.service.exists)
        result = maintenance_bootstrap(maintenance)
        assert result.returncode != 0
        assert server.poll() is None
        calls = project.calls()[maintenance.calls:]
        assert not [item for item in calls if item["command"] in {"deploy", "bootstrap-cleanup"}]
        assert git(project.repo, "rev-parse", "main") == maintenance.baseline
        assert (project.state / "deployed").read_bytes() == maintenance.deployed
        assert_history_preserved(maintenance)
    finally:
        server.terminate()
        server.wait(timeout=5)
        project.service.unlink(missing_ok=True)


def test_failed_deploy_restores_refs_and_prior_attestation_without_old_deploy(maintenance):
    project = maintenance.project
    (project.flags / "deploy").touch()
    result = maintenance_bootstrap(maintenance)
    assert result.returncode != 0, result.stdout + result.stderr
    assert_restored(maintenance)
    assert_stopped_status(project)
    failed = [entry for entry in journal(project) if entry.get("event") == "bootstrap_failed"][-1]
    assert failed["production"] == "stopped"
    assert failed["mode"] == "stopped-baseline"
    assert failed["stopped_baseline"] == maintenance.baseline
    assert failed["pre_deployed"] == maintenance.baseline
    candidate = failed["candidate"]
    calls = project.calls()[maintenance.calls:]
    deploys = [item for item in calls if item["command"] == "deploy"]
    assert len(deploys) == 1 and deploys[0]["head"] == candidate
    cleanup = [item for item in calls if item["command"] == "bootstrap-cleanup"]
    assert cleanup and all(item["head"] == candidate and item["cwd"] == str(project.repo) for item in cleanup)


@pytest.mark.parametrize("command", [("submit", "gl/candidate"), ("adopt",), ("deploy-pending",)])
def test_restored_failure_blocks_ordinary_commands_until_successful_opt_in(maintenance, command):
    project = maintenance.project
    (project.flags / "deploy").touch()
    assert maintenance_bootstrap(maintenance).returncode != 0
    assert_restored(maintenance)
    assert_stopped_status(project)
    blocked_history = (project.state / "journal.jsonl").read_bytes()
    result = cli(project.repo, *command)
    assert result.returncode != 0
    assert (project.state / "deployed").read_bytes() == maintenance.deployed
    assert (project.state / "journal.jsonl").read_bytes() == blocked_history
    assert git(project.repo, "rev-parse", "main") == maintenance.baseline
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == maintenance.baseline
    (project.flags / "deploy").unlink()
    retry_calls = len(project.calls())
    assert_success(maintenance_bootstrap(maintenance))
    candidate = git(project.repo, "rev-parse", "main")
    assert candidate != maintenance.baseline
    assert (project.state / "deployed").read_text() == candidate + "\n"
    assert_status(project, "OK")
    retry = project.calls()[retry_calls:]
    assert [item["command"] for item in retry].count("bootstrap-stopped") >= 2
    assert all(item["head"] == candidate for item in retry)


@pytest.mark.parametrize("entry", [
    {"event": "bootstrap_cleanup_failed", "production": "unknown"},
    {"event": "rollback_failed"},
    {"event": "needs_fix_forward"},
    {"event": "deploy_failed", "production": "unknown"},
    {"event": "fail", "stage": "release_deadline", "rollback_error": "rollback failed"},
    {"event": "adopt_failed", "stage": "release_deadline", "rollback_error": "rollback failed"},
    {"event": "fail", "stage": "release_deadline", "reconciliation": "diverged: origin candidate"},
])
def test_stopped_baseline_refuses_unknown_release_state(maintenance, entry):
    project = maintenance.project
    with (project.state / "journal.jsonl").open("a") as stream:
        stream.write(json.dumps(entry) + "\n")
    assert maintenance_bootstrap(maintenance).returncode != 0
    assert len(project.calls()) == maintenance.calls
    assert (project.state / "deployed").read_bytes() == maintenance.deployed
    assert_history_preserved(maintenance)
    assert git(project.repo, "rev-parse", "main") == maintenance.baseline
    assert not project.service.exists()


def test_stopped_baseline_cleanup_failure_resolved_by_stopped_recovery(maintenance):
    project = maintenance.project
    (project.flags / "deploy").touch()
    (project.flags / "cleanup").touch()
    assert maintenance_bootstrap(maintenance).returncode != 0
    record_path = project.state / "bootstrap.json"
    record = json.loads(record_path.read_text())
    assert record["production"] == "unknown"
    assert record["mode"] == "stopped-baseline"
    assert project.service.exists()
    assert (project.state / "deployed").read_bytes() == maintenance.deployed
    assert_history_preserved(maintenance)
    (project.flags / "cleanup").unlink()
    assert_success(cli(project.repo, "bootstrap-recover"))
    assert_restored(maintenance)
    assert_stopped_status(project)
    failed = [entry for entry in journal(project)
              if entry.get("event") == "bootstrap_failed" and entry.get("candidate") == record["candidate"]]
    assert len(failed) == 1 and failed[0]["production"] == "stopped"
    (project.flags / "deploy").unlink()
    retry_calls = len(project.calls())
    assert_success(maintenance_bootstrap(maintenance))
    assert_status(project, "OK")
    retry = project.calls()[retry_calls:]
    assert [item["command"] for item in retry].count("bootstrap-stopped") >= 2


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_stopped_baseline_interruption_requires_explicit_recovery(maintenance, signum):
    project = maintenance.project
    record, _ = interrupt_maintenance(maintenance, signum)
    if signum == signal.SIGKILL:
        assert (project.state / "bootstrap.json").exists()
        assert project.service.exists()
    else:
        assert not (project.state / "bootstrap.json").exists()
        assert not project.service.exists()
    assert_success(cli(project.repo, "bootstrap-recover"))
    assert_restored(maintenance)
    assert_stopped_status(project)
    failed = [entry for entry in journal(project)
              if entry.get("event") == "bootstrap_failed" and entry.get("candidate") == record["candidate"]]
    assert len(failed) == 1
    assert failed[0]["production"] == "stopped"
    assert failed[0]["mode"] == "stopped-baseline"
    assert failed[0]["pre_deployed"] == maintenance.baseline


@pytest.mark.parametrize("field,value", [
    ("mode", "first-deploy"),
    ("mode", "unrecognized"),
    ("stopped_baseline", None),
    ("stopped_baseline", "0" * 40),
    ("pre_deployed", None),
    ("pre_deployed", "0" * 40),
])
def test_corrupt_maintenance_metadata_fails_closed(maintenance, field, value):
    project = maintenance.project
    record, original = interrupt_maintenance(maintenance, signal.SIGKILL)
    path = project.state / "bootstrap.json"
    record[field] = value
    path.write_text(json.dumps(record))
    damaged = path.read_bytes()
    result = cli(project.repo, "bootstrap-recover")
    assert result.returncode != 0
    assert path.read_bytes() == damaged
    assert project.service.exists()
    assert not [item for item in project.calls()[maintenance.calls:] if item["command"] == "bootstrap-cleanup"]
    assert git(project.repo, "rev-parse", "main") == record["candidate"]
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == maintenance.baseline
    assert (project.state / "deployed").read_bytes() == maintenance.deployed
    assert_history_preserved(maintenance)
    path.write_bytes(original)
    assert_success(cli(project.repo, "bootstrap-recover"))
    assert_restored(maintenance)


def test_legacy_first_deploy_record_recovers_without_new_metadata(bootstrap_project):
    project = bootstrap_project
    record = interrupt_first_bootstrap(project, signal.SIGKILL)
    path = project.state / "bootstrap.json"
    for field in ("version", "mode", "stopped_baseline", "pre_deployed"):
        record.pop(field)
    path.write_text(json.dumps(record))
    assert_success(cli(project.repo, "bootstrap-recover"))
    project.restored()


@pytest.mark.parametrize("candidate_attested", [False, True])
def test_maintenance_record_without_any_new_metadata_fails_closed(maintenance, candidate_attested):
    project = maintenance.project
    record, original = interrupt_maintenance(maintenance, signal.SIGKILL)
    path = project.state / "bootstrap.json"
    if candidate_attested:
        # Reproduce a crash between candidate attestation and record removal.
        # The real candidate listener is already running from the interrupted CLI.
        assert_success(candidate_command(project, "health"))
        (project.state / "deployed").write_text(record["candidate"] + "\n")
    attestation = (project.state / "deployed").read_bytes()
    for field in ("version", "mode", "stopped_baseline", "pre_deployed"):
        record.pop(field)
    path.write_text(json.dumps(record))
    assert cli(project.repo, "bootstrap-recover").returncode != 0
    assert path.exists() and project.service.exists()
    assert (project.state / "deployed").read_bytes() == attestation
    assert not [item for item in project.calls()[maintenance.calls:] if item["command"] == "bootstrap-cleanup"]
    path.write_bytes(original)
    assert_success(cli(project.repo, "bootstrap-recover"))
    assert_restored(maintenance)
