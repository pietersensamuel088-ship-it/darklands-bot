"""
views.py – UI components, embeds, modals, and persistent views for Suggestions.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Optional

import discord
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordSuggestion

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.suggestions")

# ── Status metadata ───────────────────────────────────────────────────────────
STATUS_CONFIG = {
    "Pending": {
        "emoji": "🟡",
        "label": "Pending",
        "colour": 0xFEE75C,
    },
    "Approved": {
        "emoji": "🟢",
        "label": "Approved",
        "colour": 0x57F287,
    },
    "Accepted": {
        "emoji": "🟢",
        "label": "Accepted",
        "colour": 0x57F287,
    },
    "Denied": {
        "emoji": "🔴",
        "label": "Denied",
        "colour": 0xED4245,
    },
    "In Progress": {
        "emoji": "🔵",
        "label": "In Progress",
        "colour": 0x5865F2,
    },
}


def render_progress_bar(pct: float, length: int = 12) -> str:
    """Render a clean block-style percentage progress bar."""
    if pct <= 0:
        return "░" * length
    filled = max(0, min(length, round((pct / 100.0) * length)))
    return "█" * filled + "░" * (length - filled)


def build_suggestion_embed(suggestion: DiscordSuggestion) -> discord.Embed:
    """Construct the exact embed shown in the reference design."""
    up, down, pct = suggestion.calculate_votes()
    bar = render_progress_bar(pct, length=12)

    status_info = STATUS_CONFIG.get(
        suggestion.status,
        {"emoji": "🟡", "label": suggestion.status, "colour": 0xFEE75C},
    )

    embed = discord.Embed(
        title=f"#{suggestion.suggestion_number} — {suggestion.title}",
        colour=status_info["colour"],
    )

    # Header timestamp
    created_ts = int(suggestion.created_at.timestamp()) if suggestion.created_at else int(datetime.datetime.now().timestamp())
    embed.description = f"Suggested by <@{suggestion.author_id}> • <t:{created_ts}:R>"

    # Fields
    embed.add_field(
        name="Suggestion",
        value=suggestion.suggestion,
        inline=False,
    )
    embed.add_field(
        name="Why it helps Darklands",
        value=suggestion.reason,
        inline=False,
    )

    votes_text = f"👍 {up} 👎 {down}\n{bar} {int(pct)}% in favour"
    embed.add_field(
        name="Votes",
        value=votes_text,
        inline=False,
    )

    status_text = f"{status_info['emoji']} {status_info['label']}"
    if suggestion.status_reason:
        status_text += f"\n> *{suggestion.status_reason}*"
        if suggestion.status_updated_by:
            status_text += f" — **{suggestion.status_updated_by}**"

    embed.add_field(
        name="Status",
        value=status_text,
        inline=False,
    )

    now_fmt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    embed.set_footer(
        text=f"Darklands Network • Suggestion #{suggestion.suggestion_number} • {now_fmt}"
    )

    return embed


# ── Suggestion Modal ──────────────────────────────────────────────────────────

class SuggestionModal(discord.ui.Modal, title="Submit a Server Suggestion"):
    """Modal that prompts the user for title, suggestion text, and reason."""

    title_input = discord.ui.TextInput(
        label="Suggestion Title / Summary",
        placeholder="e.g. Allow max buy from market",
        max_length=100,
        required=True,
    )

    suggestion_input = discord.ui.TextInput(
        label="What is your suggestion?",
        style=discord.TextStyle.paragraph,
        placeholder="Describe the feature or improvement in detail...",
        max_length=2000,
        required=True,
    )

    reason_input = discord.ui.TextInput(
        label="Why it helps Darklands",
        style=discord.TextStyle.paragraph,
        placeholder="How does this improve the economy, gameplay, or experience?",
        max_length=2000,
        required=True,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        target_channel_id = self.cfg.channels.suggestion_channel_id or interaction.channel_id
        target_channel = interaction.guild.get_channel(target_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Suggestion channel is not configured or could not be found.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            # Determine next suggestion number (start at 101 for realistic feel)
            res = await session.execute(func.max(DiscordSuggestion.suggestion_number))
            max_num = res.scalar()
            next_num = (max_num + 1) if max_num is not None else 101

            author_avatar = interaction.user.display_avatar.url if interaction.user.display_avatar else None

            suggestion = DiscordSuggestion(
                suggestion_number=next_num,
                author_id=interaction.user.id,
                author_name=str(interaction.user),
                author_avatar=author_avatar,
                title=self.title_input.value.strip(),
                suggestion=self.suggestion_input.value.strip(),
                reason=self.reason_input.value.strip(),
                status="Pending",
                upvotes=[],
                downvotes=[],
            )
            session.add(suggestion)
            await session.flush()

            # Build embed & initial view
            embed = build_suggestion_embed(suggestion)
            view = SuggestionItemView(self.cfg, up_count=0, down_count=0)

            # Post message to channel
            msg = await target_channel.send(embed=embed, view=view)
            suggestion.message_id = msg.id
            suggestion.channel_id = target_channel.id

            # Create public thread for community discussion
            thread_title = f"#{next_num} {self.title_input.value.strip()}"[:95]
            try:
                thread = await msg.create_thread(
                    name=thread_title,
                    auto_archive_duration=1440,  # 24 hours
                    reason=f"Discussion thread for Suggestion #{next_num}",
                )
                suggestion.thread_id = thread.id
                await thread.send(
                    f"💬 Discussion thread for **Suggestion #{next_num}** by {interaction.user.mention}!\n"
                    f"*Feel free to leave feedback, ideas, or vote above.*"
                )
            except discord.DiscordException as exc:
                log.warning("Could not create thread for suggestion #%d: %s", next_num, exc)

            await session.commit()

        await interaction.followup.send(
            f"✅ Your suggestion **#{next_num}** has been posted in {target_channel.mention}!\n"
            f"[Jump to Suggestion]({msg.jump_url})",
            ephemeral=True,
        )


# ── Suggestion Item View (Buttons) ────────────────────────────────────────────

class SuggestionItemView(discord.ui.View):
    """
    Persistent button view attached to each suggestion message:
    [👍 Upvotes] [👎 Downvotes] [📊 Details] [💡 Suggest]
    """

    def __init__(
        self,
        cfg: BotConfig,
        up_count: int = 0,
        down_count: int = 0,
    ) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

        # Dynamically set button labels with vote counts
        self.upvote_button.label = str(up_count)
        self.downvote_button.label = str(down_count)

    @discord.ui.button(
        label="0",
        style=discord.ButtonStyle.success,
        emoji="👍",
        custom_id="suggestion_btn_upvote",
    )
    async def upvote_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_vote(interaction, vote_type="up")

    @discord.ui.button(
        label="0",
        style=discord.ButtonStyle.danger,
        emoji="👎",
        custom_id="suggestion_btn_downvote",
    )
    async def downvote_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self._handle_vote(interaction, vote_type="down")

    @discord.ui.button(
        label="Details",
        style=discord.ButtonStyle.secondary,
        emoji="📊",
        custom_id="suggestion_btn_details",
    )
    async def details_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordSuggestion).where(
                    DiscordSuggestion.message_id == interaction.message.id
                )
            )
            suggestion = res.scalar_one_or_none()
            if suggestion is None:
                await interaction.followup.send(
                    "❌ Suggestion record could not be found.", ephemeral=True
                )
                return

            up, down, pct = suggestion.calculate_votes()
            embed = discord.Embed(
                title=f"📊 Suggestion #{suggestion.suggestion_number} Details",
                colour=0x5865F2,
            )
            embed.add_field(name="Title", value=suggestion.title, inline=False)
            embed.add_field(name="Author", value=f"<@{suggestion.author_id}>", inline=True)
            embed.add_field(name="Status", value=suggestion.status, inline=True)
            embed.add_field(
                name="Vote Breakdown",
                value=f"• **Upvotes:** {up}\n• **Downvotes:** {down}\n• **Approval:** {pct:.1f}%",
                inline=False,
            )
            if suggestion.status_reason:
                embed.add_field(
                    name="Staff Response",
                    value=f"> {suggestion.status_reason}\n*By: {suggestion.status_updated_by or 'Staff'}*",
                    inline=False,
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Suggest",
        style=discord.ButtonStyle.primary,
        emoji="💡",
        custom_id="suggestion_btn_create_modal",
    )
    async def suggest_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = SuggestionModal(self.cfg)
        await interaction.response.send_modal(modal)

    async def _handle_vote(self, interaction: discord.Interaction, vote_type: str) -> None:
        await interaction.response.defer(ephemeral=True)
        user_id = interaction.user.id

        async with get_session() as session:
            res = await session.execute(
                select(DiscordSuggestion).where(
                    DiscordSuggestion.message_id == interaction.message.id
                )
            )
            suggestion = res.scalar_one_or_none()
            if suggestion is None:
                await interaction.followup.send(
                    "❌ Suggestion record not found.", ephemeral=True
                )
                return

            upvotes = list(suggestion.upvotes or [])
            downvotes = list(suggestion.downvotes or [])
            feedback = ""

            if vote_type == "up":
                if user_id in upvotes:
                    upvotes.remove(user_id)
                    feedback = "Removed your upvote."
                else:
                    upvotes.append(user_id)
                    if user_id in downvotes:
                        downvotes.remove(user_id)
                    feedback = "👍 You upvoted this suggestion!"
            elif vote_type == "down":
                if user_id in downvotes:
                    downvotes.remove(user_id)
                    feedback = "Removed your downvote."
                else:
                    downvotes.append(user_id)
                    if user_id in upvotes:
                        upvotes.remove(user_id)
                    feedback = "👎 You downvoted this suggestion."

            suggestion.upvotes = upvotes
            suggestion.downvotes = downvotes
            await session.commit()

            # Refresh embed & view
            new_embed = build_suggestion_embed(suggestion)
            new_view = SuggestionItemView(
                self.cfg,
                up_count=len(upvotes),
                down_count=len(downvotes),
            )

            await interaction.message.edit(embed=new_embed, view=new_view)
            await interaction.followup.send(feedback, ephemeral=True)


# ── Suggestion Panel Launch View ──────────────────────────────────────────────

class SuggestionLaunchView(discord.ui.View):
    """Persistent starter panel view with a prominent 'Submit a Suggestion' button."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Submit a Suggestion",
        style=discord.ButtonStyle.primary,
        emoji="💡",
        custom_id="suggestion_panel_launch_btn",
    )
    async def launch_suggestion(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = SuggestionModal(self.cfg)
        await interaction.response.send_modal(modal)
