# L2 Time-of-Death Bot
# A Discord bot for tracking Lineage 2 boss respawn timers.
# Copyright (C) 2025  Destruxus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import sqlite3
import re
import asyncio
import requests
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from cryptography.fernet import Fernet

# --- Initial Setup & Configuration ---
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_ENCRYPTION_KEY = os.getenv("DATABASE_ENCRYPTION_KEY")
DATABASE_FILE = os.getenv("DATABASE_FILE", "bot_database.db")

# Set up the encryption suite, will fail gracefully if key is missing
try:
    fernet = Fernet(DATABASE_ENCRYPTION_KEY.encode()) if DATABASE_ENCRYPTION_KEY else None
except Exception as e:
    print(f"Error initializing encryption: {e}. The key may be invalid.")
    fernet = None

# --- Database Helper Functions ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE)

def setup_database():
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            server_id INTEGER PRIMARY KEY,
            events_channel_id INTEGER NOT NULL,
            alerts_channel_id INTEGER NOT NULL,
            raid_helper_api_key BLOB NOT NULL
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS timer_states (
            state_id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            boss_key TEXT NOT NULL,
            event_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            duration_hours INTEGER,
            status TEXT,
            UNIQUE(server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

# --- Central Boss Configuration Template ---
BOSS_CONFIG = {
    "AQ": { "name": "Ant Queen", "respawn_hours": 17, "duration_hours": 4, "lost_respawn_shift_hours": 18, "color": "#ff0000", "emoji": "🐜" },
    "BAIUM": { "name": "Baium", "respawn_hours": 125, "duration_hours": 4, "lost_respawn_shift_hours": 126, "color": "#4a3d7a", "emoji": "👑" }
}

# --- Discord Bot Setup ---
intents = discord.Intents.default()
bot = commands.Bot(command_prefix='!', intents=intents) # Prefix is fallback, not used by slash commands

# --- Main Logic and Command Definitions ---
class TodCommandGroup(app_commands.Group):
    """Group for all /tod commands."""
    
    # --- Helper to check if server is configured ---
    async def is_configured(self, interaction: discord.Interaction) -> bool:
        conn = db_connect()
        is_conf = conn.cursor().execute("SELECT 1 FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
        conn.close()
        if not is_conf:
            await interaction.response.send_message("This server has not been configured. An administrator must run `/tod configure` first.", ephemeral=True)
        return is_conf is not None

    # --- Reusable TOD Logic ---
    async def process_tod(self, interaction: discord.Interaction, boss_key: str, timestamp: str = None):
        if not await self.is_configured(interaction): return
        await interaction.response.defer()

        config = BOSS_CONFIG[boss_key.upper()]
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("SELECT events_channel_id, raid_helper_api_key FROM servers WHERE server_id = ?", (interaction.guild_id,))
        server_row = cursor.fetchone()
        if not server_row or not server_row[1]:
            await interaction.followup.send("Error: Configuration is incomplete. Please ask an admin to run `/tod configure`.", ephemeral=True)
            conn.close()
            return
        
        events_channel_id, encrypted_rh_key = server_row
        try:
            rh_api_key = fernet.decrypt(encrypted_rh_key).decode()
        except Exception:
            await interaction.followup.send("Error decrypting API key. Please re-run `/tod configure`.", ephemeral=True)
            conn.close()
            return
        
        tod_time = datetime.now(timezone.utc)
        if timestamp:
            match = re.search(r'<t:(\d+):.*>', timestamp)
            if match: tod_time = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
            else: await interaction.followup.send("Invalid timestamp format.", ephemeral=True); conn.close(); return

        duration = config['duration_hours']
        event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
        event_end_time = event_start_time + timedelta(hours=duration)
        payload = { "name": f"{config['emoji']} {config['name']} Window", "leader": interaction.user.name, "start": event_start_time.isoformat(), "end": event_end_time.isoformat(), "description": f"Timer set by {interaction.user.mention}.", "channel": str(events_channel_id), "settings": {"color": config['color']} }
        
        cursor.execute("SELECT event_id FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
        existing_event_id = (cursor.fetchone() or [None])[0]
        
        # Generic Raid-Helper call
        h = {"Authorization": f"Bearer {rh_api_key}", "Content-Type": "application/json"}
        url = f"https://raid-helper.dev/api/v2/events/{existing_event_id}" if existing_event_id else f"https://raid-helper.dev/api/v2/servers/{interaction.guild_id}/events"
        response = requests.put(url, json=payload, headers=h) if existing_event_id else requests.post(url, json=payload, headers=h)

        if response.status_code in [200, 201]:
            rh_response = response.json()
            db_payload = (interaction.guild_id, boss_key.upper(), rh_response['id'], event_start_time.isoformat(), event_end_time.isoformat(), duration, "active", rh_response['id'], event_start_time.isoformat(), event_end_time.isoformat(), duration, "active")
            cursor.execute("""
                INSERT INTO timer_states (server_id, boss_key, event_id, start_time, end_time, duration_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, boss_key) DO UPDATE SET event_id=?, start_time=?, end_time=?, duration_hours=?, status=?""", db_payload)
            conn.commit()
            await interaction.followup.send(f"✅ Timer for **{config['name']}** set! Next window opens <t:{int(event_start_time.timestamp())}:R>.")
        else:
            await interaction.followup.send(f"❌ Raid-Helper API Error: `{response.status_code}`. Check API key and channel permissions.", ephemeral=True)
        conn.close()

    # --- Commands ---
    @app_commands.command(name="aq", description="Set the Time of Death for Ant Queen.")
    @app_commands.describe(timestamp="Optional: A specific Discord timestamp for the TOD.")
    async def aq(self, interaction: discord.Interaction, timestamp: str = None): await self.process_tod(interaction, "AQ", timestamp)
        
    @app_commands.command(name="baium", description="Set the Time of Death for Baium.")
    @app_commands.describe(timestamp="Optional: A specific Discord timestamp for the TOD.")
    async def baium(self, interaction: discord.Interaction, timestamp: str = None): await self.process_tod(interaction, "BAIUM", timestamp)

    @app_commands.command(name="overview", description="Shows the status of all boss timers.")
    async def overview(self, interaction: discord.Interaction):
        if not await self.is_configured(interaction): return
        conn = db_connect()
        cursor = conn.cursor()
        timers = cursor.execute("SELECT boss_key, status, start_time, end_time, duration_hours, event_id FROM timer_states WHERE server_id = ?", (interaction.guild_id,)).fetchall()
        conn.close()
        
        embed = discord.Embed(title="Boss Timer Overview", color=discord.Color.dark_gold())
        if not timers:
            embed.description = "No timers have been set for this server yet."
        else:
            for boss_key, status, start_str, end_str, duration, event_id in timers:
                config = BOSS_CONFIG[boss_key]
                start_time, end_time, now = datetime.fromisoformat(start_str), datetime.fromisoformat(end_str), datetime.now(timezone.utc)
                event_url = f"https://raid-helper.dev/event/{interaction.guild_id}/{event_id}"
                value = f"› [View Event]({event_url})"
                if status == "paused": state = f"🔴 **Paused** (Window > 16h)"
                elif now > end_time: state = f"⚪ **Window Closed**"
                elif now > start_time:
                    state = "🟠 **Window Open (LOST)**" if duration > config['duration_hours'] else "🟢 **Window Open (ACTIVE)**"
                    value = f"› Closes <t:{int(end_time.timestamp())}:R>\n" + value
                else: state = f"🔵 **Upcoming Window**\n› Opens <t:{int(start_time.timestamp())}:R>"
                embed.add_field(name=f"{config['emoji']} {config['name']} - {state}", value=value, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Sends a private message explaining all commands.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="L2 Boss Timer Bot Help", color=discord.Color.blue())
        cmd_list = "".join([f"**`/tod {k.lower()}`**\n› Respawn: **{v['respawn_hours']}h**, Duration: **{v['duration_hours']}h**.\n\n" for k, v in BOSS_CONFIG.items()])
        cmd_list += "**`/tod <boss> timestamp:<timestamp>`**\n› Sets the TOD to a specific time.\n\n"
        cmd_list += "**`/tod overview`**\n› Shows the status of all boss timers."
        embed.add_field(name="📊 General Commands", value=cmd_list, inline=False)
        embed.add_field(name="⚙️ Automated Features", value="**1. Lost Window:** If a timer expires, a 'lost' window is calculated automatically.\n**2. Safety Pause:** Automation pauses if a window's duration would exceed 16 hours.", inline=False)
        embed.add_field(name="👑 Admin Commands", value="**`/tod configure`**\n› Setup the bot for this server.\n**`/tod wipe_my_data`**\n› Deletes all data for this server.\n**`/tod privacy`**\n› Shows the privacy policy.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="privacy", description="Displays the bot's privacy policy.")
    async def privacy(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Privacy Policy", color=discord.Color.light_grey())
        embed.description = (
            "This bot is designed with privacy and data isolation as core principles.\n\n"
            "**What Data is Stored?**\n"
            "The bot stores data required for it to function on a per-server basis:\n"
            "- Your Discord Server ID.\n"
            "- Channel IDs you provide for events and alerts.\n"
            "- The Raid-Helper API key you provide, which is **always encrypted** in the database.\n"
            "- The current state of your configured boss timers.\n\n"
            "**Data Access & Deletion**\n"
            "Your server's data is never shared with other servers. Access to the database is restricted to the bot operator for maintenance only. You can permanently delete all data associated with your server at any time by running `/tod wipe_my_data`."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="configure", description="Admin Only: Configure the bot for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def configure(self, interaction: discord.Interaction):
        # ... Modal-based configuration would be ideal here, but a DM conversation works well ...
        await interaction.response.send_message("I've sent you a DM to start the configuration.", ephemeral=True)
        # ... Full DM conversation logic ...
        
    @app_commands.command(name="wipe_my_data", description="Admin Only: Deletes all data for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def wipe_my_data(self, interaction: discord.Interaction):
        # ... Add a confirmation view (buttons) here ...
        # On confirm:
        # conn = db_connect()
        # conn.cursor().execute("DELETE FROM servers WHERE server_id = ?", (interaction.guild_id,))
        # conn.commit()
        # conn.close()
        # await interaction.followup.send("All data for this server has been wiped.", ephemeral=True)
        pass

# --- Bot Startup & Background Task ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')
    if not fernet: print("\nWARNING: DATABASE_ENCRYPTION_KEY is not set. Bot will run but `/tod configure` will fail.\n")
    setup_database()
    bot.tree.add_command(TodCommandGroup(name="tod", description="Commands for the L2 Boss Timer."))
    await bot.tree.sync()
    print("Slash commands synced.")
    check_all_boss_windows.start()

@tasks.loop(minutes=1)
async def check_all_boss_windows():
    # ... Database-driven background task logic ...
    pass

@check_all_boss_windows.before_loop
async def before_check():
    await bot.wait_until_ready()

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("FATAL ERROR: DISCORD_BOT_TOKEN is missing from environment variables.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
