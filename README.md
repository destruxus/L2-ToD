# L2 Time of Death Timer Bot
A sophisticated, multi-server Discord bot for tracking Lineage 2 boss respawn timers with precision and automation. Built with Python and discord.py, this bot integrates with Raid-Helper and uses a secure, database-driven backend to provide a robust and scalable timing solution for any number of guilds.

## Key Features
Multi-Boss & Customization: Track default bosses (Ant Queen, Baium) and add your own server-specific custom bosses with unique timers and images.

Slash Command Interface: All commands are modern, intuitive slash commands (e.g., /tod, /boss, /overview).

Database Backend: Uses a persistent SQLite database to remember all timer and configuration states, even after a restart. All server data is kept completely separate.

Secure & Private: All sensitive credentials (like Raid-Helper API keys) are encrypted at rest in the database. The bot includes a clear privacy policy and data-wiping commands for user control.

Automated "Lost Window" Calculation: If a timer expires, the bot automatically creates a new "lost" window event with an extended duration.

Smart Automation Pause: To prevent infinite extensions, the automation for any boss is automatically paused if its timer window grows beyond 16 hours.

Admin-Friendly: A simple, DM-based configuration wizard (/configure) allows server owners to set up the bot without needing to touch any code.

## User Commands
### General Commands
/tod boss:<boss> action:<action> [timestamp:<timestamp>]
The primary command for all timer actions.

boss: The boss you want to manage (provides autocomplete).

action: Set Timer or Reset Timer.

timestamp: (Optional) A specific Discord timestamp to use for the Time of Death. If omitted, the current time is used.

Example 1: /tod boss:AQ action:Set Timer

Example 2: /tod boss:BAIUM action:Reset Timer

/boss <add|remove|list>
Allows server admins to manage their own custom boss timers.

/boss add: Opens a pop-up form to define a new custom boss.

/boss remove: Deletes a custom boss.

/boss list: Shows all default and custom bosses for the server.

/overview
Displays a public embed showing the current status of all configured boss timers for the server.

/help
Sends a private message (visible only to you) detailing all bot features and commands.

### Admin-Only Commands
(Requires "Administrator" permission on the server)

/configure
Initiates a private DM conversation to guide you through setting up the bot's required credentials for your server.

/wipe_my_data
IRREVERSIBLE. Permanently deletes all configuration and timers associated with your server from the bot's database.

/privacy
Displays the bot's privacy policy.

## Self-Hosting & Installation Guide
Follow these steps to host your own instance of the bot.

### 1. Prerequisites
Python 3.8+

A Git client

A Discord Bot application with a Token.

The Message Content Intent enabled for your bot in the Discord Developer Portal.

### 2. Project Setup
Clone the Repository:
```
git clone <your-repository-url>
cd <your-repository-name>
```
Create a Virtual Environment:
```
python -m venv venv
# On Windows
.\venv\Scripts\Activate.ps1
# On macOS/Linux
source venv/bin/activate
```
Install Dependencies:
```
pip install -r requirements.txt
```
Generate Encryption Key:
This bot encrypts sensitive data. You need a master key for this. Run this command in your terminal:

python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Copy the long string it outputs.

Configure Environment Variables:
Create a file named .env in the root directory. This file must not be committed to Git.
```
# Your Discord Bot's Token
DISCORD_BOT_TOKEN="PASTE_YOUR_DISCORD_BOT_TOKEN_HERE"

# The master key you generated in the previous step
DATABASE_ENCRYPTION_KEY="PASTE_YOUR_GENERATED_ENCRYPTION_KEY_HERE"
```
### 3. Hosting on Render
For 24/7 operation, a free service like Render is recommended.

Push to GitHub: Create a private GitHub repository and push your project files (bot.py, requirements.txt, .gitignore, LICENSE).

Create Render Service:

Log in to Render and click New + -> Background Worker.

Connect your GitHub repository.

Name: Give your bot a unique name.

Runtime: Python 3.

Build Command: 
```
pip install -r requirements.txt
```
Start Command: 
```
python bot.py
```
Instance Type: Free.

Add Secrets:

Go to the "Environment" tab.

Click "Add Secret File".

Filename: .env

Contents: Paste the content of your local .env file.

Add Persistent Disk (CRITICAL):

This step is required to save your bot's database.

Go to the "Disks" tab.

Click "Add Disk".

Name: bot-data

Mount Path: /data

Size (GB): 1

Go back to the "Environment" tab and click "Add Environment Variable":

Key: DATABASE_FILE

Value: /data/bot_database.db

Deploy: Click "Create Background Worker". The bot will build and start.

## License
This project is licensed under the GNU Affero General Public License v3.0. A copy of the license is included in the LICENSE file in this repository. In short, any modifications to this software that are run on a network must also have their source code made available to users under the same license.
```
Copyright (C) 2025 Destruxus
```
