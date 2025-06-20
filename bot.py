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

    duration = config['duration_hours']
    event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
    
    description_text = (
        f"**✅ Window Opens:** <t:{int(event_start_time.timestamp())}:F> (<t:{int(event_start_time.timestamp())}:R>)\n"
        f"**Window Duration:** {duration} hours\n\n"
        f"*Timer set by {interaction.user.mention} based on a ToD of <t:{int(tod_time.timestamp())}:T>.*\n"
        f"*Note: Please use the time in this description as the official time.*"
    )
    
    payload = { 
        "title": f"{config['emoji']} {config['name']} Window", 
        "leaderId": str(interaction.user.id),
        "leaderName": interaction.user.display_name,
        "startTime": int(event_start_time.timestamp()),
        "description": description_text, 
        "templateId": "standard",
        "advancedSettings": { "duration": duration * 60 }
    }
    
    create_url = f"https://raid-helper.dev/api/v2/servers/{interaction.guild_id}/channels/{events_channel_id}/event"
    response = requests.post(url=create_url, json=payload, headers=h)

    if response.status_code in [200, 201]:
        rh_response = response.json()
        new_event_id = rh_response.get('event', {}).get('id')
        if not new_event_id:
            await interaction.followup.send("❌ Event was created, but I could not get the new Event ID from Raid-Helper's response.", ephemeral=True); conn.close(); return

        event_end_time = event_start_time + timedelta(hours=duration)
        cursor.execute("""
            INSERT INTO timer_states (server_id, boss_key, event_id, start_time, end_time, duration_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, boss_key) DO UPDATE SET event_id=excluded.event_id, start_time=excluded.start_time, end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
        """, (interaction.guild_id, boss_key.upper(), new_event_id, event_start_time.isoformat(), event_end_time.isoformat(), duration, "active"))
        conn.commit()
        await interaction.followup.send(f"✅ New event for **{config['name']}** created! The correct time is in the event description.")
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
    if action.value == "set": await _process_tod(interaction, boss.value, timestamp)
    elif action.value == "reset":
        if timestamp: await interaction.response.send_message("You cannot provide a timestamp when resetting a timer.", ephemeral=True); return
        await _process_reset(interaction, boss.value)

@bot.tree.command(name="overview", description="Shows the status of all boss timers.")
async def overview(interaction: discord.Interaction):
    if not await _is_configured(interaction): return
    await interaction.response.defer()
    conn = db_connect()
    timers = conn.cursor().execute("SELECT boss_key, status, start_time, end_time, duration_hours, event_id FROM timer_states WHERE server_id = ?", (interaction.guild_id,)).fetchall()
    conn.close()
    embed = discord.Embed(title="Boss Timer Overview", color=discord.Color.dark_gold(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Server: {interaction.guild.name}")
    if not timers:
        embed.description = "No timers have been set for this server yet. Use `/tod` to start one."
    for boss_key, status, start_str, end_str, duration, event_id in sorted(timers, key=lambda x: x[2]):
        config = BOSS_CONFIG[boss_key]
        start_time, end_time, now = datetime.fromisoformat(start_str), datetime.fromisoformat(end_str), datetime.now(timezone.utc)
        event_url = f"https://raid-helper.dev/event/{interaction.guild_id}/{event_id}"
        value = f"› [View Event]({event_url})"
        if status == "paused": state, value = f"🔴 Paused", f"*Window exceeded 16h.*\n{value}"
        elif now > end_time: state, value = f"⚪ Window Closed", f"*Awaiting automated update.*\n{value}"
        elif now > start_time:
            state = "🟠 Open (LOST)" if duration > config['duration_hours'] else "🟢 Open (ACTIVE)"
            value = f"› Closes <t:{int(end_time.timestamp())}:R>\n{value}"
        else:
            state = "🔵 Upcoming"
            value = f"› Opens <t:{int(start_time.timestamp())}:R>\n{value}"
        embed.add_field(name=f"{config['emoji']} {config['name']} - {state}", value=value, inline=False)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="help", description="Sends a private message explaining all bot commands.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="L2 Boss Timer Bot Help", color=discord.Color.blue())
    cmd_list = "**/tod**\n› This is the main command. Use it to `set` or `reset` a timer for a specific boss.\n\n"
    cmd_list += "**/overview**\n› Shows the status of all currently active boss timers.\n\n"
    cmd_list += "**/help**\n› Shows this help message."
    embed.add_field(name="📊 General Commands", value=cmd_list, inline=False)
    embed.add_field(name="⚙️ Automated Features", value="**1. Lost Window:** When a timer expires, a new event is created for the 'lost' window.\n**2. Safety Pause:** Automation pauses if a window's duration would exceed 16 hours.", inline=False)
    embed.add_field(name="👑 Admin Commands", value="**`/configure`**\n› Setup the bot for this server.\n**`/wipe_my_data`**\n› Deletes all data for this server.\n**`/privacy`**\n› Shows the privacy policy.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="privacy", description="Displays the bot's privacy policy.")
async def privacy(interaction: discord.Interaction):
    embed = discord.Embed(title="Privacy Policy", color=discord.Color.light_grey(), description="This bot is designed with privacy and data isolation as core principles.")
    embed.add_field(name="What Data is Stored?", value="- Your Discord Server ID & configured Channel IDs.\n- The Raid-Helper API key you provide, which is **always encrypted**.\n- The current state of your configured boss timers.", inline=False)
    embed.add_field(name="Data Access & Deletion", value="Your server's data is never shared. You can permanently delete all data for your server at any time by running `/wipe_my_data`.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="configure", description="Admin Only: Configure the bot for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def configure(interaction: discord.Interaction):
    if not fernet:
        return await interaction.response.send_message("❌ **Configuration Error:** The bot operator has not set a `DATABASE_ENCRYPTION_KEY`. This command is disabled.", ephemeral=True)
    await interaction.response.send_message("I've sent you a DM to start the configuration.", ephemeral=True)
    try: dm_channel = await interaction.user.create_dm()
    except discord.Forbidden: return await interaction.followup.send("I couldn't send you a DM. Please check your server privacy settings.", ephemeral=True)
    questions = { "events_channel_id": "What is the exact **name** of the channel for Raid-Helper events?", "alerts_channel_id": "What is the exact **name** of the channel for bot alerts?", "raid_helper_api_key": "Finally, what is your **Raid-Helper API Key**?" }
    answers = {}
    await dm_channel.send(f"👋 Let's configure the bot for **{interaction.guild.name}**. You can type `cancel` at any time to stop.")
    for key, question in questions.items():
        try:
            await dm_channel.send(question)
            def check(m): return m.author == interaction.user and m.channel == dm_channel
            msg = await bot.wait_for('message', timeout=300.0, check=check)
            reply_content = msg.content.strip()
            if reply_content.lower() == 'cancel': return await dm_channel.send("Configuration cancelled.")
            if "channel" in key:
                target_channel = await _find_channel_by_name(interaction.guild, reply_content)
                if not target_channel: return await dm_channel.send(f"❌ I could not find a channel named `#{reply_content}`. Configuration cancelled.")
                answers[key] = target_channel.id
                await dm_channel.send(f"✅ Found it! I will use `#{target_channel.name}`.")
            else:
                answers[key] = reply_content
                await dm_channel.send(f"✅ API Key has been recorded.")
        except asyncio.TimeoutError: return await dm_channel.send("Configuration timed out.")
    try:
        encrypted_key = fernet.encrypt(answers['raid_helper_api_key'].encode())
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("""INSERT INTO servers (server_id, events_channel_id, alerts_channel_id, raid_helper_api_key) VALUES (?, ?, ?, ?)
                          ON CONFLICT(server_id) DO UPDATE SET events_channel_id=excluded.events_channel_id, alerts_channel_id=excluded.alerts_channel_id, raid_helper_api_key=excluded.raid_helper_api_key;
                       """, (interaction.guild_id, answers['events_channel_id'], answers['alerts_channel_id'], encrypted_key))
        conn.commit(); conn.close()
        await dm_channel.send("✅ **Configuration saved securely!**")
    except Exception as e: await dm_channel.send(f"❌ **Error saving configuration!** Check that your inputs are correct.\n`{e}`")

@bot.tree.command(name="wipe_my_data", description="Admin Only: Deletes all data for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def wipe_my_data(interaction: discord.Interaction):
    view = ConfirmationView(author=interaction.user)
    await interaction.response.send_message("**Are you absolutely sure?** This will permanently delete all timers and configuration for this server.", view=view, ephemeral=True)
    await view.wait()
    if view.value:
        conn = db_connect()
        conn.cursor().execute("DELETE FROM servers WHERE server_id = ?", (interaction.guild_id,))
        conn.commit(); conn.close()
        await interaction.followup.send("All data for this server has been wiped.", ephemeral=True)
    elif view.value is False: await interaction.followup.send("Deletion cancelled.", ephemeral=True)

# --- Automated Background Task ---
@tasks.loop(minutes=1)
async def check_all_boss_windows():
    conn = db_connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    active_timers = cursor.execute("SELECT * FROM timer_states WHERE status = 'active'").fetchall()
    for timer in active_timers:
        end_time = datetime.fromisoformat(timer['end_time'])
        if datetime.now(timezone.utc) > end_time:
            server_config = cursor.execute("SELECT * FROM servers WHERE server_id = ?", (timer['server_id'],)).fetchone()
            if not server_config: continue
            try: rh_api_key = fernet.decrypt(server_config['raid_helper_api_key']).decode()
            except Exception: print(f"BACKGROUND TASK: Could not decrypt key for server {timer['server_id']}. Skipping."); continue
            config = BOSS_CONFIG[timer['boss_key']]
            new_duration = timer['duration_hours'] + 4
            if new_duration > 16:
                cursor.execute("UPDATE timer_states SET status = 'paused' WHERE server_id = ? AND boss_key = ?", (timer['server_id'], timer['boss_key']))
                try:
                    alert_channel = await bot.fetch_channel(server_config['alerts_channel_id'])
                    await alert_channel.send(f"🔥 The **{config['name']}** window has exceeded 16h and is now **paused**. Use `/tod` with `action:set` to reset.")
                except (discord.NotFound, discord.Forbidden): pass
            else:
                start_time = datetime.fromisoformat(timer['start_time'])
                new_start_time = start_time + timedelta(hours=config['lost_respawn_shift_hours'])
                
                description_text = (
                    f"**✅ Window Opens:** <t:{int(new_start_time.timestamp())}:F> (<t:{int(new_start_time.timestamp())}:R>)\n"
                    f"**Window Duration:** {new_duration} hours\n\n"
                    f"*This is an automated 'lost window' calculation. Please use this description as the official time.*"
                )
                
                payload = { "title": f"{config['emoji']} {config['name']} Window (LOST - {new_duration}h)", "leaderId": str(bot.user.id), "leaderName": bot.user.display_name, "startTime": int(new_start_time.timestamp()), "description": description_text, "templateId": "standard", "advancedSettings": { "duration": new_duration * 60 } }
                
                h = {"Authorization": rh_api_key, "Content-Type": "application/json"}
                url = f"https://raid-helper.dev/api/v2/events/{timer['event_id']}"
                response = requests.patch(url=url, json=payload, headers=h)
                
                if response.status_code == 200:
                    new_end_time = new_start_time + timedelta(hours=new_duration)
                    cursor.execute("UPDATE timer_states SET start_time=?, end_time=?, duration_hours=? WHERE server_id=? AND boss_key=?", (new_start_time.isoformat(), new_end_time.isoformat(), new_duration, timer['server_id'], timer['boss_key']))
    conn.commit()
    conn.close()

# --- Bot Startup ---
@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name} ({bot.user.id})')
    if not fernet: print("\nWARNING: DATABASE_ENCRYPTION_KEY not set. Bot will run but configuration commands will fail.\n")
    setup_database()
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
