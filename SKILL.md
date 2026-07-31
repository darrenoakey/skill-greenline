---
name: greenline
description: Local serialized gated CI/CD for a solo-dev Mac where many AI agents work in git worktrees. Use to "set this repo up with greenline" (setup), create a worktree branched from last-green, submit a branch through the one serialized quality gate that runs the full check + real prod deploy, adopt commits that reached main outside the gate, or diagnose gate state (status/doctor). Every merge to main goes through the gate; main always == deployed == green.
---

# greenline

A local, single-machine, serialized gated CI/CD tool. One **canonical** checkout
per repo (the repo root) is always on `main`, always clean, and always identical
to what prod runs. All work happens in git worktrees branched from `last-green`.
Every merge to `main` goes through **one exclusive gate** that squash-merges the
candidate, runs the full `check` suite, fast-forwards `main`, runs a **real**
prod deploy, and publishes — one submission at a time, with automatic rollback if
deploy fails.

Executable: `~/.claude/skills/greenline/greenline` (symlinked at `~/bin/greenline`).
Python 3.11+ stdlib only. No environment-variable configuration — all config comes
from `greenline.toml` (committed at the repo root) and CLI args.

## Commands

| Command | Action | Exit |
|---|---|---|
| `greenline setup [--repo PATH]` | Set the enclosing repo up: write `greenline.toml`, create state dir + gate worktree + `last-green` ref, install the pre-push hook, install `docs/DOCTRINE.md` + `docs/greenline.md`, patch `AGENTS.md`, commit its own scaffolding on main (under the gate's allow-main authorization — never commit it by hand), run doctor. Idempotent. | 0 / 2 |
| `greenline worktree NAME [--repo PATH]` | Create branch `gl/NAME` off `last-green` and a worktree at `<worktree_base>/NAME`. Prints the path. | 0 / 1 |
| `greenline submit [BRANCH] [--repo PATH]` | Run BRANCH (default: current worktree's branch) through the serialized gate. Blocks on the lock; prints the holder every 30s while waiting. | 0 pass / 1 gate-fail / 2 env |
| `greenline adopt [--repo PATH]` | Gate the CURRENT main tip in place — for commits that reached main outside the gate (legacy workflow, hotfixes) AND for bootstrapping a freshly set-up repo that has never been gate-deployed (`deployed` missing/stale even though main == last-green). Runs check + deploy + publish on the tip. NEVER resets main: check failure leaves main and prod untouched; deploy failure restores prod to the best known-good SHA (deployed, then last-green) — or, in true bootstrap with no known-good SHA, skips rollback and prints a loud prod-unknown banner. | 0 / 1 / 2 |
| `greenline done [--force] [--repo PATH]` | From a merged worktree: verify it merged into `last-green`, then remove the worktree + delete the branch. `--force` to skip verification / dirty check. | 0 / 1 |
| `greenline deploy-pending [--repo PATH]` | Deploy a gated `main` whose deploy was coalesced away and never picked up (only reachable with `coalesce_deploys = true`, and only if the process that should have deployed died). No-op when nothing is pending. | 0 / 1 |
| `greenline status [--repo PATH]` | No lock. Show lock holder (+ PID liveness), SHA drift (main / origin / last-green / deployed) with an OK/DRIFT verdict, last 3 journal entries, gate worktree cleanliness, latest log. | 0 |
| `greenline doctor [--fix] [--repo PATH]` | Check all invariants and report. `--fix` acquires the lock and runs the preflight reconcile (crash recovery + drift reconciliation). | 0 ok / 2 problems |

Add `-v`/`--verbose` for git + command output (default is terse — house convention).

## Invariants

1. `main` == deployed == green, always.
2. The canonical checkout is pristine — no human or agent ever edits it; only greenline writes to it.
3. All work happens in worktrees branched from `last-green`.
4. Every merge goes through the serialized gate: full `check` + real `deploy`.
5. Test data never touches prod datastores.

Drift is never destroyed: if commits reached local `main` outside the gate, the
preflight refuses (exit 2) and directs you to `greenline adopt` — greenline never
resets or discards commits on `main`.

See `DOCTRINE.md` (installed into repos as `docs/DOCTRINE.md`) for the full
doctrine, including the **test/code co-design rules**
(parallel-safe tests, namespaced entities, no global-state assertions, OS-assigned
ports, one-version-backward-compatible migrations) that make the real gate viable.

## The contract (implemented in the repo's `./run`)

- `./run check` — cwd = the worktree being gated. Build + lint + FULL tests vs a
  TEST datastore. Exit code is the verdict. Must run concurrently from multiple worktrees.
  **HARD-CAPPED at 5 minutes** (`CHECK_TIMEOUT_SECONDS = 300`, no config/CLI override):
  a longer check is killed (whole process group) and the gate FAILS like red tests.
  The remedy is never raising the limit — speed the suite up (parallelize with isolated
  namespaces, prebuilt fixtures, warm per-worktree build caches, fail fast on build/lint
  before slow suites). Playbook: DOCTRINE.md 'The five-minute check'.
- `./run deploy` — cwd = the canonical checkout. Rebuild/restart prod (e.g.
  `auto -q restart <svc>`); MUST health-check, exit nonzero on unhealthy, and be idempotent.
- `./run health` *(optional)* — probe only; if absent, greenline re-runs `deploy` as the probe.

## State

All state lives in the shared git dir (`git rev-parse --git-common-dir`):
`.git/greenline/{lock, journal.jsonl, status.json, deployed, logs/, allow-push, allow-main}`
and the ref `refs/greenline/last-green`. The gate builds candidates in a persistent
worktree at `<worktree_base>/gate`.

**Main is hard-locked.** Three hooks installed by `greenline setup`:
- `reference-transaction` — refuses ANY local update of `refs/heads/main` unless
  `.git/greenline/allow-main` exists and names a still-alive PID. Cannot be
  bypassed with `--no-verify`. This is what actually prevents working on main.
- `pre-commit` — friendly early reject when HEAD is on main (bypassable; the
  reference-transaction hook is the real lock).
- `pre-push` — refuses direct pushes to main unless the gate sets transient
  `allow-push`.

The gate writes `allow-main` (PID + timestamp) only around intentional main-ref
mutations (ff, rollback reset, origin reconcile). A crashed gate cannot leave
main writable: a dead PID fails the liveness check.

## Recovery

`greenline submit` and `greenline doctor --fix` run a deterministic preflight
reconcile that recovers a crashed gate purely from the journal + git state +
the `deployed` file (see the Recovery rules in the source). A gate that crashed
after `ff` but before deploy is either completed (if prod already runs the
candidate and is healthy) or rolled back to the previous commit and redeployed.
A crash after push but before `last-green` update prints a LOUD "prod is behind
main — fix forward" banner and never force-pushes. A crashed `adopt` is recovered
without ever resetting main: completed if prod already runs the tip and is
healthy, otherwise prod is restored to last-green and `adopt_failed` journaled.

## Implementation

- Single file: `~/.claude/skills/greenline/greenline`. Templates in `templates/`.
- Tests: `tests/test_greenline.py` — real end-to-end pytest against scratch git
  repos in tmp dirs with fake `run` scripts. Run:

```bash
cd ~/.claude/skills/greenline
python3 -m pytest tests/ -x -q
```

## Gotchas & Known Pitfalls

- **`greenline submit` fails "canonical checkout is DIRTY" repeatedly when in-daemon autonomous agents (agentd3 taskdoers) work directly in the canonical checkout.** Cleaning the canonical once is not enough — the worker's next turn re-dirties it (and it may even rebuild+restart a prod color-slot binary from its dirty tree, bouncing production). Fix the SOURCE: list running conversations (`GET /v1/conversations`, filter cwd == canonical repo), interrupt superseded workers (`POST /v1/conversations/{id}/interrupt`), absorb any uncommitted work you want into your branch (copy files / apply diffs, then `git checkout --` + remove untracked in the canonical), and only then resubmit. (agentd3, 2026-07-23.)


- **A persistent gate worktree and the canonical deploy checkout must force-update submodules after every superproject checkout.** `git reset --hard` and `git merge --ff-only` update the gitlink in the index but do not move the submodule working tree, so a candidate can compile stale dependency code while the superproject diff and SHA look correct. Gate preparation, the post-squash candidate check, canonical advancement, and deploy rollback must each run `git submodule sync --recursive` followed by `git submodule update --init --recursive --force`. Diagnose with `git submodule status`: a leading `+` proves the checked-out submodule differs from the recorded gitlink.
- **The check runs in the gate worktree but deploy runs in the canonical checkout, so deploy must never install a canonical `bin/` produced by some earlier run.** A gate can pass, fast-forward main, restart successfully, and record the candidate as deployed while production is still running an old binary. Make `./run deploy` compile the exact canonical `HEAD` after the fast-forward, or archive `HEAD` into a disposable build tree and invoke the same release builder the gate exercised. Prove the live artifact contains a new schema/symbol/behavior; matching Greenline SHAs alone does not prove binary provenance.
- **A gate check that dies with exit 241 (SIGTERM, i.e. -15) and NO pytest failure summary was killed externally — it is not a test failure.** Seen 2026-07-04 in darrennn: the journal showed my check `fail {'stage': 'check', 'rc': 241}` with the log ending mid-suite (`tests/test_simulate.py ...run: FAILED (pytest)` — no assertion, no traceback), and another agent's `greenline submit` took the lock 3 seconds later. Greenline itself never kills a running check (the flock wait is blocking; `os.kill(pid, 0)` is only a liveness probe), so suspect a parallel agent "unwedging" the gate or a tool-level timeout. Response: don't debug the tests — rebase onto the (possibly advanced) main, re-verify the touched tests locally, and resubmit when `greenline status` shows the lock free.
- **Always detach `greenline submit` from any tool/session timeout** (e.g. `nohup greenline submit > submit.log 2>&1 &` then poll the journal/log). A harness Bash timeout SIGTERMs the whole process group at expiry, which can kill the gate's check mid-suite — indistinguishable from the pitfall above, and it wastes a full serialized gate round.
- **`greenline submit --repo <canonical>` run from anywhere resolves the default branch from the --repo checkout (main), not your cwd's worktree** — it fails immediately with "cannot submit 'main'". When using `--repo`, ALWAYS pass the branch explicitly: `greenline submit <branch> --repo <canonical>`. (Seen 2026-07-04 in darrennn: submit from inside the worktree with `--repo` still resolved 'main'.)
- **A deploy-stage failure is silently ROLLED BACK, not left half-done** — `GATE FAILED` at deploy restores prod to last-green and main is not advanced; your branch is untouched. Fix the deploy code (e.g. a bad `auto` flag), commit, and resubmit the same branch. Verify CLI flags used by `./run deploy` against the relevant skill doc (e.g. `auto add` has NO `--workdir`; that's `auto update`) BEFORE submitting — each wrong flag costs a full serialized check cycle (~7-50 min).
- **`greenline worktree` blocks for the ENTIRE duration of another submission's check+deploy, not just a quick lock check** — `cmd_worktree` wraps its `git worktree add` in the same `GateLock` that `submit` holds for its whole run, so if another branch is mid-check (can be 10-50+ min with a cold/large dependency tree), your `greenline worktree` call just hangs waiting, even though creating a worktree doesn't actually conflict with a concurrent check running in the separate gate worktree. If you need to start work immediately, bypass it safely: read the free-standing ref instead of the CLI — `LAST_GREEN=$(git rev-parse refs/greenline/last-green)` in the canonical checkout, confirm the branch/target path are free (`git rev-parse --verify refs/heads/gl/NAME` should fail; target dir under the repo's `worktree_base` — check `greenline status`'s config or `.git/greenline/status.json` — should not exist), then run the exact same `git worktree add -b gl/NAME <worktree_base>/NAME $LAST_GREEN` yourself. This is precisely what `cmd_worktree` does internally minus the lock; it's safe because worktree creation never touches canonical's working tree and doesn't collide with the other submission's gate-worktree build. Kill the now-redundant blocked `greenline worktree` process afterward (harmless — it would just fail with "already exists" once unblocked).
- **A repo with git submodules needs `git submodule update --init --recursive` in EVERY new worktree — `greenline worktree` (and a manual `git worktree add`) never does this.** `git worktree add` only checks out the tracked tree; submodule directories are created empty, so the first `cargo build`/`cargo check`/equivalent fails with a missing-path or "unable to update" dependency error referencing a path under the submodule. Run the init command once per new worktree right after creating it, before any build/check command.
- **`greenline submit` runs the gate check as a CHILD of the invoking session — it inherits that session's network/TCC context.** Seen 2026-07-23 in agentd3: after a macOS GUI crash-logout, freshly spawned unsigned binaries in interactive sessions were denied Local Network (EHOSTUNREACH to the LAN ollama box), so the gate's live-LAN tests failed identically from the terminal session, from `launchctl asuser`, and from an `auto` one-shot service — while a pre-existing daemon's tree kept LAN access and passed the same check. The gate is not a system service; do not assume it runs in a privileged/granted context. If live-LAN tests fail with "no route to host" while `curl` succeeds, fix the machine's Local Network grant (System Settings → Privacy & Security → Local Network) before burning gate cycles.
- **If the gate's DEPLOY step restarts the process that supervises your session, a synchronous `greenline submit` cannot survive its own gate.** This is distinct from the tool-timeout pitfall above: the killer is the deploy itself. In agentd3, `./run deploy` runs `auto -q restart agentd3`, which SIGTERMs the daemon's whole process group — and a submit launched from inside a daemon turn is a grandchild in that group, so the gate dies at `phase=deploying` and the holder is later found DEAD. Greenline serializes gates against each other but does NOT detach a self-submit from the daemon it restarts. Fix: submit through something that spawns the gate in its OWN process group (agentd3 exposes `curl -sXPOST 127.0.0.1:8620/v1/greenline/submit -d '{"branch":"gl/x","cwd":"<worktree>"}'`, whose `internal/gatejob` sets `Setpgid`), then poll the job log / `greenline status`. Confirm the detachment worked by checking the reported `pid == pgid`. From a real shell (no supervising daemon in the group), plain `greenline submit` is still correct. Corollary: expect your own session to be restarted mid-poll by the deploy — resume by reading the gate log, never by resubmitting.
- **A docs/markdown-only gate finishes in seconds and legitimately leaves `deployed` SHA ahead of the running binary's `build_sha`.** With a test selector that treats markdown as inert, the check selects no packages and the deploy performs no rebuild, so `greenline status` can read `main=deployed=<docs sha> [OK]` while `/healthz` still reports the previous code commit. That is NOT drift and NOT a stale-binary bug (contrast the deploy-provenance pitfall above): verify by confirming the only diff between the two SHAs is non-code. Reconcile SHAs by asking "did any compiled file change?", not by assuming a mismatch means a failed deploy.
