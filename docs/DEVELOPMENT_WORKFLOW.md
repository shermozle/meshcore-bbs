# Development workflow

This project uses **Linear + Cursor Cloud Agents + GitHub Actions** to turn
issues into running container images with almost no manual work in between.

```
Linear issue ──► Cursor Cloud Agent ──► PR on GitHub ──► dev image on ghcr.io ──► Unraid dev container
                                              │
                                         merge to main
                                              ▼
                                       latest image on ghcr.io ──► Unraid production container
```

This document is for the **human operator** of the repo. The Cloud Agent
side reads `AGENTS.md` and `CLAUDE.md` for its own instructions.

---

## 1. One-time setup

### a) Linear ↔ Cursor integration

1. In Linear, open **Settings → Integrations → Cursor** and authorise
   the workspace. (Cursor side: Dashboard → **Integrations → Linear**.)
2. Connect the `shermozle/meshcore-bbs` GitHub repo to Cursor under
   **Dashboard → Repositories** so cloud agents can clone and push to it.
3. Confirm Linear shows a `Cursor` (or `Delegate to Cursor`) action on
   issue context menus. That's the trigger.

### b) Cloud Agent environment

The agent runs in a clean Ubuntu VM each time. The repo's `pyproject.toml`
+ `uv.lock` are enough for it to bring up Python via `uv sync --extra dev`
and run `uv run pytest`. No extra cloud-agent setup is required for the
default `pytest` flow — only if we add steps that need credentials would
secrets need to be added under **Dashboard → Cloud Agents → Secrets**.

### c) GitHub permissions

`.github/workflows/docker.yml` declares `packages: write` on the job and
uses the default `GITHUB_TOKEN`. No PAT or extra secret is needed for
publishing to `ghcr.io/shermozle/meshcore-bbs`. Make sure the package's
visibility on ghcr.io is set to **Public** (one-time, in the package
settings on GitHub) or the Unraid pull will need a registry login.

### d) Unraid dev container

Copy `docker-compose.unraid-dev.yml` to your Unraid appdata folder and:

1. Edit the `devices:` line to point at the **dev** companion radio's
   `/dev/serial/by-id/...` path (your production radio stays attached
   to the production container).
2. Create the appdata directory and seed a config file:
   ```bash
   mkdir -p /mnt/user/appdata/meshcore-bbs-dev
   cp /mnt/user/appdata/meshcore-bbs/config.yaml \
      /mnt/user/appdata/meshcore-bbs-dev/config.yaml
   # Edit admin_pubkeys, callsign, board names so dev and prod stay distinct.
   ```
3. Bring it up:
   ```bash
   docker compose -f docker-compose.unraid-dev.yml pull
   docker compose -f docker-compose.unraid-dev.yml up -d
   ```

Health endpoint will be on port **8889** (prod stays on 8888).

---

## 2. The day-to-day loop

1. **Create a Linear issue** describing the change. Be concrete — the
   agent gets the title and description verbatim.
2. **Delegate it to Cursor** from the Linear issue menu. Cursor spins up
   a cloud agent VM, clones the repo, branches off `main` with a
   `cursor/<slug>-d1f0` name, makes the changes, runs `uv run pytest`,
   commits, pushes, and opens a draft PR.
3. **GitHub Actions** runs `.github/workflows/docker.yml` on the PR:
   - Builds the `test` stage of the Dockerfile, which runs `pytest`
     inside the same Python image the runtime uses.
   - If tests pass, builds the `runtime` stage and pushes it to
     `ghcr.io/shermozle/meshcore-bbs` with tags:
     - `:pr-<number>` — pinned to this PR's HEAD
     - `:dev` — always the most recent PR build
     - `:sha-<short>` — pinned to the exact commit
4. **Pull on Unraid dev**:
   ```bash
   docker compose -f docker-compose.unraid-dev.yml pull
   docker compose -f docker-compose.unraid-dev.yml up -d
   ```
   By default this grabs `:dev`. To pin to a specific PR while others
   are open:
   ```bash
   BBS_DEV_TAG=pr-42 docker compose -f docker-compose.unraid-dev.yml pull
   BBS_DEV_TAG=pr-42 docker compose -f docker-compose.unraid-dev.yml up -d
   ```
5. **Test over the mesh** by DMing the dev BBS from any MeshCore client.
6. If happy, **merge the PR**. The push to `main` builds `:latest` and
   `:sha-<short>`. Production update is the usual Unraid `Update` button
   on the `meshcore-bbs` container.
7. If not happy, reply to the Linear issue or push more commits to the
   branch — the workflow rebuilds `:pr-<n>` and `:dev` on every push.

---

## 3. Tag cheatsheet

| Tag           | When it moves                         | Use for                          |
| ------------- | ------------------------------------- | -------------------------------- |
| `:latest`     | push to `main` (post-merge)           | Production Unraid container      |
| `:dev`        | every PR build (overwritten)          | Dev Unraid container default     |
| `:pr-<n>`     | every push to PR `n`'s branch         | Pinning dev to a specific PR     |
| `:sha-<hex>`  | every push                            | Exact reproducibility / rollback |

---

## 4. Safety notes

- **Forked PRs don't publish images.** The workflow detects
  `pull_request.head.repo.full_name != github.repository` and skips the
  push step (`GITHUB_TOKEN` from a fork lacks `packages:write` anyway).
  Only same-repo branches — which is what Cloud Agents create — produce
  dev images.
- **Tests gate the push.** The runtime image build step only runs after
  the `test` stage build step succeeds, so a failing `pytest` blocks both
  the production `:latest` and the dev `:dev`/`:pr-<n>` images.
- **`:dev` is overwritten without coordination.** If two PRs are open
  and both get pushed to, whichever was pushed most recently wins for
  `:dev`. Pin to `:pr-<n>` when you need stability while reviewing.
- **Docs-only / compose-only PRs don't trigger rebuilds.** The workflow
  has a `paths-ignore` for `**.md`, `docs/**`, `docker-compose*.yml`,
  `LICENSE`, `deploy/**`, and `scripts/**`. A PR that mixes any of those
  with a code change still builds — the ignore only applies when *all*
  changed files match. To force a rebuild after a docs-only change
  (e.g. to retag `:dev`), push a no-op commit that touches `src/` or
  the Dockerfile.
- **Migrations run on container start** (`MIGRATIONS` in `src/bbs/db.py`).
  Keep dev and prod DBs in separate appdata directories so a schema
  change in a PR can't corrupt the production DB.
