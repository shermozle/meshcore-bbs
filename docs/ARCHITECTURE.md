# Architecture

A summary of how the BBS is wired together. For the design rationale see the spec at the repository root.

---

## Process model

A single `asyncio` Python process. Inside it:

```
   ┌─────────────────────────────────────────────────────────────┐
   │                       BBS process                           │
   │                                                             │
   │  Transport ──events queue──> Dispatcher ──> Service modules │
   │     ▲                            │                          │
   │     │                            ▼                          │
   │     │                       (enqueue reply)                 │
   │     │                            │                          │
   │     │                            ▼                          │
   │     └─────send_msg──── Outbound queue worker ───┐           │
   │                                                 │           │
   │  Scheduled jobs ────────enqueue notifications───┘           │
   │     - news refresh                                          │
   │     - mail notify                                           │
   │     - contact prune                                         │
   │     - DB vacuum                                             │
   │     - audit prune                                           │
   │     - time resync                                           │
   │                                                             │
   │  Health HTTP server (port 8080)                             │
   │  Metrics HTTP server (port 9090, optional)                  │
   │                                                             │
   │  SQLite (WAL) backs:                                        │
   │     - users, sessions, boards, posts, mail                  │
   │     - news_feeds, news_items, weather_cache                 │
   │     - rate_limits, audit_log, outbound_queue                │
   └─────────────────────────────────────────────────────────────┘
```

All blocking work is async. No thread pool. The only blocking-looking call is SQLite (via `aiosqlite`, which is async).

---

## Module layout

```
src/bbs/
├── __init__.py            Version string
├── __main__.py            Entry point + signal handling
├── config.py              YAML config + dataclasses + SIGHUP reload
├── db.py                  aiosqlite layer + migrations
├── models.py              Domain dataclasses (User, Mail, etc.)
├── commands.py            Command parser + help text
├── dispatcher.py          Inbound→command routing
├── onboarding.py          First-contact name flow
├── rate_limit.py          Sliding-window limiter
├── outbound.py            Persistent queue worker
├── format.py              Packet splitting (140-byte budget)
├── scheduler.py           asyncio.Task loops for background jobs
├── health.py              /health + /metrics HTTP server
├── log.py                 Logging setup
├── services/
│   ├── boards.py          Public message boards
│   ├── mail.py            User-to-user mail + notifications
│   ├── news.py            RSS ingestion
│   ├── weather.py         BoM JSON feeds
│   └── admin.py           Admin commands
└── transport/
    ├── base.py            Transport protocol + event types
    ├── meshcore.py        Real meshcore_py wrapper
    └── mock.py            In-memory test/dev transport
```

---

## Inbound message lifecycle

```
   MeshCore radio
        │
        ▼
   companion firmware  ── decrypts using shared X25519 secret
        │
        ▼ (USB-serial)
   meshcore_py
        │
        ▼ (callback)
   MeshCoreTransport._on_contact_msg
        │
        ▼ (asyncio.create_task)
   _handle_contact_msg
        │  - resolves pubkey_prefix → full pubkey via contact cache
        │  - drops if it's a loopback message from self
        ▼
   events queue (asyncio.Queue)
        │
        ▼
   __main__._event_pump
        │
        ▼
   Dispatcher.handle_inbound
        │  - upsert user, touch last_seen
        │  - check ban
        │  - check inbound rate limits
        │  - if not onboarded → onboarding flow
        │  - else → command parse + dispatch
        ▼
   Service module / handler
        │
        ▼
   Dispatcher._enqueue_reply
        │  - split into packets if needed
        │  - INSERT into outbound_queue table
        ▼ (done with this inbound; dispatcher returns)
   OutboundWorker._tick (separate task, polling)
        │  - claim_next_outbound
        │  - apply per-recipient + global throttle
        │  - transport.send_msg → meshcore_py
        │     - which handles ACK retry + flood fallback
        ▼
   Mark sent | reschedule with backoff | mark failed after N tries
```

---

## Authentication model

There is no application-layer authentication. A user's identity is the 32-byte Curve25519 public key embedded in their MeshCore contact record. The companion firmware verifies the encrypted DM packet using a shared secret derived from each party's key, so by the time `CONTACT_MSG_RECV` reaches the host, the message has been cryptographically authenticated.

Implications:

- The BBS never asks for or stores passwords.
- A user changing their `adv_name` doesn't change their identity.
- Display names are application-layer aliases stored in the `users` table, mapped 1:1 to pubkeys.
- Banning by pubkey is effective; a new pubkey is trivially generated, so blocklists are best-effort.

See spec §13 for the full security threat model.

---

## Data lifetime

| Data | Retention | Where |
|---|---|---|
| Users | Forever (until manual delete) | `users` |
| Board posts | Forever (soft-deleted by admin) | `board_posts` |
| Mail (unread) | Forever | `mail` |
| Mail (read) | 90 days (configurable) | `mail` |
| News items | Most-recent 50 per feed | `news_items` |
| Weather cache | 10 min (obs) / 1 hour (forecast) | `weather_cache` |
| Audit log | 90 days | `audit_log` |
| Rate limit windows | Until window expires | `rate_limits` |
| Outbound queue rows | Sent rows kept indefinitely (for audit); pending dropped after 24h | `outbound_queue` |

---

## Failure handling

| Failure | Detection | Recovery |
|---|---|---|
| Serial unplugged | `DISCONNECTED` event from library | Library auto-reconnect; health endpoint goes 503 after 10 min |
| Companion firmware hung | No events for 10 min, health fails | Container restart by Docker/Unraid |
| Mesh path lost | `send_msg` returns NO_ACK | Outbound queue retries with backoff |
| Contact list full | Capacity check in scheduled job | Evict oldest unseen contacts |
| Feedback loop (BBS replies to itself) | Compare `pubkey_prefix` to own | Drop silently in dispatcher and transport |
| DB corruption | SQLite error on startup | Restore from nightly backup |
| Disk full | Write errors on every insert | Health fails. Manual cleanup. |
| Config syntax error on SIGHUP reload | YAML parse error | Keep running with previous config |
| Clock skew | Periodic drift | Time resync every 6 hours |

---

## Concurrency model

- One event pump task reads inbound events.
- One outbound worker task drains the queue.
- N scheduled job tasks (one per job).
- The aiohttp server runs in the same loop, on its own task.
- aiosqlite uses a dedicated thread internally — concurrent awaits are serialised.

All shared state is in SQLite. No in-memory locks needed. The only non-DB state is per-request: the rate limiter's last-notify timestamps (lost on restart, which is intentional — the durable path is the DB).
