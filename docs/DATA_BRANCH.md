# The `data` branch — SQLite archive off of `main` (2026-07-23)

## Why

The point-in-time archive (`data/surge.db`, ~84MB) used to be tracked on `main`
and re-committed by every pipeline run. That bloated `main`'s history and caused
a binary merge conflict on **every** PR (resolved by hand each time). The archive
is the project's crown jewel, so the fix keeps it in GitHub (free, durable) but
off the code history.

## How it works

- **`main` is code only.** `data/` is git-ignored; `data/surge.db` is no longer
  tracked.
- **The `data` branch holds exactly one file, `surge.db`, as a single orphan
  commit** (no history — the DB is a full snapshot each time, and the archive's
  real history lives *inside* the immutable rows). Each pipeline run force-pushes
  a fresh single-commit snapshot, so the branch never grows.
- **The pipeline restores it at job start** (`git fetch origin data` →
  `git cat-file -p origin/data:surge.db > data/surge.db`), runs, then **persists
  it back** via `hash-object` / `mktree` / `commit-tree` + `git push --force`.
- **The pages job** restores the same way before `surge pages-export`.

## The safety invariant — the archive can never be destroyed

The persist step has a **shrink guard**: it refuses to push a DB smaller than
90% of what's already on the branch (`::error::` + `exit 1`). So a failed
restore, a truncated run, or a bug fails the *run* — visibly — instead of
overwriting the accumulated archive with a smaller/empty DB. Worst case is a
stale dashboard until the run is fixed, never data loss.

Restore also runs an integrity check (`sqlite3` opens + has schema objects);
an invalid file is discarded so `surge init` recreates the schema, and the
shrink guard then blocks persisting that fresh (small) DB.

## First-run validation

This is a live-pipeline change that can only be fully exercised on a runner.
Before relying on the nightly schedule, trigger one manual run:

> repo → **Actions** → *surge-daily-pipeline* → **Run workflow**

Watch that the **Restore** step reports ~84MB and the **Persist** step reports a
non-shrinking size. The `data` branch was seeded with the current archive, so
the first restore already has the full DB.

## Rollback (if the data-branch path misbehaves)

1. Revert the externalization commit (restores `data/surge.db` tracking on
   `main` and the old commit-back workflow step).
2. Repopulate the tracked file from the branch:
   `git fetch origin data && git cat-file -p origin/data:surge.db > data/surge.db`
3. Commit. The pipeline is back to committing the DB on `main`.

No data is lost in either direction — the DB content is identical on the branch
and (after rollback) on `main`.
