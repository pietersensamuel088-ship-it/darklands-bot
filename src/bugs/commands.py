"""
commands.py – Slash commands for Bug Reports.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from sqlalchemy import select

from bugs.views import (
    BugItemView,
    BugLaunchView,
    BugReportModal,
    build_bug_embed,
)
from database.engine import get_session
from database.models import DiscordBugReport
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.bugs.commands")


class BugCommands(app_commands.Group, name="bug", description="Darklands SMP Bug Report commands."):
    """Group of bug report commands under /bug."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    @app_commands.command(
        name="report",
        description="Report a bug or glitch found on Darklands SMP.",
    )
    async def report_bug(self, interaction: discord.Interaction) -> None:
        modal = BugReportModal(self.cfg)
        await interaction.response.send_modal(modal)

    @app_commands.command(
        name="panel",
        description="Post the interactive 'Report a Bug' panel in this channel.",
    )
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post the bug report panel.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🐛 Darklands SMP — Bug & Exploit Reports",
            description=(
                "Found a bug, glitch, duplication issue, or error in-game or on Discord?\n\n"
                "Click the button below to **Report a Bug**!\n\n"
                "• All reports create an automated discussion thread for developers.\n"
                "• Please include exact steps to reproduce the issue.\n"
                "• Critical exploits reported responsibly may be eligible for in-game rewards!"
            ),
            colour=0xED4245,
        )
        embed.set_footer(text="Darklands Network • Bug Tracker")

        view = BugLaunchView(self.cfg)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Bug report panel posted successfully!", ephemeral=True
        )

    @app_commands.command(
        name="status",
        description="Update the status of a bug report (Investigating, Confirmed, Fixed, Invalid).",
    )
    @app_commands.describe(
        report_number="The ID/number of the bug report (e.g. 15)",
        status="New status for the bug report",
        note="Optional explanation or developer note",
    )
    async def update_status(
        self,
        interaction: discord.Interaction,
        report_number: int,
        status: Literal["Investigating", "Confirmed", "Fixed", "Invalid", "Closed"],
        note: str | None = None,
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ You must be a staff member to update bug report statuses.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordBugReport).where(
                    DiscordBugReport.report_number == report_number
                )
            )
            b = res.scalar_one_or_none()
            if b is None:
                await interaction.followup.send(
                    f"❌ Bug report **#{report_number}** was not found.",
                    ephemeral=True,
                )
                return

            b.status = status
            b.status_reason = note
            b.status_updated_by = interaction.user.display_name
            await session.commit()

            # Update original message embed
            if b.channel_id and b.message_id:
                channel = interaction.guild.get_channel(b.channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(b.message_id)
                        new_embed = build_bug_embed(b)
                        new_view = BugItemView(self.cfg)
                        await msg.edit(embed=new_embed, view=new_view)
                    except discord.NotFound:
                        pass

            # Update thread
            if b.thread_id:
                thread = interaction.guild.get_thread(b.thread_id)
                if thread:
                    status_emojis = {
                        "Investigating": "🟡",
                        "Confirmed": "🔵",
                        "Fixed": "🟢",
                        "Invalid": "🔴",
                        "Closed": "🔒",
                    }
                    emoji = status_emojis.get(status, "📢")
                    reason_msg = f"\n> *{note}*" if note else ""
                    await thread.send(
                        f"{emoji} **Status Update**: Bug report has been marked as **{status}** "
                        f"by {interaction.user.mention}!{reason_msg}"
                    )
                    if status in ("Fixed", "Closed", "Invalid"):
                        try:
                            await thread.edit(locked=True, archived=True)
                        except discord.DiscordException:
                            pass

        await interaction.followup.send(
            f"✅ Bug report **#{report_number}** marked as **{status}**!",
            ephemeral=True,
        )
