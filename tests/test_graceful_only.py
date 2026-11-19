"""Real Git and process regressions for the explicit graceful release route."""

import fcntl
import hashlib
import json
import textwrap
import time
from pathlib import Path

import pytest

from test_greenline import (
    common_dir,
    expire_deadline_when,
    gl,
    journal_events,
    load_greenline_module,
    make_worktree,
    record_lines,
    run_git,
    setup_repo,
    sha,
    with_main_unlocked,
)

DRIVER = "tools/graceful-release"


def commit_driver(repo, body):
    path = repo / DRIVER
    path.parent.mkdir(exist_ok=True)
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(0o755)
    run_git(repo, "add", DRIVER)
    run_git(repo, "commit", "-qm", "Set graceful driver")


def assert_no_legacy(repo):
    # ./run logs every phase; checking is allowed, deploy and health are not.
    assert all(line.startswith("check ") for line in record_lines(repo))


def assert_baseline(repo, root, baseline):
    assert sha(repo, "main") == baseline
    assert sha(repo, "refs/greenline/last-green") == baseline
    assert (root / "deployed").read_text().strip() == baseline


@pytest.mark.parametrize("forward_rc,rollback_rc", [(0, 0), (41, 0), (41, 42)])
def test_graceful_submit_uses_pinned_driver_and_state(tmp_path, forward_rc, rollback_rc):
    repo = setup_repo(tmp_path, "graceful-pin")
    root = common_dir(repo) / "greenline"
    with with_main_unlocked(repo):
        commit_driver(repo, "raise AssertionError('baseline driver must not be used')\n")
    baseline = sha(repo, "main")
    run_git(repo, "update-ref", "refs/greenline/last-green", baseline)
    (root / "deployed").write_text(baseline + "\n")
    wt = make_worktree(repo, "graceful")
    calls = tmp_path / "calls"
    permit_rollback = tmp_path / "permit-rollback"
    commit_driver(wt, f"""
        import hashlib, json, os, stat, subprocess, sys
        from pathlib import Path
        mode, target, state = sys.argv[1:]
        state = Path(state)
        assert stat.S_IMODE(state.stat().st_mode) == 0o700
        assert subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip() == target
        digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        with open({str(calls)!r}, 'a') as log:
            print(json.dumps([mode, target, str(state), os.getcwd(), digest]), file=log)
        if mode == 'deploy':
            (state / 'snapshot').write_text('prior bytes')
            sys.exit({forward_rc})
        assert (state / 'snapshot').read_text() == 'prior bytes'
        sys.exit(0 if Path({str(permit_rollback)!r}).exists() else {rollback_rc})
    """)
    source = sha(wt, "HEAD")
    proc = gl(wt, "submit", "--graceful-only", expect=int(bool(forward_rc)))
    entries = journal_events(repo)
    checked = next(entry["candidate"] for entry in entries if entry["event"] == "checked")
    deploy, *rollbacks = [json.loads(line) for line in calls.read_text().splitlines()]
    _, _, state, cwd, digest = deploy
    assert deploy[:2] == ["deploy", checked]
    assert Path(state).is_absolute() and Path(state).is_relative_to(root)
    assert Path(cwd) == repo.resolve()
    assert digest == hashlib.sha256((wt / DRIVER).read_bytes()).hexdigest()
    expected_rollback = ["rollback", baseline, state, cwd, digest]
    if not forward_rc:
        assert not rollbacks
        assert not Path(state).exists()
        assert_baseline(repo, root, checked)
        assert entries[-1]["event"] == "complete"
    else:
        assert rollbacks == [expected_rollback]
        if rollback_rc:
            assert "ROLLBACK FAILED" in proc.stdout
            assert Path(state).is_dir()
            assert entries[-1]["event"] == "rollback_failed"
            # Even a stale pending-deploy record must never select the old command.
            (root / "pending-deploy").write_text(checked + "\n")
            stale_routes = [
                (repo, ["deploy-pending"]), (repo, ["adopt"]),
                (wt, ["submit"]), (repo, ["doctor", "--fix"]), (repo, ["doctor", "--fix"]),
            ]
            for cwd_path, args in stale_routes:
                failed = gl(cwd_path, *args, expect=1)
                assert "graceful rollback failed (exit 42)" in failed.stderr
                assert Path(state).is_dir()
                assert_no_legacy(repo)
            assert [json.loads(line) for line in calls.read_text().splitlines()] == (
                [deploy] + [expected_rollback] * (1 + len(stale_routes))
            )
            permit_rollback.touch()
            gl(repo, "doctor", "--fix", expect=0)
            assert json.loads(calls.read_text().splitlines()[-1]) == expected_rollback
            assert not (root / "pending-deploy").exists()
        else:
            assert entries[-1]["event"] == "deploy_failed"
        assert not Path(state).exists()
        assert_baseline(repo, root, baseline)
    assert sha(wt, "HEAD") == source
    assert_no_legacy(repo)


@pytest.mark.parametrize("mode", ["missing", "symlink", "nonexec"])
def test_graceful_only_rejects_unsafe_driver_before_mutation(tmp_path, mode):
    repo = setup_repo(tmp_path, "unsafe-" + mode)
    baseline = sha(repo, "main")
    wt = make_worktree(repo, "unsafe")
    path = wt / DRIVER
    path.parent.mkdir()
    if mode == "symlink":
        path.symlink_to("../run")
    elif mode == "nonexec":
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o644)
    (wt / "change.txt").write_text("candidate\n")
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-qm", "Candidate without a valid driver")
    proc = gl(wt, "submit", "--graceful-only", expect=2)
    assert "requires a committed executable regular file" in proc.stderr
    assert sha(repo, "main") == baseline
    assert sha(repo, "refs/greenline/last-green") == baseline
    assert not (common_dir(repo) / "greenline" / "deployed").exists()
    assert record_lines(repo) == []


@pytest.mark.parametrize("check_rc", [0, 7])
def test_check_must_pass_without_rewriting_the_driver(tmp_path, check_rc):
    repo = setup_repo(tmp_path, "graceful-check")
    baseline = sha(repo, "main")
    root = common_dir(repo) / "greenline"
    (root / "deployed").write_text(baseline + "\n")
    wt = make_worktree(repo, "graceful")
    called = tmp_path / "driver-called"
    commit_driver(wt, f"from pathlib import Path\nPath({str(called)!r}).touch()\n")
    run = wt / "run"
    run.write_text(run.read_text().replace(
        '    flag="$FLAGDIR/FAIL_CHECK"',
        f'    printf "\\n# check mutation\\n" >> {DRIVER}\n    exit {check_rc}',
    ))
    run_git(wt, "add", "run")
    run_git(wt, "commit", "-qm", "Make check rewrite the worktree driver")
    proc = gl(wt, "submit", "--graceful-only", expect=1 if check_rc else 2)
    assert_baseline(repo, root, baseline)
    assert not called.exists()
    assert_no_legacy(repo)
    assert not list(root.glob("graceful-*"))
    if check_rc:
        assert "CHECK FAILED" in proc.stdout
    else:
        assert "does not match its committed executable" in proc.stderr


def test_coordinator_timeout_leaves_driver_unsignalled_and_default_retry_never_deploys(tmp_path):
    repo = setup_repo(tmp_path, "graceful-timeout", with_origin=True)
    baseline = sha(repo, "main")
    origin = Path(run_git(repo, "remote", "get-url", "origin"))
    run_git(origin, "fetch", str(repo), "refs/heads/main:refs/heads/main")
    run_git(repo, "fetch", "origin", "main")
    root = common_dir(repo) / "greenline"
    (root / "deployed").write_text(baseline + "\n")
    wt = make_worktree(repo, "graceful")
    module = load_greenline_module()
    started = tmp_path / "started"
    resume = tmp_path / "resume"
    exited = tmp_path / "exited"
    signalled = tmp_path / "signalled"
    calls = tmp_path / "calls"
    commit_driver(wt, f"""
        import json, os, signal, sys, time
        from pathlib import Path
        mode, target, state = sys.argv[1:]
        with open({str(calls)!r}, 'a') as log:
            print(json.dumps([mode, target, state, os.getcwd()]), file=log)
        snapshot = Path(state) / 'snapshot'
        if mode == 'rollback':
            assert snapshot.read_text() == 'prior bytes'
            sys.exit(0)
        def term(sig, frame):
            Path({str(signalled)!r}).touch()
        signal.signal(signal.SIGTERM, term)
        snapshot.write_text('prior bytes')
        Path({str(started)!r}).touch()
        deadline = time.monotonic() + 120
        while not Path({str(resume)!r}).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        Path({str(exited)!r}).touch()
        sys.exit(41)
    """)
    try:
        with expire_deadline_when(module, started, wait=60):
            assert module.main(["submit", "gl/graceful", "--repo", str(repo), "--graceful-only"]) == 1
        deploy, = [json.loads(line) for line in calls.read_text().splitlines()]
        mode, candidate, state, cwd = deploy
        assert mode == "deploy"
        assert sha(repo, "main") == candidate != baseline
        assert sha(repo, "origin/main") == baseline
        assert (Path(state) / "snapshot").exists()
        assert not exited.exists() and not signalled.exists()
        for cwd_path, args in [
            (repo, ["deploy-pending"]), (repo, ["adopt"]), (wt, ["submit"]), (repo, ["doctor", "--fix"]),
        ]:
            proc = gl(cwd_path, *args, expect=1)
            assert "previous graceful driver is still running" in proc.stdout + proc.stderr
            assert len(calls.read_text().splitlines()) == 1
        assert_no_legacy(repo)
    finally:
        resume.touch()
    # The real driver owns the lease after the coordinator exits; wait for release.
    with (Path(state).parent / "lease").open("r+") as lease:
        limit = time.monotonic() + 20
        while True:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                assert time.monotonic() < limit, "graceful driver did not exit"
                time.sleep(0.02)
    assert exited.exists() and not signalled.exists()
    proc = gl(wt, "submit", expect=2)
    assert "no legacy release started" in proc.stdout
    assert [json.loads(line) for line in calls.read_text().splitlines()] == [
        deploy, ["rollback", baseline, state, cwd]
    ]
    assert not Path(state).exists()
    assert_baseline(repo, root, baseline)
    assert_no_legacy(repo)
