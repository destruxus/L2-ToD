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
bot = commands.Bot(command_prefix='!', intents=intents)

# --- Confirmation View for Dangerous Actions ---
class ConfirmationView(ui.View):
    def __init__(self, author: discord.User):
        super().__init__(timeout=60)
        self.value = None
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    @ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        self.value = True
        self.stop()
        await interaction.response.edit_message(content="Confirmed. Deleting data...", view=None)

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

    async def _process_tod(self, interaction: discord.Interaction, boss_key: str, timestamp: str = None):
        if not await self._is_configured(interaction): return
        await interaction.response.defer()

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
        event_end_time = event_start_time + timedelta(hours=duration)
        payload = { "name": f"{config['emoji']} {config['name']} Window", "leader": interaction.user.name, "start": event_start_time.isoformat(), "end": event_end_time.isoformat(), "description": f"Timer set by {interaction.user.mention}.", "channel": str(events_channel_id), "settings": {"color": config['color']} }
        
        existing_event_id = (cursor.execute("SELECT event_id FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper())).fetchone() or [None])[0]
        
        h = {"Authorization": f"Bearer {rh_api_key}", "Content-Type": "application/json"}
        url = f"https://raid-helper.dev/api/v2/events/{existing_event_id}" if existing_event_id else f"https://raid-helper.dev/api/v2/servers/{interaction.guild_id}/events"
        response = requests.put(url, json=payload, headers=h) if existing_event_id else requests.post(url, json=payload, headers=h)

        if response.status_code in [200, 201]:
            rh_response = response.json()
            cursor.execute("""
                INSERT INTO timer_states (server_id, boss_key, event_id, start_time, end_time, duration_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, boss_key) DO UPDATE SET event_id=excluded.event_id, start_time=excluded.start_time, end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
            """, (interaction.guild_id, boss_key.upper(), rh_response['id'], event_start_time.isoformat(), event_end_time.isoformat(), duration, "active"))
            conn.commit()
            await interaction.followup.send(f"✅ Timer for **{config['name']}** set! Next window opens <t:{int(event_start_time.timestamp())}:R>.")
        else:
            await interaction.followup.send(f"❌ Raid-Helper API Error: `{response.status_code}`. Check API key and channel permissions.", ephemeral=True)
        conn.close()

    @app_commands.command(name="aq", description="Set the Time of Death for Ant Queen.")
    @app_commands.describe(timestamp="Optional: A specific Discord timestamp for the TOD.")
    async def aq(self, interaction: discord.Interaction, timestamp: str = None): await self._process_tod(interaction, "AQ", timestamp)
        
    @app_commands.command(name="baium", description="Set the Time of Death for Baium.")
    @app_commands.describe(timestamp="Optional: A specific Discord timestamp for the TOD.")
    async def baium(self, interaction: discord.Interaction, timestamp: str = None): await self._process_tod(interaction, "BAIUM", timestamp)

    @app_commands.command(name="overview", description="Shows the status of all boss timers.")
    async def overview(self, interaction: discord.Interaction):
        if not await self._is_configured(interaction): return
        conn = db_connect()
        timers = conn.cursor().execute("SELECT boss_key, status, start_time, end_time, duration_hours, event_id FROM timer_states WHERE server_id = ?", (interaction.guild_id,)).fetchall()
        conn.close()
        
        embed = discord.Embed(title="Boss Timer Overview", color=discord.Color.dark_gold(), timestamp=datetime.now(timezone.utc))
        if not timers:
            embed.description = "No timers have been set for this server yet. Use `/tod <boss>` to start one."
        
        for boss_key, status, start_str, end_str, duration, event_id in sorted(timers, key=lambda x: x[2]):
            config = BOSS_CONFIG[boss_key]
            start_time, end_time, now = datetime.fromisoformat(start_str), datetime.fromisoformat(end_str), datetime.now(timezone.utc)
            event_url = f"https://raid-helper.dev/event/{interaction.guild_id}/{event_id}"
            
            value = f"› [View Event]({event_url})"
            if status == "paused":
                state, value = f"🔴 Paused", f"*Window exceeded 16h. Automation is stopped.*\n{value}"
            elif now > end_time:
                state, value = f"⚪ Window Closed", f"*Waiting for the next automated update.*\n{value}"
            elif now > start_time:
                state = "🟠 Open (LOST)" if duration > config['duration_hours'] else "🟢 Open (ACTIVE)"
                value = f"› Closes <t:{int(end_time.timestamp())}:R>\n{value}"
            else:
                state = "🔵 Upcoming"
                value = f"› Opens <t:{int(start_time.timestamp())}:R>\n{value}"
            embed.add_field(name=f"{config['emoji']} {config['name']} - {state}", value=value, inline=False)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="help", description="Sends a private message explaining all commands.")
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="L2 Boss Timer Bot Help", color=discord.Color.blue())
        cmd_list = "".join([f"**`/tod {k.lower()}`**\n› Respawn: **{v['respawn_hours']}h**, Duration: **{v['duration_hours']}h**.\n\n" for k, v in BOSS_CONFIG.items()])
        cmd_list += "**`/tod <boss> timestamp:<timestamp>`**\n› Sets the TOD to a specific time.\n\n**`/tod overview`**\n› Shows the status of all boss timers."
        embed.add_field(name="📊 General Commands", value=cmd_list, inline=False)
        embed.add_field(name="⚙️ Automated Features", value="**1. Lost Window:** If a timer expires, a 'lost' window is calculated automatically.\n**2. Safety Pause:** Automation pauses if a window's duration exceeds 16 hours.", inline=False)
        embed.add_field(name="👑 Admin Commands", value="**`/tod configure`**\n› Setup the bot for this server.\n**`/tod wipe_my_data`**\n› Deletes all data for this server.\n**`/tod privacy`**\n› Shows the privacy policy.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="privacy", description="Displays the bot's privacy policy.")
    async def privacy(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Privacy Policy", color=discord.Color.light_grey())
        embed.description = ("This bot is designed with privacy as a core principle.\n\n"
                             "**What Data is Stored?**\n- Your Discord Server ID & configured Channel IDs.\n- The Raid-Helper API key you provide, which is **always encrypted**.\n- The current state of your boss timers.\n\n"
                             "**Data Access & Deletion**\nYour server's data is never shared. You can permanently delete all data for your server at any time by running `/tod wipe_my_data`.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="configure", description="Admin Only: Configure the bot for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def configure(self, interaction: discord.Interaction):
        if not fernet:
            await interaction.response.send_message("❌ **Configuration Error:** The bot operator has not set a `DATABASE_ENCRYPTION_KEY`. This command is disabled.", ephemeral=True)
            return
        
        await interaction.response.send_message("I've sent you a DM to start the configuration.", ephemeral=True)
        dm_channel = await interaction.user.create_dm()
        
        questions = { "events_channel_id": "What is the **Channel ID** for Raid-Helper events?", "alerts_channel_id": "What is the **Channel ID** for bot alerts?", "raid_helper_api_key": "What is your **Raid-Helper API Key**?" }
        answers = {}

        for key, question in questions.items():
            try:
                await dm_channel.send(question)
                def check(m): return m.author == interaction.user and m.channel == dm_channel
                msg = await bot.wait_for('message', timeout=300.0, check=check)
                answers[key] = msg.content
                await dm_channel.send(f"✅ Set **{key}** to `{'******' if 'key' in key else msg.content}`")
            except asyncio.TimeoutError: return await dm_channel.send("Configuration timed out.")

        try:
            encrypted_key = fernet.encrypt(answers['raid_helper_api_key'].encode())
            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO servers (server_id, events_channel_id, alerts_channel_id, raid_helper_api_key) VALUES (?, ?, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET events_channel_id=excluded.events_channel_id, alerts_channel_id=excluded.alerts_channel_id, raid_helper_api_key=excluded.raid_helper_api_key;
            """, (interaction.guild_id, int(answers['events_channel_id']), int(answers['alerts_channel_id']), encrypted_key))
            conn.commit()
            conn.close()
            await dm_channel.send("✅ **Configuration saved securely!**")
        except Exception as e: await dm_channel.send(f"❌ **Error saving configuration!** Check that your channel IDs are correct.\n`{e}`")

    @app_commands.command(name="wipe_my_data", description="Admin Only: Deletes all data for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    async def wipe_my_data(self, interaction: discord.Interaction):
        view = ConfirmationView(author=interaction.user)
        await interaction.response.send_message("**Are you absolutely sure?** This will permanently delete all timers and configuration for this server.", view=view, ephemeral=True)
        await view.wait()
        if view.value:
            conn = db_connect()
            conn.cursor().execute("DELETE FROM servers WHERE server_id = ?", (interaction.guild_id,))
            conn.commit()
            conn.close()

# --- Automated Background Task ---
@tasks.loop(minutes=1)
async def check_all_boss_windows():
    conn = db_connect()
    cursor = conn.cursor()
    active_timers = cursor.execute("SELECT server_id, boss_key, start_time, end_time, duration_hours, event_id FROM timer_states WHERE status = 'active'").fetchall()
    
    for server_id, boss_key, start_str, end_str, duration, event_id in active_timers:
        end_time = datetime.fromisoformat(end_str)
        if datetime.now(timezone.utc) > end_time:
            server_config = cursor.execute("SELECT alerts_channel_id, raid_helper_api_key FROM servers WHERE server_id = ?", (server_id,)).fetchone()
            if not server_config: continue
            
            alerts_channel_id, encrypted_rh_key = server_config
            rh_api_key = fernet.decrypt(encrypted_rh_key).decode()
            config = BOSS_CONFIG[boss_key]
            new_duration = duration + 4

            if new_duration > 16:
                cursor.execute("UPDATE timer_states SET status = 'paused' WHERE server_id = ? AND boss_key = ?", (server_id, boss_key))
                alert_channel = bot.get_channel(alerts_channel_id)
                if alert_channel: await alert_channel.send(f"🔥 The **{config['name']}** window has exceeded 16h and is now **paused**. Use `/tod {boss_key.lower()}` to reset.")
            else:
                start_time = datetime.fromisoformat(start_str)
                new_start_time = start_time + timedelta(hours=config['lost_respawn_shift_hours'])
                new_end_time = new_start_time + timedelta(hours=new_duration)
                payload = { "name": f"{config['emoji']} {config['name']} Window (LOST - {new_duration}h)", "leader": "BOT", "start": new_start_time.isoformat(), "end": new_end_time.isoformat(), "description": "Previous window missed. Calculating max respawn.", "channel": str(alerts_channel_id), "settings": {"color": "#800000"} }
                
                h = {"Authorization": f"Bearer {rh_api_key}", "Content-Type": "application/json"}
                url = f"https://raid-helper.dev/api/v2/events/{event_id}"
                response = requests.put(url, json=payload, headers=h)
                
                if response.status_code == 200:
                    cursor.execute("UPDATE timer_states SET start_time=?, end_time=?, duration_hours=? WHERE server_id=? AND boss_key=?", (new_start_time.isoformat(), new_end_time.isoformat(), new_duration, server_id, boss_key))
    conn.commit()
    conn.close()

# --- Bot Startup ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    if not fernet: print("\nWARNING: DATABASE_ENCRYPTION_KEY not set. Bot will run but configuration commands will fail.\n")
    setup_database()
    bot.tree.add_command(TodCommandGroup(name="tod", description="L2 Boss Timer Commands"))
    await bot.tree.sync()
    print("Slash commands synced.")
    check_all_boss_windows.start()

@check_all_boss_windows.before_loop
async def before_check():
    await bot.wait_until_ready()

if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("FATAL ERROR: DISCORD_BOT_TOKEN is missing from environment variables.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
