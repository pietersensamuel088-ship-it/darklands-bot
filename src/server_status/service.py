"""
service.py – Minecraft server query, database snapshotting, and graph generation.
Works with Java & Bedrock (Geyser/Floodgate crossplay), querying player counts,
latency, version, and generating a 24-hour player activity line graph.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging
from typing import TYPE_CHECKING, Any, Optional

import aiohttp
import discord
from mcstatus import JavaServer
from sqlalchemy import delete, desc, select

from database.engine import get_session
from database.models import MinecraftStatusHistory

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.server_status.service")


def render_progress_bar(current: int, maximum: int, length: int = 12) -> str:
    """Render a clean block-style percentage progress bar."""
    if maximum <= 0:
        return "░" * length
    pct = min(100.0, max(0.0, (current / maximum) * 100.0))
    filled = max(0, min(length, round((pct / 100.0) * length)))
    return "█" * filled + "░" * (length - filled)


async def query_minecraft_server(host: str, port: int = 25565) -> dict[str, Any]:
    """Query the Minecraft server via Server List Ping (SLP).

    Returns a dictionary of status data.
    """
    address = f"{host}:{port}" if port != 25565 else host
    try:
        server = await JavaServer.async_lookup(address)
        status = await asyncio.wait_for(server.async_status(), timeout=6.0)

        sample_names = []
        if status.players and status.players.sample:
            sample_names = [p.name for p in status.players.sample if p.name]

        version_name = status.version.name if status.version else "Crossplay"

        return {
            "online": True,
            "players_online": status.players.online if status.players else 0,
            "players_max": status.players.max if status.players else 0,
            "ping_ms": round(status.latency, 1),
            "version": version_name,
            "sample": sample_names,
            "motd": str(status.description) if status.description else "",
        }
    except Exception as exc:
        log.debug("Server query error for %s: %s", address, exc)
        return {
            "online": False,
            "players_online": 0,
            "players_max": 0,
            "ping_ms": None,
            "version": "Offline",
            "sample": [],
            "motd": "",
        }


async def record_status_snapshot(data: dict[str, Any]) -> None:
    """Save a status snapshot in SQLite and purge data older than 7 days."""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        async with get_session() as session:
            record = MinecraftStatusHistory(
                timestamp=now,
                is_online=data.get("online", False),
                players_online=data.get("players_online", 0),
                players_max=data.get("players_max", 0),
                ping_ms=data.get("ping_ms"),
                version=data.get("version"),
            )
            session.add(record)

            # Cleanup older than 7 days
            cutoff = now - datetime.timedelta(days=7)
            await session.execute(
                delete(MinecraftStatusHistory).where(MinecraftStatusHistory.timestamp < cutoff)
            )
    except Exception as exc:
        log.warning("Could not record server status snapshot: %s", exc)


async def generate_player_chart_image() -> Optional[bytes]:
    """Generate a sleek dark-themed 24-hour player activity line graph via QuickChart.

    Returns raw PNG bytes, or None if insufficient data or unreachable.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=24)

    try:
        async with get_session() as session:
            stmt = (
                select(MinecraftStatusHistory)
                .where(MinecraftStatusHistory.timestamp >= since)
                .order_by(MinecraftStatusHistory.timestamp.asc())
            )
            res = await session.execute(stmt)
            rows = res.scalars().all()

        if len(rows) < 2:
            return None

        # Sample points to max 48 points (every ~30 mins) for a clean chart
        step = max(1, len(rows) // 48)
        sampled_rows = rows[::step]

        labels = [r.timestamp.strftime("%H:%M") for r in sampled_rows]
        data_points = [r.players_online for r in sampled_rows]
        max_seen = max(data_points) if data_points else 10
        y_max = max(max_seen + 2, 5)

        chart_config = {
            "type": "line",
            "data": {
                "labels": labels,
                "datasets": [
                    {
                        "label": "Online Players",
                        "data": data_points,
                        "borderColor": "#5865F2",
                        "backgroundColor": "rgba(88, 101, 242, 0.2)",
                        "fill": True,
                        "tension": 0.35,
                        "pointRadius": 0,
                        "borderWidth": 2,
                    }
                ],
            },
            "options": {
                "responsive": True,
                "title": {
                    "display": True,
                    "text": "24-Hour Player Activity",
                    "fontColor": "#FFFFFF",
                    "fontSize": 14,
                },
                "legend": {"display": False},
                "scales": {
                    "xAxes": [
                        {
                            "gridLines": {"color": "rgba(255, 255, 255, 0.08)"},
                            "ticks": {
                                "fontColor": "#99AAB5",
                                "maxTicksLimit": 8,
                                "fontSize": 10,
                            },
                        }
                    ],
                    "yAxes": [
                        {
                            "gridLines": {"color": "rgba(255, 255, 255, 0.08)"},
                            "ticks": {
                                "fontColor": "#99AAB5",
                                "beginAtZero": True,
                                "stepSize": 1,
                                "max": y_max,
                                "fontSize": 10,
                            },
                        }
                    ],
                },
            },
        }

        # Request PNG from QuickChart
        async with aiohttp.ClientSession() as http:
            payload = {
                "chart": chart_config,
                "width": 600,
                "height": 220,
                "backgroundColor": "#1E1F22",
                "format": "png",
            }
            async with http.post(
                "https://quickchart.io/chart",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                log.warning("QuickChart returned HTTP %s", resp.status)
                return None
    except Exception as exc:
        log.debug("Could not generate player chart: %s", exc)
        return None


def build_server_status_embed(
    data: dict[str, Any],
    cfg: BotConfig,
    has_graph: bool = False,
) -> discord.Embed:
    """Build the rich embed representing live Minecraft server status."""
    is_online = data.get("online", False)
    players = data.get("players_online", 0)
    max_players = data.get("players_max", 0)
    ping = data.get("ping_ms")
    version = data.get("version", "Unknown")
    sample = data.get("sample", [])

    colour = 0x57F287 if is_online else 0xED4245
    status_text = "🟢 **Server is Online & Operational**" if is_online else "🔴 **Server is Currently Offline**"

    embed = discord.Embed(
        title=f"📡 {cfg.minecraft_server.display_name} — Live Server Status",
        description=f"{status_text}\n─────────────────────────────",
        colour=colour,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )

    # Server IPs
    embed.add_field(
        name="🖥️ Java Edition",
        value=f"IP: `{cfg.minecraft_server.host}`\nPort: `{cfg.minecraft_server.port}`",
        inline=True,
    )
    embed.add_field(
        name="📱 Bedrock / Geyser",
        value=f"IP: `{cfg.minecraft_server.host}`\nPort: `{cfg.minecraft_server.bedrock_port}`",
        inline=True,
    )
    embed.add_field(
        name="⚡ Status & Ping",
        value=f"{'Online' if is_online else 'Offline'} • `{ping}ms`" if ping is not None else "Offline",
        inline=True,
    )

    # Player Count Bar
    bar = render_progress_bar(players, max_players)
    embed.add_field(
        name=f"👥 Players ({players}/{max_players})",
        value=f"`{bar}` **{players}/{max_players}**",
        inline=False,
    )

    # Version & Info
    embed.add_field(
        name="🏷️ Version",
        value=f"`{version}` *(Crossplay Enabled)*",
        inline=True,
    )

    if sample:
        player_list = ", ".join(f"`{name}`" for name in sample[:12])
        if len(sample) > 12:
            player_list += f" *+{len(sample) - 12} more*"
        embed.add_field(
            name="🎮 Active Players",
            value=player_list,
            inline=False,
        )

    if has_graph:
        embed.set_image(url="attachment://player_history.png")

    embed.set_footer(
        text=f"Darklands SMP • Auto-refreshes every {cfg.minecraft_server.update_interval_seconds}s"
    )
    return embed
