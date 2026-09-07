# Release wait guidance

Use `scripts/greenline-wait.sh <branch> --repo <worktree>` once. It launches one
submission in its own process group and correlates branch, source SHA, candidate,
terminal journal event, and process exit. It does not load-gate, chain retries,
poll with fixed sleeps, or accept a peer's result.

Release the smallest coherent, useful change as soon as its relevant impact is
validated, then continue with the next chunk without asking. A reported
production blocker comes first and requires a working, deployed fix. Do not
broaden ready-to-ship scope
or bundle speculative improvements; keep impact selection smart enough that
product growth does not increase release latency.

The same 600-second release deadline covers lock admission through publish. A
successful release normally targets 180 seconds; exceeding that target starts a
separate automatic speed-up investigation and does not make the release fail.
At five minutes inspect the active stage immediately. At ten minutes terminate
and reap the attempt, then fix the cause; never wait for load, rearm a TTL
watcher, or retry unchanged.

# What this repo's release time is actually made of

Measured, not assumed. `deploy` is a no-op here (a CLI skill has no runtime), so
the release IS `./run check`, and the check is almost entirely the test suite.
The suite drives the real CLI against real git repos, so its cost is process
spawns, not computation. Before optimizing anything here, measure again — these
are the numbers that mattered:

- **The suite is latency-bound, not CPU-bound.** A full run burns ~128s of CPU
  across a ~180s wall: under one core of ten. That is why the worker count is
  deliberately above the core count (`2*cores+4`). Measured on the gate machine:
  `-n 10` 181.7s, `-n 24` 143.2s, `-n 40` 173.6s, `-n 48` 209.9s. There is a
  real knee; re-measure before changing it.
- **`greenline setup` was the single biggest cost**: ~40 git subprocesses,
  8-15s wall per call, paid by most tests just to obtain a repo setup had
  already finished with. It now runs once into a fingerprinted template that
  tests copy and rebind (`repo_template` / `clone_repo_template`), which cut it
  to ~2.2s. `setup_repo_the_slow_way` still exists for tests that assert on
  setup itself.
- **The template is fingerprinted on the greenline script**, so any candidate
  touching it rebuilds the template once (~10s). That is deliberate: a stale
  template would test the wrong setup output.
- **A `submit` runs ~34 git subprocesses.** Removing one is worth ~0.4s per
  submit under load, and the suite performs ~90 of them. Cheap wins already
  taken: no submodule sync without `.gitmodules`, memoized common dir, and the
  gate's crash self-heal only when checkout actually refuses.

# Gotchas

- **The gate squash-merges.** After a candidate lands, the same worktree's next
  submission re-applies changes already on main and hits a merge conflict.
  Rebase onto main (or start a fresh worktree) between landings.
- **Deadline tests must rendezvous on evidence, never on a guessed budget.**
  A fixed small deadline is a measurement of machine speed: under load the
  pre-stage git work outruns it and the deadline lands in the wrong stage. Arm a
  generous bound, wait for proof the stage is running (its own pid file or
  marker), then shorten the live deadline — `expire_deadline_when` in
  `tests/test_greenline.py`. Note `run_shell_timed` re-reads the authoritative
  deadline every loop, which is what makes that work; `operation_timeout` alone
  only samples it at stage start.
- **Keep the gate's caches gitignored.** Untracked `__pycache__`/`.pytest_cache`
  in the persistent gate worktree would look like unknown paths to
  `scripts/impact_select.py`, which fails closed and would silently run the
  whole suite on every release forever.
