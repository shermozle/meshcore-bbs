# MeshCore BBS

A single-node bulletin board system reachable over [MeshCore](https://meshcore.co.uk/) direct messages. Runs in a Docker container on Unraid (or anywhere Docker runs) and talks to a USB-attached MeshCore companion device.

Authentication is implicit: a user's identity is their MeshCore Curve25519 public key, verified end-to-end by the protocol's encrypted DM channel.

## Features (v0.1)

- **Public message boards** (`BOARDS`, `READ`, `POST`)
- **Asynchronous user-to-user mail** (`SEND`, `INBOX`, `READMAIL`, `DELETE`)
- **News headlines** from configurable RSS feeds (`NEWS`)
- **Weather lookup** from Bureau of Meteorology (`WX`)
- **Admin commands** (BAN/UNBAN, BOARD ADD/DEL, BROADCAST)
- **Onboarding flow** — first-time users pick a display name
- **Rate limiting**, **audit logging**, **persistent outbound queue**
- **Health endpoint** for container orchestration
- **Prometheus metrics** (optional)

## Quick start

```bash
# Build the image
git clone https://github.com/shermozle/meshcore-bbs.git
cd meshcore-bbs
docker build -t meshcore-bbs:latest .

# Configure
mkdir -p data
cp config/config.example.yaml data/config.yaml
# Edit data/config.yaml — at minimum set bbs.admin_pubkeys

# Find your companion's stable USB-serial path
ls -l /dev/serial/by-id/

# Edit docker-compose.yml — set the device path to the one above
docker compose up -d
docker compose logs -f
```

DM your BBS from any MeshCore client. The first message produces an onboarding prompt; pick a name with `NAME <yourname>` and you're in.

## Documentation

- **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** — every command, what to type, what you'll see
- **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** — Docker, Unraid, and bare-metal deploy instructions
- **[docs/OPERATIONS.md](docs/OPERATIONS.md)** — day-to-day care: logs, admin ops, metrics, recovery
- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — module layout, data flow, failure handling
- **[meshcore-bbs-spec.md](meshcore-bbs-spec.md)** — the design spec this was built from

## Project layout

```
src/bbs/                  Application code
  __main__.py             Entry point
  config.py               YAML config loader
  db.py                   SQLite + migrations
  models.py               Domain models
  dispatcher.py           Inbound message routing
  commands.py             Command parser + registry
  rate_limit.py           Sliding-window rate limiter
  outbound.py             Persistent send queue worker
  onboarding.py           First-contact name-setting flow
  scheduler.py            Background jobs
  health.py               HTTP /health + /metrics
  format.py               Packet splitting
  services/               News, weather, boards, mail, admin
  transport/              MeshCore interface (real + mock)
tests/                    pytest suite (74 tests)
docs/                     Deployment, architecture, operations, user guide
deploy/                   systemd unit, Unraid template
scripts/                  Helper scripts (seed, backup, inspect)
```

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
PYTHONPATH=src .venv/bin/pytest

# Run locally with no hardware:
PYTHONPATH=src .venv/bin/python -m bbs --mock --config config/config.example.yaml --db /tmp/bbs.db
```

The `--mock` flag swaps in an in-memory transport so you can poke at the dispatcher without a companion device.

To populate a dev DB with sample boards, users, news, and mail:

```bash
.venv/bin/python scripts/seed_dev_db.py /tmp/bbs.db
```

## License

MIT — see [LICENSE](LICENSE).
