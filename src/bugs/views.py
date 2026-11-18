"""
views.py – Embeds, modals, and persistent views for Bug Reports.
Includes:
- Revamped 5-field BugReportModal for detailed player reports.
- Private forwarding to staff channel (1572703183979942088).
- Staff action buttons: [✅ Mark Fixed], [❌ Deny / Invalid], [🎫 Open Ticket].
- Direct Message notifications to reporter upon Fix, Denial, or Ticket opening.
- Persistent launch panel view for public reporting.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import ui
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordBugReport, DiscordTicket
from tickets.permissions import is_admin, is_staff
from tickets.views import TicketControlView, build_control_embed

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.bugs")

BUG_STATUS_CONFIG = {
    "Investigating": {
        "emoji": "🟡",
        "label": "Investigating / Pending Review",
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
    "Denied": {
        "emoji": "🔴",
        "label": "Denied / Invalid",
        "colour": 0xED4245,
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


# ==============================================================================
# 1. Embed Builder
# ==============================================================================

def build_bug_embed(bug: DiscordBugReport, guild: Optional[discord.Guild] = None) -> discord.Embed:
    """Construct the comprehensive staff bug report embed."""
    status_info = BUG_STATUS_CONFIG.get(
        bug.status,
        {"emoji": "🟡", "label": bug.status, "colour": 0xFEE75C},
    )

    embed = discord.Embed(
        title=f"🐛 Bug Report #{bug.report_number:04d} — {bug.title}",
        colour=status_info["colour"],
    )

    created_ts = (
        int(bug.created_at.timestamp())
        if bug.created_at
        else int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    )
    embed.description = (
        f"**Reported by:** <@{bug.author_id}> (`{bug.author_name}` • ID `{bug.author_id}`)\n"
        f"**Submitted:** <t:{created_ts}:F> (<t:{created_ts}:R>)"
    )

    # 1. Minecraft Account & Platform
    ign_text = bug.minecraft_ign if bug.minecraft_ign else "Not specified"
    embed.add_field(
        name="🎮 Minecraft Username & Platform",
        value=f"**`{ign_text}`**",
        inline=True,
    )

    # 2. Current Status
    status_field = f"{status_info['emoji']} **{status_info['label']}**"
    if bug.ticket_channel_id:
        status_field += f"\n🎫 **Active Ticket:** <#{bug.ticket_channel_id}>"
    if bug.status_updated_by:
        status_field += f"\n👤 *Updated by:* **{bug.status_updated_by}**"
    embed.add_field(
        name="⚡ Current Status",
        value=status_field,
        inline=True,
    )

    # 3. Expected Behavior
    if bug.expected_behavior:
        embed.add_field(
            name="📝 Expected Behavior",
            value=bug.expected_behavior[:1024],
            inline=False,
        )

    # 4. Actual Behavior (What Went Wrong)
    embed.add_field(
        name="⚠️ Actual Behavior (What Happened)",
        value=bug.description[:1024],
        inline=False,
    )

    # 5. Steps to Reproduce
    embed.add_field(
        name="🔁 Steps to Reproduce",
        value=bug.reproduce_steps[:1024],
        inline=False,
    )

    # 6. Coordinates / Media Links
    if bug.evidence:
        embed.add_field(
            name="📍 In-Game Coordinates / Evidence Links",
            value=bug.evidence[:1024],
            inline=False,
        )

    # 7. Staff Resolution / Denial Reason
    if bug.status_reason:
        embed.add_field(
            name="📋 Staff Notes / Reason",
            value=f"> *{bug.status_reason}*",
            inline=False,
        )

    if bug.author_avatar:
        embed.set_thumbnail(url=bug.author_avatar)

    embed.set_footer(
        text=f"Darklands SMP • Staff Bug Tracker • Report #{bug.report_number:04d}"
    )
    return embed


# ==============================================================================
# 2. Player Bug Submission Modal (Revamped & Expanded)
# ==============================================================================

class BugReportModal(ui.Modal, title="Submit a Bug Report"):
    """Revamped modal with 5 structured fields for comprehensive player bug reporting."""

    title_input: ui.TextInput = ui.TextInput(
        label="Bug Title / Summary",
        placeholder="Brief summary (e.g. Money printer stops producing after restart)",
        max_length=150,
        required=True,
    )

    mc_ign_input: ui.TextInput = ui.TextInput(
        label="Minecraft IGN & Platform",
        placeholder="e.g. Steve123 | Java 1.21.1 (or Bedrock / Geyser)",
        max_length=100,
        required=True,
    )

    behavior_input: ui.TextInput = ui.TextInput(
        label="Expected vs Actual Behavior",
        style=discord.TextStyle.paragraph,
        placeholder="Expected: Money updates in balance.\nActual: GUI freezes and money resets to 0.",
        max_length=1500,
        required=True,
    )

    steps_input: ui.TextInput = ui.TextInput(
        label="Steps to Reproduce",
        style=discord.TextStyle.paragraph,
        placeholder="1. Place a stone printer\n2. Open GUI\n3. Click harvest...",
        max_length=1500,
        required=True,
    )

    coords_evidence_input: ui.TextInput = ui.TextInput(
        label="Coordinates & Media Links (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="XYZ coordinates, world name, or video/screenshot link (Imgur/YouTube)",
        max_length=1000,
        required=False,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        target_channel_id = self.cfg.channels.bug_staff_channel_id or 1572703183979942088
        target_channel = interaction.guild.get_channel(target_channel_id) if interaction.guild else None
        if not isinstance(target_channel, discord.TextChannel):
            try:
                target_channel = await interaction.client.fetch_channel(target_channel_id)
            except Exception:
                target_channel = None

        if not isinstance(target_channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Staff bug channel is not configured or could not be found. Please alert an administrator.",
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

            title_val = self.title_input.value.strip()
            mc_val = self.mc_ign_input.value.strip()
            behavior_val = self.behavior_input.value.strip()
            steps_val = self.steps_input.value.strip()
            evidence_val = self.coords_evidence_input.value.strip() or None

            bug = DiscordBugReport(
                report_number=next_num,
                author_id=interaction.user.id,
                author_name=str(interaction.user),
                author_avatar=author_avatar,
                title=title_val,
                description=behavior_val,
                reproduce_steps=steps_val,
                minecraft_ign=mc_val,
                expected_behavior=behavior_val,
                evidence=evidence_val,
                status="Investigating",
            )
            session.add(bug)
            await session.flush()

            embed = build_bug_embed(bug, interaction.guild)
            view = BugStaffActionView(self.cfg)

            msg = await target_channel.send(embed=embed, view=view)
            bug.message_id = msg.id
            bug.channel_id = target_channel.id

            thread_title = f"#{next_num:04d} {title_val}"[:95]
            try:
                thread = await msg.create_thread(
                    name=thread_title,
                    auto_archive_duration=1440,
                    reason=f"Staff investigation thread for Bug #{next_num}",
                )
                bug.thread_id = thread.id
                await thread.send(
                    f"🐛 **Staff Investigation Thread for Bug Report #{next_num:04d}**\n"
                    f"Reported by {interaction.user.mention} (`{mc_val}`).\n"
                    f"*Staff can discuss investigation details or coordinate testing here.*"
                )
            except discord.DiscordException as exc:
                log.warning("Could not create thread for bug #%d: %s", next_num, exc)

            await session.commit()

        # Ephemeral confirmation to user (Never posts to the public channel)
        await interaction.followup.send(
            f"✅ **Bug Report #{next_num:04d} Submitted Successfully!**\n\n"
            f"Thank you for helping us polish Darklands SMP. Your report has been delivered **privately** to our development and staff team for investigation.\n\n"
            f"• We will notify you directly in your DMs once it has been reviewed or marked as fixed!\n"
            f"• If our team needs more details, we may open a support ticket with you.",
            ephemeral=True,
        )


# ==============================================================================
# 3. Staff Action Persistent View (For Staff Channel: 1572703183979942088)
# ==============================================================================

class BugStaffActionView(ui.View):
    """
    Persistent button view under every bug report message in staff channel 1572703183979942088:
    [✅ Mark Fixed] [❌ Deny / Invalid] [🎫 Open Ticket]
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # [✅ Mark Fixed]
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Mark Fixed",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="bug:staff:fix",
    )
    async def mark_fixed_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
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
                await interaction.response.send_message(
                    "❌ Bug report not found in database.", ephemeral=True
                )
                return

            if bug.status == "Fixed":
                await interaction.response.send_message(
                    "ℹ️ This bug report has already been marked as Fixed.", ephemeral=True
                )
                return

            modal = BugFixModal(bug.id, self.cfg, interaction.message.id)
            await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────────────────────
    # [❌ Deny / Invalid]
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Deny / Invalid",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="bug:staff:deny",
    )
    async def deny_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can deny bug reports.", ephemeral=True
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
                await interaction.response.send_message(
                    "❌ Bug report not found in database.", ephemeral=True
                )
                return

            if bug.status in ("Denied", "Invalid"):
                await interaction.response.send_message(
                    f"ℹ️ This bug report has already been marked as {bug.status}.", ephemeral=True
                )
                return

            modal = BugDenyModal(bug.id, self.cfg, interaction.message.id)
            await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────────────────────
    # [🎫 Open Ticket]
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="bug:staff:ticket",
    )
    async def open_ticket_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can open bug tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            res = await session.execute(
                select(DiscordBugReport).where(
                    DiscordBugReport.message_id == interaction.message.id
                )
            )
            bug = res.scalar_one_or_none()
            if bug is None:
                await interaction.followup.send(
                    "❌ Bug report record not found in database.", ephemeral=True
                )
                return

            if bug.ticket_channel_id:
                existing_chan = interaction.guild.get_channel(bug.ticket_channel_id)
                if existing_chan:
                    await interaction.followup.send(
                        f"ℹ️ A ticket is already open for this bug report: {existing_chan.mention}",
                        ephemeral=True,
                    )
                    return

            reporter_member = interaction.guild.get_member(bug.author_id)
            if not reporter_member:
                try:
                    reporter_member = await interaction.guild.fetch_member(bug.author_id)
                except Exception:
                    pass

            if not reporter_member:
                await interaction.followup.send(
                    "❌ The reporter is no longer in this Discord server.", ephemeral=True
                )
                return

            channel = await open_ticket_for_bug(
                guild=interaction.guild,
                reporter=reporter_member,
                staff_user=interaction.user,
                bug=bug,
                cfg=self.cfg,
            )

            if not channel:
                await interaction.followup.send(
                    "❌ Failed to create ticket channel. Please check bot permissions and category settings.",
                    ephemeral=True,
                )
                return

            # Refresh staff message embed to show ticket reference
            new_embed = build_bug_embed(bug, interaction.guild)
            try:
                await interaction.message.edit(embed=new_embed, view=self)
            except Exception as exc:
                log.warning("Could not refresh bug message with ticket: %s", exc)

            await interaction.followup.send(
                f"✅ Ticket created successfully: {channel.mention}",
                ephemeral=True,
            )


# ==============================================================================
# 4. Modals for Staff Actions
# ==============================================================================

class BugFixModal(ui.Modal, title="Mark Bug as Fixed"):
    """Modal for staff to specify resolution notes when marking a bug as fixed."""

    note_input: ui.TextInput = ui.TextInput(
        label="Fix Notes for Reporter (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="e.g. Issue resolved in patch v1.2. Money printer logic fixed.",
        default="The reported bug has been investigated and resolved.",
        max_length=500,
        required=False,
    )

    def __init__(self, bug_id: int, cfg: BotConfig, message_id: int) -> None:
        super().__init__()
        self.bug_id = bug_id
        self.cfg = cfg
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordBugReport).where(DiscordBugReport.id == self.bug_id)
            res = await session.execute(stmt)
            bug = res.scalar_one_or_none()

            if not bug:
                await interaction.followup.send("❌ Bug report not found.", ephemeral=True)
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            note = self.note_input.value.strip() or "The reported bug has been investigated and resolved."

            bug.status = "Fixed"
            bug.status_updated_by = interaction.user.display_name
            bug.status_reason = note
            bug.updated_at = now

            # Refresh review embed in staff channel
            embed = build_bug_embed(bug, interaction.guild)
            view = BugStaffActionView(self.cfg)
            try:
                msg = await interaction.channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
            except Exception as exc:
                log.warning("Could not edit staff bug message %s: %s", self.message_id, exc)

            # Update thread if exists
            if bug.thread_id:
                thread = interaction.guild.get_thread(bug.thread_id)
                if thread:
                    await thread.send(
                        f"🟢 **Bug Report #{bug.report_number:04d} Marked as Fixed** by {interaction.user.mention}!\n"
                        f"> *{note}*"
                    )
                    try:
                        await thread.edit(locked=True, archived=True)
                    except Exception:
                        pass

            # DM Reporter
            dm_status = "and reporter notified via DM"
            try:
                reporter = await interaction.client.fetch_user(bug.author_id)
                if reporter:
                    dm_embed = discord.Embed(
                        title=f"🎉 Bug Report #{bug.report_number:04d} Marked as Fixed!",
                        description=(
                            f"Hello {reporter.mention},\n\n"
                            f"Your bug report for **Darklands SMP** has been marked as **Fixed** by our staff team!\n\n"
                            f"**Bug Title:** {bug.title}\n"
                            f"**Resolution Note:**\n> {note}\n\n"
                            f"**Resolved by:** {interaction.user.display_name}\n\n"
                            f"Thank you for helping us test and improve Darklands SMP!"
                        ),
                        colour=0x57F287,
                        timestamp=now,
                    )
                    dm_embed.set_footer(text="Darklands SMP • Thank you for your support!")
                    await reporter.send(embed=dm_embed)
            except Exception as exc:
                log.info("Could not DM reporter %d about bug fix: %s", bug.author_id, exc)
                dm_status = "(could not DM user: DMs disabled or blocked)"

            await interaction.followup.send(
                f"✅ **Bug Report #{bug.report_number:04d} marked as Fixed {dm_status}!**",
                ephemeral=True,
            )


class BugDenyModal(ui.Modal, title="Deny Bug Report"):
    """Modal for staff to specify why a bug report was denied or deemed invalid."""

    reason_input: ui.TextInput = ui.TextInput(
        label="Reason for Denial (Sent to Reporter)",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this report is denied (e.g. Intended gameplay mechanic, unable to reproduce, duplicate)",
        max_length=500,
        required=True,
    )

    def __init__(self, bug_id: int, cfg: BotConfig, message_id: int) -> None:
        super().__init__()
        self.bug_id = bug_id
        self.cfg = cfg
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordBugReport).where(DiscordBugReport.id == self.bug_id)
            res = await session.execute(stmt)
            bug = res.scalar_one_or_none()

            if not bug:
                await interaction.followup.send("❌ Bug report not found.", ephemeral=True)
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            reason = self.reason_input.value.strip()

            bug.status = "Denied"
            bug.status_updated_by = interaction.user.display_name
            bug.status_reason = reason
            bug.updated_at = now

            # Refresh review embed in staff channel
            embed = build_bug_embed(bug, interaction.guild)
            view = BugStaffActionView(self.cfg)
            try:
                msg = await interaction.channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
            except Exception as exc:
                log.warning("Could not edit staff bug message %s: %s", self.message_id, exc)

            # Update thread if exists
            if bug.thread_id:
                thread = interaction.guild.get_thread(bug.thread_id)
                if thread:
                    await thread.send(
                        f"🔴 **Bug Report #{bug.report_number:04d} Closed as Denied** by {interaction.user.mention}.\n"
                        f"> *{reason}*"
                    )
                    try:
                        await thread.edit(locked=True, archived=True)
                    except Exception:
                        pass

            # DM Reporter
            dm_status = "and reporter notified via DM"
            try:
                reporter = await interaction.client.fetch_user(bug.author_id)
                if reporter:
                    dm_embed = discord.Embed(
                        title=f"❌ Bug Report #{bug.report_number:04d} Status Update",
                        description=(
                            f"Hello {reporter.mention},\n\n"
                            f"Your bug report for **Darklands SMP** has been reviewed and closed as **Denied / Invalid**.\n\n"
                            f"**Bug Title:** {bug.title}\n"
                            f"**Reason:**\n> {reason}\n\n"
                            f"**Reviewed by:** {interaction.user.display_name}"
                        ),
                        colour=0xED4245,
                        timestamp=now,
                    )
                    dm_embed.set_footer(text="Darklands SMP • Bug Tracker")
                    await reporter.send(embed=dm_embed)
            except Exception as exc:
                log.info("Could not DM reporter %d about bug denial: %s", bug.author_id, exc)
                dm_status = "(could not DM user: DMs disabled or blocked)"

            await interaction.followup.send(
                f"❌ **Bug Report #{bug.report_number:04d} closed as Denied {dm_status}!**",
                ephemeral=True,
            )


# ==============================================================================
# 5. Open Ticket Helper (Uses Ticket Infrastructure)
# ==============================================================================

async def open_ticket_for_bug(
    guild: discord.Guild,
    reporter: discord.Member,
    staff_user: discord.Member | discord.User,
    bug: DiscordBugReport,
    cfg: BotConfig,
) -> Optional[discord.TextChannel]:
    """Create a private Discord ticket channel for a bug report using the ticket system."""
    try:
        async with get_session() as session:
            # 1. Determine next ticket number
            max_num_res = await session.execute(
                select(func.max(DiscordTicket.ticket_number))
            )
            max_num = max_num_res.scalar_one_or_none()
            ticket_number = (max_num or 0) + 1
            ticket_code = f"ticket-{ticket_number:04d}"

            # 2. Pick ticket category (prefer application/bot ticket category: 1570845327995572314)
            category_id = (
                cfg.channels.application_ticket_category_id
                or (cfg.ticket_types[0].category_id if cfg.ticket_types else None)
            )
            category: Optional[discord.CategoryChannel] = (
                guild.get_channel(category_id) if category_id else None
            )

            # 3. Staff roles
            staff_role_ids = cfg.role_ids.staff + cfg.role_ids.admin
            staff_roles = [r for rid in staff_role_ids if (r := guild.get_role(rid)) is not None]

            # 4. Permissions
            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                reporter: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    attach_files=True,
                    read_message_history=True,
                ),
                guild.me: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_channels=True,
                    manage_messages=True,
                    read_message_history=True,
                ),
            }
            for role in staff_roles:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    read_message_history=True,
                )

            # 5. Create Channel
            channel = await guild.create_text_channel(
                name=f"bug-{bug.report_number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Inquiry for Bug #{bug.report_number:04d} | {bug.title} | {reporter}",
                reason=f"Bug inquiry ticket opened by {staff_user} for {reporter}",
            )

            # 6. Create Ticket ORM Record
            ticket = DiscordTicket(
                ticket_number=ticket_number,
                ticket_code=ticket_code,
                type_key="bug_report",
                type_name=f"Bug #{bug.report_number:04d} Inquiry",
                category_id=category.id if category else 0,
                channel_id=channel.id,
                channel_name=channel.name,
                creator_id=reporter.id,
                creator_name=str(reporter),
                creator_avatar=reporter.display_avatar.url if reporter.display_avatar else None,
                claimed_by_id=staff_user.id,
                claimed_by_name=staff_user.display_name,
                claimed_at=datetime.datetime.now(datetime.timezone.utc),
                priority="Medium",
                status="open",
            )
            session.add(ticket)
            await session.flush()

            # 7. Post Control View
            control_embed = build_control_embed(ticket, cfg)
            control_view = TicketControlView(cfg)
            control_msg = await channel.send(embed=control_embed, view=control_view)
            try:
                await control_msg.pin()
            except Exception:
                pass
            ticket.control_message_id = control_msg.id

            # 8. Post welcome & bug reference message
            intro_embed = discord.Embed(
                title=f"🐛 Bug Report Inquiry — #{bug.report_number:04d}",
                description=(
                    f"Welcome {reporter.mention}!\n\n"
                    f"Staff member {staff_user.mention} has opened this private channel with you regarding your bug report:\n"
                    f"**`{bug.title}`**\n\n"
                    f"• **Minecraft IGN:** `{bug.minecraft_ign or 'Not specified'}`\n"
                    f"• **Issue Summary:** {bug.description[:300]}\n\n"
                    f"Please discuss details or provide any further replication information here.\n"
                    f"When the investigation is complete, staff can close the ticket using the controls above."
                ),
                colour=0x5865F2,
            )
            await channel.send(content=f"{reporter.mention} {staff_user.mention}", embed=intro_embed)

            # Update bug record in database
            stmt = select(DiscordBugReport).where(DiscordBugReport.id == bug.id)
            b_res = await session.execute(stmt)
            curr_bug = b_res.scalar_one_or_none()
            if curr_bug:
                curr_bug.ticket_channel_id = channel.id
            bug.ticket_channel_id = channel.id

            # DM reporter about ticket
            try:
                dm_embed = discord.Embed(
                    title=f"🎫 Support Ticket Opened for Bug Report #{bug.report_number:04d}",
                    description=(
                        f"Hello {reporter.mention},\n\n"
                        f"Staff member **{staff_user.display_name}** has opened a private ticket channel to discuss your bug report: **{bug.title}**.\n\n"
                        f"👉 Head over to your ticket channel: {channel.mention}"
                    ),
                    colour=0x5865F2,
                )
                await reporter.send(embed=dm_embed)
            except Exception as exc:
                log.info("Could not DM reporter %d about opened bug ticket: %s", reporter.id, exc)

            log.info("Created bug ticket %s (channel %d) for Bug #%d", ticket_code, channel.id, bug.report_number)
            return channel
    except Exception as exc:
        log.exception("Error opening bug report ticket: %s", exc)
        return None


# ==============================================================================
# 6. Legacy & Public Views
# ==============================================================================

class BugItemView(ui.View):
    """
    Backwards-compatibility view for previously posted bug items:
    [✅ Mark Fixed] [🔒 Close] [🐛 Report Bug]
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @ui.button(
        label="Mark Fixed",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="bug_btn_mark_fixed",
    )
    async def mark_fixed_button(
        self, interaction: discord.Interaction, button: ui.Button
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

            new_embed = build_bug_embed(bug, interaction.guild)
            await interaction.message.edit(embed=new_embed, view=self)

            # DM Reporter
            try:
                reporter = await interaction.client.fetch_user(bug.author_id)
                if reporter:
                    dm_embed = discord.Embed(
                        title=f"🎉 Bug Report #{bug.report_number:04d} Marked as Fixed!",
                        description=f"Your bug report **{bug.title}** has been marked as **Fixed** by staff!",
                        colour=0x57F287,
                    )
                    await reporter.send(embed=dm_embed)
            except Exception:
                pass

            await interaction.followup.send("✅ Marked bug report as fixed!", ephemeral=True)

    @ui.button(
        label="Close",
        style=discord.ButtonStyle.secondary,
        emoji="🔒",
        custom_id="bug_btn_close",
    )
    async def close_button(
        self, interaction: discord.Interaction, button: ui.Button
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

            new_embed = build_bug_embed(bug, interaction.guild)
            await interaction.message.edit(embed=new_embed, view=self)

            await interaction.followup.send("🔒 Bug report closed.", ephemeral=True)

    @ui.button(
        label="Report Bug",
        style=discord.ButtonStyle.danger,
        emoji="🐛",
        custom_id="bug_btn_report_modal",
    )
    async def report_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        modal = BugReportModal(self.cfg)
        await interaction.response.send_modal(modal)


class BugLaunchView(ui.View):
    """Persistent starter panel for #・bug-reports."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @ui.button(
        label="Report a Bug",
        style=discord.ButtonStyle.danger,
        emoji="🐛",
        custom_id="bug_panel_launch_btn",
    )
    async def launch_bug_report(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        modal = BugReportModal(self.cfg)
        await interaction.response.send_modal(modal)
