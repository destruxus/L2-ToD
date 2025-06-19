# L2-ToD
This is a hobby project to create a discord bot for Clan use in the Lineage 2 game, to allow to trigger on Time of death of raidbosses, and create a event in the raid-helper discord tooling for the next raidboss window.

A sophisticated, multi-server Discord bot for tracking Lineage 2 boss respawn timers with precision and automation. Built with Python and discord.py, this bot integrates with Raid-Helper and uses a secure, database-driven backend to provide a robust and scalable timing solution for any number of guilds.

## Key Features
Multi-Boss & Multi-Server: Track any number of custom-configured bosses simultaneously, with all data securely isolated per Discord server.
Slash Command Interface: All commands are modern, intuitive slash commands (e.g., /tod aq).
Configuration-Driven: Easily add, edit, or remove boss timers by modifying a central configuration dictionary in the code.
Automated "Lost Window" Calculation: If a timer expires, the bot automatically calculates and posts a "lost" window with an extended duration, providing a worst-case scenario.
Smart Automation Pause: To prevent infinite extensions, the automation for any boss is automatically paused if its timer window grows beyond 16 hours, requiring manual intervention.
Database Backend: Uses a persistent SQLite database to remember all timer and configuration states, even after a restart.
Secure & Private: All sensitive data (like Raid-Helper API keys) is encrypted at rest in the database. The bot includes clear privacy policies and data-wiping commands for user control.
Admin-Friendly: A simple, DM-based configuration wizard (/tod configure) allows server owners to set up the bot without needing to touch any code.

## User Commands
### General Commands
/tod <boss> \
Sets the Time of Death (TOD) for the specified boss to the current time. This is the primary command for resetting a timer. \
Example: /tod aq

/tod <boss> timestamp:<timestamp> \
Sets the TOD for a boss to a specific time. You can get a valid timestamp string by typing /timestamp in any Discord channel. \
Example: /tod baium timestamp:<t:1718823600:F>

/tod overview \
Displays a public embed showing the current status of all configured boss timers for the server. \ 
Aliases: /tod status, /tod timers.

/tod help \
Sends a private message (visible only to you) detailing all bot features and commands.

/tod privacy \
Displays the bot's privacy policy, explaining what data is stored and why.

### Admin-Only Commands
(Requires "Administrator" permission on the server)

/tod configure \
Initiates a private DM conversation to guide you through setting up or updating the bot's required credentials for your server (Channel IDs and Raid-Helper API Key).

/tod wipe_my_data \
IRREVERSIBLE. Permanently deletes all data associated with your server from the bot's database. A confirmation prompt is required.

## Self-Hosting & Installation Guide
Follow these steps to host your own instance of the bot.

### 1. Prerequisites
Python 3.8+
A Git client
A Discord Bot application with a Token.
A Raid-Helper API Key for your server.
2. Setup
Clone the Repository:
```
Bash

git clone <your-repository-url>
cd <your-repository-name>
```
Install Dependencies:
```
Bash

pip install -r requirements.txt
```
Generate Encryption Key:
This bot encrypts sensitive data in its database. You need a master key for this. Run the generate_key.py script (if included) or create one with the following Python code:

Python
```
from cryptography.fernet import Fernet
key = Fernet.generate_key()
print("Copy this key to your .env file:")
print(key.decode())
```
### Configure Environment Variables:
Create a file named .env in the root directory and fill it out. Do not share this file.
Code snippet
```
# Your Discord Bot's Token
DISCORD_BOT_TOKEN="PASTE_YOUR_DISCORD_BOT_TOKEN_HERE"
 The master key you generated in the previous step
# DATABASE_ENCRYPTION_KEY="PASTE_YOUR_GENERATED_ENCRYPTION_KEY_HERE"
```
Note: Server-specific settings like channel IDs are now configured via the /tod configure command after the bot is running.

### Run the Bot:

Bash

python bot.py
The first time the bot runs, it will create a bot_database.db file. The slash commands may take up to an hour to sync with Discord globally for the first time.

## 3. Hosting
For 24/7 operation, you must host the bot on a server.

Recommended: A service like Render on their free tier. You must use their Persistent Disks feature to store the bot_database.db file, preventing data loss on restarts. \
Advanced: A Virtual Private Server (VPS) where you can run the bot as a systemd service for reliability.

## Bot Management
Adding or Editing Bosses
The primary strength of this bot is its easy configuration.

Open the bot.py file.
Locate the BOSS_CONFIG dictionary at the top.
To edit a boss, change its values (e.g., respawn_hours).
To add a new boss, copy an existing boss block, paste it, and change the key (e.g., "ZAKEN") and all its associated values.
Save the file and restart the bot for changes to take effect. The /tod subcommands will update automatically.

### First-Time Setup on a New Server
When the bot joins a new server, an administrator must run /tod configure. This will trigger a private DM conversation to securely provide the server's unique settings, which are then saved to the bot's database.

## License
This project is licensed under the GNU Affero General Public License v3.0. A copy of the license is included in the LICENSE file in this repository. In short, any modifications to this software that are run on a network must also have their source code made available to users under the same license.

Copyright (C) 2025 Destruxus
