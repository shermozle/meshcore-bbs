# AGENTS.md — instructions for autonomous coding agents

This repo is wired up so that Linear issues can be delegated to Cursor
Cloud Agents. The human flow is documented in
[`docs/DEVELOPMENT_WORKFLOW.md`](docs/DEVELOPMENT_WORKFLOW.md). This file
is for **you**, the agent.

You MUST also follow `CLAUDE.md` (the ship checklist). The rules below
extend it; they don't replace it.

---

## What this project is

A single-node BBS reachable over the MeshCore radio mesh. It runs as a
Docker container on Unraid and talks to a USB MeshCore companion radio.
Bandwidth is tiny (hundreds of bytes per DM, single-digit-per-minute
throughput), so user-visible output must stay short. Code is Python 3.12,
async, SQLite-backed.

## Required: use uv

Use `uv` for everything Python:

```bash
uv sync --extra dev          # install deps
uv run pytest                # run tests
uv run python -m bbs --mock --config config/config.example.yaml --db /tmp/bbs.db
```

Do not call `pip`, `python -m venv`, or `pytest` directly.

## Branch and PR conventions

- **Branch name**: `cursor/<short-kebab-slug>-d1f0`. Always lowercase.
- **Base branch**: `main`.
- **Push** the branch and **open a draft PR** as soon as the change
  compiles and tests pass. Don't wait until "everything is done" —
  CI will build a `:pr-<n>` and `:dev` image off the PR head so the
  operator can smoke-test it on a real radio.
- Re-push to the same branch for follow-ups; the workflow rebuilds
  `:dev` and `:pr-<n>` on every push.

## Mandatory test pass

You MUST run `uv run pytest` locally and see it green before pushing.
A failing test blocks BOTH the production `:latest` image and the
PR's `:dev`/`:pr-<n>` image, so a red CI run wastes operator time on
the radio side.

If you change anything in `src/bbs/db.py` migrations, also bump
`MIGRATIONS` and add the migration SQL — tests will fail otherwise.

## The ship checklist (from CLAUDE.md)

For user-visible changes, all of these must be done in the same PR:

1. **Help text** — `src/bbs/commands.py`: add the verb to
   `HELP_OVERVIEW` and a `HELP_TOPICS` entry.
2. **User guide** — `docs/USER_GUIDE.md`: add or update the command
   table row(s).
3. **Changelog** — `CHANGELOG.md`: add a new SemVer section at the top
   with Added / Changed / Fixed bullets.
4. **Version bump** — `pyproject.toml`: bump `version` (patch for
   fixes, minor for features). Single source of truth — runtime reads
   it via `importlib.metadata`.
5. **Tests** — `uv run pytest` passes.

Infrastructure-only changes (CI, Dockerfile, deploy compose, docs about
the dev workflow, agent instructions) are exempt from the version bump
and CHANGELOG entry. They still must pass `uv run pytest`.

## Bandwidth discipline

When adding user-facing output, keep replies under ~140 bytes where
practical. Many DMs are split into multiple packets by `src/bbs/format.py`
— check there before adding new verbose responses. Prefer short labels
and Unicode icons over English words when they save bytes (see how the
`WX` and `WHO` commands abbreviate).

## Things that are easy to get wrong

- **Don't fetch from BOM.** Weather is via Open-Meteo with `latitude`
  and `longitude` in config. There used to be a BOM station ID; it's
  gone. Don't reintroduce it.
- **Don't blocking-IO in the dispatcher.** Everything inbound is async.
  Use `httpx.AsyncClient` for HTTP, `aiosqlite` for DB.
- **Don't restart the container with `docker restart`.** It won't pick
  up a new image. Use `docker compose pull && docker compose up -d`.
- **Don't change the `:latest` tag behaviour** in
  `.github/workflows/docker.yml` without also updating
  `docs/DEVELOPMENT_WORKFLOW.md`.

## When you finish

- Commit each logical change separately, with a descriptive message.
- Push the branch.
- Open (or update) a draft PR. Body should summarise the change in 3–5
  bullets plus a "Test plan" section listing what you ran.
- Don't merge the PR — the human operator does that after testing
  the `:pr-<n>` or `:dev` image on the dev Unraid container.
