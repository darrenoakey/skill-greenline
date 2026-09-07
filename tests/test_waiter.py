"""Real subprocess and journal regressions for greenline-wait.sh."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

from test_greenline import (
    GREENLINE_WAIT,
    RUN_RECORDER,
    SLOW_CHECK,
    commit_in,
    common_dir,
    make_worktree,
    run_git,
    setup_repo,
    with_main_unlocked,
    write_run_script,
)


def append_event(repo, event, **fields):
    journal = common_dir(repo) / "greenline" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a") as stream:
        stream.write(json.dumps({"event": event, "ts": datetime.now(timezone.utc).isoformat(), **fields}) + "\n")


# Completion/startup waits below are pure event waits, not semantic bounds:
# on a heavily loaded gate machine a full submit takes tens of seconds, so
# they all get generous ceilings. The waiter script carries its own internal
# 600s release bound; these timeouts only guard against a hung test.
def wait_for_event(repo, event, branch=None, timeout=120):
    journal = common_dir(repo) / "greenline" / "journal.jsonl"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if journal.exists():
            entries = [json.loads(line) for line in journal.read_text().splitlines() if line]
            for entry in entries:
                if entry.get("event") == event and (branch is None or entry.get("branch") == branch):
                    return entry
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {event} branch={branch}")


def wait_for_path(path, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def process_is_running(pid):
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return bool(state) and not state.startswith("Z")


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def stop_pid(pid):
    if pid is None or not process_is_running(pid):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not process_is_running(pid):
            return
        time.sleep(0.02)
    os.kill(pid, signal.SIGKILL)


def test_waiter_ignores_peer_span_before_its_exact_start(tmp_path):
    repo = setup_repo(tmp_path, "wait-peer")
    slow = RUN_RECORDER.replace("check)\n", "check)\n        sleep 0.5\n", 1)
    write_run_script(repo, slow)
    with with_main_unlocked(repo):
        run_git(repo, "add", "run")
        run_git(repo, "commit", "-q", "-m", "slow observable check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    peer = make_worktree(repo, "peer")
    commit_in(peer, "peer.txt", "peer\n", "peer")
    own = make_worktree(repo, "own")
    commit_in(own, "own.txt", "own\n", "own")

    peer_proc = subprocess.Popen(
        [GREENLINE_WAIT, "--repo", str(peer), "gl/peer"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    own_proc = None
    try:
        wait_for_event(repo, "start", "gl/peer")
        own_proc = subprocess.Popen(
            [GREENLINE_WAIT, "--repo", str(own), "gl/own"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        own_output, _ = own_proc.communicate(timeout=120)
        peer_output, _ = peer_proc.communicate(timeout=120)

        assert peer_proc.returncode == 0, peer_output
        assert own_proc.returncode == 0, own_output
        own_complete = wait_for_event(repo, "complete", "gl/own")
        peer_complete = wait_for_event(repo, "complete", "gl/peer")
        assert f"candidate={own_complete['merged']}" in own_output
        assert peer_complete["merged"] not in own_output
    finally:
        stop_process(peer_proc)
        if own_proc is not None:
            stop_process(own_proc)


def test_waiter_rejects_complete_attestation_when_owned_submit_exits_nonzero(tmp_path):
    repo = setup_repo(tmp_path, "wait-exit")
    slow = RUN_RECORDER.replace("check)\n", "check)\n        sleep 120\n", 1)
    write_run_script(repo, slow)
    with with_main_unlocked(repo):
        run_git(repo, "add", "run")
        run_git(repo, "commit", "-q", "-m", "block check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    own = make_worktree(repo, "exit-owner")
    commit_in(own, "owned.txt", "owned\n", "owned")

    waiter = subprocess.Popen(
        [GREENLINE_WAIT, "--repo", str(own), "gl/exit-owner"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    greenline_pid = None
    try:
        start = wait_for_event(repo, "start", "gl/exit-owner")
        candidate = run_git(repo, "--git-dir", str(common_dir(repo)), "rev-parse", "refs/heads/main")
        append_event(repo, "checked", candidate=candidate)
        append_event(repo, "complete", branch="gl/exit-owner", merged=candidate)
        status = json.loads((common_dir(repo) / "greenline" / "status.json").read_text())
        greenline_pid = status["pid"]
        os.kill(greenline_pid, signal.SIGTERM)
        output, _ = waiter.communicate(timeout=120)

        assert start["candidate_src_sha"] == run_git(own, "rev-parse", "HEAD")
        assert waiter.returncode != 0
        assert "process_exit=" in output
        assert "GREENLINE PASS" not in output
    finally:
        stop_process(waiter)
        stop_pid(greenline_pid)


def test_terminal_journal_failure_reaps_owned_submission_tree(tmp_path):
    repo = setup_repo(tmp_path, "wait-terminal")
    write_run_script(repo, SLOW_CHECK)
    with with_main_unlocked(repo):
        run_git(repo, "add", "run")
        run_git(repo, "commit", "-q", "-m", "block check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    own = make_worktree(repo, "terminal-owner")
    commit_in(own, "owned.txt", "owned\n", "owned")

    waiter = subprocess.Popen(
        [GREENLINE_WAIT, "--repo", str(own), "gl/terminal-owner"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    greenline_pid = None
    child_pid = None
    try:
        wait_for_event(repo, "start", "gl/terminal-owner")
        status = json.loads((common_dir(repo) / "greenline" / "status.json").read_text())
        greenline_pid = status["pid"]
        child_pid = int(wait_for_path(common_dir(repo) / "slow-child.pid").read_text())
        append_event(repo, "fail", branch="gl/terminal-owner", stage="injected-terminal")
        output, _ = waiter.communicate(timeout=120)

        assert waiter.returncode == 1, output
        assert "event={" in output
        assert not process_is_running(child_pid)
    finally:
        stop_process(waiter)
        stop_pid(greenline_pid)
        stop_pid(child_pid)


def test_attach_uses_recorded_start_deadline_without_owning_process(tmp_path):
    repo = setup_repo(tmp_path, "wait-attach")
    own = make_worktree(repo, "attach-owner")
    commit_in(own, "attach.txt", "attach\n", "attach")
    source = run_git(own, "rev-parse", "HEAD")
    old = (datetime.now(timezone.utc) - timedelta(seconds=601)).isoformat()
    journal = common_dir(repo) / "greenline" / "journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.open("a") as stream:
        stream.write(json.dumps({"event": "start", "ts": old, "branch": "gl/attach-owner", "candidate_src_sha": source}) + "\n")

    sleeper = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        result = subprocess.run(
            [GREENLINE_WAIT, "--attach", "--repo", str(own), "gl/attach-owner"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 2
        assert "release deadline exceeded" in result.stdout
        assert sleeper.poll() is None
    finally:
        os.killpg(sleeper.pid, signal.SIGTERM)
        sleeper.wait(timeout=3)
