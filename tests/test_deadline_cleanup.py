"""Real deadline regressions for process cleanup and publish reconciliation."""

from __future__ import annotations

import os
import shlex
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from test_greenline import (
    commit_in,
    common_dir,
    journal_events,
    load_greenline_module,
    make_worktree,
    pid_is_gone,
    run_git,
    setup_repo,
    sha,
)


def test_timeout_kills_term_ignoring_grandchild_after_leader_exits(tmp_path):
    module = load_greenline_module()
    pid_path = tmp_path / "grandchild.pid"
    descendant = (
        "import os,signal,sys,threading;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "threading.Event().wait(30)"
    )
    leader = (
        "import subprocess,sys,threading;"
        f"subprocess.Popen([sys.executable,'-c',{descendant!r},sys.argv[1]]);"
        "threading.Event().wait(30)"
    )
    module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
    module.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
    module.PROCESS_PIPE_DRAIN_SECONDS = 0.5
    module.ACTIVE_RELEASE_DEADLINE = module.ReleaseDeadline(
        time.monotonic(), 0.5, module.RELEASE_TIMEOUT_REASON
    )
    started = time.monotonic()
    try:
        with pytest.raises(module.ReleaseDeadlineExceeded):
            module.run_argv([sys.executable, "-c", leader, str(pid_path)], "regression")
    finally:
        module.ACTIVE_RELEASE_DEADLINE = None

    elapsed = time.monotonic() - started
    assert pid_path.exists(), "the real descendant must start before expiry"
    descendant_pid = int(pid_path.read_text())
    assert elapsed < 2.5, "pipe drain and group cleanup must remain bounded"
    assert pid_is_gone(descendant_pid), (
        f"TERM-ignoring descendant {descendant_pid} survived group cleanup"
    )


def test_timeout_fails_when_escaped_child_keeps_stdout_open(tmp_path):
    module = load_greenline_module()
    pid_path = tmp_path / "escaped-child.pid"
    log_path = tmp_path / "escaped-child.log"
    escaped = (
        "import os,sys,threading;"
        "open(sys.argv[1],'w').write(str(os.getpid()));"
        "threading.Event().wait(30)"
    )
    leader = (
        "import subprocess,sys,threading;"
        f"subprocess.Popen([sys.executable,'-c',{escaped!r},sys.argv[1]],"
        "start_new_session=True);"
        "threading.Event().wait(30)"
    )
    command = shlex.join([sys.executable, "-c", leader, str(pid_path)])
    module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.05
    module.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.05
    module.PROCESS_PIPE_DRAIN_SECONDS = 0.2
    escaped_pid = None
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="output did not close after group cleanup"):
            module.run_shell_timed(
                command,
                tmp_path,
                log_path,
                0.3,
                "escaped stdout regression",
            )
        assert pid_path.exists(), "the escaped child must start before timeout"
        escaped_pid = int(pid_path.read_text())
        assert time.monotonic() - started < 1.5
    finally:
        if escaped_pid is None and pid_path.exists():
            escaped_pid = int(pid_path.read_text())
        if escaped_pid is not None:
            try:
                os.killpg(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_push_deadline_reconciles_remote_acceptance_without_rewind(tmp_path, capsys):
    repo = setup_repo(tmp_path, "push-deadline", with_origin=True)
    worktree = make_worktree(repo, "candidate")
    commit_in(worktree, "candidate.txt", "accepted\n", "publish candidate")
    origin = Path(run_git(repo, "remote", "get-url", "origin"))
    receive = tmp_path / "receive-and-hold"
    accepted = tmp_path / "remote-accepted"
    receive.write_text(
        "#!/bin/sh\n"
        "git-receive-pack \"$1\"\n"
        "rc=$?\n"
        "[ \"$rc\" -eq 0 ] || exit \"$rc\"\n"
        f"touch {str(accepted)!r}\n"
        "exec python3 -c 'import threading; threading.Event().wait(30)'\n"
    )
    receive.chmod(0o755)
    run_git(repo, "config", "remote.origin.receivepack", str(receive))

    module = load_greenline_module()
    module.RELEASE_HARD_TIMEOUT_SECONDS = 30.0
    module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
    module.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
    module.PROCESS_PIPE_DRAIN_SECONDS = 0.5
    rendezvous_error = []

    def expire_after_remote_acceptance():
        wait_until = time.monotonic() + 20
        while not accepted.exists() and time.monotonic() < wait_until:
            threading.Event().wait(0.01)
        if not accepted.exists():
            rendezvous_error.append("remote acceptance marker was not written")
            return
        deadline = module.ACTIVE_RELEASE_DEADLINE
        if deadline is None:
            rendezvous_error.append("release deadline was not active at remote acceptance")
            return
        deadline.hard_seconds = time.monotonic() - deadline.started + 0.2

    controller = threading.Thread(target=expire_after_remote_acceptance)
    controller.start()
    result = module.main(["submit", "--repo", str(worktree)])
    controller.join(timeout=1)
    output = capsys.readouterr().out

    candidate = sha(repo, "main")
    assert not rendezvous_error
    assert not controller.is_alive()
    assert result == 1, "the release exceeded its authoritative hard deadline"
    assert run_git(origin, "rev-parse", "refs/heads/main") == candidate
    assert sha(repo, "refs/greenline/last-green") == candidate
    assert (common_dir(repo) / "greenline" / "deployed").read_text().strip() == candidate
    assert "without rewinding origin" in output
    terminal = journal_events(repo)[-1]
    assert terminal["event"] == "fail"
    assert terminal["reconciliation"] == "candidate_published"


def test_rollback_submit_restores_last_green_without_origin(tmp_path):
    repo_path = setup_repo(tmp_path, "rollback-last-green")
    module = load_greenline_module()
    repo = module.load_repo(repo_path)
    known_good = sha(repo_path, "main")
    worktree = make_worktree(repo_path, "rollback-candidate")
    commit_in(worktree, "candidate.txt", "candidate\n", "rollback candidate")
    candidate = sha(worktree, "HEAD")
    module.set_last_green(repo, candidate)
    module.write_deployed(repo, candidate)

    module.rollback_submit(repo, known_good, repo.logs_dir / "rollback.log")

    assert sha(repo_path, "main") == known_good
    assert sha(repo_path, "refs/greenline/last-green") == known_good
    assert (common_dir(repo_path) / "greenline" / "deployed").read_text().strip() == known_good
