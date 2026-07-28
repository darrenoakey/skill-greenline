![](banner.jpg)

# greenline

A local, serialized gated CI/CD system for solo-dev Mac environments where multiple AI agents work simultaneously in git worktrees. Every merge to `main` goes through one exclusive gate that runs your full check suite and performs a real production deploy — so `main` is always green, always deployed, and always clean.

## Purpose

When AI agents work in parallel on the same repository, pushing directly to `main` becomes dangerous: half-finished work, failed tests, and broken deploys accumulate fast. greenline solves this by enforcing a single invariant: **`main` == deployed == green, always.**

It does this by:

- Keeping the canonical repo checkout on `main`, pristine, untouched by any agent or human
- Routing all work through isolated git worktrees branched from the last known-good commit
- Serializing every merge through one gate that squash-merges, checks, deploys, and publishes — or rolls back cleanly if anything fails

## Installation

greenline is a skill built into ChatGPT/Codex. No separate installation is required. The executable lives at `~/.claude/skills/greenline/greenline` and is symlinked at `~/bin/greenline`.

To set up a repository to use greenline, run the setup command from inside the repo:

```bash
greenline setup
```

This writes `greenline.toml`, creates the gate worktree, installs git hooks that hard-lock `main`, and runs a doctor check. It is idempotent — safe to run more than once.

## Commands

| Command | What it does |
|---|---|
| `greenline setup` | Set up a repo for greenline. Installs hooks, config, and state. |
| `greenline worktree NAME` | Create a new worktree and branch `gl/NAME` off the last green commit. |
| `greenline submit [BRANCH]` | Run a branch through the gate: check + deploy. Blocks until the lock is free. |
| `greenline adopt` | Gate the current `main` tip in place — for commits that arrived outside the gate. |
| `greenline done` | Remove a merged worktree and delete its branch. |
| `greenline status` | Show lock state, SHA drift, last journal entries, and gate health. |
| `greenline doctor [--fix]` | Check all invariants; `--fix` recovers from a crashed gate. |
| `greenline deploy-pending` | Deploy a gated `main` whose deploy was deferred (only relevant with `coalesce_deploys = true`). |

Add `-v` / `--verbose` to any command for full git and command output.

## How to Use

### Setting up a new repo

```bash
cd ~/src/myproject
greenline setup
```

After setup, `main` is hard-locked — no agent or human can push to it directly. All work must go through the gate.

### Starting work

Create a worktree for your task. This branches from the last known-good commit:

```bash
greenline worktree my-feature
# prints: /Users/you/src/myproject/.worktrees/my-feature
```

Work in that directory. Commit freely. When ready to merge:

```bash
cd /Users/you/src/myproject/.worktrees/my-feature
greenline submit
```

The gate squash-merges your branch, runs `./run check`, fast-forwards `main`, runs `./run deploy`, and publishes. If check fails, `main` is untouched. If deploy fails, prod is rolled back to the previous green commit automatically.

### Checking what's happening

```bash
greenline status
```

Shows who holds the gate lock (and whether that process is still alive), SHA drift between `main`, `origin`, `last-green`, and `deployed`, and the last few gate journal entries.

### Recovering from a crashed gate

```bash
greenline doctor --fix
```

Acquires the lock and runs a preflight reconcile. Recovers from interrupted gate runs purely from journal state and git history — no manual intervention needed in most cases.

### Handling commits that arrived outside the gate

If a commit landed on `main` via some other path (a hotfix, legacy workflow, or direct push), adopt it through the gate before doing more work:

```bash
greenline adopt
```

This runs check + deploy against the current `main` tip without resetting or discarding anything.

### Cleaning up after a merge

Once your branch has been gated and merged:

```bash
greenline done
```

Removes the worktree and deletes the `gl/my-feature` branch.

## Examples

**Typical agent workflow:**

```bash
# Create isolated workspace
greenline worktree add-auth

# Work in the worktree
cd $(greenline worktree add-auth 2>/dev/null || echo .worktrees/add-auth)
# ... make changes, commit ...

# Submit through the gate (blocks if another submission is running)
greenline submit

# Clean up
greenline done
```

**Checking gate state while waiting:**

```bash
greenline status
# Lock: held by PID 12345 (alive) — greenline submit gl/other-feature
# SHA drift: main=a1b2c3 last-green=a1b2c3 deployed=a1b2c3 OK
# Last gate: gl/other-feature PASS 14m ago
```

**Submitting from outside the worktree:**

```bash
# Must pass the branch explicitly when using --repo
greenline submit gl/my-feature --repo /Users/you/src/myproject
```

**Running submit detached from any tool timeout:**

```bash
nohup greenline submit > submit.log 2>&1 &
tail -f submit.log
```

## The contract your repo must fulfil

greenline calls two scripts in your repo:

- **`./run check`** — run from the gate worktree. Must build, lint, and run the full test suite against a test datastore. Exit code is the verdict. Must be safe to run concurrently from multiple worktrees.
- **`./run deploy`** — run from the canonical checkout. Must rebuild and restart production, health-check, and exit nonzero if unhealthy. Must be idempotent.
- **`./run health`** *(optional)* — a probe with no side effects. If absent, greenline re-runs `deploy` as the health probe.

## Key rules for agents working in greenline repos

- **Never edit the canonical checkout.** All edits go in a worktree.
- **Never push directly to `main`.** The pre-push hook blocks it; the reference-transaction hook makes it impossible even without `--no-verify`.
- **Always use `greenline worktree`** to start work, not raw `git worktree add`.
- **Submit detached from any session timeout** — use `nohup` or a background process so a harness timeout cannot SIGTERM the gate mid-check.
- **When `--repo` is passed to `greenline submit`, always pass the branch explicitly** — without it, submit resolves to `main` and fails immediately.

## License

This project is licensed under [CC BY-NC 4.0](https://darren-static.waft.dev/license) - free to use and modify, but no commercial use without permission.
