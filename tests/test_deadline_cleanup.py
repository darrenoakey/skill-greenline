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
    expire_deadline_when,
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
    # Generous outer bound: the rendezvous expires the deadline once the nested
    # grandchild has provably spawned (its pid file exists), so the kill path is
    # exercised at the right moment on a quiet or a loaded machine alike.
    module.ACTIVE_RELEASE_DEADLINE = module.ReleaseDeadline(
        time.monotonic(), 300.0, module.RELEASE_TIMEOUT_REASON
    )
    started = time.monotonic()
    try:
        with expire_deadline_when(module, pid_path):
            with pytest.raises(module.ReleaseDeadlineExceeded):
                module.run_argv(
                    [sys.executable, "-c", leader, str(pid_path)], "regression"
                )
    finally:
        module.ACTIVE_RELEASE_DEADLINE = None

    elapsed = time.monotonic() - started
    assert pid_path.exists(), "the real descendant must start before expiry"
    descendant_pid = int(pid_path.read_text())
    # Bounded = rendezvous grace + term/kill/drain, not the descendant's 30s wait.
    assert elapsed < 25, "pipe drain and group cleanup must remain bounded"
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
    escaped_pid = None
    started = time.monotonic()
    try:
        # run_shell_timed fixes its budget when it is called, so — unlike the
        # run_argv tests — this one cannot rendezvous on the pid file. The kill
        # must still land after both nested interpreters have spawned and while
        # their 30s waits run: a sub-second cap fires before the escaped child
        # exists on a loaded machine, the pipe closes cleanly, and the escape
        # regression stops being exercised. 8s covers interpreter startup under
        # load and is nowhere near the 30s natural exit.
        with pytest.raises(RuntimeError, match="output did not close after group cleanup"):
            module.run_shell_timed(
                command,
                tmp_path,
                log_path,
                8.0,
                "escaped stdout regression",
            )
        assert pid_path.exists(), "the escaped child must start before timeout"
        escaped_pid = int(pid_path.read_text())
        # Bounded = cap + term/kill/drain grace, not the child's 30s wait.
        assert time.monotonic() - started < 20
    finally:
        if escaped_pid is None and pid_path.exists():
            escaped_pid = int(pid_path.read_text())
        if escaped_pid is not None:
            try:
                os.killpg(escaped_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_running_stage_honours_a_release_deadline_that_shrinks_mid_stage(tmp_path):
    """The aggregate clock is authoritative for the whole stage, not just its start.

    A stage that sampled the deadline once when it began would keep running long
    after the release was out of time, and the release would then die at the next
    incidental git call instead of at the stage actually overrunning.
    """
    module = load_greenline_module()
    marker = tmp_path / "stage-running.marker"
    log_path = tmp_path / "stage.log"
    command = f"touch {shlex.quote(str(marker))}; sleep 60"
    module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
    module.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
    module.PROCESS_PIPE_DRAIN_SECONDS = 0.5
    module.ACTIVE_RELEASE_DEADLINE = module.ReleaseDeadline(
        time.monotonic(), 300.0, module.RELEASE_TIMEOUT_REASON
    )
    started = time.monotonic()
    try:
        with expire_deadline_when(module, marker):
            outcome = module.run_shell_timed(
                command, tmp_path, log_path, 600.0, "shrinking stage"
            )
    finally:
        module.ACTIVE_RELEASE_DEADLINE = None

    assert outcome.timed_out
    assert outcome.timeout_reason == module.RELEASE_TIMEOUT_REASON
    assert time.monotonic() - started < 30, "the stage must die on the release clock"
    assert "timeout after" in log_path.read_text()


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
    # Generous outer bound only: the controller thread expires the deadline
    # 0.2s after the remote actually accepts the push, so this value just
    # needs to survive a heavily loaded machine's journey to publish.
    module.RELEASE_HARD_TIMEOUT_SECONDS = 300.0
    module.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
    module.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
    module.PROCESS_PIPE_DRAIN_SECONDS = 0.5
    rendezvous_error = []

    def expire_after_remote_acceptance():
        # Wait for the push to actually reach the remote. Under heavy gate load
        # the whole submit up to publish can take tens of seconds, so a 20s
        # rendezvous false-fails; this is a pure event wait, not a semantic
        # bound.
        wait_until = time.monotonic() + 120
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
    # The thread only needs one scheduler quantum after main() returns; under
    # heavy gate load a 1s join false-fails. Pure event wait, not a bound.
    controller.join(timeout=120)
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
