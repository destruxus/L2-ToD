# L2 Multi-Boss Timer Bot
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
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv
from cryptography.fernet import Fernet

# --- Initial Setup & Configuration ---
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_ENCRYPTION_KEY = os.getenv("DATABASE_ENCRYPTION_KEY")
DATABASE_FILE = os.getenv("DATABASE_FILE", "bot_database.db")

try:
    fernet = Fernet(DATABASE_ENCRYPTION_KEY.encode()) if DATABASE_ENCRYPTION_KEY else None
except Exception as e:
    print(f"CRITICAL ERROR: Could not initialize encryption suite: {e}. The DATABASE_ENCRYPTION_KEY may be invalid.")
    fernet = None

# --- Database Helper Functions ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE)

def setup_database():
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
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
            server_id INTEGER NOT NULL,
            boss_key TEXT NOT NULL,
            event_id INTEGER,
            start_time TEXT,
            end_time TEXT,
            duration_hours INTEGER,
            status TEXT,
            PRIMARY KEY (server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

# --- Central Boss Configuration Template ---
BOSS_CONFIG = {
    "AQ": { "name": "Ant Queen", "respawn_hours": 17, "duration_hours": 4, "lost_respawn_shift_hours": 18, "color": "#e74c3c", "emoji": "🐜" },
    "BAIUM": { "name": "Baium", "respawn_hours": 125, "duration_hours": 4, "lost_respawn_shift_hours": 126, "color": "#9b59b6", "emoji": "👑" }
}

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- UI Views ---
class ConfirmationView(ui.View):
    def __init__(self, author: discord.User):
        super().__init__(timeout=60)
        self.value = None
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author.id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return False
        return True

    @ui.button(label="Confirm Deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="Confirmed. Deleting all data for this server...", view=None)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Action cancelled.", view=None)

# --- Main Slash Command Group ---
class TodCommandGroup(app_commands.Group):
    
    async def _is_configured(self, interaction: discord.Interaction) -> bool:
        conn = db_connect()
        is_conf = conn.cursor().execute("SELECT 1 FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
        conn.close()
        if not is_conf:
            await interaction.response.send_message("This server has not been configured. An administrator must run `/tod configure` first.", ephemeral=True)
        return is_conf is not None

    async def _find_channel_by_name(self, guild: discord.Guild, name: str) -> discord.TextChannel | None:
        return discord.utils.get(guild.text_channels, name=name)

    async def _process_tod(self, interaction: discord.Interaction, boss_key: str, timestamp: str = None):
        if not await self._is_configured(interaction): return
        await interaction.response.defer(ephemeral=False)

        config = BOSS_CONFIG[boss_key.upper()]
        conn = db_connect()
        cursor = conn.cursor()
        
        server_row = cursor.execute("SELECT events_channel_id, raid_helper_api_key FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
        events_channel_id, encrypted_rh_key = server_row
        
        try:
            rh_api_key = fernet.decrypt(encrypted_rh_key).decode()
        except Exception:
            await interaction.followup.send("Error: Could not decrypt the server's API key. An admin must re-run `/tod configure`.", ephemeral=True)
            conn.close()
            return
        
        tod_time = datetime.now(timezone.utc)
        if timestamp:
            match = re.search(r'<t:(\d+):.*>', timestamp)
            if match: tod_time = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
            else: await interaction.followup.send("Invalid timestamp format.", ephemeral=True); conn.close(); return

        duration = config['duration_hours']
        event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
        event_end_time
