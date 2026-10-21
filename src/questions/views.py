"""
views.py – Embeds, modals, and persistent views for Questions.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

import discord
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordQuestion
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.questions")

QUESTION_STATUS_CONFIG = {
    "Open": {
        "emoji": "🟡",
        "label": "Open",
        "colour": 0xFEE75C,
    },
    "Answered": {
        "emoji": "🟢",
        "label": "Answered",
        "colour": 0x57F287,
    },
    "Closed": {
        "emoji": "🔒",
        "label": "Closed",
        "colour": 0xED4245,
    },
}


def build_question_embed(question: DiscordQuestion) -> discord.Embed:
    """Construct the exact embed shown in the reference design."""
    status_info = QUESTION_STATUS_CONFIG.get(
        question.status,
        {"emoji": "🟡", "label": question.status, "colour": 0xFEE75C},
    )

    embed = discord.Embed(
        title=f"❓ #{question.question_number} — {question.title}",
        colour=status_info["colour"],
    )

    created_ts = (
        int(question.created_at.timestamp())
        if question.created_at
        else int(datetime.datetime.now().timestamp())
    )
    embed.description = f"Asked by <@{question.author_id}> • <t:{created_ts}:R>"

    embed.add_field(
        name="Question",
        value=question.question,
        inline=False,
    )

    status_text = f"{status_info['emoji']} {status_info['label']}"
    if question.answered_by_name:
        status_text += f"\n*Marked by: {question.answered_by_name}*"
    if question.status_reason:
        status_text += f"\n> *{question.status_reason}*"

    embed.add_field(
        name="Status",
        value=status_text,
        inline=False,
    )

    now_fmt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    embed.set_footer(
        text=f"Darklands Network • Question #{question.question_number} • {now_fmt}"
    )

    return embed


class QuestionModal(discord.ui.Modal, title="Ask a Question"):
    """Modal that prompts the user for a question title and details."""

    title_input = discord.ui.TextInput(
        label="Question Summary / Title",
        placeholder="e.g. What is the point of iron blocks stacking?",
        max_length=150,
        required=True,
    )

    question_input = discord.ui.TextInput(
        label="Your Question Details",
        style=discord.TextStyle.paragraph,
        placeholder="Describe what you need help with or are wondering about...",
        max_length=2000,
        required=True,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        target_channel_id = self.cfg.channels.question_channel_id or interaction.channel_id
        target_channel = interaction.guild.get_channel(target_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Question channel is not configured or could not be found.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            res = await session.execute(func.max(DiscordQuestion.question_number))
            max_num = res.scalar()
            next_num = (max_num + 1) if max_num is not None else 38

            author_avatar = (
                interaction.user.display_avatar.url
                if interaction.user.display_avatar
                else None
            )

            question = DiscordQuestion(
                question_number=next_num,
                author_id=interaction.user.id,
                author_name=str(interaction.user),
                author_avatar=author_avatar,
                title=self.title_input.value.strip(),
                question=self.question_input.value.strip(),
                status="Open",
            )
            session.add(question)
            await session.flush()

            embed = build_question_embed(question)
            view = QuestionItemView(self.cfg)

            msg = await target_channel.send(embed=embed, view=view)
            question.message_id = msg.id
            question.channel_id = target_channel.id

            thread_title = f"#{next_num} {self.title_input.value.strip()}"[:95]
            try:
                thread = await msg.create_thread(
                    name=thread_title,
                    auto_archive_duration=1440,
                    reason=f"Discussion thread for Question #{next_num}",
                )
                question.thread_id = thread.id
                await thread.send(
                    f"💬 Answers and discussion for **Question #{next_num}** asked by {interaction.user.mention}!\n"
                    f"*Feel free to leave helpful answers below. The author or staff can click [Mark answered] above!*"
                )
            except discord.DiscordException as exc:
                log.warning("Could not create thread for question #%d: %s", next_num, exc)

            await session.commit()

        await interaction.followup.send(
            f"✅ Your question **#{next_num}** has been posted in {target_channel.mention}!\n"
            f"[Jump to Question]({msg.jump_url})",
            ephemeral=True,
        )


class QuestionItemView(discord.ui.View):
    """
    Persistent button view under every question message:
    [✅ Mark answered] [🔒 Close] [❓ Ask]
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Mark answered",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="question_btn_mark_answered",
    )
    async def mark_answered_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordQuestion).where(
                    DiscordQuestion.message_id == interaction.message.id
                )
            )
            q = res.scalar_one_or_none()
            if q is None:
                await interaction.followup.send("❌ Question not found.", ephemeral=True)
                return

            if q.author_id != interaction.user.id and not is_staff(interaction.user, self.cfg):
                await interaction.followup.send(
                    "❌ Only the question author or staff can mark this question as answered.",
                    ephemeral=True,
                )
                return

            q.status = "Answered"
            q.answered_by_id = interaction.user.id
            q.answered_by_name = interaction.user.display_name
            await session.commit()

            new_embed = build_question_embed(q)
            await interaction.message.edit(embed=new_embed, view=self)

            if q.thread_id:
                thread = interaction.guild.get_thread(q.thread_id)
                if thread:
                    await thread.send(
                        f"✅ **Question Marked as Answered** by {interaction.user.mention}!"
                    )

            await interaction.followup.send(
                "✅ Marked question as answered!", ephemeral=True
            )

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="question_btn_close",
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordQuestion).where(
                    DiscordQuestion.message_id == interaction.message.id
                )
            )
            q = res.scalar_one_or_none()
            if q is None:
                await interaction.followup.send("❌ Question not found.", ephemeral=True)
                return

            if q.author_id != interaction.user.id and not is_staff(interaction.user, self.cfg):
                await interaction.followup.send(
                    "❌ Only the question author or staff can close this question.",
                    ephemeral=True,
                )
                return

            q.status = "Closed"
            await session.commit()

            new_embed = build_question_embed(q)
            await interaction.message.edit(embed=new_embed, view=self)

            if q.thread_id:
                thread = interaction.guild.get_thread(q.thread_id)
                if thread:
                    await thread.send(
                        f"🔒 **Question Closed** by {interaction.user.mention}."
                    )
                    try:
                        await thread.edit(locked=True, archived=True)
                    except discord.DiscordException:
                        pass

            await interaction.followup.send("🔒 Question closed.", ephemeral=True)

    @discord.ui.button(
        label="Ask",
        style=discord.ButtonStyle.primary,
        emoji="❓",
        custom_id="question_btn_ask_modal",
    )
    async def ask_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = QuestionModal(self.cfg)
        await interaction.response.send_modal(modal)


class QuestionLaunchView(discord.ui.View):
    """Persistent starter panel for #・questions."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Ask a Question",
        style=discord.ButtonStyle.primary,
        emoji="❓",
        custom_id="question_panel_launch_btn",
    )
    async def launch_question(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = QuestionModal(self.cfg)
        await interaction.response.send_modal(modal)
