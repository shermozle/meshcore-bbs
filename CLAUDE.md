# Claude Code notes for meshcore-bbs

## Ship checklist

Every time new user-visible features or fixes are committed, go through this list before pushing:

1. **Help text** — `src/bbs/commands.py`: add verb to `HELP_OVERVIEW` and a `HELP_TOPICS` entry.
2. **User guide** — `docs/USER_GUIDE.md`: add/update the relevant command table row(s).
3. **Changelog** — `CHANGELOG.md`: add a new semver section at the top with Added/Changed/Fixed bullets.
4. **Version bump** — `pyproject.toml`: increment `version` (patch for fixes, minor for new features). This is the single source of truth; `importlib.metadata` reads it at runtime.
5. **Tests** — run `uv run pytest` before committing. All tests must pass.
6. **Commit and push** — GitHub Actions builds, runs the test stage, then pushes `ghcr.io/shermozle/meshcore-bbs:latest` on success.

## Deploying to Unraid

After GitHub Actions goes green, the new image is available on ghcr.io. Unraid picks it up manually: go to the Docker tab in the Unraid UI, click the container, and choose **Update**. Unraid handles the pull and recreate.

## Key facts

- **Serial device** on Unraid is owned by GID 0 (root), so the container runs as `user: root`.
- **Port** 8888 is the external health/metrics port (maps to container 8080).
- **Config** lives at `/mnt/user/appdata/meshcore-bbs/config.yaml` on Unraid, mounted as `/data/config.yaml`.
- **DB migrations** are automatic on startup — just bump `MIGRATIONS` in `src/bbs/db.py`.
- Weather is via Open-Meteo (free, no API key). Config uses `latitude`/`longitude`, not BOM station IDs.
- Version is read from package metadata at runtime — only change it in `pyproject.toml`.
