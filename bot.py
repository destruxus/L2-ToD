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
            duration_hours REAL,
            status TEXT,
            PRIMARY KEY (server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)
    conn.commit()
    conn.close()

# --- Central Boss Configuration Template ---
BOSS_CONFIG = {
    "AQ": { 
        "name": "Ant Queen", "respawn_hours": 17, "duration_hours": 4, 
        "lost_respawn_shift_hours": 18, "color": "#e74c3c", "emoji": "🐜",
        "imageUrl": "https://i.imgur.com/GjY2X8k.png"
    },
    "BAIUM": { 
        "name": "Baium", "respawn_hours": 125, "duration_hours": 4, 
        "lost_respawn_shift_hours": 126, "color": "#9b59b6", "emoji": "�",
        "imageUrl": "https://i.imgur.com/yS4w5Tf.png"
    }
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

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        button.label = "Processing..."
        button.disabled = True
        await interaction.response.edit_message(view=self)

    @ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Action cancelled.", view=None)

# --- Main Command Logic (Helper Functions) ---
async def _is_configured(interaction: discord.Interaction) -> bool:
    conn = db_connect()
    is_conf = conn.cursor().execute("SELECT 1 FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
    conn.close()
    if not is_conf:
        if not interaction.response.is_done():
            await interaction.response.send_message("This server has not been configured. An administrator must run `/configure` first.", ephemeral=True)
        else:
            await interaction.followup.send("This server has not been configured. An administrator must run `/configure` first.", ephemeral=True)
    return is_conf is not None

async def _find_channel_by_name(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    return discord.utils.get(guild.text_channels, name=name)

async def _process_tod(interaction: discord.Interaction, boss_key: str, timestamp: str = None):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=False)

    config = BOSS_CONFIG[boss_key.upper()]
    conn = db_connect()
    cursor = conn.cursor()
    
    server_row = cursor.execute("SELECT events_channel_id, raid_helper_api_key FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
    events_channel_id, encrypted_rh_key = server_row
    
    try:
        rh_api_key = fernet.decrypt(encrypted_rh_key).decode()
    except Exception:
        await interaction.followup.send("Error: Could not decrypt the server's API key. An admin must re-run `/configure`.", ephemeral=True); conn.close(); return
    
    tod_time = datetime.now(timezone.utc)
    if timestamp:
        match = re.search(r'<t:(\d+):.*>', timestamp)
        if match: tod_time = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        else: await interaction.followup.send("Invalid timestamp format.", ephemeral=True); conn.close(); return
    
    h = {"Authorization": rh_api_key, "Content-Type": "application/json"}
    
    existing_event_id = (cursor.execute("SELECT event_id FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper())).fetchone() or [None])[0]
    if existing_event_id:
        stop_payload = {"endTime": int(datetime.now(timezone.utc).timestamp())}
        update_url = f"https://raid-helper.dev/api/v2/events/{existing_event_id}"
        requests.patch(url=update_url, json=stop_payload, headers=h)
        await asyncio.sleep(1)

    duration_hours = config['duration_hours']
    duration_minutes = int(duration_hours * 60)
    event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
    event_unix_timestamp = int(event_start_time.timestamp())
    
    duration_text = f"{duration_hours:.0f} hours" if duration_hours >= 1 else f"{duration_minutes} minutes"

    # --- FINAL PAYLOAD using the Unix timestamp in date and time fields ---
    payload = { 
        "title": f"{config['emoji']} {config['name']} Window", 
        "leaderId": str(interaction.user.id),
        "leaderName": interaction.user.display_name,
        "date": event_unix_timestamp,
        "time": event_unix_timestamp,
        "description": f"Timer set by {interaction.user.mention}.\nWindow is open for **{duration_text}**.", 
        "templateId": "standard",
        "imageUrl": config.get("imageUrl", "none"),
        "advancedSettings": { "duration": duration_minutes }
    }
    
    create_url = f"https://raid-helper.dev/api/v2/servers/{interaction.guild_id}/channels/{events_channel_id}/event"
    response = requests.post(url=create_url, json=payload, headers=h)

    if response.status_code in [200, 201]:
        rh_response = response.json()
        new_event_id = rh_response.get('event', {}).get('id')
        if not new_event_id:
            await interaction.followup.send("❌ Event was created, but I could not get the new Event ID from Raid-Helper's response.", ephemeral=True); conn.close(); return

        event_end_time = event_start_time + timedelta(hours=duration_hours)
        cursor.execute("""
            INSERT INTO timer_states (server_id, boss_key, event_id, start_time, end_time, duration_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, boss_key) DO UPDATE SET event_id=excluded.event_id, start_time=excluded.start_time, end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
        """, (interaction.guild_id, boss_key.upper(), new_event_id, event_start_time.isoformat(), event_end_time.isoformat(), duration_hours, "active"))
        conn.commit()
        await interaction.followup.send(f"✅ New event for **{config['name']}** created! Next window opens <t:{int(event_start_time.timestamp())}:R>.")
    else:
        await interaction.followup.send(f"❌ Raid-Helper API Error on new event creation: `{response.status_code}`\n```json\n{response.text[:1500]}\n```", ephemeral=True)
    conn.close()

async def _process_reset(interaction: discord.Interaction, boss_key: str):
    await interaction.response.defer(ephemeral=True)
    config = BOSS_CONFIG[boss_key.upper()]
    conn = db_connect()
    cursor = conn.cursor()
    existing_event_id = (cursor.execute("SELECT event_id FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper())).fetchone() or [None])[0]
    if not existing_event_id:
        await interaction.followup.send(f"There is no active timer for **{config['name']}** to reset.", ephemeral=True); conn.close(); return
    server_row = cursor.execute("SELECT raid_helper_api_key FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
    encrypted_rh_key = server_row[0]
    try: rh_api_key = fernet.decrypt(encrypted_rh_key).decode()
    except Exception: await interaction.followup.send("Error: Could not decrypt the server's API key.", ephemeral=True); conn.close(); return
    h = {"Authorization": rh_api_key}
    delete_url = f"https://raid-helper.dev/api/v2/events/{existing_event_id}"
    response = requests.delete(url=delete_url, headers=h)
    if response.status_code in [200, 204, 404]:
        cursor.execute("DELETE FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
        conn.commit()
        await interaction.followup.send(f"✅ The timer for **{config['name']}** has been successfully reset.", ephemeral=True)
    else:
        await interaction.followup.send(f"❌ Failed to delete the event from Raid-Helper. API Error: `{response.status_code}`\n```json\n{response.text[:1500]}\n```", ephemeral=True)
    conn.close()

# --- Slash Command Definitions ---
@bot.tree.command(name="tod", description="Main command for boss timers.")
@app_commands.describe(boss="The boss you want to manage.", action="The action to perform: 'set' a timer or 'reset' it.", timestamp="[Optional] A specific Discord timestamp for the TOD when using the 'set' action.")
@app_commands.choices(boss=[app_commands.Choice(name=v['name'], value=k) for k, v in BOSS_CONFIG.items()], action=[app_commands.Choice(name="Set Timer", value="set"), app_commands.Choice(name="Reset Timer", value="reset")])
async def tod(interaction: discord.Interaction, boss: app_commands.Choice[str], action: app_commands.Choice[str], timestamp: str = None):
    if not await _is_configured(interaction): return
    if action.value == "set": await _process_tod(interaction, b�
