"""
views.py – Embeds, modals, and persistent views for Bug Reports.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING

import discord
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordBugReport
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.bugs")

BUG_STATUS_CONFIG = {
    "Investigating": {
        "emoji": "🟡",
        "label": "Investigating",
        "colour": 0xFEE75C,
    },
    "Confirmed": {
        "emoji": "🔵",
        "label": "Confirmed / In Progress",
        "colour": 0x5865F2,
    },
    "Fixed": {
        "emoji": "🟢",
        "label": "Fixed / Resolved",
        "colour": 0x57F287,
    },
    "Invalid": {
        "emoji": "🔴",
        "label": "Cannot Reproduce / Invalid",
        "colour": 0xED4245,
    },
    "Closed": {
        "emoji": "🔒",
        "label": "Closed",
        "colour": 0x72767D,
    },
}


def build_bug_embed(bug: DiscordBugReport) -> discord.Embed:
    """Construct the bug report embed."""
    status_info = BUG_STATUS_CONFIG.get(
        bug.status,
        {"emoji": "🟡", "label": bug.status, "colour": 0xFEE75C},
    )

    embed = discord.Embed(
        title=f"🐛 #{bug.report_number} — {bug.title}",
        colour=status_info["colour"],
    )

    created_ts = (
        int(bug.created_at.timestamp())
        if bug.created_at
        else int(datetime.datetime.now().timestamp())
    )
    embed.description = f"Reported by <@{bug.author_id}> • <t:{created_ts}:R>"

    embed.add_field(
        name="Bug Description",
        value=bug.description,
        inline=False,
    )

    embed.add_field(
        name="Steps to Reproduce",
        value=bug.reproduce_steps,
        inline=False,
    )

    status_text = f"{status_info['emoji']} {status_info['label']}"
    if bug.status_updated_by:
        status_text += f"\n*Updated by: {bug.status_updated_by}*"
    if bug.status_reason:
        status_text += f"\n> *{bug.status_reason}*"

    embed.add_field(
        name="Status",
        value=status_text,
        inline=False,
    )

    now_fmt = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    embed.set_footer(
        text=f"Darklands Network • Bug Report #{bug.report_number} • {now_fmt}"
    )

    return embed


class BugReportModal(discord.ui.Modal, title="Submit a Bug Report"):
    """Modal that prompts the user for bug title, description, and reproduction steps."""

    title_input = discord.ui.TextInput(
        label="Bug Summary / Title",
        placeholder="e.g. Money printer stops producing after server restart",
        max_length=150,
        required=True,
    )

    description_input = discord.ui.TextInput(
        label="Bug Description",
        style=discord.TextStyle.paragraph,
        placeholder="Describe what went wrong and what you expected to happen...",
        max_length=2000,
        required=True,
    )

    steps_input = discord.ui.TextInput(
        label="Steps to Reproduce",
        style=discord.TextStyle.paragraph,
        placeholder="1. Place a stone printer\n2. Open GUI\n3. Click claim...",
        max_length=2000,
        required=True,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        target_channel_id = self.cfg.channels.bug_report_channel_id or interaction.channel_id
        target_channel = interaction.guild.get_channel(target_channel_id)
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Bug report channel is not configured or could not be found.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            res = await session.execute(func.max(DiscordBugReport.report_number))
            max_num = res.scalar()
            next_num = (max_num + 1) if max_num is not None else 1

            author_avatar = (
                interaction.user.display_avatar.url
                if interaction.user.display_avatar
                else None
            )

            bug = DiscordBugReport(
                report_number=next_num,
                author_id=interaction.user.id,
                author_name=str(interaction.user),
                author_avatar=author_avatar,
                title=self.title_input.value.strip(),
                description=self.description_input.value.strip(),
                reproduce_steps=self.steps_input.value.strip(),
                status="Investigating",
            )
            session.add(bug)
            await session.flush()

            embed = build_bug_embed(bug)
            view = BugItemView(self.cfg)

            msg = await target_channel.send(embed=embed, view=view)
            bug.message_id = msg.id
            bug.channel_id = target_channel.id

            thread_title = f"#{next_num} {self.title_input.value.strip()}"[:95]
            try:
                thread = await msg.create_thread(
                    name=thread_title,
                    auto_archive_duration=1440,
                    reason=f"Investigation thread for Bug #{next_num}",
                )
                bug.thread_id = thread.id
                await thread.send(
                    f"🐛 Investigation thread for **Bug Report #{next_num}** reported by {interaction.user.mention}!\n"
                    f"*Staff and developers can investigate, leave reproduction details, or post patches here.*"
                )
            except discord.DiscordException as exc:
                log.warning("Could not create thread for bug #%d: %s", next_num, exc)

            await session.commit()

        await interaction.followup.send(
            f"✅ Bug report **#{next_num}** submitted in {target_channel.mention}!\n"
            f"[Jump to Bug Report]({msg.jump_url})",
            ephemeral=True,
        )


class BugItemView(discord.ui.View):
    """
    Persistent button view under every bug report message:
    [✅ Mark Fixed] [🔒 Close] [🐛 Report Bug]
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Mark Fixed",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="bug_btn_mark_fixed",
    )
    async def mark_fixed_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not is_staff(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ Only staff members can mark bug reports as fixed.", ephemeral=True
            )
            return

        async with get_session() as session:
            res = await session.execute(
                select(DiscordBugReport).where(
                    DiscordBugReport.message_id == interaction.message.id
                )
            )
            bug = res.scalar_one_or_none()
            if bug is None:
                await interaction.followup.send("❌ Bug report not found.", ephemeral=True)
                return

            bug.status = "Fixed"
            bug.status_updated_by = interaction.user.display_name
            await session.commit()

            new_embed = build_bug_embed(bug)
            await interaction.message.edit(embed=new_embed, view=self)

            if bug.thread_id:
                thread = interaction.guild.get_thread(bug.thread_id)
                if thread:
                    await thread.send(
                        f"✅ **Bug Marked as Fixed** by {interaction.user.mention}!"
                    )
                    try:
                        await thread.edit(locked=True, archived=True)
                    except discord.DiscordException:
                        pass

            await interaction.followup.send("✅ Marked bug report as fixed!", ephemeral=True)

    @discord.ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="bug_btn_close",
    )
    async def close_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        if not is_staff(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ Only staff members can close bug reports.", ephemeral=True
            )
            return

        async with get_session() as session:
            res = await session.execute(
                select(DiscordBugReport).where(
                    DiscordBugReport.message_id == interaction.message.id
                )
            )
            bug = res.scalar_one_or_none()
            if bug is None:
                await interaction.followup.send("❌ Bug report not found.", ephemeral=True)
                return

            bug.status = "Closed"
            bug.status_updated_by = interaction.user.display_name
            await session.commit()

            new_embed = build_bug_embed(bug)
            await interaction.message.edit(embed=new_embed, view=self)

            if bug.thread_id:
                thread = interaction.guild.get_thread(bug.thread_id)
                if thread:
                    await thread.send(
                        f"🔒 **Bug Report Closed** by {interaction.user.mention}."
                    )
                    try:
                        await thread.edit(locked=True, archived=True)
                    except discord.DiscordException:
                        pass

            await interaction.followup.send("🔒 Bug report closed.", ephemeral=True)

    @discord.ui.button(
        label="Report Bug",
        style=discord.ButtonStyle.danger,
        emoji="🐛",
        custom_id="bug_btn_report_modal",
    )
    async def report_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = BugReportModal(self.cfg)
        await interaction.response.send_modal(modal)


class BugLaunchView(discord.ui.View):
    """Persistent starter panel for #・bug-reports."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Report a Bug",
        style=discord.ButtonStyle.danger,
        emoji="🐛",
        custom_id="bug_panel_launch_btn",
    )
    async def launch_bug_report(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        modal = BugReportModal(self.cfg)
        await interaction.response.send_modal(modal)
