# Deployment

This guide covers running the MeshCore BBS in production. Three deployment paths:

1. **Docker on a generic Linux host** — quickest path
2. **Docker on Unraid** — the primary target
3. **Bare metal / systemd** — if you don't want Docker

All three require a MeshCore **companion** identity the BBS can talk to:

- **USB serial** (default) — companion firmware on a device attached to the Docker host via USB, or  
- **TCP** — a companion exposed by [pyMC_Repeater](https://github.com/pyMC-dev/pyMC_Repeater) on the LAN ([PYMC_MIGRATION.md](PYMC_MIGRATION.md)).

---

## 1. Prerequisites

### Hardware

- A MeshCore-compatible companion device flashed with **companion** firmware (not repeater, not room-server). Supported hardware includes:
  - **Heltec T114** (nRF52840 + SX1262) — USB-C, presents as `/dev/ttyACM0`, uses CDC ACM (no extra driver needed on Linux 5.x+)
  - **Heltec V3** (ESP32-S3 + SX1262) — USB-C, CH9102X chip, presents as `/dev/ttyUSB0`
  - **RAK4631** (nRF52840 + SX1262) — USB-C, CDC ACM, presents as `/dev/ttyACM0`
  - **T-Deck** (ESP32-S3) — USB-C, presents as `/dev/ttyUSB0`
- USB cable to the host. Quality matters — a flaky data cable causes intermittent serial drops. Use the cable that shipped with the device or a known-good data cable (not a charge-only cable).
- A proper external antenna with known-good SWR. The companion sitting inside a server case has terrible range; mount the antenna outside.

#### Heltec T114 notes

The T114 uses the nRF52840 USB CDC ACM interface (no separate USB-serial chip). On Linux 5.x+ no driver installation is needed. The device path will be:

```
/dev/ttyACM0   (or /dev/ttyACM1 if another ACM device is already present)
```

The stable by-id path looks like:
```
/dev/serial/by-id/usb-Adafruit_nRF_Open_DFU_Bootloader_...-if00
```
or when running companion firmware:
```
/dev/serial/by-id/usb-MeshCore_Companion_...-if00
```

Run `ls -l /dev/serial/by-id/` after plugging in to find the exact path. If nothing appears, check `dmesg | tail -20` — you should see a `cdc_acm` line when the device enumerates.

### Host

- Linux with a working USB stack.
- Docker 20.10+ (for paths 1 and 2), or Python 3.11+ (for path 3).
- The companion appearing as `/dev/ttyUSB0`, `/dev/ttyACM0`, or similar.

### Verify the companion is reachable

```bash
ls -l /dev/serial/by-id/
# ESP32-based (Heltec V3, T-Deck):
# usb-Silicon_Labs_CP2102N_USB_to_UART_Bridge_Controller_abc123-if00-port0 -> ../../ttyUSB0
# nRF52840-based (Heltec T114, RAK4631):
# usb-MeshCore_Companion_abc123-if00 -> ../../ttyACM0
```

Note the full `/dev/serial/by-id/...` path. Use it everywhere instead of `/dev/ttyUSB0` — the by-id path is stable across reboots and re-plugs.

---

## 2. Docker on generic Linux

### 2.1 Build the image

```bash
git clone https://github.com/shermozle/meshcore-bbs.git
cd meshcore-bbs
docker build -t meshcore-bbs:latest .
```

### 2.2 Prepare the data directory

The container persists everything to `/data`:

```bash
mkdir -p ./data
cp config/config.example.yaml ./data/config.yaml
```

Edit `./data/config.yaml`:

- `bbs.name`: your BBS's display name (shown in the welcome message).
- `bbs.admin_pubkeys`: at least one full hex pubkey. Yours, from a MeshCore client. To find it: in the official MeshCore client, open your contact / profile screen and copy the full public key.
- `device.serial_path`: leave as `/dev/ttyUSB0` (the container path).
- `device.expected_pubkey`: **optional but recommended** — paste your companion's pubkey here. If the connected device's pubkey doesn't match at startup, the BBS refuses to run. Prevents talking to the wrong device after a firmware swap.

### 2.3 Run

Replace the `by-id` path with yours from §1:

```bash
docker run -d \
  --name meshcore-bbs \
  --restart unless-stopped \
  --device=/dev/serial/by-id/usb-Silicon_Labs_CP2102N_..._-if00-port0:/dev/ttyUSB0 \
  --group-add dialout \
  -v $(pwd)/data:/data \
  -p 8080:8080 \
  meshcore-bbs:latest
```

If you want metrics on :9090 add `-p 9090:9090` and set `metrics.enabled: true` in the config.

### 2.4 Verify

```bash
docker logs -f meshcore-bbs
# You should see:
#   starting meshcore-bbs ...
#   applying migration 1
#   health server listening on 0.0.0.0:8080
#   BBS ready. self_pubkey=...
```

Check health:

```bash
curl -fsS http://localhost:8080/health
# {"status": "ok"}
```

DM your BBS from a MeshCore client. The first message you send should produce the onboarding prompt.

### 2.5 With docker compose

The included `docker-compose.yml` does the same thing:

```bash
# Edit the device path in docker-compose.yml first.
docker compose up -d
docker compose logs -f
```

---

## 3. Docker on Unraid

### 3.1 Add the container

From the Unraid web UI:

1. **Docker** tab → **Add Container**.
2. **Name**: `meshcore-bbs`
3. **Repository**: `meshcore-bbs:latest` (if you built locally) or your registry URL.
4. **Network Type**: `Bridge` (or `Host` if you want easier Prometheus scraping).
5. **Console shell command**: `bash` (handy for debugging).
6. **Privileged**: leave off.
7. **Extra parameters** (Advanced view):
   ```
   --device=/dev/serial/by-id/usb-Silicon_Labs_CP2102N_..._-if00-port0:/dev/ttyUSB0 --group-add dialout
   ```
   Substitute your actual by-id path.

### 3.2 Add port mappings

| Container Port | Host Port | Protocol | Description |
|---|---|---|---|
| 8080 | 8080 | TCP | Health endpoint |
| 9090 | 9090 | TCP | (Optional) Prometheus metrics |

### 3.3 Add path mappings

| Container Path | Host Path | Mode |
|---|---|---|
| `/data` | `/mnt/user/appdata/meshcore-bbs` | RW |

### 3.4 First-run config

Before starting the container, drop the config into place from a terminal:

```bash
mkdir -p /mnt/user/appdata/meshcore-bbs
cp config/config.example.yaml /mnt/user/appdata/meshcore-bbs/config.yaml
nano /mnt/user/appdata/meshcore-bbs/config.yaml
```

### 3.5 Start

Start the container from the Docker tab. Verify with the container logs button.

### 3.6 USB renumeration notes

The `by-id` path is stable, but if you ever swap the companion for a different model (different USB-serial chip), the `by-id` will change. Update the device parameter and restart the container.

If your USB hub is unstable (under-voltage causes intermittent drops), use a powered hub. Under-voltage on the LoRa radio causes mysterious TX failures.

### 3.7 Health check integration

Unraid's container view shows the Dockerfile's `HEALTHCHECK` status. Green = OK. Red means either the serial connection is down, the DB isn't writable, or no events have flowed in the last 10 minutes.

---

## 4. Bare metal / systemd

If you don't want Docker:

```bash
sudo apt-get install -y python3.12 python3.12-venv git
git clone https://github.com/shermozle/meshcore-bbs.git /opt/meshcore-bbs
cd /opt/meshcore-bbs
python3.12 -m venv .venv
.venv/bin/pip install -e .
sudo usermod -aG dialout $(whoami)  # for serial access
mkdir -p /var/lib/meshcore-bbs
cp config/config.example.yaml /var/lib/meshcore-bbs/config.yaml
# edit /var/lib/meshcore-bbs/config.yaml
```

Then install the systemd unit (see `deploy/meshcore-bbs.service`):

```bash
sudo cp deploy/meshcore-bbs.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meshcore-bbs
sudo journalctl -u meshcore-bbs -f
```

---

## 5. Backups

The entire state lives in `/data/bbs.db`. Nightly backup:

```bash
# Use the bundled script:
docker exec meshcore-bbs /app/scripts/backup_db.sh /data /data/backups
```

Or as a host cron job (preferred — survives container destruction):

```cron
0 3 * * * sqlite3 /mnt/user/appdata/meshcore-bbs/bbs.db ".backup /mnt/user/backups/meshcore-bbs/bbs-$(date +\%F).db"
```

Retention: keep 30 daily, 12 monthly. Your Unraid backup target handles the off-host copy.

---

## 6. Updating

```bash
cd /opt/meshcore-bbs
git pull
docker build -t meshcore-bbs:latest .
docker restart meshcore-bbs
```

Schema migrations run automatically on startup; rollbacks require a DB restore from backup.

---

## 7. Operations cheat sheet

| Task | Command |
|---|---|
| Tail logs | `docker logs -f meshcore-bbs` |
| Reload config (no restart) | `docker kill -s HUP meshcore-bbs` |
| Restart | `docker restart meshcore-bbs` |
| Open a shell | `docker exec -it meshcore-bbs bash` |
| Inspect DB | `docker exec -it meshcore-bbs sqlite3 /data/bbs.db` |
| Check queue depth | `docker exec meshcore-bbs sqlite3 /data/bbs.db "SELECT status, COUNT(*) FROM outbound_queue GROUP BY status"` |
| Backup DB | `docker exec meshcore-bbs sqlite3 /data/bbs.db ".backup /data/bbs.bak"` |
| Health | `curl http://localhost:8080/health` |
| Metrics | `curl http://localhost:9090/metrics` |

---

## 8. Troubleshooting

### "Permission denied" on the serial device

The container runs as UID 1000 and is added to `dialout`. On most Debian/Ubuntu/Unraid hosts that's GID 20. If your host has a different GID for the device:

```bash
ls -l /dev/serial/by-id/usb-Silicon_Labs_*  # note the group
```

Then add `--group-add <gid>` to the run command (or `group_add` in compose).

### "Could not read self pubkey from companion SELF_INFO"

The companion isn't responding to `send_appstart`. Check:

1. Is the device actually a companion (not repeater or room-server firmware)?
2. Is another process holding the serial port? `lsof /dev/ttyUSB0`
3. Is the cable a real data cable (not power-only)?
4. Try `screen /dev/ttyUSB0 115200` and reset the device — you should see firmware output.

### BBS hangs after a while

Check `/health`. If the heartbeat threshold is exceeded, the event loop isn't getting events. Usually this means the firmware crashed (try power-cycling the companion) or the serial connection silently died (the auto-reconnect should handle this, but doesn't always).

If reconnect attempts fail, the container exits non-zero and Docker restarts it. Look for `ERROR` in the log.

### "contact list >80% full" warnings

The scheduled `contact_prune` job will evict the oldest unseen contacts. If you regularly fill the list, increase `contacts.prune_after_days` aggressiveness (lower number) or use a companion with more storage.

### Mail / posts delayed

Check `outbound_queue` depth. The throttle is intentionally conservative (1 send/sec global, 1/3sec per recipient). If depth grows unbounded, you may have too many users for the airtime budget. The circuit breaker kicks in at depth 100 by default.

### Schema migration failures

The DB version is tracked via `PRAGMA user_version`. If a migration fails mid-way, the DB is left at the previous version and the next start will retry. Restore from backup if a migration leaves the DB in a bad state.

---

## 9. pyMC repeater (TCP companion)

When the LoRa radio runs on a pyMC repeater elsewhere on your LAN, configure the BBS for TCP instead of USB serial.

### 9.1 pyMC side

1. Install and configure [pyMC_Repeater](https://github.com/pyMC-dev/pyMC_Repeater).  
2. Add a companion under `mesh.identities.companions` with your migrated `identity_key` and `tcp_port` (default **5000**).  
3. Restart `pymc-repeater` and confirm the companion TCP port is listening.

Full identity migration steps (export keys from the old USB companion) are in **[PYMC_MIGRATION.md](PYMC_MIGRATION.md)**.

### 9.2 BBS config

```yaml
device:
  connection: tcp
  tcp_host: "192.168.1.50"   # pyMC host IP (reachable from inside the container)
  tcp_port: 5000
  expected_pubkey: "<64-char-hex-pubkey>"
```

### 9.3 Docker

- **Remove** the `devices:` USB mapping when using TCP-only.  
- Ensure the container can reach `tcp_host` (use the LAN IP, not `127.0.0.1`, unless pyMC runs in the same network namespace).  
- On Linux, optional compose snippet:

  ```yaml
  extra_hosts:
    - "host.docker.internal:host-gateway"
  ```

  then `tcp_host: host.docker.internal` if pyMC listens on the Docker host.

### 9.4 Verify

Same as §2.4: logs should show `BBS ready. self_pubkey=...` with the **same** pubkey prefix as before migration if the identity was copied correctly.
