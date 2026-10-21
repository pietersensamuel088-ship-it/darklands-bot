"""
commands.py – Slash commands for submitting and managing suggestions.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands
from sqlalchemy import select

from database.engine import get_session
from database.models import DiscordSuggestion
from suggestions.views import (
    SuggestionItemView,
    SuggestionLaunchView,
    SuggestionModal,
    build_suggestion_embed,
)
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.suggestions.commands")


class SuggestionCommands(app_commands.Group, name="suggestion", description="Darklands SMP Suggestion commands."):
    """Group of suggestion commands under /suggestion."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # /suggestion submit
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="submit",
        description="Submit a new server suggestion for Darklands SMP.",
    )
    async def submit_suggestion(self, interaction: discord.Interaction) -> None:
        modal = SuggestionModal(self.cfg)
        await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────────────────────
    # /suggestion panel
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="panel",
        description="Post the interactive 'Submit a Suggestion' panel in this channel.",
    )
    async def post_panel(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post the suggestion panel.", ephemeral=True
            )
            return

        embed = discord.Embed(
            title="💡 Darklands SMP — Community Suggestions",
            description=(
                "Have an idea for a new feature, Bitcoin rack balance, syndicate system, "
                "or server quality-of-life improvement?\n\n"
                "Click the button below to **Submit a Suggestion**!\n\n"
                "• All suggestions are voted on by the community with 👍 and 👎.\n"
                "• Each idea gets an automatic discussion thread.\n"
                "• Staff regularly reviews high-voted ideas for implementation!"
            ),
            colour=0xFEE75C,
        )
        embed.set_footer(text="Darklands Network • Empowering our community")

        view = SuggestionLaunchView(self.cfg)
        await interaction.channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            "✅ Suggestion panel posted successfully!", ephemeral=True
        )

    # ──────────────────────────────────────────────────────────────────────────
    # /suggestion status
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="status",
        description="Update the status of a suggestion (Pending, Approved, Denied, In Progress).",
    )
    @app_commands.describe(
        suggestion_number="The ID/number of the suggestion (e.g. 102)",
        status="New status for the suggestion",
        reason="Optional explanation or staff note",
    )
    async def update_status(
        self,
        interaction: discord.Interaction,
        suggestion_number: int,
        status: Literal["Pending", "Approved", "Denied", "In Progress"],
        reason: str | None = None,
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ You must be a staff member to update suggestion statuses.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordSuggestion).where(
                    DiscordSuggestion.suggestion_number == suggestion_number
                )
            )
            suggestion = res.scalar_one_or_none()
            if suggestion is None:
                await interaction.followup.send(
                    f"❌ Suggestion **#{suggestion_number}** was not found in the database.",
                    ephemeral=True,
                )
                return

            suggestion.status = status
            suggestion.status_reason = reason
            suggestion.status_updated_by = str(interaction.user.display_name)
            await session.commit()

            # Update the original Discord message embed
            if suggestion.channel_id and suggestion.message_id:
                channel = interaction.guild.get_channel(suggestion.channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        msg = await channel.fetch_message(suggestion.message_id)
                        up, down, _ = suggestion.calculate_votes()
                        new_embed = build_suggestion_embed(suggestion)
                        new_view = SuggestionItemView(
                            self.cfg, up_count=up, down_count=down
                        )
                        await msg.edit(embed=new_embed, view=new_view)
                    except discord.NotFound:
                        log.warning("Original suggestion message #%d was deleted", suggestion_number)

            # Update thread if exists
            if suggestion.thread_id:
                thread = interaction.guild.get_thread(suggestion.thread_id)
                if thread:
                    status_emojis = {
                        "Pending": "🟡",
                        "Approved": "🟢",
                        "Denied": "🔴",
                        "In Progress": "🔵",
                    }
                    emoji = status_emojis.get(status, "📢")
                    note = f"\n> *{reason}*" if reason else ""
                    await thread.send(
                        f"{emoji} **Status Update**: This suggestion has been marked as **{status}** "
                        f"by {interaction.user.mention}!{note}"
                    )
                    if status in ("Approved", "Denied"):
                        try:
                            await thread.edit(locked=True, archived=True)
                        except discord.DiscordException:
                            pass

        await interaction.followup.send(
            f"✅ Suggestion **#{suggestion_number}** status updated to **{status}**!",
            ephemeral=True,
        )
