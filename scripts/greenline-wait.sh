#!/usr/bin/env bash
set -euo pipefail
exec python3 - "$0" "$@" <<'PY'
import argparse
import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

HARD_SECONDS = 600.0
TERMINAL_FAILURES = {
    "fail", "error", "deploy_failed", "rollback_failed", "adopt_failed", "abandoned",
    "needs_fix_forward", "recovered_rollback",
}


def git(repo, *args):
    cmd = ["git", "-C", str(repo), *args]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=max(0.001, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=1)
        raise SystemExit(f"greenline-wait: release deadline exceeded during {' '.join(cmd)}")
    if proc.returncode != 0:
        raise SystemExit(f"greenline-wait: git failed rc={proc.returncode}: {stderr.strip()}")
    return stdout.strip()


def read_entries(path):
    entries = []
    for line in path.read_text().splitlines():
        if line.strip():
            entries.append(json.loads(line))
    return entries


def cancel_owned_cli(proc):
    if proc.poll() is not None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


parser = argparse.ArgumentParser(prog="greenline-wait.sh")
script_path = Path(sys.argv.pop(1)).resolve()
parser.add_argument("branch", nargs="?")
parser.add_argument("--repo", default=os.getcwd())
parser.add_argument("--attach", action="store_true")
args = parser.parse_args()

deadline = time.monotonic() + HARD_SECONDS
repo = Path(git(args.repo, "rev-parse", "--show-toplevel")).resolve()
common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
journal = common / "greenline" / "journal.jsonl"
journal.parent.mkdir(parents=True, exist_ok=True)
journal.touch(exist_ok=True)
branch = args.branch or git(args.repo, "rev-parse", "--abbrev-ref", "HEAD")
if branch in ("HEAD", "main"):
    parser.error("an exact non-main branch is required")
source = git(repo, "rev-parse", f"refs/heads/{branch}")
existing = read_entries(journal)
baseline = len(existing)
attached_start = None
if args.attach:
    matches = [
        index for index, event in enumerate(existing)
        if event.get("event") == "start"
        and event.get("branch") == branch
        and event.get("candidate_src_sha") == source
    ]
    if not matches:
        raise SystemExit(
            f"greenline-wait: no release start for branch={branch} source={source}"
        )
    baseline = matches[-1]
    attached_start = existing[baseline]
    try:
        started_at = datetime.fromisoformat(attached_start["ts"])
        if started_at.tzinfo is None:
            raise ValueError("timestamp has no timezone")
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"greenline-wait: release start has invalid timestamp: {exc}")
    elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
    deadline = time.monotonic() + max(0.0, HARD_SECONDS - elapsed)
proc = None
log_path = None

if not args.attach:
    greenline = script_path.parent.parent / "greenline"
    if not greenline.exists():
        raise SystemExit(f"greenline-wait: paired executable does not exist: {greenline}")
    log_file = tempfile.NamedTemporaryFile(prefix="greenline-wait-", suffix=".log", delete=False)
    log_path = Path(log_file.name)
    proc = subprocess.Popen(
        [sys.executable, str(greenline), "submit", branch, "--repo", str(repo)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_file.close()
    print(f"submitted branch={branch} source={source[:12]} pid={proc.pid}")

fd = os.open(journal, os.O_RDONLY)
kqueue = select.kqueue()
change = select.kevent(
    fd,
    filter=select.KQ_FILTER_VNODE,
    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
    fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_EXTEND | select.KQ_NOTE_RENAME,
)
kqueue.control([change], 0, 0)
started = False
in_own_span = False
candidate = None
exit_seen = None
complete_candidate = None
succeeded = False
cancellation_requested = False

try:
    while True:
        entries = read_entries(journal)[baseline:]
        for event in entries:
            kind = event.get("event")
            if kind == "start":
                if event.get("branch") == branch and event.get("candidate_src_sha") != source:
                    raise SystemExit(
                        "GREENLINE FAIL: branch moved before admission; "
                        f"expected {source}, got {event.get('candidate_src_sha')}"
                    )
                is_own_start = (
                    event.get("branch") == branch
                    and event.get("candidate_src_sha") == source
                )
                if not is_own_start:
                    if started:
                        in_own_span = False
                    continue
                started = True
                in_own_span = True
                candidate = event.get("candidate")
                continue

            if not in_own_span:
                continue
            event_branch = event.get("branch")
            if event_branch not in (None, branch):
                continue
            event_candidate = event.get("candidate")
            if event_candidate is not None:
                if candidate is None:
                    candidate = event_candidate
                elif event_candidate != candidate:
                    continue
            if kind == "complete":
                if event_branch == branch and candidate is not None and event.get("merged") == candidate:
                    complete_candidate = candidate
                    in_own_span = False
                continue
            if kind in TERMINAL_FAILURES:
                print(f"GREENLINE FAIL branch={branch} event={json.dumps(event, separators=(',', ':'))}")
                raise SystemExit(1)
        baseline += len(entries)

        if proc is not None and proc.poll() is not None:
            if proc.returncode != 0:
                tail = "\n".join(log_path.read_text().splitlines()[-30:]) if log_path else ""
                print(f"GREENLINE FAIL branch={branch} process_exit={proc.returncode}\n{tail}")
                raise SystemExit(1)
            if complete_candidate is not None:
                if time.monotonic() >= deadline:
                    print(f"GREENLINE FAIL branch={branch} release deadline exceeded")
                    raise SystemExit(2)
                succeeded = True
                print(f"GREENLINE PASS branch={branch} candidate={complete_candidate}")
                raise SystemExit(0)
            if exit_seen is None:
                exit_seen = time.monotonic()
            elif time.monotonic() - exit_seen >= 0.5:
                tail = "\n".join(log_path.read_text().splitlines()[-30:]) if log_path else ""
                print(f"GREENLINE FAIL branch={branch} process_exit={proc.returncode}\n{tail}")
                raise SystemExit(1)
        elif args.attach and complete_candidate is not None:
            if time.monotonic() >= deadline:
                print(f"GREENLINE FAIL branch={branch} release deadline exceeded")
                raise SystemExit(2)
            succeeded = True
            print(f"GREENLINE PASS branch={branch} candidate={complete_candidate}")
            raise SystemExit(0)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if proc is not None:
                cancel_owned_cli(proc)
                cancellation_requested = True
            tail = "\n".join(log_path.read_text().splitlines()[-30:]) if log_path else ""
            print(f"GREENLINE FAIL branch={branch} release deadline exceeded\n{tail}")
            raise SystemExit(2)
        kqueue.control(None, 1, min(0.2, remaining))
finally:
    if proc is not None and not succeeded and not cancellation_requested:
        cancel_owned_cli(proc)
    kqueue.close()
    os.close(fd)
PY
