# L2-ToD

A multi-server Discord bot for tracking Lineage 2 raid boss respawn timers. Set a boss Time of Death (ToD) with a slash command and the bot calculates the next spawn window, keeps a live-updating overview embed in your server, and automatically rolls over missed windows.

## Features

- **Multi-server**: all data is isolated per Discord server in a persistent SQLite database.
- **Live overview embed**: the bot posts and continuously updates a timer overview in a channel you choose.
- **Lost window automation**: if a window expires without a new ToD, the bot extends the window automatically ("lost window").
- **Safety pause**: automation pauses when a window would exceed 16 hours, and posts an alert.
- **Custom bosses**: admins can add per-server bosses next to the built-in defaults.
- **Timestamp helper**: convert any time expression into Discord timestamp formats.

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

## License

GNU Affero General Public License v3.0 — see the LICENSE file.

Copyright (C) 2025-2026 Destruxus
