"""
commands.py – Slash command /status for checking live Minecraft server status.
"""
from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from server_status.service import (
    build_server_status_embed,
    generate_player_chart_image,
    query_minecraft_server,
)

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.server_status.commands")


class ServerStatusCommands(app_commands.Group, name="server", description="Minecraft server status commands."):
    """Group of Minecraft server commands under /server."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    @app_commands.command(
        name="status",
        description="Check live Darklands SMP server statistics, online players, and ping.",
    )
    async def server_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        data = await query_minecraft_server(
            host=self.cfg.minecraft_server.host,
            port=self.cfg.minecraft_server.port,
        )
        chart_bytes = await generate_player_chart_image()
        has_graph = chart_bytes is not None
        embed = build_server_status_embed(data, self.cfg, has_graph=has_graph)

        if chart_bytes:
            file = discord.File(fp=io.BytesIO(chart_bytes), filename="player_history.png")
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)
