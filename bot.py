# L2 ToD Timer Bot
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
from typing import Optional

import discord
import dateparser  # type: ignore[import-untyped]
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv

# --- Initial Setup & Configuration ---
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_FILE = os.getenv("DATABASE_FILE", "bot_database.db")

# --- Database Helper Functions ---
def db_connect():
    return sqlite3.connect(DATABASE_FILE)

def setup_database():
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("PRAGMA foreign_keys = ON;")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS servers (
            server_id           INTEGER PRIMARY KEY,
            alerts_channel_id   INTEGER NOT NULL,
            timer_channel_id    INTEGER NOT NULL,
            overview_message_id INTEGER,
        public_channel_id INTEGER,
        public_message_id INTEGER,
            lost_window_enabled BOOLEAN NOT NULL DEFAULT 1
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS timer_states (
            server_id      INTEGER NOT NULL,
            boss_key       TEXT NOT NULL,
            tod_time       TEXT,
            start_time     TEXT,
            end_time       TEXT,
            duration_hours REAL,
            status         TEXT,
            PRIMARY KEY (server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tod_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            server_id INTEGER NOT NULL,
            boss_key TEXT NOT NULL,
            set_by_user_id INTEGER,
            created_at TEXT,
            reverted INTEGER NOT NULL DEFAULT 0,
            prev_tod_time TEXT,
            prev_start_time TEXT,
            prev_end_time TEXT,
            prev_duration_hours REAL,
            prev_status TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS custom_bosses (
            server_id      INTEGER NOT NULL,
            boss_key       TEXT NOT NULL,
            name           TEXT NOT NULL,
            respawn_hours  REAL NOT NULL,
            duration_hours REAL NOT NULL,
            imageUrl       TEXT,
            PRIMARY KEY (server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)

    # Migrations: add new columns to existing databases without errors
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS announcements (
            server_id INTEGER NOT NULL,
            boss_key TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            role_ids TEXT,
            lead_minutes INTEGER NOT NULL,
            announced_for TEXT,
            PRIMARY KEY (server_id, boss_key),
            FOREIGN KEY(server_id) REFERENCES servers(server_id) ON DELETE CASCADE
        );
    """)
    for migration in [
        "ALTER TABLE servers ADD COLUMN timer_channel_id INTEGER NOT NULL DEFAULT 0;",
        "ALTER TABLE servers ADD COLUMN overview_message_id INTEGER;",
        "ALTER TABLE servers ADD COLUMN public_channel_id INTEGER;",
        "ALTER TABLE servers ADD COLUMN public_message_id INTEGER;",
        "ALTER TABLE servers ADD COLUMN lost_window_enabled BOOLEAN NOT NULL DEFAULT 1;",
        "ALTER TABLE timer_states ADD COLUMN tod_time TEXT;",
        "ALTER TABLE servers ADD COLUMN timer_role_id INTEGER;",
        "ALTER TABLE servers ADD COLUMN unreachable_since TEXT;",
    ]:
        try:
            cursor.execute(migration)
        except sqlite3.OperationalError:
            pass  # Column already exists

    # Drop legacy table no longer needed
    cursor.execute("DROP TABLE IF EXISTS boss_settings;")

    conn.commit()
    conn.close()

# --- Central Boss Configuration (Defaults) ---
# Adjust respawn_hours / duration_hours to match your server's rates.
# Beleth and Epidos are linked: killing Epidos triggers Beleth's spawn.
BOSS_CONFIG = {
    "AQ": {
        "name": "Ant Queen", "respawn_hours": 17, "duration_hours": 4,
        "lost_respawn_shift_hours": 18, "emoji": "🐜",
        "imageUrl": "https://cdn.discordapp.com/attachments/1360048329811165204/1374070282959978638/1fc1abd4-9dae-4b8d-ba4c-9c185ddb2644.i4g.png?ex=6858e06c&is=68578eec&hm=eb596e19bd3c7feded8cd687ea2a41701c8662c1c5d9332a90ad410d0a85155b&"
    },
    "CORE": {
        "name": "Core", "respawn_hours": 48, "duration_hours": 4,
        "lost_respawn_shift_hours": 49, "emoji": "🔮",
        "imageUrl": None
    },
    "ORFEN": {
        "name": "Orfen", "respawn_hours": 33, "duration_hours": 4,
        "lost_respawn_shift_hours": 34, "emoji": "🦑",
        "imageUrl": None
    },
    "BAIUM": {
        "name": "Baium", "respawn_hours": 125, "duration_hours": 4,
        "lost_respawn_shift_hours": 126, "emoji": "🏔️",
        "imageUrl": None
    },
    "ANTHARAS": {
        "name": "Antharas", "respawn_hours": 342, "duration_hours": 4,
        "lost_respawn_shift_hours": 343, "emoji": "🐉",
        "imageUrl": None
    },
    "VALAKAS": {
        "name": "Valakas", "respawn_hours": 342, "duration_hours": 4,
        "lost_respawn_shift_hours": 343, "emoji": "🔥",
        "imageUrl": None
    },
    # Epidos must be killed first — its death triggers Beleth's spawn.
    "ELPY": {
        "name": "Epidos", "respawn_hours": 21, "duration_hours": 21,
        "lost_respawn_shift_hours": 22, "emoji": "🧪",
        "imageUrl": None
    },
    "BELETH": {
        "name": "Beleth", "respawn_hours": 342, "duration_hours": 4,
        "lost_respawn_shift_hours": 343, "emoji": "😈",
        "imageUrl": None
    },
}

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# --- UI Views ---
LEAD_TIME_CHOICES = [5, 10, 15, 30, 45, 60, 90, 120]


class AnnounceSetupView(ui.View):
    # DM view: four dropdowns (boss, channel, roles, lead time) + Save.
    def __init__(self, guild: discord.Guild, author: discord.User, boss_options: list):
        super().__init__(timeout=300)
        self.guild = guild
        self.author = author
        self.boss_key = None
        self.channel_id = None
        self.role_ids = []
        self.lead_minutes = None

        boss_select = ui.Select(placeholder="1. Which boss?",
            options=[discord.SelectOption(label=name, value=key) for key, name in boss_options[:25]])
        boss_select.callback = self._make_cb(boss_select, "boss_key")
        self.add_item(boss_select)

        channels = [c for c in guild.text_channels if c.permissions_for(guild.me).send_messages][:25]
        chan_select = ui.Select(placeholder="2. Post in which channel?",
            options=[discord.SelectOption(label=f"#{c.name}"[:100], value=str(c.id)) for c in channels])
        chan_select.callback = self._make_cb(chan_select, "channel_id", cast=int)
        self.add_item(chan_select)

        roles = [r for r in guild.roles if not r.managed and r.name != "@everyone"][:25]
        role_select = ui.Select(placeholder="3. Mention which roles? (optional, multi)",
            min_values=0, max_values=len(roles) if roles else 1,
            options=[discord.SelectOption(label=r.name[:100], value=str(r.id)) for r in roles]
                    or [discord.SelectOption(label="No roles available", value="0")])
        role_select.callback = self._make_roles_cb(role_select)
        self.add_item(role_select)

        time_select = ui.Select(placeholder="4. How long before the window opens?",
            options=[discord.SelectOption(label=f"{m} minutes before", value=str(m)) for m in LEAD_TIME_CHOICES])
        time_select.callback = self._make_cb(time_select, "lead_minutes", cast=int)
        self.add_item(time_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    def _make_cb(self, select: ui.Select, attr: str, cast=str):
        async def cb(interaction: discord.Interaction):
            setattr(self, attr, cast(select.values[0]))
            await interaction.response.defer()
        return cb

    def _make_roles_cb(self, select: ui.Select):
        async def cb(interaction: discord.Interaction):
            self.role_ids = [int(v) for v in select.values if v != "0"]
            await interaction.response.defer()
        return cb

    @ui.button(label="Save announcement", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, button: ui.Button):
        if not all([self.boss_key, self.channel_id, self.lead_minutes]):
            return await interaction.response.send_message(
                "⚠️ Please pick at least a boss, a channel, and a lead time first.")
        conn = db_connect()
        conn.cursor().execute("""
            INSERT INTO announcements (server_id, boss_key, channel_id, role_ids, lead_minutes, announced_for)
            VALUES (?, ?, ?, ?, ?, NULL)
            ON CONFLICT(server_id, boss_key) DO UPDATE SET
                channel_id=excluded.channel_id, role_ids=excluded.role_ids,
                lead_minutes=excluded.lead_minutes, announced_for=NULL;
        """, (self.guild.id, self.boss_key, self.channel_id,
              ",".join(map(str, self.role_ids)) or None, self.lead_minutes))
        conn.commit()
        conn.close()
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"✅ Announcement saved: **{self.boss_key}** in <#{self.channel_id}>, "
            f"{self.lead_minutes} min before the window opens.")
        self.stop()


class AnnounceRemoveView(ui.View):
    # DM view: pick an existing announcement to remove.
    def __init__(self, guild: discord.Guild, author: discord.User, existing: list):
        super().__init__(timeout=300)
        self.guild = guild
        self.author = author
        select = ui.Select(placeholder="Which announcement do you want to remove?",
            options=[discord.SelectOption(label=f"{boss_key} - {lead} min before", value=boss_key)
                     for boss_key, lead in existing[:25]])
        select.callback = self._remove_cb(select)
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author.id

    def _remove_cb(self, select: ui.Select):
        async def cb(interaction: discord.Interaction):
            boss_key = select.values[0]
            conn = db_connect()
            conn.cursor().execute("DELETE FROM announcements WHERE server_id = ? AND boss_key = ?",
                                  (self.guild.id, boss_key))
            conn.commit()
            conn.close()
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(f"🗑️ Announcement for **{boss_key}** removed.")
            self.stop()
        return cb


class ConfirmationView(ui.View):
    def __init__(self, author: discord.abc.User):
        super().__init__(timeout=60)
        self.value: Optional[bool] = None
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
    async def cancel(self, interaction: discord.Interaction, _button: ui.Button):
        self.value = False
        self.stop()
        await interaction.response.edit_message(content="Action cancelled.", view=None)

class TodNowButton(ui.Button):
    """A persistent button that sets a boss's ToD to now when clicked."""
    def __init__(self, boss_key: str, label: str, emoji_str: Optional[str]):
        super().__init__(
            label=label,
            emoji=emoji_str,
            style=discord.ButtonStyle.primary,
            custom_id=f"tod_now_{boss_key}",
        )
        self.boss_key = boss_key

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await _can_manage_timers(interaction):
            return
        if not interaction.guild_id:
            await interaction.followup.send("❌ No server context.", ephemeral=True)
            return

        config = await _get_boss_config(interaction.guild_id, self.boss_key)
        if not config:
            await interaction.followup.send("❌ Boss configuration not found.", ephemeral=True)
            return

        tod_time = datetime.now(timezone.utc)
        duration_hours = config['duration_hours']
        event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
        event_end_time = event_start_time + timedelta(hours=duration_hours)

        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO timer_states (server_id, boss_key, tod_time, start_time, end_time, duration_hours, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, boss_key) DO UPDATE SET
                tod_time=excluded.tod_time, start_time=excluded.start_time,
                end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
        """, (interaction.guild_id, self.boss_key, tod_time.isoformat(),
              event_start_time.isoformat(), event_end_time.isoformat(), duration_hours, "active"))
        conn.commit()
        conn.close()

        await post_or_update_overview(interaction.guild_id)

        # Post the report embed to the alerts (report) channel
        duration_hours = config['duration_hours']
        duration_text = f"{duration_hours:.0f}h" if duration_hours >= 1 else f"{int(duration_hours * 60)}m"
        embed = discord.Embed(
            title=f"{config.get('emoji', '🗓️')} {config['name']} — Timer Set",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Time of Death", value=f"<t:{int(tod_time.timestamp())}:f>", inline=False)
        embed.add_field(name="Window Opens", value=f"<t:{int(event_start_time.timestamp())}:f> (<t:{int(event_start_time.timestamp())}:R>)", inline=False)
        embed.add_field(name="Window Closes", value=f"<t:{int(event_end_time.timestamp())}:f> — duration {duration_text}", inline=False)
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        if config.get("imageUrl"):
            embed.set_thumbnail(url=config["imageUrl"])

        conn2 = db_connect()
        row = conn2.cursor().execute(
            "SELECT alerts_channel_id FROM servers WHERE server_id = ?", (interaction.guild_id,)
        ).fetchone()
        conn2.close()

        report_sent = False
        if row:
            try:
                report_channel = await bot.fetch_channel(row[0])
                await report_channel.send(embed=embed)  # type: ignore[union-attr]
                report_sent = True
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"TodNowButton: could not post to report channel: {e}")

        ack = f"✅ **{config['name']}** — ToD set to <t:{int(tod_time.timestamp())}:t>."
        if not report_sent:
            ack += " (Report could not be posted to the report channel.)"
        await interaction.followup.send(ack, ephemeral=True)

async def _fetch_timer_rows(guild_id: int):
    """Return sorted timer rows for the given guild."""
    conn = db_connect()
    rows = conn.cursor().execute(
        "SELECT boss_key, tod_time, start_time, end_time FROM timer_states WHERE server_id = ?",
        (guild_id,)
    ).fetchall()
    conn.close()
    return sorted(rows, key=lambda x: x[2] or "")


async def _dm_or_ephemeral(interaction: discord.Interaction, msg: str) -> None:
    """DM the user; fall back to ephemeral if DMs are disabled."""
    try:
        dm = await interaction.user.create_dm()
        await dm.send(msg)
        await interaction.followup.send("📬 Sent to your DMs!", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(msg, ephemeral=True)


class CopyLocalButton(ui.Button):
    """DMs a code block with Discord local timestamps (<t:...:f>) for easy pasting."""
    def __init__(self):
        super().__init__(
            label="🕐 Local Timestamps",
            style=discord.ButtonStyle.secondary,
            custom_id="overview_copy_local",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild_id:
            await interaction.followup.send("❌ No server context.", ephemeral=True)
            return

        timers = await _fetch_timer_rows(interaction.guild_id)
        if not timers:
            await interaction.followup.send("No timers are currently set.", ephemeral=True)
            return

        lines = []
        for boss_key, tod_str, start_str, end_str in timers:
            config = await _get_boss_config(interaction.guild_id, boss_key)
            if not config:
                continue
            parts = [config['name']]
            if tod_str:
                parts.append(f"ToD: <t:{int(datetime.fromisoformat(tod_str).timestamp())}:f>")
            if start_str:
                parts.append(f"Opens: <t:{int(datetime.fromisoformat(start_str).timestamp())}:f>")
            if end_str:
                parts.append(f"Closes: <t:{int(datetime.fromisoformat(end_str).timestamp())}:f>")
            lines.append(" | ".join(parts))

        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        raw = f"Boss Timers — {now_str}\n" + "\n".join(lines)
        msg = f"-# Paste this in any channel — timestamps render in everyone's local time.\n```\n{raw}\n```"
        await _dm_or_ephemeral(interaction, msg)


class CopyUtcButton(ui.Button):
    """DMs a code block with plain UTC times."""
    def __init__(self):
        super().__init__(
            label="🌐 UTC Times",
            style=discord.ButtonStyle.secondary,
            custom_id="overview_copy_utc",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild_id:
            await interaction.followup.send("❌ No server context.", ephemeral=True)
            return

        timers = await _fetch_timer_rows(interaction.guild_id)
        if not timers:
            await interaction.followup.send("No timers are currently set.", ephemeral=True)
            return

        def utc_fmt(iso: str) -> str:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.strftime('%d %b %Y %H:%M UTC')

        lines = []
        for boss_key, tod_str, start_str, end_str in timers:
            config = await _get_boss_config(interaction.guild_id, boss_key)
            if not config:
                continue
            parts = [config['name']]
            if tod_str:
                parts.append(f"ToD: {utc_fmt(tod_str)}")
            if start_str:
                parts.append(f"Opens: {utc_fmt(start_str)}")
            if end_str:
                parts.append(f"Closes: {utc_fmt(end_str)}")
            lines.append(" | ".join(parts))

        now_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        raw = f"Boss Timers — {now_str}\n" + "\n".join(lines)
        msg = f"-# All times in UTC.\n```\n{raw}\n```"
        await _dm_or_ephemeral(interaction, msg)

class BossTimerView(ui.View):
    """Persistent view holding one 'ToD Now' button per boss plus two copy buttons."""
    def __init__(self, boss_entries: list[tuple[str, str, Optional[str]]]):
        super().__init__(timeout=None)
        for boss_key, boss_name, emoji_str in boss_entries[:23]:  # Reserve 2 slots for copy buttons
            self.add_item(TodNowButton(boss_key=boss_key, label=boss_name, emoji_str=emoji_str))
        self.add_item(CopyLocalButton())
        self.add_item(CopyUtcButton())

class AddBossModal(ui.Modal, title='Add a New Custom Boss'):  # type: ignore[call-arg]
    boss_name = ui.TextInput(label='Boss Name', placeholder='e.g., Zaken')
    respawn_hours = ui.TextInput(label='Respawn Window Start (in hours)', placeholder='e.g., 60 or 0.5 for 30 mins')
    duration_hours = ui.TextInput(label='Window Duration (in hours)', placeholder='e.g., 8 or 0.25 for 15 mins')
    image_url = ui.TextInput(label='Image URL (Optional)', style=discord.TextStyle.long, placeholder='https://i.imgur.com/...', required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            respawn = float(self.respawn_hours.value)
            duration = float(self.duration_hours.value)
            name = self.boss_name.value.strip()

            if not name or len(name) > 100:
                return await interaction.followup.send("❌ Boss name must be between 1 and 100 characters.", ephemeral=True)
            if respawn <= 0 or respawn > 720:
                return await interaction.followup.send("❌ Respawn hours must be between 0 and 720 (30 days).", ephemeral=True)
            if duration <= 0 or duration > 168:
                return await interaction.followup.send("❌ Duration must be between 0 and 168 hours (7 days).", ephemeral=True)

            key = "".join(filter(str.isalnum, name)).upper()[:10]
            if not key:
                return await interaction.followup.send("❌ Boss name must contain at least one alphanumeric character.", ephemeral=True)
            if not interaction.guild:
                return await interaction.followup.send("❌ Could not determine the server context.", ephemeral=True)

            conn = db_connect()
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO custom_bosses (server_id, boss_key, name, respawn_hours, duration_hours, imageUrl) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, boss_key) DO UPDATE SET name=excluded.name, respawn_hours=excluded.respawn_hours, duration_hours=excluded.duration_hours, imageUrl=excluded.imageUrl
            """, (interaction.guild_id, key, name, respawn, duration, self.image_url.value or None))
            conn.commit()
            conn.close()

            # Register the new button persistently so it works without a restart
            bot.add_view(BossTimerView([(key, name, None)]))
            await interaction.followup.send(f"✅ Custom boss **{name}** (`{key}`) added! Use `/tod set` to start a timer.", ephemeral=True)
        except ValueError:
            await interaction.followup.send("❌ Error: Respawn and Duration must be valid numbers.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"An error occurred: {e}", ephemeral=True)

# --- Helper Functions ---
async def _can_manage_timers(interaction: discord.Interaction) -> bool:
    conn = db_connect()
    row = conn.cursor().execute("SELECT timer_role_id FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
    conn.close()
    role_id = row[0] if row else None
    if not role_id:
        return True
    member = interaction.user
    if isinstance(member, discord.Member):
        if member.guild_permissions.administrator:
            return True
        if any(r.id == role_id for r in member.roles):
            return True
    msg = f"❌ You need the <@&{role_id}> role to manage boss timers."
    if not interaction.response.is_done():
        await interaction.response.send_message(msg, ephemeral=True)
    else:
        await interaction.followup.send(msg, ephemeral=True)
    return False


async def _is_configured(interaction: discord.Interaction) -> bool:
    conn = db_connect()
    is_conf = conn.cursor().execute("SELECT 1 FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
    conn.close()
    if not is_conf:
        msg = "This server has not been configured. An administrator must run `/configure` first."
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, ephemeral=True)
        else:
            await interaction.followup.send(msg, ephemeral=True)
    return is_conf is not None

async def _get_boss_config(server_id: Optional[int], boss_key: str) -> Optional[dict]:
    if server_id is None:
        return None
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT name, respawn_hours, duration_hours, imageUrl FROM custom_bosses WHERE server_id = ? AND boss_key = ?", (server_id, boss_key.upper()))
    custom_boss = cursor.fetchone()
    conn.close()

    if custom_boss:
        return {
            "name": custom_boss[0], "respawn_hours": custom_boss[1], "duration_hours": custom_boss[2],
            "lost_respawn_shift_hours": custom_boss[1] + (1/60), "emoji": "🔥",
            "imageUrl": custom_boss[3]
        }
    return BOSS_CONFIG.get(boss_key.upper())

async def _find_channel_by_name(guild: discord.Guild, name: str, channel_type: discord.ChannelType) -> Optional[discord.abc.GuildChannel | discord.Thread]:
    channel = discord.utils.get(guild.channels, name=name, type=channel_type)
    if channel:
        return channel
    if channel_type == discord.ChannelType.text:
        for thread in guild.threads:
            if thread.name == name:
                return thread
    return None

# --- Overview Embed Builder & Poster ---
async def build_overview_embed(guild_id: Optional[int], only_bosses: Optional[list] = None, title: str = "Boss Timer Overview") -> discord.Embed:
    if guild_id is None:
        return discord.Embed(title="Boss Timer Overview", description="No server context.", color=discord.Color.dark_gold())

    conn = db_connect()
    cursor = conn.cursor()
    timers = cursor.execute(
        "SELECT boss_key, status, tod_time, start_time, end_time, duration_hours FROM timer_states WHERE server_id = ?",
        (guild_id,)
    ).fetchall()
    conn.close()

    embed = discord.Embed(title=title, color=discord.Color.dark_gold(), timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Last updated")

    if not timers:
        embed.description = "No timers have been set yet. Use `/tod <boss> set` to start one."
        return embed

    for boss_key, status, tod_str, start_str, end_str, duration_hours in sorted(timers, key=lambda x: x[3] or ""):
        if only_bosses is not None and boss_key not in only_bosses:
            continue
        config = await _get_boss_config(guild_id, boss_key)
        if not config:
            continue

        start_time = datetime.fromisoformat(start_str)
        end_time = datetime.fromisoformat(end_str)
        now = datetime.now(timezone.utc)

        def fmt(dt: datetime) -> str:
            """Discord local timestamp + UTC side by side."""
            return f"<t:{int(dt.timestamp())}:f>  `{dt.strftime('%d %b %H:%M UTC')}`"

        def fmt_rel(dt: datetime) -> str:
            return f"<t:{int(dt.timestamp())}:f>  `{dt.strftime('%d %b %H:%M UTC')}`  (<t:{int(dt.timestamp())}:R>)"

        tod_ts = (fmt(datetime.fromisoformat(tod_str).replace(tzinfo=timezone.utc)
                      if datetime.fromisoformat(tod_str).tzinfo is None
                      else datetime.fromisoformat(tod_str))
                  if tod_str else "*unknown*")
        open_fmt  = fmt_rel(start_time)
        close_fmt = fmt(end_time)

        if status == "paused":
            state = "🔴 Paused"
            value = (
                f"› **Last ToD:** {tod_ts}\n"
                f"› Window exceeded 16h — use `/tod <boss> set` to reset."
            )
        elif now > end_time:
            state = "⚪ Window Closed"
            value = (
                f"› **Last ToD:** {tod_ts}\n"
                f"› *Awaiting automated update...*"
            )
        elif now > start_time:
            state = "🟠 Open (LOST)" if duration_hours > config['duration_hours'] else "🟢 Open (ACTIVE)"
            value = (
                f"› **Last ToD:** {tod_ts}\n"
                f"› **Opened:** {fmt(start_time)}\n"
                f"› **Closes:** {close_fmt}  (<t:{int(end_time.timestamp())}:R>)"
            )
        else:
            state = "🔵 Upcoming"
            value = (
                f"› **Last ToD:** {tod_ts}\n"
                f"› **Opens:** {open_fmt}\n"
                f"› **Closes:** {close_fmt}"
            )

        embed.add_field(name=f"{config.get('emoji', '🗓️')} {config['name']} — {state}", value=value, inline=False)

    return embed

PUBLIC_OVERVIEW_BOSSES = ["ORFEN", "AQ", "CORE"]


async def build_public_overview_embed(guild_id: int) -> discord.Embed:
    # Reuses the exact officer-overview formatting, filtered to the public bosses.
    embed = await build_overview_embed(guild_id, only_bosses=PUBLIC_OVERVIEW_BOSSES, title="Boss Timer Overview")
    embed.description = "🔘 Press a boss button below to set its Time of Death to **now**."
    return embed


class PublicTodButton(ui.Button):
    """Public 'ToD Now' button: sets ToD to now, NO permission check (open to all).
    Posts the same Timer Set report to the officer alerts channel."""
    def __init__(self, boss_key: str, label: str, emoji_str):
        super().__init__(label=label, emoji=emoji_str, style=discord.ButtonStyle.success,
                         custom_id=f"public_tod_{boss_key}")
        self.boss_key = boss_key

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if not interaction.guild_id:
            await interaction.followup.send("❌ No server context.", ephemeral=True)
            return
        config = await _get_boss_config(interaction.guild_id, self.boss_key)
        if not config:
            await interaction.followup.send("❌ Boss configuration not found.", ephemeral=True)
            return
        # 5-minute cooldown: block if this boss's ToD was already set recently (by anyone).
        conn_cd = db_connect()
        cd_row = conn_cd.cursor().execute(
            "SELECT tod_time FROM timer_states WHERE server_id = ? AND boss_key = ?",
            (interaction.guild_id, self.boss_key)).fetchone()
        conn_cd.close()
        if cd_row and cd_row[0]:
            try:
                last_tod = datetime.fromisoformat(cd_row[0])
                elapsed = (datetime.now(timezone.utc) - last_tod).total_seconds()
                if 0 <= elapsed < 300:
                    await interaction.followup.send(
                        f"⏱️ **{config['name']}**'s ToD was already set <t:{int(last_tod.timestamp())}:R> "
                        f"(less than 5 minutes ago). Skipping to avoid a double-reset.", ephemeral=True)
                    return
            except (ValueError, TypeError):
                pass
        tod_time = datetime.now(timezone.utc)
        duration_hours = config['duration_hours']
        event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
        event_end_time = event_start_time + timedelta(hours=duration_hours)
        conn = db_connect()
        cursor = conn.cursor()
        prev = cursor.execute("SELECT tod_time, start_time, end_time, duration_hours, status FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, self.boss_key)).fetchone()
        cursor.execute("INSERT INTO tod_history (server_id, boss_key, set_by_user_id, created_at, reverted, prev_tod_time, prev_start_time, prev_end_time, prev_duration_hours, prev_status) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)", (interaction.guild_id, self.boss_key, interaction.user.id, tod_time.isoformat(), prev[0] if prev else None, prev[1] if prev else None, prev[2] if prev else None, prev[3] if prev else None, prev[4] if prev else None))
        history_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO timer_states (server_id, boss_key, tod_time, start_time, end_time, duration_hours, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id, boss_key) DO UPDATE SET
                tod_time=excluded.tod_time, start_time=excluded.start_time,
                end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
        """, (interaction.guild_id, self.boss_key, tod_time.isoformat(),
              event_start_time.isoformat(), event_end_time.isoformat(), duration_hours, "active"))
        conn.commit()
        conn.close()
        await post_or_update_overview(interaction.guild_id)
        duration_text = f"{duration_hours:.0f}h" if duration_hours >= 1 else f"{int(duration_hours * 60)}m"
        embed = discord.Embed(
            title=f"{config.get('emoji', '🗓️')} {config['name']} — Timer Set",
            color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Time of Death", value=f"<t:{int(tod_time.timestamp())}:f>", inline=False)
        embed.add_field(name="Window Opens", value=f"<t:{int(event_start_time.timestamp())}:f> (<t:{int(event_start_time.timestamp())}:R>)", inline=False)
        embed.add_field(name="Window Closes", value=f"<t:{int(event_end_time.timestamp())}:f> — duration {duration_text}", inline=False)
        embed.set_footer(text=f"Set by {interaction.user.display_name}")
        if config.get("imageUrl"):
            embed.set_thumbnail(url=config["imageUrl"])
        conn2 = db_connect()
        row = conn2.cursor().execute("SELECT alerts_channel_id FROM servers WHERE server_id = ?", (interaction.guild_id,)).fetchone()
        conn2.close()
        report_sent = False
        if row:
            try:
                report_channel = await bot.fetch_channel(row[0])
                await report_channel.send(embed=embed, view=RevertView(history_id))
                report_sent = True
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"PublicTodButton: could not post to report channel: {e}")
        ack = f"✅ **{config['name']}** — ToD set to <t:{int(tod_time.timestamp())}:t>."
        if not report_sent:
            ack += " (Report could not be posted to the report channel.)"
        await interaction.followup.send(ack, view=RevertView(history_id), ephemeral=True)


class RevertButton(ui.Button):
    """Reverts a public-button ToD set back to the state before that click.
    Allowed for the original setter OR anyone with the officer timer-role."""
    def __init__(self, history_id: int):
        super().__init__(label="Revert", emoji="↩️", style=discord.ButtonStyle.secondary,
                         custom_id=f"revert_{history_id}")
        self.history_id = history_id

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        conn = db_connect()
        cur = conn.cursor()
        h = cur.execute("SELECT server_id, boss_key, set_by_user_id, reverted, prev_tod_time, prev_start_time, prev_end_time, prev_duration_hours, prev_status FROM tod_history WHERE id = ?", (self.history_id,)).fetchone()
        if not h:
            conn.close()
            await interaction.followup.send("❌ This revert is no longer available.", ephemeral=True)
            return
        server_id, boss_key, set_by, reverted, p_tod, p_start, p_end, p_dur, p_status = h
        if reverted:
            conn.close()
            await interaction.followup.send("ℹ️ This action was already reverted.", ephemeral=True)
            return
        # Permission: original setter OR officer timer-role
        allowed = (interaction.user.id == set_by)
        if not allowed:
            role_row = cur.execute("SELECT timer_role_id FROM servers WHERE server_id = ?", (server_id,)).fetchone()
            role_id = role_row[0] if role_row else None
            member = interaction.user
            if isinstance(member, discord.Member):
                if member.guild_permissions.administrator:
                    allowed = True
                elif role_id and any(r.id == role_id for r in member.roles):
                    allowed = True
                elif role_id is None:
                    allowed = True
        if not allowed:
            conn.close()
            await interaction.followup.send("❌ Only the person who set this timer or an officer can revert it.", ephemeral=True)
            return
        config = await _get_boss_config(server_id, boss_key)
        bossname = config['name'] if config else boss_key
        if p_tod is None:
            # No prior timer existed: delete the current one.
            cur.execute("DELETE FROM timer_states WHERE server_id = ? AND boss_key = ?", (server_id, boss_key))
            outcome = f"↩️ **{bossname}** timer reverted — no previous timer existed, so it was cleared."
        else:
            cur.execute("""
                INSERT INTO timer_states (server_id, boss_key, tod_time, start_time, end_time, duration_hours, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, boss_key) DO UPDATE SET
                    tod_time=excluded.tod_time, start_time=excluded.start_time, end_time=excluded.end_time,
                    duration_hours=excluded.duration_hours, status=excluded.status;
            """, (server_id, boss_key, p_tod, p_start, p_end, p_dur, p_status))
            outcome = f"↩️ **{bossname}** ToD reverted to <t:{int(datetime.fromisoformat(p_tod).timestamp())}:f>."
        cur.execute("UPDATE tod_history SET reverted = 1 WHERE id = ?", (self.history_id,))
        conn.commit()
        conn.close()
        await post_or_update_overview(server_id)
        # Notify officer channel
        conn3 = db_connect()
        arow = conn3.cursor().execute("SELECT alerts_channel_id FROM servers WHERE server_id = ?", (server_id,)).fetchone()
        conn3.close()
        if arow:
            try:
                ach = await bot.fetch_channel(arow[0])
                await ach.send(f"↩️ **{bossname}** ToD reverted by {interaction.user.display_name}.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        # Disable this button on the message it lives on
        try:
            view = ui.View.from_message(interaction.message)
            for item in view.children:
                item.disabled = True
            await interaction.message.edit(view=view)
        except Exception:
            pass
        await interaction.followup.send(outcome, ephemeral=True)


class RevertView(ui.View):
    def __init__(self, history_id: int):
        super().__init__(timeout=None)
        self.add_item(RevertButton(history_id))




class PublicBossTimerView(ui.View):
    """Persistent view with one open ToD button per public boss (no copy buttons)."""
    def __init__(self):
        super().__init__(timeout=None)
        for boss_key in PUBLIC_OVERVIEW_BOSSES:
            cfg = BOSS_CONFIG.get(boss_key, {})
            self.add_item(PublicTodButton(boss_key=boss_key, label=cfg.get('name', boss_key), emoji_str=cfg.get('emoji')))




async def post_or_update_public_overview(guild_id: Optional[int]) -> None:
    if guild_id is None:
        return
    conn = db_connect()
    row = conn.cursor().execute(
        "SELECT public_channel_id, public_message_id FROM servers WHERE server_id = ?", (guild_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        return
    public_channel_id, public_message_id = row
    embed = await build_public_overview_embed(guild_id)
    try:
        channel = await bot.fetch_channel(public_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return
        if public_message_id:
            try:
                message = await channel.fetch_message(public_message_id)
                await message.edit(embed=embed, view=PublicBossTimerView())
                return
            except discord.NotFound:
                pass
        message = await channel.send(embed=embed, view=PublicBossTimerView())
        conn = db_connect()
        conn.cursor().execute("UPDATE servers SET public_message_id = ? WHERE server_id = ?", (message.id, guild_id))
        conn.commit()
        conn.close()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"Could not post/update public overview for server {guild_id}: {e}")


async def post_or_update_overview(guild_id: Optional[int]) -> None:
    if guild_id is None:
        return
    conn = db_connect()
    row = conn.cursor().execute(
        "SELECT timer_channel_id, overview_message_id FROM servers WHERE server_id = ?", (guild_id,)
    ).fetchone()
    conn.close()

    if not row or not row[0]:
        return

    timer_channel_id, overview_message_id = row
    embed = await build_overview_embed(guild_id)

    # Build the button view for this server's bosses
    boss_entries: list[tuple[str, str, Optional[str]]] = [
        (k, v['name'], v.get('emoji')) for k, v in BOSS_CONFIG.items()
    ]
    conn2 = db_connect()
    custom_rows = conn2.cursor().execute(
        "SELECT boss_key, name FROM custom_bosses WHERE server_id = ?", (guild_id,)
    ).fetchall()
    conn2.close()
    boss_entries += [(key, name, None) for key, name in custom_rows]
    view = BossTimerView(boss_entries)

    try:
        channel = await bot.fetch_channel(timer_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return

        if overview_message_id:
            try:
                message = await channel.fetch_message(overview_message_id)
                await message.edit(embed=embed, view=view)
                await post_or_update_public_overview(guild_id)
                return
            except discord.NotFound:
                pass  # Message was deleted; fall through to post a new one

        message = await channel.send(embed=embed, view=view)
        conn = db_connect()
        conn.cursor().execute("UPDATE servers SET overview_message_id = ? WHERE server_id = ?", (message.id, guild_id))
        conn.commit()
        conn.close()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        print(f"Could not post/update overview for server {guild_id}: {e}")
    await post_or_update_public_overview(guild_id)

# --- Main Timer Command Logic ---
async def _process_tod(interaction: discord.Interaction, boss_key: str, tod_time_utc: Optional[datetime] = None) -> None:
    await interaction.response.defer()
    if interaction.guild_id is None:
        await interaction.edit_original_response(content="❌ Could not determine the server context.")
        return

    config = await _get_boss_config(interaction.guild_id, boss_key)
    if not config:
        await interaction.edit_original_response(content=f"❌ Boss `{boss_key}` not found. Use `/boss add` to add a custom boss.")
        return

    tod_time = tod_time_utc if tod_time_utc is not None else datetime.now(timezone.utc)

    duration_hours = config['duration_hours']
    event_start_time = tod_time + timedelta(hours=config['respawn_hours'])
    event_end_time = event_start_time + timedelta(hours=duration_hours)
    duration_text = f"{duration_hours:.0f}h" if duration_hours >= 1 else f"{int(duration_hours * 60)}m"

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO timer_states (server_id, boss_key, tod_time, start_time, end_time, duration_hours, status) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_id, boss_key) DO UPDATE SET
            tod_time=excluded.tod_time, start_time=excluded.start_time,
            end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
    """, (interaction.guild_id, boss_key.upper(), tod_time.isoformat(),
          event_start_time.isoformat(), event_end_time.isoformat(), duration_hours, "active"))
    conn.commit()
    conn.close()

    embed = discord.Embed(
        title=f"{config.get('emoji', '🗓️')} {config['name']} — Timer Set",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Time of Death", value=f"<t:{int(tod_time.timestamp())}:f>", inline=False)
    embed.add_field(name="Window Opens", value=f"<t:{int(event_start_time.timestamp())}:f> (<t:{int(event_start_time.timestamp())}:R>)", inline=False)
    embed.add_field(name="Window Closes", value=f"<t:{int(event_end_time.timestamp())}:f> — duration {duration_text}", inline=False)
    embed.set_footer(text=f"Set by {interaction.user.display_name}")
    if config.get("imageUrl"):
        embed.set_thumbnail(url=config["imageUrl"])

    await interaction.edit_original_response(embed=embed)
    await post_or_update_overview(interaction.guild_id)

async def _process_reset(interaction: discord.Interaction, boss_key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id is None:
        await interaction.followup.send("❌ Could not determine the server context.", ephemeral=True)
        return

    config = await _get_boss_config(interaction.guild_id, boss_key)
    if not config:
        await interaction.followup.send(f"❌ Boss with key `{boss_key}` not found.", ephemeral=True)
        return

    conn = db_connect()
    cursor = conn.cursor()
    existing = cursor.execute("SELECT 1 FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper())).fetchone()
    if not existing:
        conn.close()
        await interaction.followup.send(f"There is no active timer for **{config['name']}** to reset.", ephemeral=True)
        return

    cursor.execute("DELETE FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"✅ The timer for **{config['name']}** has been reset.", ephemeral=True)
    await post_or_update_overview(interaction.guild_id)

async def _process_correction(interaction: discord.Interaction, boss_key: str, adjustment: str) -> None:
    await interaction.response.defer(ephemeral=True)
    try:
        minutes = int(adjustment)
        if abs(minutes) > 1440:
            return await interaction.followup.send("❌ Adjustment cannot exceed ±1440 minutes (24 hours).", ephemeral=True)
    except ValueError:
        return await interaction.followup.send("❌ Invalid adjustment. Please use a number (e.g., `+10` or `-15`).", ephemeral=True)

    config = await _get_boss_config(interaction.guild_id, boss_key)
    if not config:
        return await interaction.followup.send(f"❌ Boss with key `{boss_key}` not found.", ephemeral=True)

    conn = db_connect()
    cursor = conn.cursor()
    timer_data = cursor.execute(
        "SELECT start_time, duration_hours FROM timer_states WHERE server_id = ? AND boss_key = ?",
        (interaction.guild_id, boss_key.upper())
    ).fetchone()

    if not timer_data:
        conn.close()
        return await interaction.followup.send(f"There is no active timer for **{config['name']}** to correct.", ephemeral=True)

    start_time_iso, duration_hours = timer_data
    new_start_time = datetime.fromisoformat(start_time_iso) + timedelta(minutes=minutes)
    new_end_time = new_start_time + timedelta(hours=duration_hours)

    cursor.execute(
        "UPDATE timer_states SET start_time = ?, end_time = ? WHERE server_id = ? AND boss_key = ?",
        (new_start_time.isoformat(), new_end_time.isoformat(), interaction.guild_id, boss_key.upper())
    )
    conn.commit()
    conn.close()

    await interaction.followup.send(
        f"✅ Timer for **{config['name']}** adjusted by {minutes:+d} minutes. Window opens <t:{int(new_start_time.timestamp())}:R>.",
        ephemeral=True
    )
    await post_or_update_overview(interaction.guild_id)

# --- Autocomplete ---
async def custom_boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Returns only custom (user-added) bosses for the current server."""
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT boss_key, name FROM custom_bosses WHERE server_id = ?", (interaction.guild_id,))
    choices = [app_commands.Choice(name=name, value=key) for key, name in cursor.fetchall()]
    conn.close()
    return [c for c in choices if current.lower() in c.name.lower() or current.lower() in c.value.lower()][:25]

async def all_boss_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    """Returns all bosses (default + custom) for the current server."""
    choices = [app_commands.Choice(name=f"{v['emoji']} {v['name']}", value=k) for k, v in BOSS_CONFIG.items()]
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT boss_key, name FROM custom_bosses WHERE server_id = ?", (interaction.guild_id,))
    choices += [app_commands.Choice(name=name, value=key) for key, name in cursor.fetchall()]
    conn.close()
    return [c for c in choices if current.lower() in c.name.lower() or current.lower() in c.value.lower()][:25]

# --- Slash Command Groups ---
tod_group = app_commands.Group(name="tod", description="Boss timer commands")
boss_group = app_commands.Group(name="boss", description="Manage custom bosses")

# ── /tod set ──────────────────────────────────────────────────────────────────
@tod_group.command(name="set", description="Set the Time of Death for a boss.")
@app_commands.autocomplete(boss=all_boss_autocomplete)
@app_commands.describe(
    boss="The boss that died.",
    when="When did it die?",
    timestamp="Discord timestamp <t:…:F> OR natural language: 'yesterday 8pm', 'tuesday 20:30', '22 feb 21:00' (UTC)."
)
@app_commands.choices(when=[
    app_commands.Choice(name="Now", value="now"),
    app_commands.Choice(name="Last 10 minutes", value="minus10"),
    app_commands.Choice(name="Timestamp", value="timestamp"),
])
async def tod_set(interaction: discord.Interaction, boss: str, when: app_commands.Choice[str], timestamp: Optional[str] = None):
    if not await _is_configured(interaction): return
    if not await _can_manage_timers(interaction): return

    tod_time: Optional[datetime] = None
    if when.value == "minus10":
        tod_time = datetime.now(timezone.utc) - timedelta(minutes=10)
    elif when.value == "timestamp":
        if not timestamp:
            await interaction.response.send_message(
                "❌ Please provide a timestamp when selecting 'Timestamp'.", ephemeral=True)
            return
        # Try Discord timestamp format first: <t:1234567890:F>
        match = re.search(r'<t:(\d+)(?::[A-Za-z])?>', timestamp)
        if match:
            tod_time = datetime.fromtimestamp(int(match.group(1)), tz=timezone.utc)
        else:
            # Fall back to natural language (assumed UTC)
            parsed = dateparser.parse(
                timestamp,
                settings={
                    'RETURN_AS_TIMEZONE_AWARE': True,
                    'TO_TIMEZONE': 'UTC',
                    'PREFER_DATES_FROM': 'past',
                }
            )
            if not parsed:
                await interaction.response.send_message(
                    "❌ Could not parse that time. Use a Discord timestamp like `<t:1234567890:F>` "
                    "or natural language like `yesterday 8pm`, `tuesday 20:30`, `22 feb 21:00` (UTC).",
                    ephemeral=True)
                return
            tod_time = parsed

    await _process_tod(interaction, boss, tod_time)

# ── /tod reset ────────────────────────────────────────────────────────────────
@tod_group.command(name="reset", description="Clear the active timer for a boss.")
@app_commands.autocomplete(boss=all_boss_autocomplete)
@app_commands.describe(boss="The boss whose timer should be cleared.")
async def tod_reset(interaction: discord.Interaction, boss: str):
    if not await _is_configured(interaction): return
    if not await _can_manage_timers(interaction): return
    await _process_reset(interaction, boss)

# ── /tod correction ───────────────────────────────────────────────────────────
@tod_group.command(name="correction", description="Shift a boss timer by ±minutes.")
@app_commands.autocomplete(boss=all_boss_autocomplete)
@app_commands.describe(boss="The boss to adjust.", adjustment="Minutes to add or subtract (e.g. +10 or -15).")
async def tod_correction(interaction: discord.Interaction, boss: str, adjustment: str):
    if not await _is_configured(interaction): return
    if not await _can_manage_timers(interaction): return
    await _process_correction(interaction, boss, adjustment)

# ── Boss management ────────────────────────────────────────────────────────────
@boss_group.command(name="add", description="Add or update a custom boss timer.")
@app_commands.checks.has_permissions(administrator=True)
async def boss_add(interaction: discord.Interaction):
    if not await _is_configured(interaction): return
    await interaction.response.send_modal(AddBossModal())

@boss_group.command(name="remove", description="Remove a custom boss.")
@app_commands.autocomplete(boss_key=custom_boss_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(boss_key="The custom boss to remove.")
async def boss_remove(interaction: discord.Interaction, boss_key: str):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=True)
    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM custom_bosses WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
    removed = cursor.rowcount > 0
    cursor.execute("DELETE FROM timer_states WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
    cursor.execute("DELETE FROM announcements WHERE server_id = ? AND boss_key = ?", (interaction.guild_id, boss_key.upper()))
    conn.commit()
    conn.close()
    if removed:
        await interaction.followup.send(f"✅ Custom boss `{boss_key.upper()}` has been removed.", ephemeral=True)
    else:
        await interaction.followup.send(f"⚠️ No custom boss with key `{boss_key.upper()}` found.", ephemeral=True)

@boss_group.command(name="list", description="List all default and custom bosses.")
async def boss_list(interaction: discord.Interaction):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="Boss Configuration List", color=discord.Color.blue())

    default_lines = "\n".join([f"{v['emoji']} **{v['name']}** (`{k}`) — {v['respawn_hours']}h respawn, {v['duration_hours']}h window" for k, v in BOSS_CONFIG.items()])
    embed.add_field(name="Default Bosses", value=default_lines or "None", inline=False)

    conn = db_connect()
    cursor = conn.cursor()
    cursor.execute("SELECT boss_key, name, respawn_hours, duration_hours FROM custom_bosses WHERE server_id = ?", (interaction.guild_id,))
    custom_data = cursor.fetchall()
    conn.close()
    custom_lines = "\n".join([f"🔥 **{name}** (`{k}`) — {rh}h respawn, {dh}h window" for k, name, rh, dh in custom_data])
    embed.add_field(name="Custom Bosses", value=custom_lines or "None", inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True)

# --- /options command group ---
options_group = app_commands.Group(name="options", description="Configure server-wide bot options.")

@options_group.command(name="lost_window", description="Enable or disable automated 'lost window' tracking.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(enabled="Enable or disable the automated lost window feature.")
@app_commands.choices(enabled=[
    app_commands.Choice(name="Enable", value=1),
    app_commands.Choice(name="Disable", value=0)
])
async def set_lost_window(interaction: discord.Interaction, enabled: app_commands.Choice[int]):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=True)
    conn = db_connect()
    conn.cursor().execute("UPDATE servers SET lost_window_enabled = ? WHERE server_id = ?", (enabled.value, interaction.guild_id))
    conn.commit()
    conn.close()
    await interaction.followup.send(f"✅ Automated 'lost window' tracking has been **{enabled.name}d**.", ephemeral=True)

@options_group.command(name="timer_role", description="Restrict timer changes to a role. Run without a role to allow everyone.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(role="Members need this role to set/reset/correct timers. Omit to remove the restriction.")
async def set_timer_role(interaction: discord.Interaction, role: Optional[discord.Role] = None):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=True)
    conn = db_connect()
    conn.cursor().execute("UPDATE servers SET timer_role_id = ? WHERE server_id = ?", (role.id if role else None, interaction.guild_id))
    conn.commit()
    conn.close()
    if role:
        await interaction.followup.send(f"✅ Timer commands are now restricted to **{role.name}** (admins always allowed).", ephemeral=True)
    else:
        await interaction.followup.send("✅ Timer commands can now be used by everyone.", ephemeral=True)


@options_group.command(name="announce", description="Set up or remove a boss window announcement via DM.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(action="Set up a new/updated announcement, or remove an existing one.")
@app_commands.choices(action=[
    app_commands.Choice(name="Set up announcement", value="setup"),
    app_commands.Choice(name="Remove announcement", value="remove"),
])
async def options_announce(interaction: discord.Interaction, action: app_commands.Choice[str]):
    if not await _is_configured(interaction): return
    await interaction.response.send_message("📬 I've sent you a DM to continue.", ephemeral=True)
    try:
        dm = await interaction.user.create_dm()
        if action.value == "setup":
            boss_options = [(key, cfg["name"]) for key, cfg in BOSS_CONFIG.items()]
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("SELECT boss_key, name FROM custom_bosses WHERE server_id = ?", (interaction.guild_id,))
            boss_options += [(k, f"{n} (custom)") for k, n in cur.fetchall()]
            conn.close()
            view = AnnounceSetupView(interaction.guild, interaction.user, boss_options)
            await dm.send(
                f"📢 Let's set up a window announcement for **{interaction.guild.name}**.\n"
                "Pick a boss, a channel, optional roles to mention, and how long before "
                "the window opens the announcement should post - then press **Save**.", view=view)
        else:
            conn = db_connect()
            cur = conn.cursor()
            cur.execute("SELECT boss_key, lead_minutes FROM announcements WHERE server_id = ?", (interaction.guild_id,))
            existing = cur.fetchall()
            conn.close()
            if not existing:
                return await dm.send("There are no announcements configured for this server.")
            view = AnnounceRemoveView(interaction.guild, interaction.user, existing)
            await dm.send(f"Select the announcement to remove for **{interaction.guild.name}**:", view=view)
    except discord.Forbidden:
        await interaction.followup.send("❌ I couldn't DM you. Please check your privacy settings.", ephemeral=True)


@bot.tree.command(name="setpublicchannel", description="Admin: set (or clear) a channel for a compact public boss overview.")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="The channel for the public overview. Leave empty to turn it off.")
async def setpublicchannel(interaction: discord.Interaction, channel: Optional[discord.TextChannel] = None):
    if not await _is_configured(interaction):
        return
    await interaction.response.defer(ephemeral=True)
    conn = db_connect()
    if channel is None:
        conn.cursor().execute("UPDATE servers SET public_channel_id = NULL, public_message_id = NULL WHERE server_id = ?", (interaction.guild_id,))
        conn.commit()
        conn.close()
        return await interaction.followup.send("✅ Public overview turned **off**.", ephemeral=True)
    conn.cursor().execute("UPDATE servers SET public_channel_id = ?, public_message_id = NULL WHERE server_id = ?", (channel.id, interaction.guild_id))
    conn.commit()
    conn.close()
    await post_or_update_public_overview(interaction.guild_id)
    await interaction.followup.send(f"✅ Public overview will be posted in {channel.mention} (showing Orfen, Ant Queen, Core).", ephemeral=True)


@bot.tree.command(name="overview", description="Show a snapshot of all current boss timers.")
async def overview(interaction: discord.Interaction):
    if not await _is_configured(interaction): return
    await interaction.response.defer(ephemeral=True)
    if interaction.guild_id is None:
        await interaction.followup.send("❌ Could not determine the server context.", ephemeral=True)
        return
    embed = await build_overview_embed(interaction.guild_id)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="timestamp", description="Convert any time expression into Discord timestamps.")
@app_commands.describe(
    time="Any expression, e.g. 'yesterday 8pm', 'tuesday 20:30', '22 feb 21:00', 'in 3 hours'",
    timezone="Your timezone, e.g. CET, US/Eastern, Asia/Seoul (default: UTC)"
)
async def timestamp_cmd(interaction: discord.Interaction, time: str, timezone: str = "UTC"):
    await interaction.response.defer(ephemeral=True)
    result = dateparser.parse(
        time,
        settings={
            'RETURN_AS_TIMEZONE_AWARE': True,
            'TIMEZONE': timezone,
            'TO_TIMEZONE': 'UTC',
            'PREFER_DATES_FROM': 'past',
        }
    )
    if not result:
        await interaction.followup.send(
            f"❌ Could not parse `{time}`.\nTry formats like: `yesterday 8pm` · `tuesday 20:30` · `feb 22 21:00` · `2 hours ago`",
            ephemeral=True
        )
        return

    ts = int(result.timestamp())
    lines = [
        f"**Parsed:** {result.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        f"`<t:{ts}:F>` → <t:{ts}:F>",
        f"`<t:{ts}:f>` → <t:{ts}:f>",
        f"`<t:{ts}:D>` → <t:{ts}:D>",
        f"`<t:{ts}:d>` → <t:{ts}:d>",
        f"`<t:{ts}:T>` → <t:{ts}:T>",
        f"`<t:{ts}:t>` → <t:{ts}:t>",
        f"`<t:{ts}:R>` → <t:{ts}:R>",
    ]
    await interaction.followup.send("\n".join(lines), ephemeral=True)

@bot.tree.command(name="help", description="Show all bot commands.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="L2 ToD Timer Bot Help", color=discord.Color.blue())
    embed.add_field(name="⏱️ Timer Commands", value=(
        "**/tod set** `<boss>` `<when>` `[timestamp]`\n"
        "› Record a Time of Death — pick any boss, then choose:\n"
        "› **Now** · **Last 10 minutes** · **Timestamp** (paste a Discord `<t:…>` stamp)\n\n"
        "**/tod reset** `<boss>` — clear a timer\n"
        "**/tod correction** `<boss>` `<±minutes>` — shift the window\n"
    ), inline=False)
    embed.add_field(name="🔥 Boss Management", value=(
        "**/boss add** — add a custom boss (admin)\n"
        "**/boss remove** — remove a custom boss (admin)\n"
        "**/boss list** — list all bosses and their timings\n"
    ), inline=False)
    embed.add_field(name="📋 Other", value=(
        "**/overview** — private snapshot of all timers\n"
        "**/timestamp** `<time>` `[timezone]` — convert any time expression to Discord timestamps\n"
        "**/configure** — initial bot setup (admin)\n"
        "**/options lost_window** — toggle automated lost window (admin)\n"
        "**/wipe_my_data** — delete all server data (admin)\n"
        "**/privacy** — privacy policy\n"
    ), inline=False)
    embed.add_field(name="⚙️ Automated Features", value=(
        "**Lost Window:** when a timer expires the bot advances it to the next possible window automatically.\n"
        "**Safety Pause:** automation stops if the window exceeds 16h — an alert is sent to the alerts channel.\n"
        "**Live Overview:** the dedicated timer channel embed is kept up to date on every change."
    ), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="privacy", description="Displays the bot's privacy policy.")
async def privacy(interaction: discord.Interaction):
    embed = discord.Embed(title="Privacy Policy", color=discord.Color.light_grey(), description="This bot is designed with privacy and data isolation as core principles.")
    embed.add_field(name="What Data is Stored?", value="- Your Discord Server ID & configured Channel IDs.\n- The current state of your configured boss timers.", inline=False)
    embed.add_field(name="Data Access & Deletion", value="Your server's data is never shared. You can permanently delete all data for your server at any time by running `/wipe_my_data`.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="configure", description="Admin Only: Configure the bot for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def configure(interaction: discord.Interaction):
    await interaction.response.send_message("I've sent you a DM to start the configuration.", ephemeral=True)
    try:
        dm_channel = await interaction.user.create_dm()
    except discord.Forbidden:
        return await interaction.followup.send("I couldn't send you a DM. Please check your server privacy settings.", ephemeral=True)

    questions = {
        "alerts_channel_id": "What is the exact **name** of the channel for bot alerts (e.g. paused-window notifications)?",
        "timer_channel_id":  "What is the exact **name** of the channel where the **live boss timer overview** should be posted and kept updated?"
    }
    answers: dict[str, int | str | None] = {}

    guild_name = interaction.guild.name if interaction.guild else "this server"
    await dm_channel.send(f"👋 Let's configure the bot for **{guild_name}**. Type `cancel` at any time to stop.")
    for key, question in questions.items():
        try:
            await dm_channel.send(question)
            def check(m): return m.author == interaction.user and m.channel == dm_channel
            msg = await bot.wait_for('message', timeout=300.0, check=check)
            reply_content = msg.content.strip()
            if reply_content.lower() == 'cancel':
                return await dm_channel.send("Configuration cancelled.")
            if interaction.guild is None:
                return await dm_channel.send("❌ Could not determine the server context. Configuration cancelled.")
            target_channel = await _find_channel_by_name(interaction.guild, reply_content, discord.ChannelType.text)
            if not target_channel:
                return await dm_channel.send(f"❌ I could not find a text channel named `#{reply_content}`. Configuration cancelled.")
            answers[key] = target_channel.id
            await dm_channel.send(f"✅ Found it! I will use `#{target_channel.name}`.")
        except asyncio.TimeoutError:
            return await dm_channel.send("Configuration timed out.")

    try:
        conn = db_connect()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO servers (server_id, alerts_channel_id, timer_channel_id) VALUES (?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                alerts_channel_id=excluded.alerts_channel_id,
                timer_channel_id=excluded.timer_channel_id,
                overview_message_id=NULL;
        """, (interaction.guild_id, answers['alerts_channel_id'], answers['timer_channel_id']))
        conn.commit()
        conn.close()
        await dm_channel.send("✅ **Configuration saved!** Posting the live overview now...")
        await post_or_update_overview(interaction.guild_id)
        await dm_channel.send("✅ Done! Check your timer channel for the live overview embed.")
    except Exception as e:
        await dm_channel.send(f"❌ **Error saving configuration!**\n`{e}`")

@bot.tree.command(name="wipe_my_data", description="Admin Only: Permanently deletes all data for this server.")
@app_commands.checks.has_permissions(administrator=True)
async def wipe_my_data(interaction: discord.Interaction):
    view = ConfirmationView(author=interaction.user)
    await interaction.response.send_message("**Are you absolutely sure?** This will permanently delete all timers and configuration for this server.", view=view, ephemeral=True)
    await view.wait()
    if view.value:
        conn = db_connect()
        conn.cursor().execute("DELETE FROM servers WHERE server_id = ?", (interaction.guild_id,))
        conn.commit()
        conn.close()
        await interaction.followup.send("All data for this server has been wiped.", ephemeral=True)
    elif view.value is False:
        await interaction.followup.send("Deletion cancelled.", ephemeral=True)

# --- Automated Background Task ---
@tasks.loop(minutes=1)
async def announcement_loop():
    # Post announcements for windows opening within the configured lead time.
    # announced_for stores the window start already announced: one ping per window.
    now = datetime.now(timezone.utc)
    conn = db_connect()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT a.server_id, a.boss_key, a.channel_id, a.role_ids, a.lead_minutes,
               a.announced_for, t.start_time
        FROM announcements a
        JOIN timer_states t ON t.server_id = a.server_id AND t.boss_key = a.boss_key
        WHERE t.status = 'active'
    """).fetchall()
    for server_id, boss_key, channel_id, role_ids, lead, announced_for, start_iso in rows:
        start = datetime.fromisoformat(start_iso)
        if announced_for == start_iso:
            continue
        if not (start - timedelta(minutes=lead) <= now < start):
            continue
        config = await _get_boss_config(server_id, boss_key)
        if not config:
            continue
        mentions = " ".join(f"<@&{rid}>" for rid in (role_ids or "").split(",") if rid)
        try:
            channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            await channel.send(
                (f"⏰ {config.get('emoji', '')} **{config['name']}** window opens "
                 f"<t:{int(start.timestamp())}:R>! " + mentions).strip(),
                allowed_mentions=discord.AllowedMentions(roles=True))
            cur.execute("UPDATE announcements SET announced_for = ? WHERE server_id = ? AND boss_key = ?",
                        (start_iso, server_id, boss_key))
        except (discord.NotFound, discord.Forbidden):
            pass
    conn.commit()
    conn.close()


@announcement_loop.before_loop
async def before_announcement_loop():
    await bot.wait_until_ready()


@tasks.loop(minutes=1)
async def check_all_boss_windows():
    conn = db_connect()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    active_timers = cursor.execute("SELECT * FROM timer_states WHERE status = 'active'").fetchall()

    affected_servers: set[int] = set()

    for timer in active_timers:
        end_time = datetime.fromisoformat(timer['end_time'])
        if datetime.now(timezone.utc) <= end_time:
            continue

        server_config = cursor.execute("SELECT * FROM servers WHERE server_id = ?", (timer['server_id'],)).fetchone()
        if not server_config or not server_config['lost_window_enabled']:
            continue

        config = await _get_boss_config(timer['server_id'], timer['boss_key'])
        if not config:
            continue

        new_duration_hours = timer['duration_hours'] + 4
        if new_duration_hours > 16:
            cursor.execute("UPDATE timer_states SET status = 'paused' WHERE server_id = ? AND boss_key = ?", (timer['server_id'], timer['boss_key']))
            affected_servers.add(timer['server_id'])
            try:
                alert_channel = await bot.fetch_channel(server_config['alerts_channel_id'])
                if isinstance(alert_channel, (discord.TextChannel, discord.Thread)):
                    await alert_channel.send(f"🔥 The **{config['name']}** window has exceeded 16h and is now **paused**. Use `/tod <boss> set` to reset.")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                print(f"BACKGROUND TASK: Could not send alert to channel {server_config['alerts_channel_id']}: {e}")
        else:
            start_time = datetime.fromisoformat(timer['start_time'])
            new_start_time = start_time + timedelta(hours=config['lost_respawn_shift_hours'])
            new_end_time = new_start_time + timedelta(hours=new_duration_hours)
            cursor.execute(
                "UPDATE timer_states SET start_time=?, end_time=?, duration_hours=? WHERE server_id=? AND boss_key=?",
                (new_start_time.isoformat(), new_end_time.isoformat(), new_duration_hours, timer['server_id'], timer['boss_key'])
            )
            affected_servers.add(timer['server_id'])

    conn.commit()
    conn.close()

    for server_id in affected_servers:
        await post_or_update_overview(server_id)

# --- Bot Startup ---
@bot.event
async def on_ready():
    if bot.user is not None:
        print(f'Logged in as {bot.user.name} ({bot.user.id})')
    else:
        print('Logged in, but bot.user is None.')

    setup_database()

    # Register persistent view so buttons survive bot restarts
    persistent_entries: list[tuple[str, str, Optional[str]]] = [
        (k, v['name'], v.get('emoji')) for k, v in BOSS_CONFIG.items()
    ]
    conn = db_connect()
    all_custom = conn.cursor().execute("SELECT DISTINCT boss_key, name FROM custom_bosses").fetchall()
    conn.close()
    persistent_entries += [(key, name, None) for key, name in all_custom]
    bot.add_view(BossTimerView(persistent_entries))
    bot.add_view(PublicBossTimerView())
    # Re-register revert buttons for any history rows not yet reverted (survive restarts).
    try:
        conn_r = db_connect()
        pending = conn_r.cursor().execute("SELECT id FROM tod_history WHERE reverted = 0").fetchall()
        conn_r.close()
        for (hid,) in pending:
            bot.add_view(RevertView(hid))
    except Exception as e:
        print(f"Could not re-register revert views: {e}")

    bot.tree.add_command(tod_group)
    bot.tree.add_command(boss_group)
    bot.tree.add_command(options_group)

    try:
        # Dynamic per-guild sync: instant command updates on every connected server.
        # Falls back to global sync if the bot is in many guilds (rate-limit safety).
        if len(bot.guilds) <= 25:
            for guild in bot.guilds:
                try:
                    bot.tree.copy_global_to(guild=guild)
                    await bot.tree.sync(guild=guild)
                except discord.HTTPException as e:
                    print(f"WARN: Could not sync commands to guild {guild.id}: {e}")
            print(f"Slash commands synced to {len(bot.guilds)} guild(s).")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally.")
    except discord.HTTPException as e:
        print(f"ERROR: Failed to sync slash commands: {e}")

    # Refresh overview embeds for all configured servers
    conn = db_connect()
    server_ids = [row[0] for row in conn.cursor().execute("SELECT server_id FROM servers").fetchall()]
    conn.close()
    for server_id in server_ids:
        await post_or_update_overview(server_id)

    check_all_boss_windows.start()
    cleanup_stale_servers.start()
    announcement_loop.start()

@check_all_boss_windows.before_loop
async def before_check():
    await bot.wait_until_ready()

@tasks.loop(hours=6)
async def cleanup_stale_servers():
    """Remove data for servers the bot can no longer reach (kicked, or the
    configured channel was deleted / access lost) after a 7-day grace period.
    A healthy but quiet server is never touched."""
    now = datetime.now(timezone.utc)
    conn = db_connect()
    cursor = conn.cursor()
    rows = cursor.execute("SELECT server_id, timer_channel_id, unreachable_since FROM servers").fetchall()
    for server_id, channel_id, since in rows:
        reachable = False
        guild = bot.get_guild(server_id)
        if guild is not None:
            channel = guild.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await bot.fetch_channel(channel_id)
                except (discord.NotFound, discord.Forbidden):
                    channel = None
                except discord.HTTPException:
                    continue
            reachable = channel is not None
        if reachable:
            if since:
                cursor.execute("UPDATE servers SET unreachable_since = NULL WHERE server_id = ?", (server_id,))
                print(f"STALE-CHECK: server {server_id} reachable again; grace period cleared.")
        else:
            if not since:
                cursor.execute("UPDATE servers SET unreachable_since = ? WHERE server_id = ?", (now.isoformat(), server_id))
                print(f"STALE-CHECK: server {server_id} unreachable; 7-day grace period started.")
            elif now - datetime.fromisoformat(since) > timedelta(days=7):
                cursor.execute("DELETE FROM timer_states WHERE server_id = ?", (server_id,))
                cursor.execute("DELETE FROM custom_bosses WHERE server_id = ?", (server_id,))
                cursor.execute("DELETE FROM servers WHERE server_id = ?", (server_id,))
                print(f"STALE-CHECK: server {server_id} unreachable for 7+ days; all data removed.")
    conn.commit()
    conn.close()


@cleanup_stale_servers.before_loop
async def before_cleanup():
    await bot.wait_until_ready()


@bot.event
async def on_guild_join(guild: discord.Guild):
    """Register slash commands immediately when the bot joins a new server."""
    try:
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"Synced commands to new guild: {guild.name} ({guild.id})")
    except discord.HTTPException as e:
        print(f"ERROR: Failed to sync commands to new guild {guild.id}: {e}")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Administrator** permission to use this command."
    elif isinstance(error, app_commands.CheckFailure):
        msg = "❌ You don't have permission to use this command."
    else:
        print(f"Unhandled command error: {error}")
        msg = "⚠️ Something went wrong while running this command."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        print("FATAL ERROR: DISCORD_BOT_TOKEN is missing from environment variables.")  # type: ignore
    else:
        bot.run(DISCORD_BOT_TOKEN)
