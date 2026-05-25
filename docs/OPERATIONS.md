# Operations

Day-to-day care and feeding of a running MeshCore BBS.

---

## Logging

Logs go to stdout (captured by Docker) and to `/data/bbs.log` (rotated daily, 14 days retained).

Levels:

| Level | When you'll see it |
|---|---|
| `DEBUG` | Every inbound/outbound, command dispatch decisions. Verbose. |
| `INFO` | Connection events, scheduled job runs, admin actions, news refresh counts. |
| `WARN` | Rate limit hits, contact capacity high, retries. |
| `ERROR` | Serial failures, unhandled exceptions, sends past max attempts. |

Set the level in `config.yaml` under `logging.level`. Reload with `kill -HUP`.

Mail bodies and full message contents are **never** logged at INFO or above. Only at DEBUG. Don't run DEBUG in production unless you're debugging a specific issue.

---

## Admin commands

Admins are identified by their full Curve25519 pubkey in `bbs.admin_pubkeys` (the config file).

| Command | Effect |
|---|---|
| `ADMIN BAN <prefix>` | Blocks a user. Their inbound is silently dropped. |
| `ADMIN UNBAN <prefix>` | Restore a banned user. |
| `ADMIN BOARD ADD <slug> <description>` | Create a new public board. |
| `ADMIN BOARD DEL <slug>` | Delete a board (posts soft-deleted). |
| `ADMIN BROADCAST <text>` | Stages a broadcast. Requires `ADMIN BROADCAST CONFIRM` within 2 min. |
| `ADVERT` | Sends a flood advertisement so the BBS is visible across the mesh. |

The web dashboard (**Overview** tab) has a **Send flood advertisement** button for the same action.

All admin actions are written to `audit_log`. To review:

```sql
SELECT ts, actor_pubkey, action, detail
FROM audit_log
ORDER BY ts DESC
LIMIT 50;
```

---

## Common ops tasks

### Make someone an admin

Edit `config.yaml`, add their pubkey to `bbs.admin_pubkeys`, then `kill -HUP <container-pid>` to reload.

### Force a news refresh

The scheduled job runs every 15 min. To trigger immediately, restart the container or wait. There's no separate trigger command in v0.1.

### See who's currently active

```sql
SELECT display_name, pubkey,
       strftime('%Y-%m-%d %H:%M:%S', last_seen, 'unixepoch') AS last_seen,
       msg_count
FROM users
WHERE banned = 0
ORDER BY last_seen DESC
LIMIT 20;
```

### Inspect the outbound queue

```sql
SELECT status, COUNT(*) FROM outbound_queue GROUP BY status;
```

Pending depth:

```sql
SELECT id, to_pubkey, attempts, status,
       enqueued_at, next_attempt
FROM outbound_queue
WHERE status = 'pending'
ORDER BY priority DESC, enqueued_at ASC
LIMIT 20;
```

### Drain the queue manually

Mark all pending as dropped (irreversible — gone is gone):

```sql
UPDATE outbound_queue SET status = 'dropped' WHERE status = 'pending';
```

### Delete a post (no user-facing delete in v0.1)

```sql
UPDATE board_posts SET deleted = 1 WHERE id = ?;
```

### Recover from "everything's broken"

Most production issues fall into:

1. **Serial died** → restart container, check cable
2. **DB corruption** → restore from backup
3. **Companion firmware crashed** → power-cycle the device
4. **Queue jammed** → check the DB, look for stuck rows or runaway retry loops in logs

Always start with `curl http://localhost:8080/health` for a quick diagnostic.

---

## Web dashboard

The health HTTP port (8080 in the container, often mapped to 8888 on Unraid) serves a browser dashboard:

- **`/dashboard`** — overview, usage charts, recent users/audit, boards/posts management, full user directory, log tail
- **`/api/status`**, **`/api/stats`**, **`/api/activity`**, **`/api/history`** — JSON for scripts or automation
- **`/api/users`** — all users with pubkey, hop count, onboarded/banned flags, and CoreScope URLs (`https://corescope.wmcd.net.au/#/nodes/{pubkey}`)
- **`/api/boards`** — `GET` list boards; `POST` `{"slug","description"}` create; `DELETE /api/boards/{slug}` remove board (soft-deletes its posts)
- **`/api/boards/{slug}/posts`** — `GET` list posts; `POST` `{"author_pubkey","body"}` add; `DELETE /api/boards/{slug}/posts/{id}` soft-delete a post
- **`/api/logs`** — tail `logging.path` (default `/data/bbs.log`)
- **`/api/logs/stream`** — Server-Sent Events live log stream

`GET /` redirects to `/dashboard`. Container health checks still use `/health` (now returns the same status fields as `/api/status`).

No authentication is built in — bind to a trusted LAN or put a reverse proxy in front if the port is reachable from the internet.

---

## Metrics (Prometheus)

Enable in config:

```yaml
metrics:
  enabled: true
  http_port: 9090
```

Restart. Then scrape `http://host:9090/metrics`.

Useful counters/gauges:

- `bbs_messages_in_total` — total inbound messages
- `bbs_messages_out_total{outcome="ok|no_ack|error"}` — outbound by outcome
- `bbs_commands_total{verb="HELP"}` — per-verb invocation counts
- `bbs_outbound_queue_depth` — current pending queue depth
- `bbs_users_total` — total users (onboarded + not)
- `bbs_serial_reconnects_total` — every time the library reconnected

Suggested alerts:

- `bbs_outbound_queue_depth > 50 for 10m` — pile-up
- `rate(bbs_serial_reconnects_total[15m]) > 0.5` — flaky cable
- `up{job="meshcore-bbs"} == 0` — process down

---

## Disk usage

The DB grows with users, mail, posts, news items, and audit entries. With moderate use (100 users, 1k posts, 30k news items, 90 days audit) it'll be well under 100 MB.

Vacuum runs weekly. To run manually:

```bash
docker exec meshcore-bbs sqlite3 /data/bbs.db "VACUUM"
```

---

## Performance

The BBS is intentionally airtime-limited, not CPU-limited. On a modern server expect:

- < 50 MB RAM steady-state
- < 1% CPU at idle
- < 5% CPU during news refresh
- Serial throughput is the bottleneck, not the host

If the host load isn't trivial, something is wrong (likely a retry loop). Check logs for repeating errors.
