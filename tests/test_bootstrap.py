# Real bootstrap lifecycle proofs; every repository and HTTP listener is disposable.
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

GREENLINE = Path(__file__).resolve().parents[1] / "greenline"

# The candidate owns its service through an unguessable release token. Cleanup
# verifies the live HTTP identity before signalling the recorded process.
SERVICE = r'''#!/usr/bin/env python3
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import threading
import urllib.request

root = Path(subprocess.check_output(["git", "rev-parse", "--git-common-dir"], text=True).strip()).resolve()
state = root / "service.json"
command = sys.argv[1]
flags = root / "flags"
flags.mkdir(exist_ok=True)
def flag(name):
    return (flags / name).exists()
def record(event):
    with (root / "calls.jsonl").open("a") as stream:
        stream.write(json.dumps({"command": event, "head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(), "cwd": str(Path.cwd())}) + "\n")
def probe(data):
    with urllib.request.urlopen("http://127.0.0.1:%s/" % data["port"], timeout=1) as response:
        return response.read().decode()
if command == "serve":
    token = sys.argv[2]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(503 if flag("unhealthy") else 200)
            self.end_headers()
            self.wfile.write(token.encode())
        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    state.write_text(json.dumps({"pid": os.getpid(), "port": server.server_port, "token": token}))
    server.serve_forever()
record(command)
if command == "check":
    compile(Path(__file__).read_text(), str(Path(__file__)), "exec")
    assert Path("candidate-source").read_text() == "candidate\n"
    if flag("hold-check"):
        (root / "check-started").touch()
        deadline = time.monotonic() + 15
        while not flag("release-check") and time.monotonic() < deadline:
            threading.Event().wait(.01)
        assert flag("release-check")
    sys.exit(7 if flag("check") else 0)
if command == "bootstrap-stopped":
    if state.exists():
        try:
            probe(json.loads(state.read_text()))
        except urllib.error.HTTPError:
            sys.exit(8)
        except (OSError, ValueError):
            pass
        else:
            sys.exit(8)
    sys.exit(0)
if command == "deploy":
    assert sys.argv[2] == "--bootstrap-id"
    assert sys.argv[3]
    output = (root / "server.log").open("ab")
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", sys.argv[3]], stdout=output, stderr=output, start_new_session=True)
    deadline = time.monotonic() + 5
    while not state.exists() and time.monotonic() < deadline:
        threading.Event().wait(.01)
    assert state.exists()
    (root / "started").touch()
    if flag("hold"):
        (root / "held-deploy.pid").write_text(json.dumps({"pid": os.getpid(), "token": sys.argv[3]}))
        threading.Event().wait(120)
    sys.exit(9 if flag("deploy") else 0)
if command == "health":
    data = json.loads(state.read_text())
    assert probe(data) == data["token"]
    sys.exit(0)
if command == "bootstrap-cleanup":
    assert sys.argv[2] == "--bootstrap-id"
    token = sys.argv[3]
    if flag("cleanup"):
        sys.exit(10)
    if not state.exists():
        sys.exit(0)
    data = json.loads(state.read_text())
    assert data["token"] == token, "refuse foreign recorded token"
    try:
        live = probe(data)
    except urllib.error.HTTPError as error:
        live = error.read().decode()
    assert live == token, "refuse foreign live token"
    os.kill(data["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            probe(data)
        except urllib.error.HTTPError:
            pass
        except OSError:
            state.unlink()
            sys.exit(0)
        threading.Event().wait(.01)
    raise RuntimeError("owned listener did not stop")
    sys.exit(0)
raise RuntimeError(command)
'''


def git(repo, *arguments):
    return subprocess.check_output(["git", "-C", str(repo), *arguments], text=True).strip()


def cli(repo, *arguments):
    return subprocess.run([sys.executable, str(GREENLINE), *arguments, "--repo", str(repo)],
                          cwd=repo, capture_output=True, text=True, timeout=25, check=False)


def assert_success(result):
    assert result.returncode == 0, result.stdout + result.stderr


class Project:
    def __init__(self, root):
        self.owned_ids = set()
        self.repo = root / "canonical"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.name", "Bootstrap test")
        git(self.repo, "config", "user.email", "bootstrap@example.test")
        self.common = self.repo / ".git"
        self.state = self.common / "greenline"
        self.service = self.common / "service.json"
        self.flags = self.common / "flags"
        self.flags.mkdir()
        old = self.repo / "run"
        old.write_text("#!/bin/sh\nif [ \"$1\" = deploy ]; then touch .git/OLD_DEPLOY_DANGER; exit 71; fi\nexit 0\n")
        old.chmod(0o755)
        self.config = ('contract_version = 1\nmain_branch = "main"\ncheck = "./run check"\n'
                       'deploy = "./run deploy"\nhealth = "./run health"\n'
                       f'worktree_base = "{root / "worktrees"}"\n')
        (self.repo / "greenline.toml").write_text(self.config)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "old dangerous deploy")
        assert_success(cli(self.repo, "setup"))
        self.before = git(self.repo, "rev-parse", "main")
        self.green = git(self.repo, "rev-parse", "refs/greenline/last-green")
        self.candidate = root / "candidate"
        git(self.repo, "worktree", "add", "-qb", "gl/candidate", str(self.candidate))
        (self.candidate / "run").write_text(SERVICE.replace("#!/usr/bin/env python3", f"#!{sys.executable}", 1))
        (self.candidate / "run").chmod(0o755)
        (self.candidate / "candidate-source").write_text("candidate\n")
        (self.candidate / "greenline.toml").write_text(self.config + '\n[bootstrap]\n'
            'stopped = "./run bootstrap-stopped"\n'
            'cleanup = "./run bootstrap-cleanup --bootstrap-id {bootstrap_id}"\n'
            'deploy = "./run deploy --bootstrap-id {bootstrap_id}"\n')
        git(self.candidate, "add", ".")
        git(self.candidate, "commit", "-qm", "candidate real HTTP service")
        self.tip = git(self.candidate, "rev-parse", "HEAD")

    def calls(self):
        path = self.common / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def bootstrap(self):
        return cli(self.repo, "bootstrap", "gl/candidate")

    def restored(self):
        assert git(self.repo, "rev-parse", "main") == self.before
        assert git(self.repo, "rev-parse", "refs/greenline/last-green") == self.green
        assert not (self.state / "deployed").exists()
        assert not self.service.exists()
        assert not (self.common / "OLD_DEPLOY_DANGER").exists()
        assert not (self.state / "bootstrap.json").exists()

    def close(self):
        held = self.common / "held-deploy.pid"
        if held.exists():
            owned = json.loads(held.read_text())
            pid = owned["pid"]
            for identity_file, key in [(self.service, "token"), (self.state / "bootstrap.json", "bootstrap_id")]:
                if identity_file.exists():
                    self.owned_ids.add(json.loads(identity_file.read_text())[key])
            command = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
            arguments = shlex.split(command.stdout)
            expected = ["./run", "deploy", "--bootstrap-id", owned["token"]]
            if owned["token"] in self.owned_ids and arguments[-4:] == expected:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        if self.service.exists():
            data = json.loads(self.service.read_text())
            # Only this test's OS-assigned loopback listener can authorize teardown.
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{data["port"]}/', timeout=1) as response:
                    token = response.read().decode()
            except urllib.error.HTTPError as error:
                token = error.read().decode()
            except OSError:
                return
            if token == data["token"]:
                os.kill(data["pid"], signal.SIGTERM)


@pytest.fixture
def project(tmp_path):
    value = Project(tmp_path)
    try:
        yield value
    finally:
        value.close()


def test_bootstrap_success_runs_candidate_and_attests_exact_listener(project):
    assert_success(project.bootstrap())
    deployed = git(project.repo, "rev-parse", "main")
    assert git(project.repo, "rev-parse", f"{deployed}^{{tree}}") == git(project.repo, "rev-parse", f"{project.tip}^{{tree}}")
    project.tip = deployed
    assert git(project.repo, "rev-parse", "refs/greenline/last-green") == project.tip
    assert (project.state / "deployed").read_text().strip() == project.tip
    assert not (project.state / "bootstrap.json").exists()
    data = json.loads(project.service.read_text())
    with urllib.request.urlopen(f'http://127.0.0.1:{data["port"]}/', timeout=1) as response:
        assert response.read().decode() == data["token"]
    calls = project.calls()
    assert {item["command"] for item in calls} >= {"check", "deploy", "health", "bootstrap-stopped"}
    assert all(item["head"] == project.tip for item in calls)
    assert not (project.common / "OLD_DEPLOY_DANGER").exists()


@pytest.mark.parametrize("failure", ["check", "deploy", "unhealthy"])
def test_bootstrap_failure_never_deploys_old_tree(project, failure):
    (project.flags / failure).touch()
    result = project.bootstrap()
    assert result.returncode != 0, result.stdout + result.stderr
    project.restored()
    commands = [item["command"] for item in project.calls()]
    if failure == "check":
        assert "deploy" not in commands
    else:
        assert "bootstrap-cleanup" in commands


@pytest.mark.parametrize("marker", ["deployed", "pending-deploy"])
@pytest.mark.parametrize("contents", ["", "existing deployment\n"])
def test_bootstrap_refuses_existing_even_empty_markers(project, marker, contents):
    (project.state / marker).write_text(contents)
    result = project.bootstrap()
    assert result.returncode != 0
    assert not project.calls()
    assert git(project.repo, "rev-parse", "main") == project.before
    assert (project.state / marker).read_text() == contents


def test_bootstrap_refuses_live_unrelated_baseline_without_cleanup(project):
    server = subprocess.Popen([sys.executable, str(project.candidate / "run"), "serve", "unrelated-baseline"],
                              cwd=project.candidate, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(project.service.exists)
        assert project.bootstrap().returncode != 0
        assert "bootstrap-cleanup" not in [item["command"] for item in project.calls()]
        assert server.poll() is None
        assert git(project.repo, "rev-parse", "main") == project.before
    finally:
        server.terminate()
        server.wait(timeout=5)


def wait_for(predicate):
    deadline = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < deadline, "disposable subprocess did not reach expected phase"
        threading.Event().wait(.02)


def interrupt(project, signum):
    (project.flags / "hold").touch()
    log = (project.common / "interruption.log").open("w")
    process = subprocess.Popen([sys.executable, str(GREENLINE), "bootstrap", "gl/candidate", "--repo", str(project.repo)],
                               cwd=project.repo, stdout=log, stderr=log)
    try:
        wait_for(lambda: (project.common / "started").exists())
        record = json.loads((project.state / "bootstrap.json").read_text())
        project.owned_ids.add(record["bootstrap_id"])
        assert git(project.repo, "rev-parse", record["candidate"] + "^{tree}") == git(project.repo, "rev-parse", project.tip + "^{tree}")
        project.tip = record["candidate"]
        assert record["pre_main"] == project.before
        assert record["pre_green"] == project.green
        assert record["bootstrap_id"] == json.loads(project.service.read_text())["token"]
        assert record["canonical"] == str(project.repo)
        assert record["main_branch"] == "main"
        assert set(record["commands"]) >= {"check", "deploy", "health", "stopped", "cleanup"}
        assert "bootstrap-cleanup" in record["commands"]["cleanup"]
        os.kill(process.pid, signum)
        process.wait(timeout=12)
        return record
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        log.close()


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGKILL])
def test_bootstrap_interruption_restart_explicit_recovery(project, signum):
    interrupt(project, signum)
    assert_success(cli(project.repo, "bootstrap-recover"))
    project.restored()


def test_bootstrap_failed_cleanup_blocks_submit_and_adopt_until_recovery(project):
    (project.flags / "deploy").touch()
    (project.flags / "cleanup").touch()
    assert project.bootstrap().returncode != 0
    record = (project.state / "bootstrap.json").read_text()
    assert json.loads(record)["production"] == "unknown"
    assert project.service.exists()
    for command in [("submit", "gl/candidate"), ("adopt",)]:
        assert cli(project.repo, *command).returncode != 0
        assert (project.state / "bootstrap.json").read_text() == record
    (project.flags / "cleanup").unlink()
    assert_success(cli(project.repo, "bootstrap-recover"))
    project.restored()


def test_bootstrap_recovery_pins_candidate_cleanup_despite_branch_changes(project):
    interrupt(project, signal.SIGKILL)
    # Branch/config changes outside canonical cannot redirect persisted cleanup.
    (project.candidate / "run").write_text("#!/bin/sh\ntouch .git/WRONG_CLEANUP\nexit 73\n")
    (project.candidate / "greenline.toml").write_text(project.config)
    git(project.candidate, "add", ".")
    git(project.candidate, "commit", "-qm", "replace candidate cleanup and config")
    assert_success(cli(project.repo, "bootstrap-recover"))
    cleanup = [item for item in project.calls() if item["command"] == "bootstrap-cleanup"]
    assert cleanup and all(item["head"] == project.tip for item in cleanup)
    assert all(item["cwd"] == str(project.repo) for item in cleanup)
    project.restored()


def test_bootstrap_cleanup_refuses_mismatched_token_and_preserves_live_service(project):
    interrupt(project, signal.SIGKILL)
    path = project.state / "bootstrap.json"
    record = json.loads(path.read_text())
    original = record["bootstrap_id"]
    record["bootstrap_id"] = "not-the-owned-service"
    # Persisted commands may already be rendered; exercise the actual candidate
    # cleanup with a foreign identity first, independently of recovery parsing.
    denied = subprocess.run([str(project.repo / "run"), "bootstrap-cleanup", "--bootstrap-id", record["bootstrap_id"]],
                            cwd=project.repo, capture_output=True, text=True, timeout=5, check=False)
    assert denied.returncode != 0
    data = json.loads(project.service.read_text())
    with urllib.request.urlopen(f'http://127.0.0.1:{data["port"]}/', timeout=1) as response:
        assert response.read().decode() == original
    assert_success(cli(project.repo, "bootstrap-recover"))
    project.restored()


def test_bootstrap_recovery_worktree_alias_executes_cleanup_only_in_canonical(project):
    interrupt(project, signal.SIGKILL)
    assert_success(cli(project.candidate, "bootstrap-recover"))
    cleanup = [item for item in project.calls() if item["command"] == "bootstrap-cleanup"]
    assert cleanup and all(item["cwd"] == str(project.repo) and item["head"] == project.tip for item in cleanup)
    project.restored()


@pytest.mark.parametrize("command", [("submit", "gl/candidate"), ("adopt",)])
def test_bootstrap_queued_ordinary_command_rechecks_pending_identity(project, command):
    (project.flags / "hold-check").touch()
    (project.flags / "hold").touch()
    bootstrap_log = (project.common / "queued-bootstrap.log").open("w")
    ordinary_log = project.common / "queued-ordinary.log"
    ordinary_output = ordinary_log.open("w")
    bootstrap = subprocess.Popen([sys.executable, str(GREENLINE), "bootstrap", "gl/candidate", "--repo", str(project.repo)],
                                 cwd=project.repo, stdout=bootstrap_log, stderr=bootstrap_log)
    ordinary = None
    try:
        wait_for(lambda: (project.common / "check-started").exists())
        assert not (project.state / "bootstrap.json").exists()
        ordinary = subprocess.Popen([sys.executable, "-u", str(GREENLINE), *command, "--repo", str(project.repo)],
                                    cwd=project.repo, stdout=ordinary_output, stderr=ordinary_output)
        wait_for(lambda: "waiting for lock" in ordinary_log.read_text())
        (project.flags / "release-check").touch()
        wait_for(lambda: (project.common / "started").exists())
        record = (project.state / "bootstrap.json").read_text()
        project.owned_ids.add(json.loads(record)["bootstrap_id"])
        bootstrap.kill()
        bootstrap.wait(timeout=5)
        assert ordinary.wait(timeout=10) != 0
        assert (project.state / "bootstrap.json").read_text() == record
        assert not (project.common / "OLD_DEPLOY_DANGER").exists()
        assert [item["command"] for item in project.calls()].count("deploy") == 1
        assert_success(cli(project.repo, "bootstrap-recover"))
        project.restored()
    finally:
        for process in (bootstrap, ordinary):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        bootstrap_log.close()
        ordinary_output.close()


def test_bootstrap_recovery_retains_blocker_if_interrupted_push_reached_origin(project, tmp_path):
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    git(project.repo, "remote", "add", "origin", str(remote))
    allow = project.state / "allow-push"
    allow.write_text("scratch remote setup\n")
    try:
        git(project.repo, "push", "-q", "origin", "main")
    finally:
        allow.unlink()
    interrupt(project, signal.SIGKILL)
    # Reproduce the observable state of a push that reached the remote before
    # the CLI could write its local success attestation, using real git refs.
    git(remote, "fetch", "-q", str(project.repo), f"{project.tip}:refs/heads/main")
    assert cli(project.repo, "bootstrap-recover").returncode != 0
    record = json.loads((project.state / "bootstrap.json").read_text())
    assert record["production"] == "stopped"
    assert not project.service.exists()
    assert git(project.repo, "rev-parse", "main") == project.before
    assert git(remote, "rev-parse", "main") == project.tip
    for command in [("submit", "gl/candidate"), ("adopt",)]:
        assert cli(project.repo, *command).returncode != 0
    assert not (project.common / "OLD_DEPLOY_DANGER").exists()


@pytest.mark.parametrize("contents", ["not-json\n", "[]\n", '{"event": "complete"}\nnot-json\n'])
def test_bootstrap_refuses_malformed_deployment_history(project, contents):
    (project.state / "journal.jsonl").write_text(contents)
    assert project.bootstrap().returncode != 0
    assert not project.calls()
    assert not (project.state / "bootstrap.json").exists()
    project.restored()


@pytest.mark.parametrize("field,value", [("candidate", "HEAD"), ("pre_main", "main"),
                                         ("pre_green", "f" * 40)])
def test_bootstrap_recovery_refuses_nonliteral_or_unrelated_ref_identity(project, field, value):
    interrupt(project, signal.SIGKILL)
    path = project.state / "bootstrap.json"
    original = path.read_text()
    record = json.loads(original)
    record[field] = value
    path.write_text(json.dumps(record))
    assert cli(project.repo, "bootstrap-recover").returncode != 0
    assert not [item for item in project.calls() if item["command"] == "bootstrap-cleanup"]
    assert project.service.exists()
    path.write_text(original)
    assert_success(cli(project.repo, "bootstrap-recover"))
    project.restored()
