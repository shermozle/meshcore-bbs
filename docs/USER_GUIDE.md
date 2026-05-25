# User guide

What to type when you DM the BBS from a MeshCore client.

---

## First time

Send anything (`HELP`, `hi`, whatever). You'll get:

```
Welcome to <BBS name>.
Choose a display name (1-10 chars, A-Z 0-9 _ - emoji).
Reply: NAME <yourname>
```

Pick a name:

```
NAME alice
```

You'll get `OK, you are alice. Try HELP for commands.` and you're in.

Name rules:

- 1–10 characters
- letters, digits, `_`, `-`, and emoji
- must be unique
- reserved names are blocked (`admin`, `bbs`, `system`, `me`, `all`, `help`, etc.)

You can change your name later with `NAME <new>`.

---

## Commands

Everything is case-insensitive. Arguments are space-separated.

### Info

| Command | What it does |
|---|---|
| `HELP` | List commands |
| `HELP <verb>` | Detail on one command |
| `WHOAMI` | Show your name and pubkey prefix |
| `WHO` | List the 5 most recently active users with last-seen time and hop count |
| `PING` | BBS replies `PONG` with your hop count and the mesh path taken |
| `STATUS` | BBS uptime and queue depth |

### News and weather

| Command | What it does |
|---|---|
| `NEWS` | Top 5 recent headlines across all feeds |
| `NEWS <page>` | Older headlines |
| `NEWS <feed>` | Filter to a single feed (e.g. `NEWS abc`) |
| `WX` | Weather summary for the BBS-default location (via Open-Meteo) |

News pages 5 items at a time. If there's more, you'll see `[more: NEWS 2]` at the end of the reply — just type that command to see the next page.

### Boards

| Command | What it does |
|---|---|
| `BOARDS` | List public boards |
| `READ <board>` | Newest 5 posts on a board |
| `READ <board> <page>` | Older posts |
| `POST <board> <text>` | Post to a board (up to 200 chars) |

Example:

```
BOARDS
> general: General chat
> swap: Buy / sell / trade

POST general Anyone on Mt Tomah tonight?
> OK [id=42]

READ general
> [42] alice: Anyone on Mt Tomah tonight?
> ...
```

### Mail

Asynchronous user-to-user messages.

| Command | What it does |
|---|---|
| `MAIL` | Show unread / total counts |
| `INBOX` | List inbox (unread first, then read) |
| `INBOX <page>` | Older mail |
| `READMAIL <id>` | Read a mail and mark it read |
| `SEND <user> <text>` | Send mail (exact or partial name, or pubkey prefix) |
| `DELETE <id>` | Delete a mail |

When someone sends you mail and you're **online** (the BBS has seen your node on the mesh recently — e.g. a DM, advert, or public-channel traffic within about 15 minutes), it pushes `! 1 new mail. INBOX to view.` Otherwise you'll see the count on your next BBS command.

Example:

```
SEND bob meet you at the carpark at 8?
> OK [mail=17]

MAIL
> Mail: 0 unread, 3 total.

INBOX
>  [12] alice: see you tonight
> *[15] charlie: got your card from the repeater...
>  [11] bob: 73 mate
```

`*` next to an ID means unread.

---

## Admin commands

If your pubkey is listed in the BBS config as an admin, you can use moderation commands (`ADMIN BAN`, `ADMIN BOARD ADD`, etc.) and `ADVERT` to broadcast a flood mesh advertisement. See [OPERATIONS.md](OPERATIONS.md) for the full admin command list.

---

## What you'll never see

- **URLs** — they're useless on a small text client and waste airtime. News is headlines only.
- **Real-time chat** — use MeshCore channels for that. The BBS is for asynchronous communication.
- **File transfer** — not enough airtime budget.
- **Notifications during quiet hours** — actually, no, the BBS doesn't track your timezone. It does throttle mail notifications to one every 10 minutes per recipient.

---

## Rate limits

Be a good citizen of the mesh:

| Limit | Default |
|---|---|
| Total inbound | 20 per hour, 5 per minute |
| Posts to boards | 5 per hour, 20 per day |
| Mail sends | 10 per day |
| Weather/news fetches that hit the network | 1 per minute per user |

Cache hits on news and weather don't count.

If you hit a limit you'll get `! Rate limited. Try again in 30s.`

---

## Privacy

- Messages you send to the BBS pass through MeshCore's end-to-end encryption to the BBS host. The BBS decrypts and acts on them; it doesn't re-encrypt your data at rest.
- Mail bodies are stored unencrypted on the BBS server until you `DELETE` them. Read mail is auto-deleted after 90 days by default.
- Board posts are public.
- The admin can see audit-log entries for moderation actions, but the application doesn't log message contents at INFO level.
- The BBS knows your full pubkey, your chosen display name, your `adv_name` from the MeshCore protocol, and a count of messages you've sent.

---

## When something goes wrong

| Reply | What it means |
|---|---|
| `? Unknown command. Try: HELP` | Verb not recognized |
| `! Rate limited. Try again in 30s.` | You're sending too fast |
| `! Set a name first: NAME <yourname>` | You haven't completed onboarding yet |
| `! No such user.` | Recipient not found (mail) |
| `! Name taken` | Pick a different display name |
| `! Too long, max 200 chars` | Your post or mail body is too long |
| `! BBS overloaded, try later.` | The outbound queue is full; back off |
| `! Internal error.` | Something broke. Try again. If it keeps happening, tell the admin. |
