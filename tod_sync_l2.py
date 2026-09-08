"""L2-ToD adapter + admin `/sync` panel for other TOD-sync bots.

Timer commands stay in bot.py. Peers come from TOD_SYNC_PEERS in .env.
Every raid boss is exported to those peers; `/sync` is only for import.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import discord
from discord import app_commands

from tod_sync import TodSyncBridge, origin_slug, parse_peer_endpoints

logger = logging.getLogger("l2-tod-sync")

GetBossConfig = Callable[[Optional[int], str], Awaitable[Optional[dict]]]
PostOverview = Callable[[Optional[int]], Awaitable[None]]
DbConnect = Callable[[], Any]
IsConfigured = Callable[[discord.Interaction], Awaitable[bool]]

AUTO_PULL_MINUTES = 60

BOSS_ALIASES: dict[str, tuple[str, ...]] = {
    "AQ": ("AQ", "QA", "Queen Ant", "Ant Queen"),
    "CORE": ("CORE", "Core"),
    "ORFEN": ("ORFEN", "Orfen"),
    "BAIUM": ("BAIUM", "Baium"),
    "ANTHARAS": ("ANTHARAS", "Antharas"),
    "VALAKAS": ("VALAKAS", "Valakas"),
    "ELPY": ("ELPY", "Epidos"),
    "BELETH": ("BELETH", "Beleth"),
}


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm(value: str) -> str:
    return value.strip().lower()


def _auto_pull_label() -> str:
    if AUTO_PULL_MINUTES == 60:
        return "once an hour"
    if AUTO_PULL_MINUTES % 60 == 0:
        hours = AUTO_PULL_MINUTES // 60
        return "once an hour" if hours == 1 else f"every {hours} hours"
    return f"every {AUTO_PULL_MINUTES} min"


def _candidate_status_label(status: str) -> str:
    return {
        "no local window": "not on this bot yet",
        "already here": "already posted here",
        "same TOD locally": "same TOD locally",
        "newer than local": "newer than local",
        "older than local": "older than local",
    }.get(status, status)


@dataclass
class SyncPullCandidate:
    key: str
    origin: str
    peer_name: str
    boss_name: str
    tod_utc: datetime
    status: str
    report: dict[str, Any]


class L2TodSyncService:
    def __init__(
        self,
        bot: Any,
        *,
        db_connect: DbConnect,
        boss_config: dict,
        get_boss_config: GetBossConfig,
        post_or_update_overview: PostOverview,
    ) -> None:
        self.bot = bot
        self._db_connect = db_connect
        self._boss_config = boss_config
        self._get_boss_config = get_boss_config
        self._post_or_update_overview = post_or_update_overview
        self.origin = os.getenv("TOD_SYNC_ORIGIN", "").strip()
        self.name = os.getenv("TOD_SYNC_NAME", "").strip() or self.origin or "l2-tod"
        self.secret = os.getenv("TOD_SYNC_SECRET", "").strip()
        self.host = os.getenv("TOD_SYNC_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.port = int(os.getenv("TOD_SYNC_PORT", "8080") or "8080")
        self.extra_urls = parse_peer_endpoints(os.getenv("TOD_SYNC_PEERS", ""))
        guild_raw = os.getenv("TOD_SYNC_GUILD_ID", "").strip()
        self.guild_id_override = int(guild_raw) if guild_raw else None
        self._alias_to_key = self._build_alias_map()
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self.bridge: TodSyncBridge | None = None
        if self.origin and self.secret:
            self.bridge = TodSyncBridge(
                origin=self.origin,
                name=self.name,
                secret=self.secret,
                host=self.host,
                port=self.port,
                handler=self._unused_push_handler,
                info_provider=self.info_payload,
                latest_provider=self.latest_payload,
                fetch_pending=lambda: [],
                mark_delivered=lambda _row_id: None,
                mark_attempt=lambda _row_id, _err: None,
                extra_urls=self.extra_urls,
            )

    @property
    def sync_peers(self):
        if self.bridge is None:
            return []
        return list(self.bridge.peers)

    def ensure_schema(self) -> None:
        conn = self._db_connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def get_setting(self, key: str) -> str:
        conn = self._db_connect()
        try:
            row = conn.cursor().execute(
                "SELECT value FROM sync_settings WHERE key = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
        return str(row[0]) if row else ""

    def set_setting(self, key: str, value: str) -> None:
        conn = self._db_connect()
        try:
            conn.cursor().execute(
                """
                INSERT INTO sync_settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            conn.commit()
        finally:
            conn.close()

    def is_auto_pull_enabled(self) -> bool:
        return self.get_setting("auto_pull") == "1"

    def set_auto_pull_enabled(self, enabled: bool) -> None:
        self.set_setting("auto_pull", "1" if enabled else "0")

    def peer_allowed(self, origin: str) -> bool:
        """Listing TOD_SYNC_PEERS enables export to any signed peer origin."""
        if not self.extra_urls:
            return False
        return bool(origin_slug(origin or ""))

    def _build_alias_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for key, aliases in BOSS_ALIASES.items():
            mapping[_norm(key)] = key
            for alias in aliases:
                mapping[_norm(alias)] = key
        for key, cfg in self._boss_config.items():
            mapping[_norm(key)] = key
            mapping[_norm(str(cfg.get("name") or key))] = key
        return mapping

    def resolve_boss_key(self, raw: str) -> Optional[str]:
        if not raw:
            return None
        mapped = self._alias_to_key.get(_norm(raw))
        if mapped:
            return mapped
        conn = self._db_connect()
        try:
            rows = conn.cursor().execute(
                "SELECT boss_key, name FROM custom_bosses"
            ).fetchall()
        finally:
            conn.close()
        needle = _norm(raw)
        for key, name in rows:
            if _norm(str(key)) == needle or _norm(str(name)) == needle:
                return str(key)
        return None

    def display_name(self, boss_key: str, guild_id: Optional[int] = None) -> str:
        cfg = self._boss_config.get(boss_key.upper())
        if cfg:
            return str(cfg.get("name") or boss_key)
        conn = self._db_connect()
        try:
            if guild_id is None:
                row = conn.cursor().execute(
                    "SELECT name FROM custom_bosses WHERE boss_key = ? LIMIT 1",
                    (boss_key.upper(),),
                ).fetchone()
            else:
                row = conn.cursor().execute(
                    "SELECT name FROM custom_bosses WHERE server_id = ? AND boss_key = ?",
                    (guild_id, boss_key.upper()),
                ).fetchone()
        finally:
            conn.close()
        return row[0] if row else boss_key

    def set_sync_guild_id(self, guild_id: int) -> None:
        self.set_setting("guild_id", str(int(guild_id)))

    def sync_guild_id(self) -> Optional[int]:
        if self.guild_id_override:
            return self.guild_id_override
        stored = self.get_setting("guild_id")
        if stored:
            try:
                return int(stored)
            except ValueError:
                pass
        conn = self._db_connect()
        try:
            row = conn.cursor().execute(
                "SELECT server_id FROM servers ORDER BY server_id LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else None

    def all_bosses(self, guild_id: Optional[int]) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = [
            (key, str(cfg.get("name") or key)) for key, cfg in self._boss_config.items()
        ]
        if guild_id is not None:
            conn = self._db_connect()
            try:
                rows = conn.cursor().execute(
                    "SELECT boss_key, name FROM custom_bosses WHERE server_id = ?",
                    (guild_id,),
                ).fetchall()
            finally:
                conn.close()
            entries += [(str(key), str(name)) for key, name in rows]
        return entries

    def public_bosses(self, guild_id: Optional[int]) -> list[tuple[str, str]]:
        return self.all_bosses(guild_id)

    def is_boss_public(self, boss_key: str, guild_id: Optional[int]) -> bool:
        return any(key.upper() == boss_key.upper() for key, _name in self.all_bosses(guild_id))

    async def info_payload(self, requester_origin: str = "") -> dict[str, Any]:
        allowed = self.peer_allowed(requester_origin)
        guild_id = self.sync_guild_id()
        public = self.public_bosses(guild_id) if allowed else []
        names = sorted({name for _key, name in public})
        all_names = sorted({name for _key, name in self.all_bosses(guild_id)}) if allowed else []
        return {
            "ok": True,
            "origin": self.origin,
            "name": self.name,
            "public": bool(names),
            "bosses": all_names,
            "public_bosses": names,
            "pull_only": True,
            "peer_allowed": allowed,
        }

    async def latest_payload(self, requester_origin: str = "") -> dict[str, Any]:
        if not self.peer_allowed(requester_origin):
            return {
                "ok": False,
                "error": "not_allowed",
                "status": 403,
                "origin": self.origin,
                "name": self.name,
                "public_bosses": [],
                "reports": [],
            }
        guild_id = self.sync_guild_id()
        public = self.public_bosses(guild_id)
        public_names = sorted({name for _key, name in public})
        if not public_names:
            return {
                "ok": False,
                "error": "public_off",
                "status": 403,
                "origin": self.origin,
                "name": self.name,
                "public_bosses": [],
                "reports": [],
            }
        if guild_id is None:
            return {
                "ok": True,
                "origin": self.origin,
                "name": self.name,
                "public_bosses": public_names,
                "reports": [],
            }
        allowed_keys = {key.upper() for key, _name in public}
        conn = self._db_connect()
        try:
            rows = conn.cursor().execute(
                """
                SELECT boss_key, tod_time, start_time, end_time, status
                FROM timer_states
                WHERE server_id = ?
                """,
                (guild_id,),
            ).fetchall()
        finally:
            conn.close()
        now = datetime.now(timezone.utc)
        reports: list[dict[str, Any]] = []
        for boss_key, tod_time, start_time, end_time, status in rows:
            if str(boss_key).upper() not in allowed_keys:
                continue
            if status == "paused":
                continue
            try:
                end_dt = _parse_utc(end_time) if end_time else None
            except ValueError:
                end_dt = None
            if end_dt is not None and end_dt < now:
                continue
            tod_raw = tod_time or start_time
            if not tod_raw:
                continue
            try:
                tod_utc = _parse_utc(tod_raw)
            except ValueError:
                continue
            name = self.display_name(str(boss_key), guild_id)
            reports.append(
                {
                    "action": "reported",
                    "sync_id": f"l2tod-{guild_id}-{boss_key}-{int(tod_utc.timestamp())}",
                    "origin": self.origin,
                    "boss_name": name,
                    "who_killed": self.name,
                    "tod_utc": tod_utc.isoformat(),
                    "dropped": True,
                    "reporter_name": self.name,
                }
            )
        return {
            "ok": True,
            "origin": self.origin,
            "name": self.name,
            "public_bosses": public_names,
            "reports": reports,
        }

    async def _unused_push_handler(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "skipped": "pull_only"}

    def _local_tod(self, guild_id: int, boss_key: str) -> Optional[datetime]:
        conn = self._db_connect()
        try:
            row = conn.cursor().execute(
                "SELECT tod_time FROM timer_states WHERE server_id = ? AND boss_key = ?",
                (guild_id, boss_key.upper()),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return None
        try:
            return _parse_utc(row[0])
        except ValueError:
            return None

    async def apply_report(self, report: dict[str, Any]) -> dict[str, Any]:
        origin = str(report.get("origin") or "")
        if origin and not self.peer_allowed(origin):
            return {"ok": True, "skipped": "not_allowed"}
        boss_name = str(report.get("boss_name") or "")
        boss_key = self.resolve_boss_key(boss_name)
        if not boss_key:
            return {"ok": True, "skipped": "boss_not_in_config"}
        guild_id = self.sync_guild_id()
        if guild_id is None:
            return {"ok": True, "skipped": "no_local_guild"}
        if not self.is_boss_public(boss_key, guild_id):
            return {"ok": True, "skipped": "boss_not_allowed"}
        tod_raw = str(report.get("tod_utc") or "")
        try:
            tod_utc = _parse_utc(tod_raw)
        except ValueError:
            return {"ok": False, "error": "invalid_tod"}
        config = await self._get_boss_config(guild_id, boss_key)
        if not config:
            return {"ok": True, "skipped": "boss_not_in_config"}
        local_tod = self._local_tod(guild_id, boss_key)
        if local_tod == tod_utc:
            return {"ok": True, "skipped": "same TOD locally"}
        if local_tod is not None and local_tod > tod_utc:
            return {"ok": True, "skipped": "older than local"}
        duration_hours = config["duration_hours"]
        event_start_time = tod_utc + timedelta(hours=config["respawn_hours"])
        event_end_time = event_start_time + timedelta(hours=duration_hours)
        conn = self._db_connect()
        try:
            conn.cursor().execute(
                """
                INSERT INTO timer_states (server_id, boss_key, tod_time, start_time, end_time, duration_hours, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(server_id, boss_key) DO UPDATE SET
                    tod_time=excluded.tod_time, start_time=excluded.start_time,
                    end_time=excluded.end_time, duration_hours=excluded.duration_hours, status=excluded.status;
                """,
                (
                    guild_id,
                    boss_key.upper(),
                    tod_utc.isoformat(),
                    event_start_time.isoformat(),
                    event_end_time.isoformat(),
                    duration_hours,
                    "active",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        await self._post_or_update_overview(guild_id)
        logger.info("Applied synced TOD for %s from %s", boss_key, origin or "peer")
        return {"ok": True, "applied": self.display_name(boss_key, guild_id)}

    async def discover_peers(self) -> None:
        if self.bridge is None:
            return
        peers = await self.bridge.discover_peers("", [], extra_urls=self.extra_urls)
        summary = ", ".join(
            f"{peer.name} ({peer.origin} @ {peer.url})" for peer in peers
        ) or "none"
        logger.info(
            "TOD sync peers from TOD_SYNC_PEERS (%s URL(s)): %s",
            len(self.extra_urls),
            summary,
        )

    async def collect_pull_candidates(self) -> list[SyncPullCandidate]:
        if self.bridge is None or not self.sync_peers:
            return []
        guild_id = self.sync_guild_id()
        candidates: list[SyncPullCandidate] = []
        seen: set[str] = set()
        for peer in self.sync_peers:
            data = await self.bridge.fetch_latest(peer)
            if not data.get("ok"):
                continue
            for report in data.get("reports") or []:
                if not isinstance(report, dict):
                    continue
                boss_name = str(report.get("boss_name") or "")
                boss_key = self.resolve_boss_key(boss_name)
                if not boss_key:
                    continue
                origin = str(report.get("origin") or peer.origin)
                payload = dict(report)
                payload.setdefault("origin", origin)
                tod_raw = str(payload.get("tod_utc") or "")
                try:
                    tod_utc = _parse_utc(tod_raw)
                except ValueError:
                    continue
                key = f"{origin}|{boss_key}"
                if key in seen:
                    continue
                seen.add(key)
                local = self._local_tod(guild_id, boss_key) if guild_id else None
                sync_id = str(payload.get("sync_id") or "")
                if local is None:
                    status = "no local window"
                elif local == tod_utc:
                    status = "same TOD locally"
                elif local < tod_utc:
                    status = "newer than local"
                else:
                    status = "older than local"
                if sync_id and local == tod_utc:
                    status = "already here"
                display = self.display_name(boss_key, guild_id)
                candidates.append(
                    SyncPullCandidate(
                        key=key,
                        origin=origin,
                        peer_name=peer.name,
                        boss_name=display,
                        tod_utc=tod_utc,
                        status=status,
                        report=payload,
                    )
                )
        candidates.sort(key=lambda item: (item.boss_name.lower(), item.peer_name.lower()))
        return candidates

    async def pull_selected(self, keys: list[str]) -> str:
        wanted = {key for key in keys if key}
        if not wanted:
            return "No TOD windows selected."
        if self.bridge is None:
            return "Sync is not configured on this host."
        if not self.extra_urls:
            return "Add peer URLs in TOD_SYNC_PEERS, then restart."
        if not self.sync_peers:
            return "No env peers are online yet. Check TOD_SYNC_PEERS host:port and Refresh."
        applied_names: list[str] = []
        skipped = 0
        missing = 0
        matched = 0
        lines: list[str] = []
        for peer in self.sync_peers:
            data = await self.bridge.fetch_latest(peer)
            if not data.get("ok"):
                if data.get("error") == "public_off" or data.get("status") == 403:
                    continue
                lines.append(f"**{peer.name}**: {data.get('error') or 'unreachable'}")
                continue
            for report in data.get("reports") or []:
                if not isinstance(report, dict):
                    skipped += 1
                    continue
                boss_name = str(report.get("boss_name") or "")
                boss_key = self.resolve_boss_key(boss_name)
                if not boss_key:
                    continue
                origin = str(report.get("origin") or peer.origin)
                key = f"{origin}|{boss_key}"
                if key not in wanted:
                    continue
                matched += 1
                payload = dict(report)
                payload.setdefault("origin", origin)
                result = await self.apply_report(payload)
                if result.get("applied"):
                    applied_names.append(f"{result['applied']} ({peer.name})")
                elif result.get("skipped") == "boss_not_in_config":
                    missing += 1
                else:
                    skipped += 1
        if applied_names:
            lines.append("Copied here: " + ", ".join(applied_names))
        if skipped:
            lines.append(f"Skipped `{skipped}`")
        if missing:
            lines.append(f"Unknown bosses `{missing}`")
        if not matched:
            lines.append("None of the selected bosses were still available on allowed bots.")
        return "\n".join(lines) or "Nothing to copy."

    async def auto_pull_from_peers(self) -> None:
        if not self.is_auto_pull_enabled():
            return
        await self.bot.wait_until_ready()
        await self.discover_peers()
        candidates = await self.collect_pull_candidates()
        keys = [
            item.key
            for item in candidates
            if item.status in {"no local window", "newer than local"}
        ]
        if keys:
            summary = await self.pull_selected(keys)
            logger.info("Auto-import: %s", summary)

    async def _background_loop(self) -> None:
        await self.bot.wait_until_ready()
        while self._running:
            try:
                await self.discover_peers()
                if self.is_auto_pull_enabled():
                    await self.auto_pull_from_peers()
            except Exception as exc:
                logger.warning("TOD sync loop failed: %s", exc)
            await asyncio.sleep(AUTO_PULL_MINUTES * 60)

    async def build_embed(
        self,
        extra: str = "",
        candidates: list[SyncPullCandidate] | None = None,
        section: str = "approvals",
    ) -> discord.Embed:
        local_name = self.name or self.origin or "this bot"
        guild_id = self.sync_guild_id()
        auto_on = self.is_auto_pull_enabled()
        section = "pull" if section == "pull" else "approvals"
        if section == "approvals":
            embed = discord.Embed(
                title="TOD Sync — Peers",
                description=(
                    f"This bot: **{local_name}** (`{self.origin or 'unset'}`)\n"
                    "Peers are the URLs in `TOD_SYNC_PEERS`. Every raid boss on this bot "
                    "is exported to them. Use **Import TODs** only if you want to copy "
                    "their windows here."
                ),
                color=discord.Color.green() if self.extra_urls else discord.Color.dark_grey(),
            )
            rb_names = [name for _key, name in self.all_bosses(guild_id)]
            auto_text = (
                f"On — copy newer matching TODs here {_auto_pull_label()}"
                if auto_on
                else "Off — copy only when you press Import TODs"
            )
            embed.add_field(
                name="Export",
                value=(
                    f"All raid bosses ({len(rb_names)}): "
                    + (", ".join(rb_names) if rb_names else "none")
                    + f"\nAuto-import: {auto_text}"
                ),
                inline=False,
            )
            if self.bridge is None:
                embed.add_field(
                    name="Other bots",
                    value="Set `TOD_SYNC_ORIGIN` and `TOD_SYNC_SECRET` to talk to peers.",
                    inline=False,
                )
            elif not self.extra_urls:
                embed.add_field(
                    name="Other bots",
                    value=(
                        "Add `TOD_SYNC_PEERS` in `.env` (port required): "
                        "Ally1 **8081**, Ally2 **8082**. Example: "
                        "`http://ALLY1_IP:8081,http://ALLY2_IP:8082`"
                    ),
                    inline=False,
                )
            elif not self.sync_peers:
                listed = ", ".join(self.extra_urls)
                embed.add_field(
                    name="Other bots",
                    value=(
                        f"Configured: {listed}\n"
                        "None of those URLs answered `/v1/tod-sync/health` yet. "
                        "Press **Refresh** after the other bot is up."
                    ),
                    inline=False,
                )
            else:
                for peer in self.sync_peers:
                    info = await self.bridge.fetch_info(peer) if self.bridge else {}
                    status = "online" if info.get("ok") else str(info.get("error") or "offline")[:80]
                    they_allow = "yes" if info.get("ok") and info.get("peer_allowed", True) else "not yet / unknown"
                    embed.add_field(
                        name=peer.name,
                        value=(
                            f"`{peer.origin}` · {peer.url}\n"
                            f"Status: **{status}** · They allow this bot: **{they_allow}**"
                        ),
                        inline=False,
                    )
        else:
            embed = discord.Embed(
                title="TOD Sync — Import TODs",
                description=(
                    f"This bot: **{local_name}** (`{self.origin or 'unset'}`)\n"
                    "This copies the other bot's TOD into **this** Discord. "
                    "It does **not** send your TODs anywhere. A newer local TOD is left alone.\n"
                    f"Auto-import is **{'on · ' + _auto_pull_label() if auto_on else 'off'}**."
                ),
                color=discord.Color.green() if self.extra_urls else discord.Color.dark_grey(),
            )
            pull_candidates = candidates if candidates is not None else []
            if not self.extra_urls:
                pull_text = (
                    "Add `TOD_SYNC_PEERS` in `.env` (Ally1 **8081**, Ally2 **8082**), then restart."
                )
            elif not self.sync_peers:
                pull_text = (
                    "No env peer is online yet. Check host:port and press **Refresh**."
                )
            elif pull_candidates:
                lines = []
                for candidate in pull_candidates[:12]:
                    lines.append(
                        f"**{candidate.boss_name}** from {candidate.peer_name}\n"
                        f"<t:{int(candidate.tod_utc.timestamp())}:f> · "
                        f"{_candidate_status_label(candidate.status)}"
                    )
                pull_text = "\n".join(lines)
                if len(pull_candidates) > 12:
                    pull_text += f"\n+{len(pull_candidates) - 12} more in the list"
            else:
                pull_text = (
                    "Nothing to copy. The other bot needs an active window for a boss "
                    "this bot also knows."
                )
            embed.add_field(
                name="How to copy",
                value="1. Tick TOD windows in the list\n2. Press **Copy selected here**",
                inline=False,
            )
            embed.add_field(name="Ready to copy", value=pull_text[:1024], inline=False)
        if extra:
            embed.add_field(name="Last change", value=extra[:1024], inline=False)
        embed.set_footer(text="Boss names must match (aliases on this bot still resolve).")
        return embed

    async def paint_panel(
        self,
        interaction: discord.Interaction,
        *,
        extra: str = "",
        owner_id: int | None = None,
        selected_keys: list[str] | None = None,
        section: str = "approvals",
    ) -> None:
        owner = owner_id if owner_id is not None else interaction.user.id
        section = "pull" if section == "pull" else "approvals"
        await self.discover_peers()
        candidates = await self.collect_pull_candidates() if section == "pull" else []
        embed = await self.build_embed(extra, candidates, section=section)
        view = SyncPanelView(
            self,
            owner_id=owner,
            candidates=candidates,
            selected_keys=selected_keys,
            section=section,
        )
        message = interaction.message
        if message is not None:
            try:
                await message.edit(embed=embed, view=view)
                return
            except discord.HTTPException:
                logger.warning("Failed to refresh sync panel")
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
            return
        await interaction.response.edit_message(embed=embed, view=view)

    async def start(self) -> None:
        self.ensure_schema()
        if self.bridge is None:
            return
        await self.bridge.start()
        if self.extra_urls:
            logger.info("Extra TOD sync peers: %s", ", ".join(self.extra_urls))
        self._running = True
        self._loop_task = asyncio.create_task(self._background_loop())

    async def stop(self) -> None:
        self._running = False
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None
        if self.bridge is not None:
            await self.bridge.stop()


class SyncPanelView(discord.ui.View):
    def __init__(
        self,
        service: L2TodSyncService,
        *,
        owner_id: int | None,
        candidates: list[SyncPullCandidate] | None = None,
        selected_keys: list[str] | None = None,
        section: str = "approvals",
    ) -> None:
        super().__init__(timeout=600)
        self.service = service
        self.owner_id = owner_id
        self.section = "pull" if section == "pull" else "approvals"
        self.candidates = list(candidates or [])
        self.selected_pull_keys = list(selected_keys or [])

        approvals_button = discord.ui.Button(
            label="Peers",
            style=(
                discord.ButtonStyle.success
                if self.section == "approvals"
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        pull_tab_button = discord.ui.Button(
            label="Import TODs",
            style=(
                discord.ButtonStyle.success
                if self.section == "pull"
                else discord.ButtonStyle.secondary
            ),
            row=0,
        )
        refresh_button = discord.ui.Button(
            label="Refresh",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        approvals_button.callback = self._on_section_approvals
        pull_tab_button.callback = self._on_section_pull
        refresh_button.callback = self._on_refresh
        self.add_item(approvals_button)
        self.add_item(pull_tab_button)
        self.add_item(refresh_button)

        if self.section == "approvals":
            auto_on = service.is_auto_pull_enabled()
            auto_button = discord.ui.Button(
                label="Auto-import: On" if auto_on else "Auto-import: Off",
                style=(
                    discord.ButtonStyle.success if auto_on else discord.ButtonStyle.secondary
                ),
                row=1,
            )
            auto_button.callback = self._on_toggle_auto_pull
            self.add_item(auto_button)
        else:
            pull_button = discord.ui.Button(
                label="Copy selected here",
                style=discord.ButtonStyle.primary,
                row=1,
                disabled=not self.candidates,
            )
            pull_button.callback = self._on_pull
            self.add_item(pull_button)

        if self.candidates:
            selected = set(self.selected_pull_keys)
            pull_options = []
            for candidate in self.candidates[:25]:
                pull_options.append(
                    discord.SelectOption(
                        label=f"{candidate.boss_name} · {candidate.peer_name}"[:100],
                        value=candidate.key[:100],
                        description=(
                            f"{candidate.tod_utc:%b %d %H:%M UTC} · "
                            f"{_candidate_status_label(candidate.status)}"
                        )[:100],
                        default=candidate.key in selected,
                    )
                )
            pull_select = discord.ui.Select(
                placeholder="TOD windows to copy into this bot",
                min_values=0,
                max_values=len(pull_options),
                options=pull_options,
                row=2,
            )
            pull_select.callback = self._on_pick_pull
            self.add_item(pull_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        member = interaction.user
        if isinstance(member, discord.Member) and member.guild_permissions.administrator:
            return True
        await interaction.response.send_message(
            "Only an administrator who opened `/sync` can use this panel.",
            ephemeral=True,
        )
        return False

    async def _paint(
        self,
        interaction: discord.Interaction,
        *,
        extra: str = "",
        section: str | None = None,
        selected_keys: list[str] | None = None,
        clear_selected: bool = False,
    ) -> None:
        keys = [] if clear_selected else (
            self.selected_pull_keys if selected_keys is None else selected_keys
        )
        await self.service.paint_panel(
            interaction,
            extra=extra,
            owner_id=self.owner_id,
            selected_keys=keys,
            section=self.section if section is None else section,
        )

    async def _on_section_approvals(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._paint(interaction, section="approvals")

    async def _on_section_pull(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._paint(interaction, section="pull")

    async def _on_toggle_auto_pull(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        enabled = not self.service.is_auto_pull_enabled()
        self.service.set_auto_pull_enabled(enabled)
        if enabled:
            await self.service.auto_pull_from_peers()
            extra = (
                f"Auto-import on. This bot will copy newer matching TODs here "
                f"{_auto_pull_label()}."
            )
        else:
            extra = "Auto-import off. TODs copy here only when you press **Copy selected here**."
        await self._paint(interaction, extra=extra, section="approvals")

    async def _on_pick_pull(self, interaction: discord.Interaction) -> None:
        selected = []
        if interaction.data:
            selected = list(interaction.data.get("values") or [])
        self.selected_pull_keys = selected
        await interaction.response.defer()

    async def _on_pull(self, interaction: discord.Interaction) -> None:
        keys = list(self.selected_pull_keys)
        if not keys:
            await interaction.response.send_message(
                "Select one or more TOD windows, then press **Copy selected here**.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        summary = await self.service.pull_selected(keys)
        await self._paint(
            interaction,
            extra=summary,
            section="pull",
            clear_selected=True,
        )

    async def _on_refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await self._paint(interaction)


def attach_l2_tod_sync(
    bot: Any,
    *,
    db_connect: DbConnect,
    boss_config: dict,
    get_boss_config: GetBossConfig,
    post_or_update_overview: PostOverview,
    is_configured: IsConfigured,
) -> L2TodSyncService:
    service = L2TodSyncService(
        bot,
        db_connect=db_connect,
        boss_config=boss_config,
        get_boss_config=get_boss_config,
        post_or_update_overview=post_or_update_overview,
    )
    bot.tod_sync = service
    original_setup = bot.setup_hook
    original_close = bot.close

    async def setup_hook() -> None:
        await original_setup()
        await service.start()

    async def close() -> None:
        await service.stop()
        await original_close()

    bot.setup_hook = setup_hook
    bot.close = close

    @bot.tree.command(
        name="sync",
        description="Share bosses with another bot, or copy their TOD windows into this Discord.",
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def sync_command(interaction: discord.Interaction) -> None:
        if not await is_configured(interaction):
            return
        if service.bridge is None:
            await interaction.response.send_message(
                "Peer sync is not configured. Set `TOD_SYNC_ORIGIN`, `TOD_SYNC_NAME`, "
                "and `TOD_SYNC_SECRET` (same secret on every bot).",
                ephemeral=True,
            )
            return
        if interaction.guild_id:
            service.set_sync_guild_id(interaction.guild_id)
        await interaction.response.defer(ephemeral=True)
        await service.discover_peers()
        embed = await service.build_embed("", [], section="approvals")
        await interaction.followup.send(
            embed=embed,
            view=SyncPanelView(
                service,
                owner_id=interaction.user.id,
                candidates=[],
                section="approvals",
            ),
            ephemeral=True,
        )

    return service
