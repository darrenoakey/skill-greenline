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
