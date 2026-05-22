# Changelog

All notable changes to this project will be documented here.
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [0.3.0] - 2026-05-22

### Added
- `PING` command: BBS replies `PONG` with hop count and the full mesh relay path, with node pubkey prefixes resolved to contact names where known
- `WHO` command now shows last known hop count per user (e.g. `alice (5min ago 2hop)`)
- Weather output now includes a Unicode icon matching the WMO weather code and a daily precipitation probability percentage (e.g. `⛅ partly cloudy 22°C 💧40% 💨NE 15km/h`)

### Changed
- `last_hops` column added to `users` table (DB migration 2) — stored on every inbound message

---

## [0.2.0] - 2026-05-22

### Added
- `WHO` command: lists the 5 most recently active users with relative time
- Admin DM notification when a new user connects for the first time
- Hop-proportional rate limiting: direct (hops=0) and admins bypass limits entirely; 1 hop → 4× limit, 2 hops → 2×, 3+ hops → base limit
- Inbound message logging now includes display name, pubkey prefix, hop count, and body preview
- Command dispatch logging: verb and caller logged on every command

### Changed
- Weather switched from Bureau of Meteorology to [Open-Meteo](https://open-meteo.com) — free, no API key, no blocking; configured via lat/lon instead of BOM station ID
- Docker build migrated from pip to uv; added a `test` stage that runs the full suite on every build
- `STATUS` help text now explicitly mentions outbound queue depth
- Version read from package metadata (`importlib.metadata`) — single source of truth in `pyproject.toml`

### Fixed
- BOM 403 errors on news/weather fetches (now resolved by switching weather source; BOM RSS feeds can be removed from config)
- `VACUUM` crash: `cannot VACUUM from within a transaction` — now commits before vacuuming and swallows lock errors with a warning
- `docker restart` not picking up new images — documented and replaced with `docker compose ... up -d`

---

## [0.1.0] - 2026-05-19

Initial release.

- Public message boards (`BOARDS`, `READ`, `POST`)
- Asynchronous user-to-user mail (`SEND`, `INBOX`, `READMAIL`, `DELETE`)
- News headlines from configurable RSS feeds (`NEWS`)
- Weather lookup from Bureau of Meteorology (`WX`)
- Admin commands: `BAN`, `UNBAN`, `BOARD ADD/DEL`, `BROADCAST`
- Onboarding flow — first-time users pick a display name
- Sliding-window rate limiting, audit logging, persistent outbound queue
- Health endpoint, optional Prometheus metrics
- MeshCore 2.3.x transport with poll fallback
