"""Real end-to-end tests for greenline against scratch git repos in tmp dirs.

No mocks. Each test builds an actual git repo with a real (tiny shell) ./run
script, points greenline's worktree_base into the tmp dir, and drives the CLI as
a subprocess (the way a user/agent would) so the flock serialization and hooks
are exercised for real.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from contextlib import contextmanager


GREENLINE = str(Path(__file__).resolve().parent.parent / "greenline")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def run_git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def gl(cwd, *args, expect=None, env=None):
    """Invoke the greenline CLI as a subprocess."""
    proc = subprocess.run(
        [sys.executable, GREENLINE, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )
    if expect is not None:
        assert proc.returncode == expect, (
            f"greenline {args} -> {proc.returncode} (want {expect})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def write_run_script(repo: Path, body: str):
    """Install a ./run script. `body` is a shell case-dispatch on $1."""
    script = repo / "run"
    script.write_text("#!/usr/bin/env bash\nset -e\n" + body + "\n")
    script.chmod(0o755)


def make_repo(
    tmp_path: Path, name: str, run_body: str, with_origin: bool = False
) -> Path:
    """Create a scratch git repo with a ./run script and one initial commit."""
    repo = tmp_path / name
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "main")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    write_run_script(repo, run_body)
    (repo / "app.txt").write_text("v0\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")
    if with_origin:
        origin = tmp_path / (name + ".git")
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        run_git(repo, "remote", "add", "origin", str(origin))
        run_git(repo, "push", "-q", "origin", "main")
    return repo


# A ./run that records every invocation (subcommand + HEAD sha + cwd) to a
# shared file, and fails a phase if a flag file exists. A flag file with EMPTY
# content fails every invocation of that phase; a flag file containing a SHA
# fails only when HEAD == that SHA (lets rollback/restore deploys succeed).
RUN_RECORDER = textwrap.dedent(
    """
    FLAGDIR="$(git rev-parse --git-common-dir)"
    REC="$FLAGDIR/gl-record.log"
    cmd="$1"
    HEAD_SHA="$(git rev-parse HEAD)"
    echo "$cmd $HEAD_SHA $(pwd)" >> "$REC"
    flag=""
    case "$cmd" in
      check)
        flag="$FLAGDIR/FAIL_CHECK"
        ;;
      deploy)
        flag="$FLAGDIR/FAIL_DEPLOY"
        ;;
    esac
    if [ -n "$flag" ] && [ -f "$flag" ]; then
      want="$(cat "$flag")"
      if [ -z "$want" ] || [ "$want" = "$HEAD_SHA" ]; then
        echo "$cmd failed on purpose" >&2
        exit 7
      fi
    fi
    exit 0
    """
)


# A ./run whose check hangs well past any sane cap. It spawns a grandchild and
# records its pid so a test can prove the whole process group was reaped.
SLOW_CHECK = textwrap.dedent(
    """
    FLAGDIR="$(git rev-parse --git-common-dir)"
    REC="$FLAGDIR/gl-record.log"
    echo "$1 $(git rev-parse HEAD) $(pwd)" >> "$REC"
    if [ "$1" = "check" ]; then
      sleep 120 &
      child=$!
      echo "$child" > "$FLAGDIR/slow-child.pid"
      wait "$child"
    fi
    exit 0
    """
)


def load_greenline_module():
    """Import the CLI script as a module (it has no .py suffix).

    Lets a test patch module constants and call main() in-process — the only
    way to exercise CHECK_TIMEOUT_SECONDS without waiting five real minutes,
    since the cap is deliberately not configurable from outside.
    """
    import importlib.machinery
    import importlib.util

    spec = importlib.util.spec_from_loader(
        "greenline_mod",
        importlib.machinery.SourceFileLoader("greenline_mod", GREENLINE),
    )
    mod = importlib.util.module_from_spec(spec)
    # dataclasses resolves annotations via sys.modules, so register before exec.
    sys.modules["greenline_mod"] = mod
    spec.loader.exec_module(mod)
    return mod


def pid_is_gone(pid: int, wait_s: float = 10.0) -> bool:
    deadline = time.time() + wait_s
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return True
        time.sleep(0.05)
    return False


def common_dir(repo: Path) -> Path:
    out = run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(out).resolve()


def record_lines(repo: Path):
    rec = common_dir(repo) / "gl-record.log"
    if not rec.exists():
        return []
    return [ln for ln in rec.read_text().splitlines() if ln.strip()]


def journal_events(repo: Path):
    j = common_dir(repo) / "greenline" / "journal.jsonl"
    if not j.exists():
        return []
    return [json.loads(ln) for ln in j.read_text().splitlines() if ln.strip()]


def sha(repo: Path, ref: str) -> str:
    return run_git(repo, "rev-parse", ref)


def seed_config(repo: Path, tmp_path: Path, name: str, coalesce: bool = False):
    """Pre-write greenline.toml with a tmp worktree_base so setup NEVER touches
    /Volumes. setup leaves an existing toml untouched."""
    wtbase = tmp_path / "wt" / name
    (repo / "greenline.toml").write_text(
        "contract_version = 1\n"
        'main_branch = "main"\n'
        'check = "./run check"\n'
        'deploy = "./run deploy"\n'
        'health = ""\n'
        f'service = "{name}"\n'
        f'worktree_base = "{wtbase}"\n'
        + (f"coalesce_deploys = {'true' if coalesce else 'false'}\n")
    )


@contextmanager
def with_main_unlocked(repo: Path):
    """Authorize local main-ref mutations the same way the gate does.

    Only for tests that deliberately plant out-of-gate main state
    (adopt/drift/recovery). Setup commits its own scaffolding, so no test
    needs this to bootstrap a repo. Production agents/humans never get this
    path.
    """
    allow = common_dir(repo) / "greenline" / "allow-main"
    allow.parent.mkdir(parents=True, exist_ok=True)
    allow.write_text(f"{os.getpid()}\n")
    try:
        yield
    finally:
        allow.unlink(missing_ok=True)


def setup_repo(tmp_path: Path, name="proj", with_origin=False, coalesce=False) -> Path:
    repo = make_repo(tmp_path, name, RUN_RECORDER, with_origin=with_origin)
    seed_config(repo, tmp_path, name, coalesce=coalesce)
    # setup honours the pre-seeded toml -> gate worktree lands in tmp.
    # It also commits its own scaffolding on main and moves last-green to it,
    # so worktrees branch off a base that carries greenline.toml.
    gl(repo, "setup", expect=0)
    assert not run_git(repo, "status", "--porcelain")
    assert sha(repo, "refs/greenline/last-green") == sha(repo, "main")
    return repo


def test_load_repo_uses_worktree_config_during_first_bootstrap(tmp_path):
    """A bootstrap branch can define the real default branch before merge."""
    repo = tmp_path / "bootstrap-master"
    repo.mkdir()
    run_git(repo, "init", "-q", "-b", "master")
    run_git(repo, "config", "user.email", "t@t.t")
    run_git(repo, "config", "user.name", "t")
    write_run_script(repo, RUN_RECORDER)
    (repo / "app.txt").write_text("v0\n")
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "-m", "initial")

    worktree = tmp_path / "bootstrap-worktree"
    run_git(repo, "worktree", "add", "-q", "-b", "bootstrap", str(worktree))
    gate_base = tmp_path / "bootstrap-gates"
    (worktree / "greenline.toml").write_text(
        "contract_version = 1\n"
        'main_branch = "master"\n'
        'check = "./run check"\n'
        'deploy = "./run deploy"\n'
        f'worktree_base = "{gate_base}"\n'
    )

    module = load_greenline_module()
    loaded = module.load_repo(worktree)

    assert loaded.canonical == repo.resolve()
    assert loaded.main_branch == "master"
    assert loaded.gate_path == gate_base / "gate"


def make_worktree(repo: Path, name: str) -> Path:
    proc = gl(repo, "worktree", name, expect=0)
    path = Path(proc.stdout.strip().splitlines()[-1])
    assert path.exists()
    return path


def commit_in(wt: Path, filename: str, content: str, msg: str):
    (wt / filename).write_text(content)
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", msg)


def test_submit_force_updates_persistent_gate_submodule_to_candidate_gitlink(tmp_path):
    module = make_repo(tmp_path, "module", "exit 0")
    old_module_sha = sha(module, "HEAD")

    repo = setup_repo(tmp_path, "submodules")
    run_git(repo, "config", "protocol.file.allow", "always")
    with with_main_unlocked(repo):
        run_git(
            repo,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "add",
            str(module),
            "module",
        )
        run_git(repo, "commit", "-q", "-m", "add module")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")

    gl(repo, "doctor", "--fix", expect=0)
    gate = tmp_path / "wt" / "submodules" / "gate"
    assert sha(gate / "module", "HEAD") == old_module_sha

    commit_in(module, "app.txt", "v1\n", "advance module")
    new_module_sha = sha(module, "HEAD")
    wt = make_worktree(repo, "advance-module")
    run_git(wt, "update-index", "--cacheinfo", f"160000,{new_module_sha},module")
    run_git(wt, "commit", "-q", "-m", "advance module gitlink")

    write_run_script(
        wt,
        textwrap.dedent(
            """
            expected="$(git rev-parse HEAD:module)"
            actual="$(git -C module rev-parse HEAD)"
            test "$actual" = "$expected"
            exit 0
            """
        ),
    )
    run_git(wt, "add", "run")
    run_git(wt, "commit", "-q", "-m", "verify checked out module")

    gl(repo, "submit", "gl/advance-module", expect=0)

    assert sha(gate / "module", "HEAD") == new_module_sha
    assert sha(repo / "module", "HEAD") == new_module_sha


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------
def test_setup_idempotent(tmp_path):
    repo = make_repo(tmp_path, "idem", RUN_RECORDER)
    seed_config(repo, tmp_path, "idem")
    gl(repo, "setup", expect=0)
    assert (repo / "greenline.toml").exists()
    assert (repo / "docs" / "DOCTRINE.md").exists()
    assert (repo / "docs" / "greenline.md").exists()
    assert (repo / "AGENTS.md").exists()
    for hook_name in ("pre-push", "reference-transaction", "pre-commit"):
        hook = common_dir(repo) / "hooks" / hook_name
        assert hook.exists(), hook_name
        # second run must not fail, duplicate the marked section, or commit again
    first_tip = sha(repo, "main")
    gl(repo, "setup", expect=0)
    agents_twice = (repo / "AGENTS.md").read_text()
    assert agents_twice.count(">>> greenline >>>") == 1
    for hook_name in ("pre-push", "reference-transaction", "pre-commit"):
        hook_text = (common_dir(repo) / "hooks" / hook_name).read_text()
        assert hook_text.count(">>> greenline >>>") == 1, hook_name
    assert sha(repo, "refs/greenline/last-green")
    assert sha(repo, "main") == first_tip, "idempotent setup must not commit again"
    assert not run_git(repo, "status", "--porcelain")


def test_setup_commits_its_own_scaffolding(tmp_path):
    """The hooks setup installs forbid commits on main — so setup must land
    its own greenline.toml/AGENTS.md/docs itself, under allow-main."""
    repo = make_repo(tmp_path, "bootstrap", RUN_RECORDER)
    seed_config(repo, tmp_path, "bootstrap")
    # unrelated dirt must survive setup untouched
    (repo / "unrelated.txt").write_text("mine\n")

    proc = gl(repo, "setup", expect=0)
    assert "git commit -m" not in proc.stdout, "obsolete manual-commit instructions"

    # scaffolding is committed; only the unrelated file is still dirty
    assert run_git(repo, "status", "--porcelain") == "?? unrelated.txt"
    assert (
        run_git(repo, "log", "-1", "--format=%s", "main") == "Add greenline gate config"
    )
    committed = run_git(repo, "show", "--name-only", "--format=", "main").split()
    assert sorted(committed) == sorted(
        ["AGENTS.md", "docs/DOCTRINE.md", "docs/greenline.md", "greenline.toml"]
    )
    # main == last-green, and the allow-main flag is gone
    assert sha(repo, "refs/greenline/last-green") == sha(repo, "main")
    assert not (common_dir(repo) / "greenline" / "allow-main").exists()

    # unrelated dirt is still reported by doctor exactly as before
    assert "[FAIL] canonical clean" in proc.stdout
    # with it gone, doctor is fully green after a bare setup
    (repo / "unrelated.txt").unlink()
    pd = gl(repo, "doctor", expect=0)
    assert "FAIL" not in pd.stdout

    # and the lock is still real: a direct commit on main is refused
    (repo / "after.txt").write_text("nope\n")
    run_git(repo, "add", "-A")
    blocked = subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", "direct"],
        capture_output=True,
        text=True,
    )
    assert blocked.returncode != 0
    assert "greenline" in (blocked.stdout + blocked.stderr).lower()
    run_git(repo, "restore", "--staged", "--worktree", ".")
    run_git(repo, "clean", "-fd")


def test_worktree_off_last_green(tmp_path):
    repo = setup_repo(tmp_path)
    lg = sha(repo, "refs/greenline/last-green")
    wt = make_worktree(repo, "feat")
    # worktree branch is gl/feat and its base is last-green
    assert run_git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "gl/feat"
    assert run_git(wt, "rev-parse", "HEAD") == lg
    # greenline.toml is present in the worktree (came from last-green commit)
    assert (wt / "greenline.toml").exists()
    # creating the same name again fails
    p = gl(repo, "worktree", "feat")
    assert p.returncode == 1


def test_worktree_base_expands_home_in_setup_and_worktree(tmp_path):
    repo = make_repo(tmp_path, "home-path", RUN_RECORDER)
    home = tmp_path / "home"
    home.mkdir()
    relative_base = Path("src") / ".greenline-worktrees" / "home-path"
    (repo / "greenline.toml").write_text(
        "contract_version = 1\n"
        'main_branch = "main"\n'
        'check = "./run check"\n'
        'deploy = "./run deploy"\n'
        'health = ""\n'
        'service = "home-path"\n'
        f'worktree_base = "~/{relative_base}"\n'
    )
    env = os.environ.copy()
    env["HOME"] = str(home)

    gl(repo, "setup", expect=0, env=env)
    expected_base = home / relative_base
    assert (expected_base / "gate").is_dir()

    proc = gl(repo, "worktree", "feature", expect=0, env=env)
    expected_worktree = expected_base / "feature"
    assert Path(proc.stdout.strip().splitlines()[-1]) == expected_worktree
    assert expected_worktree.is_dir()
    assert not (repo / "~").exists()


def test_submit_success_end_to_end(tmp_path):
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    wt = make_worktree(repo, "feat")
    commit_in(wt, "feature.txt", "hello\n", "add feature")
    p = gl(repo, "submit", "--repo", str(wt), expect=0)
    assert "GATE PASSED" in p.stdout
    C = sha(repo, "main")
    assert C != M
    # main advanced, last-green + deployed updated to C
    assert sha(repo, "refs/greenline/last-green") == C
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == C
    # check ran before deploy, in the right cwd order
    recs = record_lines(repo)
    kinds = [r.split()[0] for r in recs]
    assert "check" in kinds and "deploy" in kinds
    assert kinds.index("check") < kinds.index("deploy")
    # journal reached complete
    events = [e["event"] for e in journal_events(repo)]
    assert "start" in events and "checked" in events and "ffed" in events
    assert events[-1] == "complete"
    # canonical is clean and on main
    assert run_git(repo, "status", "--porcelain") == ""
    assert run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # the merged file actually landed on main
    assert (repo / "feature.txt").read_text() == "hello\n"


def test_check_failure_leaves_main_untouched(tmp_path):
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    wt = make_worktree(repo, "bad")
    commit_in(wt, "bad.txt", "x\n", "bad change")
    (common_dir(repo) / "FAIL_CHECK").write_text("")
    p = gl(repo, "submit", "--repo", str(wt))
    assert p.returncode == 1
    assert "CHECK FAILED" in p.stdout
    # main unchanged, branch still exists
    assert sha(repo, "main") == M
    assert run_git(repo, "rev-parse", "--verify", "refs/heads/gl/bad")
    # deploy never ran
    kinds = [r.split()[0] for r in record_lines(repo)]
    assert "deploy" not in kinds
    events = [e["event"] for e in journal_events(repo)]
    assert events[-1] == "fail"
    # gate worktree is reusable: a good submit afterwards works
    (common_dir(repo) / "FAIL_CHECK").unlink()
    wt2 = make_worktree(repo, "good")
    commit_in(wt2, "good.txt", "y\n", "good")
    gl(repo, "submit", "--repo", str(wt2), expect=0)
    assert sha(repo, "main") != M


def test_deploy_failure_rolls_back(tmp_path):
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    wt = make_worktree(repo, "deploybad")
    commit_in(wt, "d.txt", "z\n", "change")
    (common_dir(repo) / "FAIL_DEPLOY").write_text("")
    p = gl(repo, "submit", "--repo", str(wt))
    assert p.returncode == 1
    assert "DEPLOY FAILED" in p.stdout
    # main rolled back to M
    assert sha(repo, "main") == M
    # deploy was attempted twice: the failing candidate deploy, then the restore
    kinds = [r.split()[0] for r in record_lines(repo)]
    assert kinds.count("deploy") >= 2
    # deployed file restored to M
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == M
    events = [e["event"] for e in journal_events(repo)]
    assert events[-1] == "deploy_failed"
    # branch preserved
    assert run_git(repo, "rev-parse", "--verify", "refs/heads/gl/deploybad")


def test_conflict_candidate_fails_fast_and_gate_reusable(tmp_path):
    repo = setup_repo(tmp_path)
    # first: land a change to app.txt on main
    wt1 = make_worktree(repo, "first")
    commit_in(wt1, "app.txt", "mainline change\n", "edit app")
    gl(repo, "submit", "--repo", str(wt1), expect=0)
    M = sha(repo, "main")
    # second worktree branched from OLD last-green would conflict; simulate by
    # branching a worktree then editing the same file with a divergent base.
    wt2 = repo.parent / "wt" / "proj" / "conflict"
    # create branch off the OLD commit (before wt1 landed) to force a conflict
    old = run_git(wt1, "merge-base", "gl/first", "main")
    run_git(repo, "worktree", "add", "-b", "gl/conflict", str(wt2), old)
    commit_in(wt2, "app.txt", "conflicting change\n", "conflict edit")
    p = gl(repo, "submit", "--repo", str(wt2))
    assert p.returncode == 1
    assert "MERGE CONFLICT" in p.stdout
    assert "app.txt" in p.stdout
    # main untouched by the conflict
    assert sha(repo, "main") == M
    events = [e["event"] for e in journal_events(repo)]
    assert events[-1] == "fail"
    # gate worktree reusable: a fresh good submit works
    wt3 = make_worktree(repo, "after")
    commit_in(wt3, "after.txt", "ok\n", "ok")
    gl(repo, "submit", "--repo", str(wt3), expect=0)


def test_empty_candidate_refused(tmp_path):
    repo = setup_repo(tmp_path)
    wt = make_worktree(repo, "empty")
    # no commits vs last-green -> refused before lock work
    p = gl(repo, "submit", "--repo", str(wt))
    assert p.returncode == 1
    assert "empty candidate" in (p.stdout + p.stderr)


def test_serialization_second_blocks_until_first_done(tmp_path):
    repo = setup_repo(tmp_path)
    # slow check so the first holder keeps the lock for a beat
    slow_body = RUN_RECORDER.replace("check)\n", "check)\n        sleep 2\n", 1)
    write_run_script(repo, slow_body)
    with with_main_unlocked(repo):
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "slow run")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")

    wt1 = make_worktree(repo, "s1")
    commit_in(wt1, "s1.txt", "1\n", "s1")
    wt2 = make_worktree(repo, "s2")
    commit_in(wt2, "s2.txt", "2\n", "s2")

    env = os.environ.copy()
    p1 = subprocess.Popen(
        [sys.executable, GREENLINE, "submit", "--repo", str(wt1)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    time.sleep(0.6)  # let p1 grab the lock and enter slow check
    p2 = subprocess.Popen(
        [sys.executable, GREENLINE, "submit", "--repo", str(wt2)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    out1, _ = p1.communicate(timeout=120)
    out2, _ = p2.communicate(timeout=120)
    assert p1.returncode == 0, out1
    assert p2.returncode == 0, out2
    # p2 must have finished after p1 (it was serialized behind it)
    # both landed: main has both files
    assert (repo / "s1.txt").exists() and (repo / "s2.txt").exists()
    # exactly two successful completes recorded
    completes = [e for e in journal_events(repo) if e["event"] == "complete"]
    assert len(completes) == 2


def test_crash_recovery_ffed_but_not_deployed(tmp_path):
    """Simulate a crash after ff-only but before deploy terminal record.

    Construct: main advanced to C, deployed file still M, journal has start+
    checked+ffed with NO terminal event. doctor --fix must roll back to M and
    redeploy (deployed != C so it cannot be completed).
    """
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    wt = make_worktree(repo, "crash")
    commit_in(wt, "crash.txt", "c\n", "crash change")
    # build candidate C by squashing into main manually (mimic the gate through ff)
    C_branch = "gl/crash"
    with with_main_unlocked(repo):
        run_git(repo, "merge", "--squash", C_branch)
        run_git(repo, "commit", "-q", "-m", "crash: squashed via gate")
    C = sha(repo, "main")
    assert C != M
    # deployed still points at M (deploy never completed)
    (common_dir(repo) / "greenline" / "deployed").write_text(M + "\n")
    # write an incomplete journal: start, checked, ffed, no terminal
    jpath = common_dir(repo) / "greenline" / "journal.jsonl"
    with jpath.open("a") as fh:
        for ev in (
            {
                "event": "start",
                "branch": C_branch,
                "candidate_src_sha": C,
                "pre_main": M,
            },
            {"event": "checked", "candidate": C},
            {"event": "ffed", "candidate": C, "pre_main": M},
        ):
            ev["ts"] = "2026-01-01T00:00:00+00:00"
            fh.write(json.dumps(ev) + "\n")

    # doctor --fix runs the recovery deterministically
    gl(repo, "doctor", "--fix", expect=0)
    # main rolled back to M
    assert sha(repo, "main") == M
    # deployed restored/kept at M, redeploy ran at canonical
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == M
    events = [e["event"] for e in journal_events(repo)]
    assert "recovered_rollback" in events


def test_crash_recovery_ffed_and_deployed_completes(tmp_path):
    """Crash after ff AND deploy wrote deployed==C, healthy: recovery completes."""
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    wt = make_worktree(repo, "crash2")
    commit_in(wt, "crash2.txt", "c2\n", "crash2 change")
    with with_main_unlocked(repo):
        run_git(repo, "merge", "--squash", "gl/crash2")
        run_git(repo, "commit", "-q", "-m", "crash2: squashed")
    C = sha(repo, "main")
    (common_dir(repo) / "greenline" / "deployed").write_text(C + "\n")
    jpath = common_dir(repo) / "greenline" / "journal.jsonl"
    with jpath.open("a") as fh:
        for ev in (
            {
                "event": "start",
                "branch": "gl/crash2",
                "candidate_src_sha": C,
                "pre_main": M,
            },
            {"event": "checked", "candidate": C},
            {"event": "ffed", "candidate": C, "pre_main": M},
        ):
            ev["ts"] = "2026-01-01T00:00:00+00:00"
            fh.write(json.dumps(ev) + "\n")
    gl(repo, "doctor", "--fix", expect=0)
    # healthy candidate: main stays at C, last-green advances to C, complete
    assert sha(repo, "main") == C
    assert sha(repo, "refs/greenline/last-green") == C
    events = [e["event"] for e in journal_events(repo)]
    assert events[-1] == "complete"


def test_pre_push_hook_blocks_direct_and_gate_push_succeeds(tmp_path):
    repo = setup_repo(tmp_path, with_origin=True)
    # plant a local-only main tip under the gate's own allow mechanism, then
    # prove a direct push is still rejected by the pre-push hook.
    with with_main_unlocked(repo):
        (repo / "sneaky.txt").write_text("sneaky\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "sneaky direct")
    proc = subprocess.run(
        ["git", "-C", str(repo), "push", "origin", "main"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode != 0
    assert "greenline" in proc.stderr
    # undo that local commit so the repo is clean for the gate path
    with with_main_unlocked(repo):
        run_git(repo, "reset", "--hard", "HEAD~1")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")

    # a real gate submit publishes to origin (allow-push flag lets the hook pass)
    wt = make_worktree(repo, "viagate")
    commit_in(wt, "viagate.txt", "g\n", "via gate")
    gl(repo, "submit", "--repo", str(wt), expect=0)
    # origin/main advanced to the gated candidate
    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") == sha(repo, "main")
    # allow-push flag was cleaned up
    assert not (common_dir(repo) / "greenline" / "allow-push").exists()
    assert not (common_dir(repo) / "greenline" / "allow-main").exists()


def test_status_and_doctor_clean_on_healthy_repo(tmp_path):
    repo = setup_repo(tmp_path)
    # a successful submit first
    wt = make_worktree(repo, "h")
    commit_in(wt, "h.txt", "h\n", "h")
    gl(repo, "submit", "--repo", str(wt), expect=0)
    ps = gl(repo, "status", expect=0)
    assert "[OK]" in ps.stdout
    assert "lock     : free" in ps.stdout
    pd = gl(repo, "doctor", expect=0)
    assert "FAIL" not in pd.stdout


def test_done_removes_merged_worktree(tmp_path):
    repo = setup_repo(tmp_path)
    wt = make_worktree(repo, "toremove")
    commit_in(wt, "tr.txt", "tr\n", "tr")
    gl(repo, "submit", "--repo", str(wt), expect=0)
    # from the worktree, done should verify + remove it
    gl(wt, "done", expect=0)
    assert not wt.exists()
    # branch deleted
    r = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "refs/heads/gl/toremove"],
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0


def commit_direct_to_main(repo: Path, filename: str, msg: str) -> str:
    """A legacy-workflow/hotfix commit made straight to the canonical main.

    Bypasses the reference-transaction hard lock via with_main_unlocked —
    the same mechanism the gate uses. Production agents/humans do not get
    this path; tests use it only to exercise adopt and drift recovery.
    """
    with with_main_unlocked(repo):
        (repo / filename).write_text(msg + "\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", msg)
    return sha(repo, "main")


def test_main_ahead_of_last_green_is_never_reset(tmp_path):
    """Regression test for the data-loss bug: bare drift must never be discarded."""
    repo = setup_repo(tmp_path)
    wt = make_worktree(repo, "pending")
    commit_in(wt, "p.txt", "p\n", "pending change")
    direct = commit_direct_to_main(repo, "direct.txt", "direct-to-main")
    # submit must refuse with exit 2, point at adopt, and PRESERVE the commit
    p = gl(repo, "submit", "--repo", str(wt))
    assert p.returncode == 2, p.stdout + p.stderr
    assert "adopt" in (p.stdout + p.stderr)
    assert sha(repo, "main") == direct
    # doctor --fix must also refuse and preserve
    p2 = gl(repo, "doctor", "--fix")
    assert p2.returncode == 2, p2.stdout + p2.stderr
    assert sha(repo, "main") == direct
    # the commit's content is still there
    assert (repo / "direct.txt").exists()


def test_adopt_happy_path(tmp_path):
    repo = setup_repo(tmp_path)
    direct = commit_direct_to_main(repo, "direct.txt", "hotfix on main")
    p = gl(repo, "adopt", expect=0)
    assert "ADOPTED" in p.stdout
    # main untouched, last-green and deployed advanced to the tip
    assert sha(repo, "main") == direct
    assert sha(repo, "refs/greenline/last-green") == direct
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == direct
    # check ran (in the gate worktree at the tip) then deploy (at canonical, tip)
    recs = [r.split() for r in record_lines(repo)]
    kinds = [r[0] for r in recs]
    assert "check" in kinds and "deploy" in kinds
    assert kinds.index("check") < kinds.index("deploy")
    check_rec = recs[kinds.index("check")]
    deploy_rec = recs[kinds.index("deploy")]
    assert check_rec[1] == direct and deploy_rec[1] == direct
    # journal is a full start->complete with the adopt marker
    events = journal_events(repo)
    starts = [e for e in events if e["event"] == "start"]
    assert starts[-1].get("adopt") is True and starts[-1].get("branch") == "adopt"
    assert events[-1]["event"] == "complete" and events[-1].get("adopt") is True
    # doctor is clean afterwards
    gl(repo, "doctor", expect=0)


def test_adopt_bootstraps_a_never_deployed_repo(tmp_path):
    """After setup, main == last-green but nothing has ever been deployed and
    origin is behind. Direct pushes are hook-blocked, so adopt is the ONLY path
    to first deploy — it must not refuse with 'nothing to adopt'."""
    repo = setup_repo(tmp_path, "bootstrap-adopt", with_origin=True)
    tip = sha(repo, "main")
    deployed_path = common_dir(repo) / "greenline" / "deployed"
    assert not deployed_path.exists(), "precondition: nothing deployed yet"
    assert sha(repo, "refs/greenline/last-green") == tip
    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") != tip, "precondition: origin behind main"

    p = gl(repo, "adopt", expect=0)
    assert "ADOPTED" in p.stdout

    kinds = [r.split()[0] for r in record_lines(repo)]
    assert kinds == ["check", "deploy"]
    assert deployed_path.read_text().strip() == tip
    assert sha(repo, "main") == tip
    assert sha(repo, "refs/greenline/last-green") == tip
    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") == tip, "adopt must publish the bootstrap tip"
    assert journal_events(repo)[-1]["event"] == "complete"
    gl(repo, "doctor", expect=0)

    # now everything agrees -> a second adopt is a no-op refusal
    p2 = gl(repo, "adopt", expect=0)
    assert "nothing to adopt" in p2.stdout
    assert [r.split()[0] for r in record_lines(repo)] == ["check", "deploy"]


def test_adopt_bootstrap_deploy_failure_never_attempts_rollback(tmp_path):
    """No previously-deployed SHA exists, so there is nothing to roll back to:
    say prod is unknown rather than re-running the same failing deploy."""
    repo = setup_repo(tmp_path, "bootstrap-fail")
    tip = sha(repo, "main")
    (common_dir(repo) / "FAIL_DEPLOY").write_text("")  # empty = fail every deploy

    p = gl(repo, "adopt", expect=1)
    assert "PROD STATE IS UNKNOWN" in p.stdout
    assert "PROD IS INTENTIONALLY BEHIND MAIN" not in p.stdout

    # exactly ONE deploy attempt — no rollback deploy
    assert [r.split()[0] for r in record_lines(repo)] == ["check", "deploy"]
    assert not (common_dir(repo) / "greenline" / "deployed").exists()
    assert sha(repo, "main") == tip
    assert sha(repo, "refs/greenline/last-green") == tip
    last = journal_events(repo)[-1]
    assert last["event"] == "adopt_failed"
    assert last["stage"] == "deploy" and last.get("bootstrap") is True


def test_adopt_deploy_failure_restores_prod_never_resets_main(tmp_path):
    repo = setup_repo(tmp_path)
    last_green = sha(repo, "refs/greenline/last-green")
    direct = commit_direct_to_main(repo, "direct.txt", "bad hotfix")
    # deploy fails ONLY at the new tip; the restore deploy at last-green succeeds
    (common_dir(repo) / "FAIL_DEPLOY").write_text(direct)
    p = gl(repo, "adopt")
    assert p.returncode == 1, p.stdout + p.stderr
    # main is NEVER reset — the direct commit survives
    assert sha(repo, "main") == direct
    assert (repo / "direct.txt").exists()
    # deploy was invoked at the tip (failed), then re-invoked at last-green
    deploys = [r.split() for r in record_lines(repo) if r.startswith("deploy ")]
    deploy_shas = [d[1] for d in deploys]
    assert direct in deploy_shas and last_green in deploy_shas
    assert deploy_shas.index(direct) < deploy_shas.index(last_green)
    # prod restored: deployed file == last-green; canonical back on main
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == last_green
    assert run_git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    # last-green did NOT advance
    assert sha(repo, "refs/greenline/last-green") == last_green
    # journal terminal adopt_failed + loud banner
    events = journal_events(repo)
    assert events[-1]["event"] == "adopt_failed"
    assert "BEHIND MAIN" in p.stdout


def test_main_hard_lock_blocks_commit_and_no_verify(tmp_path):
    """reference-transaction refuses main updates even with --no-verify."""
    repo = setup_repo(tmp_path)
    M = sha(repo, "main")
    (repo / "blocked.txt").write_text("nope\n")
    run_git(repo, "add", "-A")

    # soft layer: plain commit on main is refused by pre-commit
    soft = subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "blocked"],
        capture_output=True,
        text=True,
    )
    assert soft.returncode != 0
    assert "greenline" in (soft.stdout + soft.stderr).lower()
    assert sha(repo, "main") == M

    # hard layer: --no-verify still cannot move main
    hard = subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", "blocked hard"],
        capture_output=True,
        text=True,
    )
    assert hard.returncode != 0
    assert "greenline" in (hard.stdout + hard.stderr).lower()
    assert sha(repo, "main") == M

    # stale allow-main (dead PID) must also refuse
    allow = common_dir(repo) / "greenline" / "allow-main"
    allow.write_text("1\n")  # PID 1 is almost never the test process; kill -0 may
    # succeed on PID 1 (launchd/init). Use a definitely-dead PID instead.
    allow.write_text("999999\n")
    stale = subprocess.run(
        ["git", "-C", str(repo), "commit", "--no-verify", "-m", "stale allow"],
        capture_output=True,
        text=True,
    )
    assert stale.returncode != 0
    assert "greenline" in (stale.stdout + stale.stderr).lower()
    assert sha(repo, "main") == M
    allow.unlink(missing_ok=True)

    # cleanup index/worktree without any ref update (even mixed reset
    # touches refs/heads/main and trips the hard lock).
    run_git(repo, "restore", "--staged", "--worktree", ".")
    run_git(repo, "clean", "-fd")


def test_worktree_commits_unaffected_by_main_lock(tmp_path):
    """Feature-branch commits in worktrees must keep working under the hard lock."""
    repo = setup_repo(tmp_path)
    wt = make_worktree(repo, "ok")
    commit_in(wt, "ok.txt", "y\n", "worktree commit ok")
    assert run_git(wt, "rev-parse", "--abbrev-ref", "HEAD") == "gl/ok"
    assert (wt / "ok.txt").read_text() == "y\n"
    # main untouched
    assert not (repo / "ok.txt").exists()


# --------------------------------------------------------------------------
# deploy coalescing
# --------------------------------------------------------------------------
# Every deploy restarts prod. In agentd3 that meant 15 restarts in one day and
# ~20% of all agent turns being interrupted mid-work. Coalescing collapses a
# burst of queued submissions into ONE deploy at the end of the burst: each
# candidate is still gated and merged individually, so nothing skips the check —
# only the restart is shared.


def deploy_shas(repo: Path):
    """The HEAD sha of every deploy invocation, in order."""
    return [ln.split()[1] for ln in record_lines(repo) if ln.startswith("deploy ")]


def pending_deploy(repo: Path) -> str | None:
    path = common_dir(repo) / "greenline" / "pending-deploy"
    return path.read_text().strip() if path.exists() else None


def submit_burst(repo: Path, worktrees, env=None):
    """Start submissions back-to-back so later ones queue behind the first."""
    env = env or os.environ.copy()
    procs = []
    for i, wt in enumerate(worktrees):
        procs.append(
            subprocess.Popen(
                [sys.executable, GREENLINE, "submit", "--repo", str(wt)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
        )
        # Let the first process take the lock before the rest start queueing,
        # so the queue is real rather than a race we hope lands the right way.
        time.sleep(1.2 if i == 0 else 0.3)
    return [(p, p.communicate(timeout=180)[0]) for p in procs]


def slow_check_repo(tmp_path, coalesce: bool) -> Path:
    repo = setup_repo(tmp_path, coalesce=coalesce)
    slow_body = RUN_RECORDER.replace("check)\n", "check)\n        sleep 3\n", 1)
    write_run_script(repo, slow_body)
    with with_main_unlocked(repo):
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "slow check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    return repo


def test_queued_submissions_coalesce_into_one_deploy(tmp_path):
    repo = slow_check_repo(tmp_path, coalesce=True)
    worktrees = []
    for name in ("c1", "c2", "c3"):
        wt = make_worktree(repo, name)
        commit_in(wt, f"{name}.txt", "x\n", name)
        worktrees.append(wt)

    results = submit_burst(repo, worktrees)
    for proc, out in results:
        assert proc.returncode == 0, out

    # Every candidate was gated and landed on main — coalescing must never skip
    # a check or drop a commit.
    for name in ("c1", "c2", "c3"):
        assert (repo / f"{name}.txt").exists(), f"{name} did not reach main"
    checks = [ln for ln in record_lines(repo) if ln.startswith("check ")]
    assert len(checks) == 3, f"every candidate must be checked, got {len(checks)}"

    # ...but prod was restarted once, not three times.
    deploys = deploy_shas(repo)
    assert len(deploys) == 1, f"burst must collapse to ONE deploy, got {len(deploys)}"
    assert deploys[0] == sha(repo, "main"), (
        "the single deploy must ship the final main tip"
    )

    deferred = [e for e in journal_events(repo) if e["event"] == "deploy_deferred"]
    assert len(deferred) == 2, f"two deploys should have been deferred, got {deferred}"
    assert pending_deploy(repo) is None, "pending record must be cleared once deployed"


def test_coalescing_is_opt_in(tmp_path):
    """Without the config flag, every submission deploys — the old contract."""
    repo = slow_check_repo(tmp_path, coalesce=False)
    worktrees = []
    for name in ("d1", "d2"):
        wt = make_worktree(repo, name)
        commit_in(wt, f"{name}.txt", "x\n", name)
        worktrees.append(wt)

    for proc, out in submit_burst(repo, worktrees):
        assert proc.returncode == 0, out

    assert len(deploy_shas(repo)) == 2, "coalescing must stay off unless enabled"
    assert not [e for e in journal_events(repo) if e["event"] == "deploy_deferred"]


def test_failed_last_submission_still_deploys_last_good_commit(tmp_path):
    """The burst's last member fails its check; the deferred deploy must still
    ship. Otherwise prod sits behind a green main with nobody left to deploy it —
    the one way coalescing could silently lose a release."""
    # The check fails on CONTENT, not on a timing window: any candidate that
    # introduces b1.txt is rejected. That makes "the last submission of the
    # burst fails" deterministic instead of a race we hope lands right.
    repo = setup_repo(tmp_path, coalesce=True)
    body = RUN_RECORDER.replace(
        "check)\n",
        "check)\n        sleep 3\n        [ -f b1.txt ] && { echo bad candidate >&2; exit 7; }\n",
        1,
    )
    write_run_script(repo, body)
    with with_main_unlocked(repo):
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "content-gated check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")

    good = make_worktree(repo, "g1")
    commit_in(good, "g1.txt", "x\n", "g1")
    bad = make_worktree(repo, "b1")
    commit_in(bad, "b1.txt", "x\n", "b1")

    results = submit_burst(repo, [good, bad])
    assert results[0][0].returncode == 0, results[0][1]
    assert results[1][0].returncode == 1, results[1][1]

    assert (repo / "g1.txt").exists(), "the good candidate must be on main"
    assert not (repo / "b1.txt").exists(), "the failed candidate must not be on main"

    deploys = deploy_shas(repo)
    assert deploys, "the deferred deploy must still ship after the burst fails"
    assert deploys[-1] == sha(repo, "main"), "prod must end at the last good main"
    assert pending_deploy(repo) is None, "nothing may be left pending"
    assert [e for e in journal_events(repo) if e["event"] == "pending_deployed"]


def test_deploy_pending_command_ships_a_stranded_deploy(tmp_path):
    """The escape hatch for the abnormal case: the process that would have
    deployed was killed, leaving a gated-but-undeployed main."""
    repo = setup_repo(tmp_path, coalesce=True)
    wt = make_worktree(repo, "p1")
    commit_in(wt, "p1.txt", "x\n", "p1")
    gl(wt, "submit", expect=0)
    before = len(deploy_shas(repo))

    # Simulate the stranded state the killed process would have left behind.
    (common_dir(repo) / "greenline" / "pending-deploy").write_text(
        sha(repo, "main") + "\n"
    )

    proc = gl(repo, "status", expect=0)
    assert "DEPLOY DEFERRED" in proc.stdout, proc.stdout
    # doctor must call it out: nothing is queued to finish the job.
    assert gl(repo, "doctor").returncode != 0

    gl(repo, "deploy-pending", expect=0)
    assert len(deploy_shas(repo)) == before + 1
    assert pending_deploy(repo) is None
    gl(repo, "doctor", expect=0)


def test_deploy_pending_publishes_what_it_deploys(tmp_path):
    """A submit killed between the ff-merge and the publish leaves main ahead of
    origin with nobody left to push it. deploy-pending is the one process that
    comes back for that main, so it must finish the whole job — ship it AND
    publish it. Shipping without publishing is worse than not shipping: prod
    runs code that origin and last-green have never heard of, so a rollback
    would silently target an older sha than the one actually running.
    """
    repo = setup_repo(tmp_path, with_origin=True, coalesce=True)
    wt = make_worktree(repo, "p1")
    commit_in(wt, "p1.txt", "x\n", "p1")
    gl(wt, "submit", expect=0)

    published = sha(repo, "main")
    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") == published

    # The killed submit: main advances, then the process dies before publish.
    with with_main_unlocked(repo):
        (repo / "p2.txt").write_text("y\n")
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "p2")
    stranded = sha(repo, "main")
    (common_dir(repo) / "greenline" / "pending-deploy").write_text(stranded + "\n")

    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") == published, "precondition: origin is behind"

    gl(repo, "deploy-pending", expect=0)

    assert deploy_shas(repo)[-1] == stranded, "prod must be at the stranded main"
    run_git(repo, "fetch", "-q", "origin")
    assert sha(repo, "origin/main") == stranded, (
        "deploy-pending must publish what it deployed"
    )
    assert sha(repo, "refs/greenline/last-green") == stranded, (
        "last-green must follow prod, or a rollback would restore an older sha "
        "than the one running"
    )
    gl(repo, "status", expect=0)


# --------------------------------------------------------------------------
# the five-minute check cap (doctrine)
# --------------------------------------------------------------------------
# The check pipeline must finish in under 5 minutes; longer is automatically a
# gate FAIL. There is no config override, so these tests patch the constant in
# an in-process import of the CLI rather than waiting for the real cap.


def test_check_timeout_default_is_five_minutes():
    mod = load_greenline_module()
    assert mod.CHECK_TIMEOUT_SECONDS == 300


def test_check_timeout_fails_the_gate_and_reaps_children(tmp_path, capsys):
    repo = setup_repo(tmp_path, "slowcheck")
    main_before = sha(repo, "main")
    lg_before = sha(repo, "refs/greenline/last-green")
    wt = make_worktree(repo, "slow")
    write_run_script(wt, SLOW_CHECK)
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "slow check")

    mod = load_greenline_module()
    mod.CHECK_TIMEOUT_SECONDS = 2
    rc = mod.main(["submit", "--repo", str(wt)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CHECK TIMED OUT" in out
    assert "5 minutes" in out and "DOCTRINE.md" in out

    # the check's grandchild (a bare `sleep`) must have been reaped with the group
    child = int((common_dir(repo) / "slow-child.pid").read_text().strip())
    assert pid_is_gone(child), f"child {child} survived the timeout kill"

    # gate FAIL semantics: nothing moved, nothing deployed
    assert sha(repo, "main") == main_before
    assert sha(repo, "refs/greenline/last-green") == lg_before
    assert [r.split()[0] for r in record_lines(repo)] == ["check"]
    assert not (common_dir(repo) / "greenline" / "deployed").exists()
    assert sha(repo, "gl/slow")  # branch preserved

    last = journal_events(repo)[-1]
    assert last["event"] == "fail" and last["stage"] == "check"
    assert last["reason"] == mod.CHECK_TIMEOUT_REASON


def test_adopt_check_timeout_is_a_failure_too(tmp_path, capsys):
    repo = setup_repo(tmp_path, "slowadopt")
    lg_before = sha(repo, "refs/greenline/last-green")
    with with_main_unlocked(repo):
        write_run_script(repo, SLOW_CHECK)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "slow check on main")
    tip = sha(repo, "main")

    mod = load_greenline_module()
    mod.CHECK_TIMEOUT_SECONDS = 2
    rc = mod.main(["adopt", "--repo", str(repo)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CHECK TIMED OUT" in out

    assert sha(repo, "main") == tip, "main must never be reset by adopt"
    assert sha(repo, "refs/greenline/last-green") == lg_before
    assert [r.split()[0] for r in record_lines(repo)] == ["check"]
    assert not (common_dir(repo) / "greenline" / "deployed").exists()
    last = journal_events(repo)[-1]
    assert last["event"] == "adopt_failed" and last["stage"] == "check"
    assert last["reason"] == mod.CHECK_TIMEOUT_REASON


# --------------------------------------------------------------------------
# portability
# --------------------------------------------------------------------------
def test_default_worktree_base_falls_back_off_the_authors_volume(tmp_path, monkeypatch):
    """greenline is published publicly; its default must not assume one machine's
    external drive exists."""
    mod = load_greenline_module()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(mod.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        mod.Path,
        "is_dir",
        lambda self: False if str(self) == "/Volumes/Gumby" else Path.is_dir(self),
    )

    base = mod.default_worktree_base("proj")
    assert "/Volumes/Gumby" not in base
    assert base == str(home / "greenline-worktrees" / "proj")
