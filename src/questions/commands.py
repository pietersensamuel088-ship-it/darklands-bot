"""
commands.py – Slash commands for Questions.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from sqlalchemy import select

from database.engine import get_session
from database.models import DiscordQuestion
from questions.views import (
    QuestionItemView,
    QuestionLaunchView,
    QuestionModal,
    build_question_embed,
)
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.questions.commands")


class QuestionCommands(app_commands.Group, name="question", description="Darklands SMP Question commands."):
    """Group of question commands under /question."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    @app_commands.command(
        name="ask",
        description="Ask a question to the Darklands community or staff team.",
    )
    async def ask_question(self, interaction: discord.Interaction) -> None:
        modal = QuestionModal(self.cfg)
        await interaction.response.send_modal(modal)

    @app_commands.command(
        name="panel",
        description="Post the interactive 'Ask a Question' panel in this channel.",
    )
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post the question panel.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="❓ Darklands SMP — Community Q&A",
            description=(
                "Got a question about money printers, Bitcoin mining, syndicate rules, "
                "or how an in-game item works?\n\n"
                "Click the button below to **Ask a Question**!\n\n"
                "• Your question will get an automatic discussion thread.\n"
                "• Community members and staff can reply with answers.\n"
                "• Once your question is answered, you can click **[Mark answered]**!"
            ),
            colour=0xFEE75C,
        )
        embed.set_footer(text="Darklands Network • Help & Community Support")

        view = QuestionLaunchView(self.cfg)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Question panel posted successfully!", ephemeral=True
        )

    @app_commands.command(
        name="status",
        description="Update the status of a question (Open, Answered, Closed).",
    )
    @app_commands.describe(
        question_number="The ID/number of the question (e.g. 38)",
        status="New status for the question",
        note="Optional explanation or staff note",
    )
    async def update_status(
        self,
        interaction: discord.Interaction,
        question_number: int,
        status: Literal["Open", "Answered", "Closed"],
        note: str | None = None,
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ You must be a staff member to update question statuses.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordQuestion).where(
                    DiscordQuestion.question_number == question_number
                )
            )
            q = res.scalar_one_or_none()
            if q is None:
                await interaction.followup.send(
                    f"❌ Question **#{question_number}** was not found.",
                    ephemeral=True,
                )
                return

            q.status = status
            q.status_reason = note
            if status == "Answered":
                q.answered_by_name = interaction.user.display_name
            await session.commit()

            # Update original message embed
            if q.channel_id and q.message_id:
                channel = interaction.guild.get_channel(q.channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(q.message_id)
                        new_embed = build_question_embed(q)
                        new_view = QuestionItemView(self.cfg)
                        await msg.edit(embed=new_embed, view=new_view)
                    except discord.NotFound:
                        pass

            # Update thread
            if q.thread_id:
                thread = interaction.guild.get_thread(q.thread_id)
                if thread:
                    emoji = "🟢" if status == "Answered" else "🔒" if status == "Closed" else "🟡"
                    reason_msg = f"\n> *{note}*" if note else ""
                    await thread.send(
                        f"{emoji} **Status Update**: Question has been marked as **{status}** "
                        f"by {interaction.user.mention}!{reason_msg}"
                    )
                    if status == "Closed":
                        try:
                            await thread.edit(locked=True, archived=True)
                        except discord.DiscordException:
                            pass

        await interaction.followup.send(
            f"✅ Question **#{question_number}** marked as **{status}**!",
            ephemeral=True,
        )
