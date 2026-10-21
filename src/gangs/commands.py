"""
commands.py – Slash commands for submitting and managing Gang Recruitments.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands
from sqlalchemy import select

from database.engine import get_session
from database.models import DiscordGangRecruitment
from gangs.views import (
    GangItemView,
    GangLaunchView,
    GangRecruitModal,
    build_recruitment_embed,
)
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.gangs.commands")


class GangCommands(app_commands.Group, name="gangs", description="Darklands SMP Gang Recruitment commands."):
    """Group of gang commands under /gangs."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # /gangs recruit
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="recruit",
        description="Post a recruitment ad for your criminal syndicate or gang.",
    )
    async def recruit(self, interaction: discord.Interaction) -> None:
        modal = GangRecruitModal(self.cfg)
        await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────────────────────
    # /gangs panel
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="panel",
        description="Post the permanent Gang Recruitment launcher panel in this channel.",
    )
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post the gang recruitment panel.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="⚔ DarkLands SMP — Syndicate Recruitment",
            description=(
                "Looking to recruit loyal members for your criminal syndicate, or join an active gang?\n\n"
                "• Click **Post Recruitment Ad** below to showcase your gang's playstyle and requirements.\n"
                "• Browse existing ads in this channel, click **Apply** to submit an application, or contact the leader directly!\n\n"
                "Build your turf, raid vaults, and dominate the DarkLands leaderboards!"
            ),
            colour=0xC1121F,
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        embed.set_footer(text="DarkLands SMP • Gang Recruitment Hub")

        view = GangLaunchView(self.cfg)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Gang Recruitment panel posted successfully!", ephemeral=True
        )

    # ──────────────────────────────────────────────────────────────────────────
    # /gangs close
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="close",
        description="Close a gang recruitment ad when your slots are full.",
    )
    @app_commands.describe(recruitment_number="The gang recruitment number (e.g. 16)")
    async def close_recruitment(
        self, interaction: discord.Interaction, recruitment_number: int
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordGangRecruitment).where(
                DiscordGangRecruitment.recruitment_number == recruitment_number
            )
            res = await session.execute(stmt)
            rec = res.scalar_one_or_none()

            if not rec:
                await interaction.followup.send(
                    f"❌ Gang recruitment #{recruitment_number} not found.", ephemeral=True
                )
                return

            # Check if leader or staff
            if interaction.user.id != rec.leader_id and not is_staff(interaction.user, self.cfg):
                await interaction.followup.send(
                    "❌ You must be the gang leader or staff to close this recruitment ad.",
                    ephemeral=True,
                )
                return

            rec.status = "Closed"
            await session.commit()

            # Update original message
            if rec.channel_id and rec.message_id:
                channel = interaction.guild.get_channel(rec.channel_id)
                if channel:
                    try:
                        msg = await channel.fetch_message(rec.message_id)
                        embed = build_recruitment_embed(rec)
                        embed.title = f"🔒 Gang Recruitment — {rec.gang_name} [CLOSED]"
                        embed.colour = 0x2B2D31
                        # Remove apply button or disable view
                        await msg.edit(embed=embed, view=None)
                    except Exception as e:
                        log.warning("Could not edit message for closed recruitment: %s", e)

            # Close thread if open
            if rec.thread_id:
                thread = interaction.guild.get_thread(rec.thread_id)
                if thread:
                    try:
                        await thread.send("🔒 **This gang recruitment ad has been closed.**")
                        await thread.edit(archived=True, locked=True)
                    except Exception:
                        pass

        await interaction.followup.send(
            f"✅ Gang recruitment #{recruitment_number} ({rec.gang_name}) has been marked as closed.",
            ephemeral=True,
        )
