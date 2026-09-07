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
import threading
import time
from pathlib import Path
from contextlib import contextmanager


GREENLINE = str(Path(__file__).resolve().parent.parent / "greenline")
GREENLINE_WAIT = str(
    Path(__file__).resolve().parent.parent / "scripts" / "greenline-wait.sh"
)


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


# A ./run whose DEPLOY hangs, but only away from a given tree. The deploy that
# rolls prod back runs at the pre-merge main, whose committed ./run is the fast
# recorder — so only the candidate's deploy hangs, and the rollback still works.
SLOW_DEPLOY = textwrap.dedent(
    """
    FLAGDIR="$(git rev-parse --git-common-dir)"
    REC="$FLAGDIR/gl-record.log"
    echo "$1 $(git rev-parse HEAD) $(pwd)" >> "$REC"
    if [ "$1" = "deploy" ]; then
      sleep 120 &
      child=$!
      echo "$child" > "$FLAGDIR/slow-child.pid"
      wait "$child"
    fi
    exit 0
    """
)


# A ./run whose HEALTH probe hangs, with the same reapable grandchild.
SLOW_HEALTH = textwrap.dedent(
    """
    FLAGDIR="$(git rev-parse --git-common-dir)"
    REC="$FLAGDIR/gl-record.log"
    echo "$1 $(git rev-parse HEAD) $(pwd)" >> "$REC"
    if [ "$1" = "health" ]; then
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
    way to exercise the fixed timing budgets without waiting real minutes,
    since they are deliberately not configurable from outside.
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


@contextmanager
def expire_deadline_when(mod, marker: Path, grace: float = 0.5, wait: float = 240.0):
    """Rendezvous a release deadline against real progress instead of the clock.

    A deadline test needs the deadline to expire *while a stage is running* —
    never before it starts. Encoding that as a small fixed budget makes the test
    a machine-speed measurement: on a loaded gate the pre-stage git work alone
    outruns it and the deadline lands in the wrong place. Instead arm a generous
    outer bound, wait for `marker` (written by the stage itself as proof it is
    running), then shorten the live deadline to expire `grace` later. The test
    stays fast on a quiet machine and correct on a loaded one.
    """
    errors: list[str] = []

    def rendezvous():
        limit = time.monotonic() + wait
        while not marker.exists() and time.monotonic() < limit:
            time.sleep(0.01)
        if not marker.exists():
            errors.append(f"stage marker {marker} never appeared")
            return
        deadline = mod.ACTIVE_RELEASE_DEADLINE
        if deadline is None:
            errors.append("release deadline was not active at the rendezvous")
            return
        deadline.hard_seconds = time.monotonic() - deadline.started + grace

    thread = threading.Thread(target=rendezvous)
    thread.start()
    try:
        yield errors
    finally:
        thread.join(timeout=wait + 30)
    assert not thread.is_alive(), "rendezvous thread never finished"
    assert not errors, errors


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


def install_agentd3_tripwire(tmp_path: Path, record: Path) -> Path:
    """Install an `agentd3` on PATH that records ANY invocation.

    A slow gate must instruct the agent that ran it, never create work
    somewhere else. This stub is the tripwire: if greenline ever shells out to
    agentd3 again (to enqueue a scheduled task, or anything else), the record
    file appears and the test fails.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    cli = bindir / "agentd3"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    cli.chmod(0o755)
    return bindir


def sha(repo: Path, ref: str) -> str:
    return run_git(repo, "rev-parse", ref)


def seed_config(
    repo: Path, tmp_path: Path, name: str, coalesce: bool = False, health: str = ""
):
    """Pre-write greenline.toml with a tmp worktree_base so setup NEVER touches
    /Volumes. setup leaves an existing toml untouched."""
    wtbase = tmp_path / "wt" / name
    (repo / "greenline.toml").write_text(
        "contract_version = 1\n"
        'main_branch = "main"\n'
        'check = "./run check"\n'
        'deploy = "./run deploy"\n'
        f'health = "{health}"\n'
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
    candidate_only_failure = RUN_RECORDER.replace(
        "deploy)\n",
        "deploy)\n        [ -f d.txt ] && { echo candidate deploy failed >&2; exit 7; }\n",
        1,
    )
    write_run_script(repo, candidate_only_failure)
    with with_main_unlocked(repo):
        run_git(repo, "add", "run")
        run_git(repo, "commit", "-q", "-m", "candidate-specific deploy check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    M = sha(repo, "main")
    wt = make_worktree(repo, "deploybad")
    commit_in(wt, "d.txt", "z\n", "change")
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


def test_waiter_ignores_peer_terminal_and_reports_own_exact_candidate(tmp_path):
    repo = setup_repo(tmp_path, "watcher")
    slow_body = RUN_RECORDER.replace("check)\n", "check)\n        sleep 0.6\n", 1)
    write_run_script(repo, slow_body)
    with with_main_unlocked(repo):
        run_git(repo, "add", "run")
        run_git(repo, "commit", "-q", "-m", "observable queued release")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    first = make_worktree(repo, "watch-first")
    commit_in(first, "first.txt", "1\n", "first watched release")
    second = make_worktree(repo, "watch-second")
    commit_in(second, "second.txt", "2\n", "second watched release")

    peer = subprocess.Popen(
        [sys.executable, GREENLINE, "submit", "--repo", str(first)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    time.sleep(0.2)
    watched = subprocess.run(
        [
            GREENLINE_WAIT,
            "--repo",
            str(second),
            "gl/watch-second",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    peer_out, _ = peer.communicate(timeout=120)
    assert peer.returncode == 0, peer_out
    assert watched.returncode == 0, watched.stdout + watched.stderr

    completes = [event for event in journal_events(repo) if event["event"] == "complete"]
    own = next(event for event in completes if event["branch"] == "gl/watch-second")
    peer_event = next(event for event in completes if event["branch"] == "gl/watch-first")
    assert f"candidate={own['merged']}" in watched.stdout
    assert peer_event["merged"] not in watched.stdout


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


def test_main_hard_lock_allows_pack_refs_noop_but_rejects_real_update(tmp_path):
    repo = setup_repo(tmp_path)
    main_before = sha(repo, "main")
    worktree = make_worktree(repo, "pack-refs-feature")
    commit_in(worktree, "feature.txt", "feature\n", "feature commit")
    feature_sha = sha(worktree, "HEAD")

    packed = subprocess.run(
        ["git", "-C", str(repo), "pack-refs", "--all", "--no-prune"],
        capture_output=True,
        text=True,
    )
    assert packed.returncode == 0, packed.stdout + packed.stderr
    assert sha(repo, "main") == main_before

    # Leave main represented only by packed-refs so update-ref -d attempts a
    # real logical deletion rather than removing a redundant loose copy.
    (common_dir(repo) / "refs" / "heads" / "main").unlink()
    assert sha(repo, "main") == main_before

    deleted = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "-d", "refs/heads/main"],
        capture_output=True,
        text=True,
    )
    assert deleted.returncode != 0
    assert "greenline" in (deleted.stdout + deleted.stderr).lower()
    assert sha(repo, "main") == main_before

    changed = subprocess.run(
        ["git", "-C", str(repo), "update-ref", "refs/heads/main", feature_sha],
        capture_output=True,
        text=True,
    )
    assert changed.returncode != 0
    assert "greenline" in (changed.stdout + changed.stderr).lower()
    assert sha(repo, "main") == main_before


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
# deploy attestation under queued submissions
# --------------------------------------------------------------------------
# A configured legacy coalesce_deploys key must not weaken the release contract:
# each successful submission checks, deploys, publishes, and records completion.


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
        time.sleep(0.2 if i == 0 else 0.1)
    return [(p, p.communicate(timeout=180)[0]) for p in procs]


def slow_check_repo(tmp_path, coalesce: bool) -> Path:
    repo = setup_repo(tmp_path, coalesce=coalesce)
    slow_body = RUN_RECORDER.replace("check)\n", "check)\n        sleep 0.6\n", 1)
    write_run_script(repo, slow_body)
    with with_main_unlocked(repo):
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "slow check")
    run_git(repo, "update-ref", "refs/greenline/last-green", "main")
    return repo


def test_legacy_coalesce_config_never_defers_a_successful_release(tmp_path):
    repo = slow_check_repo(tmp_path, coalesce=True)
    worktrees = []
    for name in ("c1", "c2"):
        wt = make_worktree(repo, name)
        commit_in(wt, f"{name}.txt", "x\n", name)
        worktrees.append(wt)

    results = submit_burst(repo, worktrees)
    for proc, out in results:
        assert proc.returncode == 0, out

    for name in ("c1", "c2"):
        assert (repo / f"{name}.txt").exists(), f"{name} did not reach main"
    checks = [ln for ln in record_lines(repo) if ln.startswith("check ")]
    assert len(checks) == 2, f"every candidate must be checked, got {len(checks)}"
    deploys = deploy_shas(repo)
    assert len(deploys) == 2, "every successful release must attest its own deploy"
    assert deploys[-1] == sha(repo, "main")
    assert not [e for e in journal_events(repo) if e["event"] == "deploy_deferred"]
    assert pending_deploy(repo) is None, "nothing may be left pending"


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
    assert "DEPLOY UNATTESTED" in proc.stdout, proc.stdout
    # doctor must call out the missing deploy attestation.
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
# check timing contract
# --------------------------------------------------------------------------
# Five minutes is a soft optimization budget and ten is the hard timeout. There
# is no user-facing override, so tests patch module constants in process.


def test_release_timing_defaults_are_three_minute_target_ten_minute_hard_limit():
    mod = load_greenline_module()
    assert mod.RELEASE_TARGET_SECONDS == 180
    assert mod.RELEASE_HARD_TIMEOUT_SECONDS == 600
    assert mod.RECOVERY_HARD_TIMEOUT_SECONDS == 600


def test_cumulative_release_budget_bounds_check_then_deploy_and_reaps_group(
    tmp_path,
):
    repo = setup_repo(tmp_path, "cumulative")
    body = SLOW_DEPLOY.replace(
        'if [ "$1" = "deploy" ]; then',
        'if [ "$1" = "check" ]; then\n'
        '  printf "started\\n" > "$FLAGDIR/check-started.fifo"\n'
        '  IFS= read -r _ < "$FLAGDIR/check-release.fifo"\n'
        "fi\n"
        'if [ "$1" = "deploy" ]; then',
    )
    write_run_script(repo, body)

    mod = load_greenline_module()
    loaded = mod.load_repo(repo)
    log_path = loaded.logs_dir / "cumulative-stage-budget.log"
    started_fifo = common_dir(repo) / "check-started.fifo"
    release_fifo = common_dir(repo) / "check-release.fifo"
    os.mkfifo(started_fifo)
    os.mkfifo(release_fifo)
    mod.DEPLOY_HARD_TIMEOUT_SECONDS = 30.0
    deadline_started = time.monotonic()
    # One monotonic clock spans check then deploy. Its size is a generous outer
    # bound: the test shortens it at real rendezvous points, so a loaded machine
    # cannot make the check run out of budget it was supposed to survive.
    mod.ACTIVE_RELEASE_DEADLINE = mod.ReleaseDeadline(
        deadline_started, 300.0, mod.RELEASE_TIMEOUT_REASON
    )
    try:
        result = {}

        def run_check_stage():
            result["check"] = mod.run_gate_check(loaded, repo, log_path)

        check_thread = threading.Thread(target=run_check_stage)
        check_thread.start()
        with started_fifo.open() as stream:
            assert stream.readline().strip() == "started"

        remaining_at_rendezvous = mod.ACTIVE_RELEASE_DEADLINE.remaining()
        with release_fifo.open("w") as stream:
            stream.write("continue\n")
        check_thread.join(timeout=120)

        assert not check_thread.is_alive()
        assert remaining_at_rendezvous > 0, "check must run inside the live deadline"
        check_outcome, _ = result["check"]
        assert check_outcome.returncode == 0
        assert not check_outcome.timed_out

        # Deploy inherits whatever the check left on the shared clock — that is
        # the cumulative property under test. Expire it once the deploy is
        # provably running (it has written its grandchild pid), so the kill
        # lands inside deploy rather than before it has even started work.
        deploy_started = time.monotonic()
        with expire_deadline_when(mod, common_dir(repo) / "slow-child.pid"):
            deploy_outcome = mod.run_gate_deploy(loaded, log_path)
        deploy_elapsed = time.monotonic() - deploy_started
    finally:
        mod.ACTIVE_RELEASE_DEADLINE = None

    assert deploy_outcome.timed_out
    assert deploy_outcome.timeout_reason == mod.RELEASE_TIMEOUT_REASON
    assert deploy_elapsed < mod.DEPLOY_HARD_TIMEOUT_SECONDS
    child = int((common_dir(repo) / "slow-child.pid").read_text().strip())
    assert pid_is_gone(child), f"child {child} survived release deadline cleanup"


def test_lock_admission_consumes_release_budget_and_fails_before_check(
    tmp_path, capsys
):
    repo = setup_repo(tmp_path, "lockbudget")
    wt = make_worktree(repo, "waiting")
    commit_in(wt, "waiting.txt", "x\n", "wait behind lock")
    lock_path = common_dir(repo) / "greenline" / "lock"
    # The holder never releases: the waiter must lose to the release deadline,
    # not to a lucky race against a sleeping holder.
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys,threading; f=open(sys.argv[1],'w'); "
            "fcntl.flock(f,fcntl.LOCK_EX); print('held',flush=True); "
            "threading.Event().wait(600)",
            str(lock_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert holder.stdout.readline().strip() == "held"
    try:
        mod = load_greenline_module()
        # Large enough that the pre-lock git work cannot consume it on a loaded
        # machine (which would expire the release before it ever queued), small
        # enough to keep the test quick. The lock is never released, so the
        # waiter can only exit by exhausting this budget.
        mod.RELEASE_HARD_TIMEOUT_SECONDS = 10
        started = time.monotonic()
        assert mod.main(["submit", "--repo", str(wt)]) == 1
        elapsed = time.monotonic() - started
    finally:
        holder.terminate()
        holder.wait(timeout=5)

    captured = capsys.readouterr()
    assert elapsed < 30, "the waiter must give up on its own deadline"
    assert "gate lock admission" in captured.err
    assert not record_lines(repo), "deadline-expired waiter must not start check or deploy"


def test_gate_lock_phase_start_is_scoped_to_each_acquisition(tmp_path):
    repo = setup_repo(tmp_path, "lockphasestart")
    mod = load_greenline_module()
    loaded = mod.load_repo(repo)
    stale_started = "2000-01-01T00:00:00+00:00"
    loaded.status_path.write_text(
        json.dumps({"pid": 91259, "started_utc": stale_started})
    )
    lock = mod.GateLock(loaded)

    first_acquisition = mod.datetime.now(mod.timezone.utc)
    with lock:
        lock.set_phase("check", "release-one")
        first_started = json.loads(loaded.status_path.read_text())["started_utc"]
        assert mod.datetime.fromisoformat(first_started) >= first_acquisition

        lock.set_phase("deploy", "release-one")
        assert json.loads(loaded.status_path.read_text())["started_utc"] == first_started

    second_acquisition = mod.datetime.now(mod.timezone.utc)
    with lock:
        lock.set_phase("check", "release-two")
        second_status = json.loads(loaded.status_path.read_text())

    assert second_status["pid"] == os.getpid()
    assert mod.datetime.fromisoformat(second_status["started_utc"]) >= second_acquisition
    assert second_status["started_utc"] != first_started


def test_check_within_soft_budget_says_nothing(tmp_path, capsys, monkeypatch):
    repo = setup_repo(tmp_path, "fastcheck")
    wt = make_worktree(repo, "fast")
    commit_in(wt, "fast.txt", "fast\n", "fast check")
    record = tmp_path / "agentd3.json"
    bindir = install_agentd3_tripwire(tmp_path, record)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 60
    assert mod.main(["submit", "--repo", str(wt)]) == 0
    out = capsys.readouterr().out

    assert not record.exists()
    assert "MANDATORY NEXT TASK" not in out
    assert not [e for e in journal_events(repo) if e["event"] == "gate_slow"]


def test_slow_release_directive_describes_detached_optimization():
    mod = load_greenline_module()
    directive = mod.slow_gate_directive(412.0)

    assert directive.startswith("NOTE: this release was green")
    assert "412.0s" in directive and "180s target" in directive
    assert "detached speed-up investigation" in directive
    assert "preserve validation" in directive


def test_slow_submit_launches_detached_speedup_after_completion_and_unlock(
    tmp_path, capsys, monkeypatch
):
    repo = setup_repo(tmp_path, "slowsuccess")
    wt = make_worktree(repo, "slow")
    commit_in(wt, "slow.txt", "slow\n", "slow successful check")
    record = tmp_path / "speedup-args.json"
    launcher = tmp_path / "speedup-launcher"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
    )
    launcher.chmod(0o755)

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 0
    mod.SPEEDUP_CLI = launcher
    assert mod.main(["submit", "--repo", str(wt)]) == 0
    out = capsys.readouterr().out

    assert record.exists(), "the detached speed-up launcher must be invoked"
    slow = [e for e in journal_events(repo) if e["event"] == "gate_slow"]
    assert len(slow) == 1
    assert slow[0]["candidate"] == sha(repo, "main")
    assert slow[0]["target_seconds"] == 0 and slow[0]["seconds"] > 0
    assert "RELEASE TARGET EXCEEDED" in out
    events = journal_events(repo)
    assert next(i for i, e in enumerate(events) if e["event"] == "complete") < next(
        i for i, e in enumerate(events) if e["event"] == "speedup_triggered"
    )
    status = gl(repo, "status", expect=0)
    assert "gate_slow" in status.stdout
    latest_log = sorted((common_dir(repo) / "greenline" / "logs").glob("*.log"))[-1]
    logged = latest_log.read_text()
    assert "RELEASE TARGET EXCEEDED" in logged and "detached speed-up" in logged


def test_slow_report_failure_keeps_submit_green(tmp_path, capsys, monkeypatch):
    """Optimization feedback is best-effort: the gate has already passed and
    published, so a broken report must never turn it red."""
    repo = setup_repo(tmp_path, "notifyfail")
    wt = make_worktree(repo, "slow")
    commit_in(wt, "slow.txt", "slow\n", "slow successful check")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 0

    def explode(*_args, **_kwargs):
        raise RuntimeError("journal is unwritable")

    monkeypatch.setattr(mod, "report_slow_gate", explode)
    assert mod.main(["submit", "--repo", str(wt)]) == 0
    captured = capsys.readouterr()

    assert sha(repo, "main") == sha(repo, "refs/greenline/last-green")
    assert "slow-gate feedback reporting failed" in captured.err


def test_check_timeout_fails_the_gate_and_reaps_children(
    tmp_path, capsys, monkeypatch
):
    repo = setup_repo(tmp_path, "slowcheck")
    main_before = sha(repo, "main")
    lg_before = sha(repo, "refs/greenline/last-green")
    wt = make_worktree(repo, "slow")
    write_run_script(wt, SLOW_CHECK)
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "slow check")
    notification = tmp_path / "agentd3.json"
    bindir = install_agentd3_tripwire(tmp_path, notification)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 0
    # Generous outer bound; the rendezvous expires the deadline once the check
    # is provably running (its grandchild pid file exists).
    mod.RELEASE_HARD_TIMEOUT_SECONDS = 300
    with expire_deadline_when(mod, common_dir(repo) / "slow-child.pid"):
        rc = mod.main(["submit", "--repo", str(wt)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "CHECK TIMED OUT" in out
    assert "600-second deadline" in out and "DOCTRINE.md" in out

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
    assert last["reason"] == mod.RELEASE_TIMEOUT_REASON
    assert not [e for e in journal_events(repo) if e["event"] == "gate_slow"]
    assert not notification.exists()


def test_near_limit_success_is_published_before_deadline(tmp_path, capsys):
    repo = setup_repo(tmp_path, "nearlimit")
    wt = make_worktree(repo, "near")
    body = RUN_RECORDER.replace("check)\n", "check)\n        sleep 0.8\n", 1)
    body = body.replace("deploy)\n", "deploy)\n        sleep 0.8\n", 1)
    write_run_script(wt, body)
    run_git(wt, "add", "run")
    run_git(wt, "commit", "-q", "-m", "near-limit successful release")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 60
    # Any small fixed budget false-fails on a heavily loaded gate machine (every
    # subprocess spawn costs many times its CPU time there; 4.5s and even 15s
    # were observed to flake). The timeout-must-fire boundary is covered by the
    # deadline tests below; this one proves the complementary path: with the
    # deadline armed, a success still checks, deploys, publishes, and journals
    # complete before expiry. 120s is far inside the real 600s deadline with
    # headroom for a loaded machine.
    mod.RELEASE_HARD_TIMEOUT_SECONDS = 120
    assert mod.main(["submit", "--repo", str(wt)]) == 0
    capsys.readouterr()

    complete = [e for e in journal_events(repo) if e["event"] == "complete"][-1]
    assert complete["merged"] == sha(repo, "main")
    assert complete["total_seconds"] < 120
    assert (common_dir(repo) / "greenline" / "deployed").read_text().strip() == complete["merged"]


def test_adopt_check_timeout_is_a_failure_too(tmp_path, capsys):
    repo = setup_repo(tmp_path, "slowadopt")
    lg_before = sha(repo, "refs/greenline/last-green")
    with with_main_unlocked(repo):
        write_run_script(repo, SLOW_CHECK)
        run_git(repo, "add", "-A")
        run_git(repo, "commit", "-q", "-m", "slow check on main")
    tip = sha(repo, "main")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 0
    # Same rendezvous as the submit check-timeout test: expire once the check
    # is provably running rather than guessing how long adopt's setup takes.
    mod.RELEASE_HARD_TIMEOUT_SECONDS = 300
    with expire_deadline_when(mod, common_dir(repo) / "slow-child.pid"):
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
    assert last["reason"] == mod.RELEASE_TIMEOUT_REASON


def test_slow_adopt_orders_the_adopting_agent(tmp_path, capsys, monkeypatch):
    repo = setup_repo(tmp_path, "slowadoptsuccess")
    with with_main_unlocked(repo):
        (repo / "adopt.txt").write_text("adopt\n")
        run_git(repo, "add", "adopt.txt")
        run_git(repo, "commit", "-q", "-m", "out of gate change")
    tip = sha(repo, "main")
    record = tmp_path / "agentd3.json"
    bindir = install_agentd3_tripwire(tmp_path, record)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")

    mod = load_greenline_module()
    mod.RELEASE_TARGET_SECONDS = 0
    launcher = tmp_path / "speedup-launcher"
    launcher.write_text("#!/usr/bin/env sh\nexit 0\n")
    launcher.chmod(0o755)
    mod.SPEEDUP_CLI = launcher
    assert mod.main(["adopt", "--repo", str(repo)]) == 0
    out = capsys.readouterr().out

    assert not record.exists()
    assert "RELEASE TARGET EXCEEDED" in out
    slow = [e for e in journal_events(repo) if e["event"] == "gate_slow"]
    assert len(slow) == 1 and slow[0]["branch"] == "adopt"
    assert slow[0]["candidate"] == tip


# --------------------------------------------------------------------------
# deploy / health timing contract
# --------------------------------------------------------------------------
# There is exactly one serialized gate, so a deploy or health command that never
# returns blocks every agent on the machine. Both caps are hard and have no
# user-facing override, so tests patch the module constants in process.


def test_deploy_and_health_timeouts_are_three_minutes_and_five_seconds():
    mod = load_greenline_module()
    assert mod.DEPLOY_HARD_TIMEOUT_SECONDS == 180
    assert mod.HEALTH_HARD_TIMEOUT_SECONDS == 5


def test_deploy_timeout_rolls_back_like_a_failed_deploy_and_reaps_children(
    tmp_path, capsys
):
    repo = setup_repo(tmp_path, "slowdeploy")
    main_before = sha(repo, "main")
    wt = make_worktree(repo, "slow")
    write_run_script(wt, SLOW_DEPLOY)
    run_git(wt, "add", "-A")
    run_git(wt, "commit", "-q", "-m", "slow deploy")

    mod = load_greenline_module()
    # Generous outer bound; the rendezvous expires the deadline once the deploy
    # is provably running (it has written its grandchild pid). Guessing instead
    # how long check plus the surrounding git work takes just measures the
    # machine, and lands the expiry in the wrong stage when it is loaded.
    mod.RELEASE_HARD_TIMEOUT_SECONDS = 300
    mod.PROCESS_GROUP_TERM_GRACE_SECONDS = 0.2
    mod.PROCESS_GROUP_KILL_GRACE_SECONDS = 0.5
    mod.PROCESS_PIPE_DRAIN_SECONDS = 0.5

    with expire_deadline_when(mod, common_dir(repo) / "slow-child.pid"):
        rc = mod.main(["submit", "--repo", str(wt)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "DEPLOY TIMED OUT" in out
    assert mod.RELEASE_TIMEOUT_REASON in out

    # the deploy's grandchild (a bare `sleep`) must have gone with the group
    child = int((common_dir(repo) / "slow-child.pid").read_text().strip())
    assert pid_is_gone(child), f"child {child} survived the deploy timeout kill"

    # same semantics as a nonzero deploy: main and prod restored to pre-merge
    assert sha(repo, "main") == main_before
    assert sha(repo, "refs/greenline/last-green") == main_before
    deployed = (common_dir(repo) / "greenline" / "deployed").read_text().strip()
    assert deployed == main_before
    assert sha(repo, "gl/slow")  # branch preserved

    last = journal_events(repo)[-1]
    assert last["event"] == "deploy_failed" and last["pre_main"] == main_before
    assert last["reason"] == mod.RELEASE_TIMEOUT_REASON


def test_health_probe_timeout_reports_unhealthy(tmp_path):
    repo = make_repo(tmp_path, "slowhealth", SLOW_HEALTH)
    seed_config(repo, tmp_path, "slowhealth", health="./run health")
    gl(repo, "setup", expect=0)

    mod = load_greenline_module()
    # The probe waits 120s, so a 10s cap still fires mid-probe while surviving
    # interpreter spawn time on a heavily loaded machine.
    mod.HEALTH_HARD_TIMEOUT_SECONDS = 10
    loaded = mod.load_repo(repo)
    log_path = tmp_path / "health.log"

    started = time.monotonic()
    assert mod.health_probe(loaded, log_path) is False
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"health probe ran {elapsed:.1f}s — the cap did not fire"

    assert "timeout after" in log_path.read_text()
    child = int((common_dir(repo) / "slow-child.pid").read_text().strip())
    assert pid_is_gone(child), f"child {child} survived the health timeout kill"


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


def test_machine_config_worktree_base_outranks_the_committed_one(tmp_path, monkeypatch):
    """A repo gated on a removable volume must be movable without a gate run.

    greenline.toml is committed, so relocating a gate normally needs a merge —
    which needs the very gate that macOS TCC has already broken with
    `Operation not permitted`. The machine-local override is the only escape
    from that deadlock, so it has to win over the committed value.
    """
    mod = load_greenline_module()

    repo = make_repo(tmp_path, "proj", RUN_RECORDER)
    seed_config(repo, tmp_path, "proj")
    committed = tmp_path / "wt" / "proj"

    home = tmp_path / "home"
    cfg_dir = home / ".config" / "greenline"
    cfg_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    # no machine config -> greenline.toml is authoritative
    assert mod.machine_config_path() == cfg_dir / "config.toml"
    assert mod.load_repo(repo).worktree_base == committed

    # named repo -> override wins, and ~ expands against the same home
    (cfg_dir / "config.toml").write_text('[worktree_base]\nproj = "~/internal/proj"\n')
    loaded = mod.load_repo(repo)
    assert loaded.worktree_base == home / "internal" / "proj"
    assert loaded.gate_path == home / "internal" / "proj" / "gate"

    # a repo the override does not name keeps its committed base
    (cfg_dir / "config.toml").write_text('[worktree_base]\nother = "/tmp/other"\n')
    assert mod.load_repo(repo).worktree_base == committed
