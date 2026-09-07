---
name: greenline
description: Local serialized gated CI/CD for a solo-dev Mac where many AI agents work in git worktrees. Use to "set this repo up with greenline" (setup), create a worktree branched from last-green, submit a branch through the one serialized quality gate that runs change-impact validation + real prod deploy, adopt commits that reached main outside the gate, or diagnose gate state (status/doctor). Every merge to main goes through the gate; main always == deployed == green.
---

# greenline

A local, single-machine, serialized gated CI/CD tool. One **canonical** checkout
per repo (the repo root) is always on `main`, always clean, and always identical
to what prod runs. All work happens in git worktrees branched from `last-green`.
Every merge to `main` goes through **one exclusive gate** that squash-merges the
candidate, runs change-impact `check` validation, fast-forwards `main`, runs a **real**
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
| `greenline submit [BRANCH] [--repo PATH]` | Run BRANCH through queue, change-impact check, deploy, and publish; normal complete release averages/targets 180s, loaded-machine hard failure at 600s. | 0 pass / 1 gate-fail / 2 env |
| `greenline adopt [--repo PATH]` | Gate the CURRENT main tip in place — for commits that reached main outside the gate (legacy workflow, hotfixes) AND for bootstrapping a freshly set-up repo that has never been gate-deployed (`deployed` missing/stale even though main == last-green). Runs check + deploy + publish on the tip. NEVER resets main: check failure leaves main and prod untouched; deploy failure restores prod to the best known-good SHA (deployed, then last-green) — or, in true bootstrap with no known-good SHA, skips rollback and prints a loud prod-unknown banner. | 0 / 1 / 2 |
| `greenline done [--force] [--repo PATH]` | From a merged worktree: verify it merged into `last-green`, then remove the worktree + delete the branch. `--force` to skip verification / dirty check. | 0 / 1 |
| `greenline deploy-pending [--repo PATH]` | Recover a legacy or interrupted gated `main` that lacks deploy attestation. No-op when nothing is pending. | 0 / 1 |
| `scripts/greenline-wait.sh [--repo PATH] BRANCH` | Submit and event-watch one exact branch/source candidate under the same deadline; process exit and candidate-correlated terminal journal events determine the verdict. | 0 pass / 1 fail / 2 timeout |
| `greenline status [--repo PATH]` | No lock. Show lock holder (+ PID liveness), SHA drift (main / origin / last-green / deployed) with an OK/DRIFT verdict, last 3 journal entries, gate worktree cleanliness, latest log. | 0 |
| `greenline doctor [--fix] [--repo PATH]` | Check all invariants and report. `--fix` acquires the lock and runs the preflight reconcile (crash recovery + drift reconciliation). | 0 ok / 2 problems |

Add `-v`/`--verbose` for git + command output (default is terse — house convention).

## Invariants

1. `main` == deployed == green, always.
2. The canonical checkout is pristine — no human or agent ever edits it; only greenline writes to it.
3. All work happens in worktrees branched from `last-green`.
4. Every merge goes through the serialized gate: change-impact `check` + real `deploy`.
5. Test data never touches prod datastores.

Drift is never destroyed: if commits reached local `main` outside the gate, the
preflight refuses (exit 2) and directs you to `greenline adopt` — greenline never
resets or discards commits on `main`.

See `DOCTRINE.md` (installed into repos as `docs/DOCTRINE.md`) for the full
doctrine, including the **test/code co-design rules**
(parallel-safe tests, namespaced entities, no global-state assertions, OS-assigned
ports, one-version-backward-compatible migrations) that make the real gate viable.

## Release small, keep going

Choose the smallest coherent, useful change, validate its relevant impact, and
release it immediately; then take the next chunk. Prioritize a working, deployed
fix for any reported production blocker. Do not broaden ready-to-ship scope or
bundle speculative improvements. A release boundary is never a permission pause:
continue the whole authorized goal without asking. Keep validation selection
smarter as the product grows so additional scope does not increase release latency.

**Repos with a database must additionally follow the `database` skill**
(`~/.claude/skills/database/SKILL.md`): schema and test-seed data maintained only
by incremental checksummed delta scripts tracked in a `database_version` table
(steady state applies nothing); one standing migrated `<name>_test` database —
tests never run DDL or create/drop databases, only read canonical seed data,
mutate only rows they created, and use UUID ids/markers so they are parallel-
and dirty-DB-safe. That discipline helps keep the entire release inside its
180-second target and safe to run concurrently from multiple worktrees.

## The contract (implemented in the repo's `./run`)

- `./run check` — cwd = the worktree being gated. Start with all useful,
  relevant verification that fits in three minutes, then use changed paths and
  transitive impact to prove the selection covers every behavior the candidate
  could affect against a TEST datastore. Preserve relevant coverage; run broader
  or external suites only when intentionally relevant. Product growth must not
  increase standard release time. Exit code is the verdict. Must run concurrently
  from multiple worktrees.
- `./run deploy` — cwd = the canonical checkout. Rebuild/restart prod (e.g.
  `auto -q restart <svc>`); MUST health-check, exit nonzero on unhealthy, and be idempotent.
  During a release it can use the actual aggregate time remaining; there is no
  narrower deploy-stage cap.
- `./run health` *(optional)* — probe only; if absent, greenline re-runs `deploy` as the probe.
  **5-second hard timeout** (no override); a probe that does not answer in time is
  unhealthy. If your health check cannot fit, it is rebuilding rather than probing.

The normal complete release averages and targets **180 seconds**. Its one hard,
non-configurable **600-second end-to-end deadline** is a loaded-machine worst-case
failure boundary, not the target, and runs from invocation through queue, reconcile,
check, deploy, publish, and attestation. The target is never a correctness
rejection. Successful releases journal and print queue/check/deploy/publish/total
timings; total time over 180s automatically launches a detached speed-up
investigation after unlock, whose acceptance is reported only after its tool
proves it. At five minutes, immediately investigate the active stage and process
tree. At ten minutes, terminate and reap the attempt, then fix the cause before
submitting again—never wait for load, rearm a TTL watcher, or retry unchanged.
Avoid narrower inner timeouts that falsely fail useful work while aggregate time
remains. A failed release starts separately bounded recovery,
which is reported as recovery and can never turn the expired release into success.
Standalone health remains capped at 5s.

## State

All state lives in the shared git dir (`git rev-parse --git-common-dir`):
`.git/greenline/{lock, journal.jsonl, status.json, deployed, logs/, allow-push, allow-main}`
and the ref `refs/greenline/last-green`. The gate builds candidates in a persistent
worktree at `<worktree_base>/gate`.

`worktree_base` resolves in this order: `~/.config/greenline/config.toml`
(machine-local, never committed) > the repo's committed `greenline.toml` >
`default_worktree_base`. The machine file outranks the repo because where
worktrees live is a property of the machine, and because a repo whose gate has
become unusable cannot fix itself by landing a commit — landing a commit needs
that gate:

```toml
# ~/.config/greenline/config.toml — keys are canonical checkout directory names
[worktree_base]
agentd3 = "~/.greenline-worktrees/agentd3"
```

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

- **"canonical checkout is DIRTY" fires on UNTRACKED files too, not just tracked modifications.** Planning artifacts (`plan-*.md` from council/orchestration sessions) dropped at the repo root by an earlier session block every submit. Don't delete other sessions' artifacts — relocate them under a gitignored path (beezle: `local/` is ignored except `config.example.toml`, so `mkdir -p local/plans && mv plan-*.md local/plans/`), confirm `git status --porcelain` is empty, resubmit. (beezle, 2026-08-11.)

- **A persistent gate worktree and the canonical deploy checkout must force-update submodules after every superproject checkout.** `git reset --hard` and `git merge --ff-only` update the gitlink in the index but do not move the submodule working tree, so a candidate can compile stale dependency code while the superproject diff and SHA look correct. Gate preparation, the post-squash candidate check, canonical advancement, and deploy rollback must each run `git submodule sync --recursive` followed by `git submodule update --init --recursive --force`. Diagnose with `git submodule status`: a leading `+` proves the checked-out submodule differs from the recorded gitlink.
- **The check runs in the gate worktree but deploy runs in the canonical checkout, so deploy must never install a canonical `bin/` produced by some earlier run.** A gate can pass, fast-forward main, restart successfully, and record the candidate as deployed while production is still running an old binary. Make `./run deploy` compile the exact canonical `HEAD` after the fast-forward, or archive `HEAD` into a disposable build tree and invoke the same release builder the gate exercised. Prove the live artifact contains a new schema/symbol/behavior; matching Greenline SHAs alone does not prove binary provenance.
- **A gate check that dies with exit 241 (SIGTERM, i.e. -15) and NO pytest failure summary was killed externally — it is not a test failure.** Seen 2026-07-04 in darrennn: the journal showed my check `fail {'stage': 'check', 'rc': 241}` with the log ending mid-suite (`tests/test_simulate.py ...run: FAILED (pytest)` — no assertion, no traceback), and another agent's `greenline submit` took the lock 3 seconds later. Greenline itself never kills a running check (the flock wait is blocking; `os.kill(pid, 0)` is only a liveness probe), so suspect a parallel agent "unwedging" the gate or a tool-level timeout. Response: don't debug the tests — rebase onto the (possibly advanced) main, re-verify the touched tests locally, and resubmit when `greenline status` shows the lock free.
- **Use `scripts/greenline-wait.sh` instead of hand-built polling.** It submits in a separate process group, correlates the exact branch and source SHA to its own `start`, accepts only that candidate's terminal event, and reports process exit within the same release deadline.
- **`greenline submit --repo <canonical>` and `greenline done --repo <canonical>` resolve the default branch from the --repo checkout (main), not your cwd's worktree.** submit fails immediately with "cannot submit 'main'"; done fails with "not on a greenline feature branch" even when cwd is the merged worktree. When using `--repo`, ALWAYS pass the branch explicitly (`greenline submit <branch> --repo <canonical>`). For `done`, run it from inside the worktree WITHOUT `--repo`. (Seen 2026-07-04 in darrennn: submit from inside the worktree with `--repo` still resolved 'main'. Seen 2026-08-17 in agentd3: `greenline done --force --repo <canonical>` from the landed `gl/append-key-fast-path` worktree failed the same way.)
- **A deploy-stage failure is silently ROLLED BACK, not left half-done** — `GATE FAILED` at deploy restores prod to last-green and main is not advanced; your branch is untouched. Fix the deploy code (e.g. a bad `auto` flag), commit, and resubmit the same branch. Verify CLI flags used by `./run deploy` against the relevant skill doc (e.g. `auto add` has NO `--workdir`; that's `auto update`) BEFORE submitting — each wrong flag wastes a serialized release attempt.
- **An iOS `DeviceLocked` deploy failure is not a code failure.** `devicectl list devices` "available" ≠ unlocked; install can succeed while launch fails with `FBSOpenApplicationErrorDomain` / `Locked`. Greenline still rolls main back to last-green and leaves the branch untouched. Do not change deploy, do not `doctor --fix`, and do not submit again until a direct `xcrun devicectl device process launch --terminate-existing --device <UDID> <bundle>` probe succeeds. (agentd3-ios `gl/settings-per-source`, 2026-08-15.)

- **`greenline worktree` takes the same gate lock as submission, so do not leave it blocked behind an active release.** Preserve the useful isolation while avoiding a long wait: read `refs/greenline/last-green` in the canonical checkout, verify the intended `gl/NAME` branch and target path do not exist, resolve `worktree_base` from Greenline's config/status, and run `git worktree add -b gl/NAME <worktree_base>/NAME <last-green-sha>`. Worktree creation does not touch the canonical working tree or the gate worktree. If a CLI worktree process is already blocked, terminate it after validating the manual worktree; never let it linger past the release's 180-second target or 600-second deadline.
- **`greenline worktree NAME` always prefixes `gl/`.** Pass the short name only (`long-rate-limit-hop`), never `gl/long-rate-limit-hop` — that creates branch `gl/gl/long-rate-limit-hop`. Rename with `git branch -m gl/<name>` in the worktree if you already did it. (agentd3, 2026-08-16.)

- **A repo with git submodules needs `git submodule update --init --recursive` in EVERY new worktree — `greenline worktree` (and a manual `git worktree add`) never does this.** `git worktree add` only checks out the tracked tree; submodule directories are created empty, so the first `cargo build`/`cargo check`/equivalent fails with a missing-path or "unable to update" dependency error referencing a path under the submodule. Run the init command once per new worktree right after creating it, before any build/check command.
- **`greenline submit` runs the gate check as a CHILD of the invoking session — it inherits that session's network/TCC context.** Seen 2026-07-23 in agentd3: after a macOS GUI crash-logout, freshly spawned unsigned binaries in interactive sessions were denied Local Network (EHOSTUNREACH to the LAN ollama box), so the gate's live-LAN tests failed identically from the terminal session, from `launchctl asuser`, and from an `auto` one-shot service — while a pre-existing daemon's tree kept LAN access and passed the same check. The gate is not a system service; do not assume it runs in a privileged/granted context. If live-LAN tests fail with "no route to host" while `curl` succeeds, fix the machine's Local Network grant (System Settings → Privacy & Security → Local Network) before burning gate cycles.
- **If the gate's DEPLOY step restarts the process that supervises your session, a synchronous `greenline submit` cannot survive its own gate.** This is distinct from the tool-timeout pitfall above: the killer is the deploy itself. In agentd3, `./run deploy` runs `auto -q restart agentd3`, which SIGTERMs the daemon's whole process group — and a submit launched from inside a daemon turn is a grandchild in that group, so the gate dies at `phase=deploying` and the holder is later found DEAD. Greenline serializes gates against each other but does NOT detach a self-submit from the daemon it restarts. Fix: submit through something that spawns the gate in its OWN process group (agentd3 exposes `curl -sXPOST 127.0.0.1:8620/v1/greenline/submit -d '{"branch":"gl/x","cwd":"<worktree>"}'`, whose `internal/gatejob` sets `Setpgid`), then poll the job log / `greenline status`. Confirm the detachment worked by checking the reported `pid == pgid`. From a real shell (no supervising daemon in the group), plain `greenline submit` is still correct. Corollary: expect your own session to be restarted mid-poll by the deploy — resume by reading the gate log, never by resubmitting.
- **A docs/markdown-only gate finishes in seconds and legitimately leaves `deployed` SHA ahead of the running binary's `build_sha`.** With a test selector that treats markdown as inert, the check selects no packages and the deploy performs no rebuild, so `greenline status` can read `main=deployed=<docs sha> [OK]` while `/healthz` still reports the previous code commit. That is NOT drift and NOT a stale-binary bug (contrast the deploy-provenance pitfall above): verify by confirming the only diff between the two SHAs is non-code. Reconcile SHAs by asking "did any compiled file change?", not by assuming a mismatch means a failed deploy.
- **A gate that fails on a shared finite resource (DB connections, ports) is usually poisoned by processes LEAKED from an earlier interrupted run — not by your diff.** If the suite spawns real services (BDD/e2e), a cancelled or crashed run orphans them, and they keep their warmed pools forever; they do NOT drain. Seen 2026-07-31 in mnemnos: two orphaned `target/debug/mnemnosd` (one from a local `./run check` I cancelled, one from the gate's own BDD) held ~40 connections each against `max_connections=200`, and the gate then failed twice in completely unrelated, misleading places — `FATAL: sorry, too many clients already` deep in the integration tests, and a `504` on a UI health-proxy scenario. Tell-tale: the same test passes in isolation, and each gate run fails somewhere DIFFERENT. Diagnose before re-reading your diff: `pgrep -f 'worktrees.*target/debug/<daemon>'` and `select count(*) from pg_stat_activity` (compare to `show max_connections`). Kill only the debug/worktree binaries — never the `target/release/*` production ones. Sweep for orphans BEFORE every resubmit, since each failed gate leaks a fresh one.
- **The 180-second target measures the whole release, not just check.** Queue, reconcile, check, deploy, publish, and attestation all count; only the 600-second end-to-end ceiling rejects correct work.
- **A QUEUED `greenline submit` can die at its own preflight with "local main is AHEAD of last-green … Gate them in place with: greenline adopt" — and that is usually a transient race, not real drift.** While your submit waits on the lock, the holder may crash mid-deploy (in agentd3 the deploy restarts the daemon that supervises the holder, so `phase=deploying` holders die routinely). Your process then wins the lock, runs `recovering incomplete gate from journal...`, and observes main already fast-forwarded to the dead gate's candidate while `last-green` still points at the old tip — so it refuses and exits WITHOUT submitting your branch. Do NOT run `greenline adopt`: the crashed gate's own recovery/another queued gate finishes the reconcile moments later. Re-check first — `greenline status` (expect `main=origin=last-green=deployed [OK]`, lock free, journal ending in `complete`) plus `greenline doctor` (rc=0) — then simply resubmit the same branch, which then gates normally. Seen 2026-08-02 in agentd3: a UI-only branch queued behind `gl/resilience-followup`, exited at preflight on that message, and the identical resubmit passed check+deploy in ~12s once the journal showed `complete`.
- **A gate failure caused by an external service is still a release failure.** Diagnose and repair the dependency immediately; never wait for lower machine load, postpone deployment, or repeatedly rearm a long watcher. The same end-to-end deadline applies on every attempt.

- **Never run bare `rustfmt FILE` on mnemnos — it defaults to edition 2015 and spews hundreds of `async fn`/`let chains` errors. Use `cargo fmt -- FILE` (or `cargo fmt --all`).** `./run format` is check-only. Seen 2026-08-15 on `tests/recall_router.rs`.

- **beezle3 checks hard-depend on `agentd3-test` at `127.0.0.1:18620`.** A stopped/unhealthy downstream test instance fails AgentD live tests with `connection refused` / healthz mismatch and can red a docs-only candidate. Preflight before `greenline submit` in beezle3: `curl -sf http://127.0.0.1:18620/healthz | jq -e '.status=="ok" and .instance=="test" and .database=="agentd3_downstream_test"'`; if down, `auto -q start agentd3-test` and wait for ready. Do not fall back to production agentd3 (`:8620`). (beezle3, 2026-08-04.)

- **Incomplete gate stuck at `ffed` with a DEAD holder and prod already restored to last-green:** `doctor --fix` will re-run `./run deploy` of the *failed candidate* (which may be the binary that broke health). If prod is already healthy on last-green and `main == last-green == deployed`, close the journal with a `deploy_failed` for that candidate (and clear stale `status.json`) rather than letting doctor redeploy the bad build. Then submit the fixed branch. Seen 2026-08-04 on arbiter `gl/job-prune-10d` after a startup-index hang.
- **A gate on a USB/Thunderbolt volume cannot stay green on macOS: it dies with a bare `Operation not permitted` (or, worse, `No such file or directory`) that names no permission.** Every executable that touches an external volume needs its own TCC "removable volume" consent, and consent is keyed to *that binary's* identity — path for unsigned tools, bundle id + code requirement for signed ones. Interpreted repos survive (one grant for `python`/`node` covers every run), but Go/Rust/Swift test binaries are rebuilt under a fresh random path (`$TMPDIR/go-build<rand>/bNNN/pkg.test`, `target/debug/deps/<name>-<hash>`) on every run, so their consent can never be reused. The prompts never stop, they are unanswerable when the gate runs headless (no foreground responsible app ⇒ auto-deny), and one accidental "Don't Allow" is sticky. Worse still, an unanswered prompt WEDGES the volume: `tccd` dissents, a `diskutil unmount` hangs forever, and every `stat` on the volume blocks (measured: `ls` on the worktree root went from 0.03s to >180s). Unwedge with `kill -KILL` on the *user* `tccd` (`pgrep -f 'TCC.framework/Support/tccd'` — the `system` one is a different instance and respawns clean) plus killing any stuck `diskutil unmount`. Fix permanently by relocating the gate to internal storage via `~/.config/greenline/config.toml`; that override exists precisely because the repo-committed `worktree_base` cannot be changed once the gate it names is broken. (agentd3 + 8 sibling compiled repos, 2026-08-07.)
- **`lock: free` with a DEAD holder and leftover `scripts/ci.sh` / pytest still in the gate worktree is not a free gate.** After a broker restart the holder PID dies and `greenline status` reports `lock : free`, but the check process group (PPID 1, still `cd`'d into `<worktree_base>/<repo>/gate`) keeps running and still owns that tree. `doctor --fix` here is wrong: it would steal the in-flight check. Wait until those leftover PIDs exit, then read the journal. If it completed, queue behind it; if it died mid-check with no `fail`/`complete`, only then recover. Seen 2026-08-15 on waggler `gl/decline-igs-gpt-oss` (dead holder 38895, leftover `ci.sh` 39304 + pytest 39625 still in `beezle3/gate`).
- **A Go check that prints `All checks passed!` then dies on `gofmt found unformatted files` is a formatting miss, not a test failure.** `gofmt -l` runs after Go tests in beezle3/`waggler` `scripts/ci.sh`, so a one-space struct-tag drift (seen 2026-08-15 on `gl/trip-city-pages` `pkg/trips/story/spec.go` `Place`) burns a full serialized check. Run `gofmt -l` on every touched `.go` file before `greenline submit`; do not treat a green focused `go test` as gate-ready. (waggler, 2026-08-15.)
- **A beezle3/`waggler` check that dies at the 10-minute hard timeout while `ltx2runner` pytest sits at 0% CPU on `test_sdk_image_operation_recovers_ambiguous_accept_after_process_death` is leftover image-operation testers, not the candidate.** Seen 2026-08-15 on `gl/trip-city-pages`: two `Python -c … image_operation_state_path` processes (PIDs 92059/92087, PGID leader already dead, cwd `<worktree_base>/beezle3/gate/src`) survived the first gofmt-fail check for ~6h and hung the next gate's recovery tests. `greenline status` still said `lock: free` and the gate worktree was clean. Kill those leaked testers before resubmitting; do not thin the branch. (waggler, 2026-08-15.)
- **`greenline status`'s `last log :` pointer can name a stale log from an earlier episode whose tail shows a DIFFERENT deployed SHA — the journal and `deployed` file are authoritative.** Seen 2026-08-15 on agentd3 after a daemon restart: status said `deployed=3a20d9e [OK]` (journal `pending_deployed tip 3a20d9e`, `deployed` mtime matching) but `last log` pointed at `resume-deploy-pending.log` (mtime hours older) ending `deployed 02a0274` from a previous deferred-deploy episode. After any manual/detached deploy script, reconcile drift by `tail journal.jsonl` + `cat .git/greenline/deployed` + file mtimes, never by the last-log tail. (agentd3, 2026-08-15.)
- **A follow-on branch still parented on the pre-squash SHA fails the next gate at `stage: merge` on `tests/bdd/perf.json`.** Greenline squash-merges, so the landed parent (`f41ce25`) is not the worktree commit (`b922df7`) even when the tree matches. Both sides independently refresh BDD timings, so a three-way merge of that file conflicts even though the rest of the branch is clean. Fix: `git rebase --onto <squash-tip> <original-parent>` (replay only the follow-on commits), commit a post-rebase `perf.json` refresh, then resubmit. Do not rebase onto the original parent SHA. Seen 2026-08-15 on agentd3 `gl/models-defect-closure` after `gl/attention-banner-layout` landed.
- **Never chain resubmissions.** Use `scripts/greenline-wait.sh BRANCH`; it binds one process to one source SHA and only accepts the resulting candidate's complete event.
- **Machine load never postpones a release or extends its deadline.** Repair leaked processes, cache design, or test performance within the same release contract; do not load-gate submission or chain retries.
- **The standard watcher is single-run and candidate-correlated.** `scripts/greenline-wait.sh` observes the exact source SHA and process it launched; a restart or nonzero process exit is terminal, never a reason to rearm a multi-hour watcher.
- **`git diff --stat refs/greenline/last-green <branch>` shows PHANTOM deletions of peers' landed work when your branch is based on older main.** The diff counts files you never touched (they landed on main after your merge-base) as "deleted by you". Before panicking that the gate's squash-merge dropped or resurrected content, diff against your true base (`git merge-base <branch> last-green`) to see your real change, then verify the landed squash directly (`git show <squash-sha> --stat` + `git grep` for your symbols). Expect `last-green`'s tree ≠ your branch tip when peers landed in between — that's the three-way merge correctly preserving both sides. (agentd3 `gl/agentic-task` vs `gl/codex-wham-usage`, 2026-08-15.)

- **The improve skill's "append to AGENTS.md" step lands in the CANONICAL checkout — which greenline invariant #2 forbids ever editing.** Route knowledge bullets through a tiny docs-only branch instead: `git worktree add -b gl/<name>-docs <base>/gl-<name> $(git rev-parse refs/greenline/last-green)` (manual bypass while a gate holds the lock), commit, submit via the gatejob API. Docs-only gates finish in seconds; editing canonical even briefly dirties it and blocks every queued peer submit. (agentd3, 2026-08-16.)

- **When an UNLANDED peer branch touches your files, pre-simulate the gate's merge with `git merge-tree --write-tree <peer-branch> HEAD` (exit 1 + conflict list = your gate slot dies at `stage: merge` if they land first) — then DEFUSE instead of queueing blind.** Two defusals that turn a certain conflict into a clean auto-merge: (1) shared always-refreshed files like `tests/bdd/perf.json` — restore the base (`last-green`) values of the header lines every run rewrites (`last_run`, `total_duration_ms`) so your diff is purely additive new keys; the stale totals are harmless because the file self-refreshes on the next suite run; (2) never keep edits to comment/doc regions the peer rewrote — revert those hunks to base text and put your documentation in a region neither side touches. Re-run merge-tree until exit 0, amend, then submit. Verified 2026-08-16 on agentd3 `gl/inline-agent-boxes` vs unlanded `gl/model-change-inline-order`: both conflicts (hierarchy.go comment, perf.json header) defused pre-submit; gate merged clean.
- **Parallel branches that each add a migration against ONE standing shared test DB must CARRY every peer's earlier unmerged migration byte-identical.** dbmigrate-style reconcilers hard-fail when the DB's applied version N has no matching file in your tree (peer's unmerged 0015 applied to the shared test DB + your 0016 = every store test red). Fix: copy the peer's migration file VERBATIM from their branch (verify sha256 matches the checksum row in the DB), never author your own at that number, and announce on the board that the bytes are frozen. Squash-merges dedupe the identical file whichever branch lands first; if theirs never lands, the carried file is still consistent with the DB. Coordinate numbers on the message board BEFORE creating files (agentd3 2026-08-16: 0015/0016/0017 across three concurrent branches, each carrying its predecessors). (greenline + database skills, 2026-08-16.)

- **A submit killed between `ffed` and its deploy (`main` ahead, `last-green`/`origin` behind, `deployed` = an OLDER sha, DEAD holder in `phase=deploying`) MUST be finished with `greenline deploy-pending` — running `doctor --fix` or another `submit` first DESTROYS the gated commit.** `cmd_deploy_pending` deliberately skips `preflight_reconcile`; every other entry point runs it, and `recover_incomplete`'s `"ffed" in phases` branch only completes the candidate when `deployed == candidate`. With an older `deployed` marker it takes the else branch: `git reset --hard <pre_main>` on main plus a redeploy, silently discarding a commit that already passed the gate. Seen 2026-08-16 on agentd3 `gl/gemini-paste-fallback` (main=e3634ce, last-green=64c7d6c, deployed=b57094a): prod was ALREADY running e3634ce (the blue/green swap completed at 02:15 and killed the submit that was performing it), yet the marker still named b57094a, so recovery would have rolled main back to 64c7d6c. `doctor` names the right move — `[FAIL] deferred deploy has an owner … run greenline deploy-pending` — take that literally and never `--fix` it.
- **Proving a blue/green daemon deploy actually FLIPPED needs three facts — `deployed <sha> [OK]` is only the first.** (1) journal/deployed-file sha; (2) the binary the router points at embeds that revision — agentd3: `cat local/front/active.json` for the active color, then `strings output/bin/agentd3.<color> | grep -m1 vcs.revision` (Go embeds it); (3) the `serve` process for that color started after that binary's build mtime (`ps -o pid,lstart -p <pid>`). Skip (2) and a deploy can report OK while the active color still runs the previous build (cf. the deploy-provenance pitfall above); skip (3) and you can't rule out a flip-back. Seen verifying agentd3 `gl/fix-steer-skip` (9fd9abeb) after the 2026-08-16 daemon restart: blue active, binary mtime 03:10, serve pid 97378 started 03:10:23, embedded `vcs.revision=9fd9abeb` — flip confirmed live.
- **`greenline setup` on a NEW repo writes the machine's `default_worktree_base` into the committed `greenline.toml` — on this Mac that default is the external `/Volumes/Gumby`, so a fresh Go/Rust repo's gate lands on the USB-volume trap.** Fix at setup time, not after the first TCC wedge: add the repo to `[worktree_base]` in `~/.config/greenline/config.toml` (machine file outranks the repo), `git worktree remove <gumby-base>/<repo>/gate`, `mkdir -p` the internal base, then `greenline doctor --fix` recreates the gate worktree at the new path. Seen 2026-08-16 on calendar-sync: gate relocated to `~/.greenline-worktrees/calendar-sync/gate` cleanly before any gate ran there. The stale Gumby path is also baked into the scaffolded `AGENTS.md` workflow text — fix that line via a docs-only branch.
- **A repo SPLIT bootstrap must carry machine-local recorded-outcome caches into the new repo's GATE worktree, not just its checkout.** Seen 2026-08-16 on trips (split out of waggler): `pkg/trips/backfill` caches real Arbiter caption/aesthetic outcomes in gitignored `<repo>/local/arbiter-vision-cache`, content-keyed so entries are repo-independent — but only the checkout's copy was seeded. The first `greenline adopt` ran in `~/.greenline-worktrees/trips/gate`, found an empty cache, re-RECORDED against the live Arbiter (96% GPU busy, ~430s queue wait), and died at the test's 90s deadline looking like a flaky test. Fix: `cp <old-repo>/local/<cache>/*.json <worktree_base>/<new-repo>/gate/local/<cache>/`, verify with a focused `-run` that logs "replayed", then adopt — check dropped 93s→17s and passed. Any test that records live-service outcomes into a gitignored path has this failure mode on repo move AND on every fresh gate-worktree recreation.
- **A `./run deploy` preflight whose process SELF-PROBES and exits must run in the FOREGROUND — backgrounding it plus curl-looping the port races the process's own post-probe shutdown.** Seen 2026-08-16 on trips: deploy ran `trips web -preflight &` (the binary serves, probes its own /api/trips, shuts down within a 30s lifetime) then curled the scratch port 10×; the binary always won the race to shut down, so the loop saw connection-refused — and the ONE time it "passed" was when the scratch port collided with a standing instance that answered the curl instead (preflight validated the OLD binary). Correct shape: `if ! "$EXE" web -preflight; then exit 1; fi` — the exit code is the verdict. Bonus bug in the same episode: `fmt.Sprintf("http://127.0.0.1%s", *addr)` produces `127.0.0.1127.0.0.1:18796` for host:port addrs; normalise `:port` → loopback, pass `host:port` verbatim.
- **Cleaning an absorbed canonical checkout: `git reset --hard` is REFUSED by the reference-transaction hook (even as a no-op reset of HEAD), because greenline cannot distinguish it from a main move.** The hook-safe equivalent after absorbing work into a branch: `git restore --source=HEAD --staged --worktree -- .` (clears staged deletions AND recreates deleted files, no ref transaction), then `rm` the untracked files, then confirm `git status --porcelain` is empty. Seen 2026-08-16 on waggler gl/trips-extract absorption.
- **`greenline status` can show a lock-holder `started=` timestamp hours older than the real holder process — verify with `ps` before declaring the gate stuck.** Seen 2026-08-17 (agentd3): status printed `holder pid=99544 … started=2026-08-16T11:41:31Z` (~8.6h earlier, and a queued `greenline worktree`'s wait loop printed the matching `elapsed=31528s`), but `ps -p 99544 -o lstart=` showed the process had started 4 minutes ago — the lock record's start time was stale while the holder itself was fresh and healthy. A long-dead-looking `started=` is NOT evidence of a wedged gate; check the holder pid's actual start time and whether its phase advances before touching `doctor --fix` or killing anything.
- **Edit/Write with a relative path applies to the session cwd (usually the canonical checkout), not the worktree you just read.** Snapshot tags from `~/.greenline-worktrees/<repo>/.../file.go` do not retarget a later `PUT` whose header is just `file.go`. Always put the absolute worktree path in the edit header. A relative-path miss dirties canonical, blocks every queued `greenline submit`, and is recovered with `git restore --source=HEAD --staged --worktree -- <file>` (never `git reset --hard` — the reference-transaction hook refuses it). Seen 2026-08-17 on agentd3 `gl/classifier-fallback-mid`.
- **`greenline done` can refuse a squash that is already in `last-green`.** It verifies the worktree branch tip as an ancestor of `last-green`. Greenline squash-merges, so that ancestry test is false even when `git log last-green` shows `gl/<name>: …` as the squash subject. After `GATE PASSED`, confirm the squash SHA is on `last-green` (`git log --oneline refs/greenline/last-green | grep gl/<name>`), then `greenline done --force`. Do not leave the worktree around waiting for a later last-green move to make ancestry true — it never will. Seen 2026-08-17 on agentd3 `gl/front-unkeyed-503-hold` (squash `3c5c2d4` on last-green; `done` still said "cannot verify branch was merged").

- **Resuming a dead session's unlanded `gl/` branches: the content may have ALREADY landed under a different squash — reconcile by squash subject + content diff before resubmitting.** Squash subjects embed the branch name (`gl/<name>: <msg>`), so `git log main --grep="<branch-name>"` finds a peer/earlier landing that plain ancestry (`merge-base --is-ancestor`) never will. Then content-diff only the files the branch touched (`git diff main <branch> -- <paths>`): an empty or main-is-newer diff means the branch is fully superseded — delete it (and its worktree) instead of re-gating; a residual diff (e.g. a docs paragraph that never landed) is the only piece to re-land on a FRESH branch off last-green, never by resubmitting the stale branch (its base predates peers' landings and its non-touched-file diff vs main shows phantom reverts). Seen 2026-08-17 on agentd3: dead session left gl/structured-orchestrator-profiles (code had landed as its own squash 280e230 plus a later improvement) and a gl/gl/-misnamed docs branch (12-line AGENTS.md paragraph genuinely missing) — re-landed the paragraph in a seconds-long docs gate, deleted both stale branches.
