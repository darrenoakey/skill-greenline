"""Real end-to-end tests for greenline-speedup, the CLI greenline launches on
a slow-but-green gate (see trigger_speedup_task in the `greenline` script).

No mocks: each test builds a real scratch git repo via greenline itself, and
stands in for agentd3 with a real local HTTP server bound to an OS-assigned
port (never a fixed one — the same rule DOCTRINE.md requires of every check
under test) rather than mocking urllib.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_greenline import gl, run_git, sha  # noqa: F401 (sha kept for parity/reuse)

SPEEDUP = str(Path(__file__).resolve().parent.parent / "greenline-speedup")


# --------------------------------------------------------------------------
# a real HTTP double for agentd3's two endpoints this script calls
# --------------------------------------------------------------------------
class FakeAgentd3(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append({"path": self.path, "body": body})
        if self.path == "/v1/conversations":
            reply = self.server.create_reply(body)
            code = 200 if reply.get("replayed") else 201
        elif self.path.endswith("/messages"):
            reply = {}
            code = 200
        else:
            reply = {"error": "not found"}
            code = 404
        data = json.dumps(reply).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *a):  # silence BaseHTTPRequestHandler's own logging
        pass


class Agentd3Double:
    """A real, running stand-in for agentd3's conversation-create + message-post
    API, on 127.0.0.1:<os-assigned port>. `requests` accumulates every POST
    body in arrival order; `next_replayed` controls whether the NEXT create
    reply claims an idempotent replay (agentd3's own dedupe signal)."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAgentd3)
        self.server.requests = []
        self._replay_next = False
        self.server.create_reply = self._create_reply
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _create_reply(self, _body):
        replayed = self._replay_next
        self._replay_next = False
        return {"conversation_id": "conv-double-1", "replayed": replayed}

    def replay_next_create(self):
        self._replay_next = True

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def requests(self) -> list[dict]:
        return self.server.requests

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


def agentd3_double():
    return Agentd3Double()


# --------------------------------------------------------------------------
# scratch repo helper (a minimal greenline setup; no ./run contract needed —
# greenline-speedup never runs check/deploy, only `greenline worktree`)
# --------------------------------------------------------------------------
def scratch_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    run = repo / "run"
    run.write_text("#!/usr/bin/env bash\nexit 0\n")
    run.chmod(0o755)
    wtbase = tmp_path / "wt" / name
    (repo / "greenline.toml").write_text(
        "contract_version = 1\n"
        'main_branch = "main"\n'
        'check = "./run check"\n'
        'deploy = "./run deploy"\n'
        'health = ""\n'
        f'service = "{name}"\n'
        f'worktree_base = "{wtbase}"\n'
    )
    (repo / "app.txt").write_text("v0\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    gl(repo, "setup", expect=0)
    return repo


def run_speedup(repo: Path, api_base: str, **kwargs) -> subprocess.CompletedProcess:
    args = [
        sys.executable,
        SPEEDUP,
        "--repo",
        str(repo),
        "--branch",
        kwargs.get("branch", "gl/slow-thing"),
        "--candidate",
        kwargs.get("candidate", "deadbeef" * 5),
        "--seconds",
        str(kwargs.get("seconds", 250.4)),
        "--budget",
        str(kwargs.get("budget", 200)),
        "--hard-timeout",
        str(kwargs.get("hard_timeout", 600)),
        "--log",
        kwargs.get("log", ""),
    ]
    env = {**os.environ, "AGENTD3_API": api_base}
    return subprocess.run(args, capture_output=True, text=True, env=env)


def todays_speedup_name() -> str:
    return f"speedup-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d')}"


def worktree_path(repo: Path) -> Path:
    """The date-scoped worktree name is an implementation detail shared by
    the script and this helper; today's date always matches since both run
    in the same process at effectively the same instant."""
    return configured_worktree_base(repo) / todays_speedup_name()


def marker_file(repo: Path) -> Path:
    """Today's trigger marker, in the repo's greenline state dir. It must never
    live in the worktree: an untracked file at a worktree root makes `greenline
    submit` refuse it, so the speed-up task could not land its own fix."""
    return repo / ".git" / "greenline" / f"{todays_speedup_name()}.json"


def configured_worktree_base(repo: Path) -> Path:
    with (repo / "greenline.toml").open("rb") as fh:
        return Path(tomllib.load(fh)["worktree_base"]).expanduser()


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_speedup_creates_worktree_and_posts_conversation_and_prompt(tmp_path):
    repo = scratch_repo(tmp_path, "happy")
    double = agentd3_double()
    try:
        proc = run_speedup(
            repo,
            double.base_url,
            branch="gl/model-alias-decouple",
            candidate="cafebabe1234deadbeef",
            seconds=367.0,
            budget=200,
            hard_timeout=600,
            log=str(tmp_path / "check.log"),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr

        wt = worktree_path(repo)
        assert wt.exists() and wt.is_dir()
        assert (wt / "greenline.toml").exists(), "must be a real greenline worktree"

        assert len(double.requests) == 2
        create, message = double.requests
        assert create["path"] == "/v1/conversations"
        assert create["body"]["model"] == "agentic-high"
        assert create["body"]["source"] == "greenline-speedup"
        assert create["body"]["policy"] == "yolo"
        assert Path(create["body"]["cwd"]).resolve() == wt.resolve()
        assert create["body"]["idempotency_key"].startswith("greenline-speedup:happy:")
        assert create["body"]["origin"]["kind"] == "service"
        assert "gl/model-alias-decouple" in create["body"]["origin"]["ref"]

        assert message["path"] == "/v1/conversations/conv-double-1/messages"
        prompt = message["body"]["text"]
        assert "happy" in prompt
        assert "367.0s" in prompt
        assert "200s" in prompt
        assert "gl/model-alias-decouple" in prompt
        assert "greenline submit" in prompt and "greenline done" in prompt
        assert "Do NOT weaken" in prompt

        marker = marker_file(repo)
        assert marker.exists()
        assert json.loads(marker.read_text())["conversation_id"] == "conv-double-1"
        assert run_git(wt, "status", "--porcelain") == "", (
            "the trigger must leave the worktree clean, or greenline submit "
            "refuses the speed-up task's own fix"
        )
    finally:
        double.stop()


def test_speedup_is_idempotent_same_day(tmp_path):
    repo = scratch_repo(tmp_path, "dedupe")
    double = agentd3_double()
    try:
        first = run_speedup(repo, double.base_url)
        assert first.returncode == 0, first.stdout + first.stderr
        assert len(double.requests) == 2

        second = run_speedup(repo, double.base_url, branch="gl/a-different-slow-branch")
        assert second.returncode == 0, second.stdout + second.stderr
        assert "already triggered today" in second.stdout
        assert len(double.requests) == 2, "must not create a second conversation"
    finally:
        double.stop()


def test_speedup_replayed_conversation_skips_message_post(tmp_path):
    """agentd3's own idempotency replay (e.g. two greenline hosts racing) must
    not get a second prompt posted into an already-running conversation."""
    repo = scratch_repo(tmp_path, "replayed")
    double = agentd3_double()
    double.replay_next_create()
    try:
        proc = run_speedup(repo, double.base_url)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(double.requests) == 1, "a replay must not also post a message"
        assert double.requests[0]["path"] == "/v1/conversations"

        marker = marker_file(repo)
        assert marker.exists(), "a replay is still a successful trigger"
    finally:
        double.stop()


def test_speedup_reports_error_when_agentd3_unreachable(tmp_path):
    repo = scratch_repo(tmp_path, "unreachable")
    # A closed local port: connection refused, fast and deterministic.
    proc = run_speedup(repo, "http://127.0.0.1:1")
    assert proc.returncode != 0
    assert "error" in proc.stderr.lower()

    wt = worktree_path(repo)
    assert wt.exists(), "the worktree is still created before the API call"
    assert not marker_file(repo).exists()


def test_speedup_resumes_an_existing_unmarked_worktree(tmp_path):
    """A prior run that created the worktree but died before the API call
    succeeded (agentd3 was briefly down) must retry into the SAME worktree
    on the next slow gate that day, not fail on 'branch already exists'."""
    repo = scratch_repo(tmp_path, "resume")
    dead = run_speedup(repo, "http://127.0.0.1:1")
    assert dead.returncode != 0
    wt = worktree_path(repo)
    assert wt.exists()
    assert not marker_file(repo).exists()

    double = agentd3_double()
    try:
        proc = run_speedup(repo, double.base_url)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert len(double.requests) == 2
        assert Path(double.requests[0]["body"]["cwd"]).resolve() == wt.resolve()
        assert marker_file(repo).exists()
    finally:
        double.stop()


def test_speedup_resume_honors_a_machine_worktree_base_override(tmp_path, monkeypatch):
    """A ~/.config/greenline/config.toml override for this repo name wins over
    the committed greenline.toml — same as for every other greenline command
    — including on the RESUME path, which greenline-speedup must read out of
    greenline's own error message rather than re-deriving itself. This is not
    a hypothetical: several real repos (the one that motivated this feature
    among them) carry exactly this kind of override."""
    home = tmp_path / "home"
    (home / ".config" / "greenline").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    repo = scratch_repo(tmp_path, "override")
    committed_base = configured_worktree_base(repo)
    override_base = tmp_path / "machine-override-base"
    (home / ".config" / "greenline" / "config.toml").write_text(
        f'[worktree_base]\noverride = "{override_base}"\n'
    )

    dead = run_speedup(repo, "http://127.0.0.1:1")
    assert dead.returncode != 0
    wt_name = todays_speedup_name()
    overridden_wt = override_base / wt_name
    committed_wt = committed_base / wt_name
    assert overridden_wt.exists(), (
        "must land under the machine override, not the committed toml"
    )
    assert not committed_wt.exists()

    double = agentd3_double()
    try:
        proc = run_speedup(repo, double.base_url)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert (
            Path(double.requests[0]["body"]["cwd"]).resolve() == overridden_wt.resolve()
        )
        assert marker_file(repo).exists()
    finally:
        double.stop()
