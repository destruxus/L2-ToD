# L2-ToD

A multi-server Discord bot for tracking Lineage 2 raid boss respawn timers. Set a boss Time of Death (ToD) with a slash command and the bot calculates the next spawn window, keeps a live-updating overview embed in your server, and automatically rolls over missed windows.

## Features

- **Multi-server**: all data is isolated per Discord server in a persistent SQLite database.
- **Live overview embed**: the bot posts and continuously updates a timer overview in a channel you choose.
- **Lost window automation**: if a window expires without a new ToD, the bot extends the window automatically ("lost window").
- **Safety pause**: automation pauses when a window would exceed 16 hours, and posts an alert.
- **Custom bosses**: admins can add per-server bosses next to the built-in defaults.
- **Timestamp helper**: convert any time expression into Discord timestamp formats.
- **Automatic stale-server cleanup**: if the Bot is removed from a server or permanently loses access to its configured channel, that server's data is deleted automatically after a 7-day grace period.

## Default bosses

| Boss | Respawn (h) | Window (h) |
|---|---|---|
| Ant Queen | 17 | 4 |
| Epidos | 21 | 4 |
| Baium | 125 | 4 |
| Antharas | 342 | 4 |
| Valakas | 342 | 4 |
| Beleth | 342 | 4 |

Epidos must be killed first — its death triggers Beleth's spawn.

## Commands

### General
- `/tod set <boss> [timestamp]` — set the Time of Death (defaults to now).
- `/tod correction <boss> <minutes>` — shift an active timer by +/- minutes.
- `/tod reset <boss>` — clear the active timer for a boss.
- `/overview` — show a snapshot of all current boss timers.
- `/timestamp` — convert a time expression into Discord timestamps.
- `/boss list` — list all default and custom bosses.
- `/help` — list all commands.
- `/privacy` — the bot's privacy policy.

### Admin only
- `/configure` — DM wizard that sets the alerts channel and the live overview channel.
- `/boss add` / `/boss remove` — manage custom bosses.
- `/options lost_window` — enable or disable lost-window automation.
- `/wipe_my_data` — permanently delete all data for the server.

## Self-hosting

Requirements: Docker with the compose plugin, and a Discord bot token.

1. Clone this repository.
2. Create a `.env` file in the project root:

```
DISCORD_BOT_TOKEN=your-discord-bot-token
```

3. Start the bot:

```
docker compose up -d --build
```

The SQLite database is stored in `./data/` on the host via a volume, so it survives restarts and rebuilds. After code changes, redeploy with `docker compose up -d --build`.

### First-time setup per server

Invite the bot, then an administrator runs `/configure` and answers the DM wizard. Note: by default Discord may show slash commands to admins only; adjust visibility under Server Settings -> Integrations if needed.

## Peer sync (Ally1 / Ally2)

Timer commands stay unchanged. A sidecar HTTP API speaks the same pull-only format as the other TOD bots (`/v1/tod-sync`). Sync is off until `TOD_SYNC_ORIGIN` and `TOD_SYNC_SECRET` are set (use the **same secret** on every bot).

| Bot | Host port | Container port | Health URL |
|---|---|---|---|
| Ally1 | **8081** | 8080 | `http://<ally1-host>:8081/v1/tod-sync/health` |
| Ally2 | **8082** | 8080 | `http://<ally2-host>:8082/v1/tod-sync/health` |
| L2-ToD | **8083** | 8080 | `http://<l2-tod-host>:8083/v1/tod-sync/health` |

Bots on separate machines cannot use Docker's `8081-8090` scan. Paste extra base URLs (port required) in `TOD_SYNC_PEERS` on **each** side:

```
TOD_SYNC_NAME=L2-ToD
TOD_SYNC_ORIGIN=l2-tod
TOD_SYNC_SECRET=the-same-shared-secret
TOD_SYNC_PEERS=http://ALLY1_PUBLIC_IP:8081,http://ALLY2_PUBLIC_IP:8082
```

On Ally1 / Ally2, add this bot with `TOD_SYNC_PEERS=http://L2TOD_PUBLIC_IP:8083` (and allow origin `l2-tod` in their `/sync` if they still pick bosses). L2-ToD exports **every raid boss** as soon as `TOD_SYNC_PEERS` is set. Optional `/sync` on L2-ToD is only for importing their windows here.

## License

GNU Affero General Public License v3.0 — see the LICENSE file.

Copyright (C) 2025-2026 Destruxus
