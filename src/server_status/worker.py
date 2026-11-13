"""
worker.py – Periodic background task for live Minecraft server status tracking.
Queries the Minecraft server, persists snapshots, generates player graphs,
and updates the dashboard message in the server-info channel.
"""
from __future__ import annotations

import asyncio
import io
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord.ext import tasks

from server_status.service import (
    build_server_status_embed,
    generate_player_chart_image,
    query_minecraft_server,
    record_status_snapshot,
)

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.server_status.worker")


class ServerStatusWorker:
    """Background loop that polls Minecraft status and refreshes the Discord dashboard."""

    def __init__(self, bot: discord.Client, cfg: BotConfig) -> None:
        self.bot = bot
        self.cfg = cfg
        self._target_message_id: Optional[int] = None

    def start(self) -> None:
        interval = max(30, self.cfg.minecraft_server.update_interval_seconds)
        self._poll_loop.change_interval(seconds=interval)
        self._poll_loop.start()

    def stop(self) -> None:
        self._poll_loop.stop()

    @tasks.loop(seconds=60)
    async def _poll_loop(self) -> None:
        channel_id = self.cfg.channels.server_info_channel_id
        if not channel_id:
            return

        channel = self.bot.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        try:
            # 1. Query server
            data = await query_minecraft_server(
                host=self.cfg.minecraft_server.host,
                port=self.cfg.minecraft_server.port,
            )

            # 2. Record historical snapshot
            await record_status_snapshot(data)

            # 3. Generate chart image if available
            chart_bytes = await generate_player_chart_image()

            # 4. Build embed
            has_graph = chart_bytes is not None
            embed = build_server_status_embed(data, self.cfg, has_graph=has_graph)

            # 5. Find or create dashboard message
            message: Optional[discord.Message] = None
            if self._target_message_id:
                try:
                    message = await channel.fetch_message(self._target_message_id)
                except discord.NotFound:
                    message = None

            if message is None:
                # Scan recent messages in channel to avoid duplicate posting on restarts
                try:
                    async for msg in channel.history(limit=15):
                        if msg.author.id == self.bot.user.id and msg.embeds:
                            if any("Live Server Status" in (e.title or "") for e in msg.embeds):
                                message = msg
                                self._target_message_id = msg.id
                                break
                except Exception as exc:
                    log.warning("Could not scan channel history: %s", exc)

            # 6. Send or Edit Message
            if message:
                if chart_bytes:
                    file = discord.File(fp=io.BytesIO(chart_bytes), filename="player_history.png")
                    await message.edit(embed=embed, attachments=[file])
                else:
                    await message.edit(embed=embed, attachments=[])
            else:
                if chart_bytes:
                    file = discord.File(fp=io.BytesIO(chart_bytes), filename="player_history.png")
                    new_msg = await channel.send(embed=embed, file=file)
                else:
                    new_msg = await channel.send(embed=embed)
                self._target_message_id = new_msg.id
                log.info("Posted initial Server Status dashboard to channel %d", channel_id)

        except Exception as exc:
            log.exception("Error in ServerStatusWorker loop: %s", exc)

    @_poll_loop.before_loop
    async def _before_loop(self) -> None:
        await self.bot.wait_until_ready()
        log.info("ServerStatusWorker ready.")
