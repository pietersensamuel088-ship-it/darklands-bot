"""
views.py – UI components, embeds, modals, and persistent views for Applications.
Includes:
- ApplicationLaunchView: The public panel button that triggers DM interviews.
- ApplicationReviewView: Staff review controls (Accept, Deny, Open Ticket).
- ApplicationDenyModal: Modal for staff to specify a denial reason.
- Helper to spawn dedicated interview/support tickets using the ticket system.
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import ui
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordApplication, DiscordTicket
from tickets.permissions import is_staff
from tickets.views import TicketControlView, build_control_embed

if TYPE_CHECKING:
    from config import ApplicationTypeConfig, BotConfig

log = logging.getLogger("darklands.applications.views")


# ==============================================================================
# 1. Embed Builder
# ==============================================================================

def build_application_embed(
    app: DiscordApplication,
    guild: Optional[discord.Guild] = None,
) -> discord.Embed:
    """Build the comprehensive review embed for a submitted application."""
    # Color based on status
    if app.status == "Accepted":
        colour = 0x57F287  # Green
        status_label = f"✅ Accepted by <@{app.reviewer_id}>"
    elif app.status == "Denied":
        colour = 0xED4245  # Red
        status_label = f"❌ Denied by <@{app.reviewer_id}>"
    else:
        colour = 0xFEE75C  # Gold/Yellow
        status_label = "⏳ Pending Review"

    embed = discord.Embed(
        title=f"📝 {app.type_name} — #{app.application_number:04d}",
        colour=colour,
        timestamp=app.created_at or datetime.datetime.now(datetime.timezone.utc),
    )

    # Basic Info Fields
    embed.add_field(
        name="👤 Applicant",
        value=f"<@{app.applicant_id}>\n`{app.applicant_name}`",
        inline=True,
    )
    embed.add_field(
        name="📌 Status",
        value=status_label,
        inline=True,
    )

    if app.created_at:
        submitted_unix = int(app.created_at.timestamp())
        embed.add_field(
            name="📅 Submitted",
            value=f"<t:{submitted_unix}:R>",
            inline=True,
        )

    # Fetch guild member info if available
    if guild:
        member = guild.get_member(app.applicant_id)
        if member:
            created_unix = int(member.created_at.timestamp())
            joined_unix = int(member.joined_at.timestamp()) if member.joined_at else 0
            embed.add_field(
                name="🗓️ Account Age",
                value=f"<t:{created_unix}:R>",
                inline=True,
            )
            embed.add_field(
                name="🚪 Joined Server",
                value=f"<t:{joined_unix}:R>" if joined_unix else "Unknown",
                inline=True,
            )

    if app.ticket_channel_id:
        embed.add_field(
            name="🎫 Interview Ticket",
            value=f"<#{app.ticket_channel_id}>",
            inline=True,
        )

    if app.status == "Denied" and app.review_reason:
        embed.add_field(
            name="🛑 Denial Reason",
            value=app.review_reason[:1000],
            inline=False,
        )

    # Questions and Answers
    embed.add_field(
        name="────────── Questionnaire ──────────",
        value="*Applicant responses below:*",
        inline=False,
    )

    for i, item in enumerate(app.answers or [], start=1):
        q_text = item.get("question", f"Question {i}")
        a_text = item.get("answer", "*No answer provided*")
        # Field name max 256, value max 1024
        name_str = f"Q{i}: {q_text}"[:250]
        val_str = f"> {a_text}"
        if len(val_str) > 1020:
            val_str = val_str[:1017] + "..."
        embed.add_field(name=name_str, value=val_str, inline=False)

    if app.applicant_avatar:
        embed.set_thumbnail(url=app.applicant_avatar)

    embed.set_footer(text=f"Darklands SMP • Application #{app.application_number:04d}")
    return embed


# ==============================================================================
# 2. Application Launch View (Panel)
# ==============================================================================

class ApplicationTypeSelect(ui.Select):
    """Persistent select menu allowing users to choose an application to start."""

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        options = []
        for app in cfg.applications:
            desc = app.description[:100] if app.description else "Click to begin your application"
            options.append(
                discord.SelectOption(
                    label=app.name,
                    value=app.key,
                    description=desc,
                    emoji=app.emoji or "📝",
                )
            )
        if not options:
            options.append(
                discord.SelectOption(
                    label="Beta Tester Application",
                    value="beta_tester",
                    description="Apply to test Darklands SMP before public launch",
                    emoji="🧪",
                )
            )

        super().__init__(
            custom_id="app:launch:select",
            placeholder="Select an application to start applying…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        from applications.session import application_manager

        await interaction.response.defer(ephemeral=True)
        selected_key = self.values[0]

        # 1. Check if user already has an active pending application for this key
        try:
            async with get_session() as session:
                stmt = select(func.count(DiscordApplication.id)).where(
                    DiscordApplication.applicant_id == interaction.user.id,
                    DiscordApplication.type_key == selected_key,
                    DiscordApplication.status == "Pending",
                )
                res = await session.execute(stmt)
                if res.scalar_one() > 0:
                    await interaction.followup.send(
                        "❌ You already have an active application for this role awaiting staff review! "
                        "Please wait for a decision before applying again.",
                        ephemeral=True,
                    )
                    return
        except Exception as exc:
            log.exception("Error checking existing application: %s", exc)

        # 2. Check active DM session
        if application_manager.has_session(interaction.user.id):
            await interaction.followup.send(
                "⚠️ You already have an active application session running in your Direct Messages! "
                "Please check your DMs or reply `cancel` there to reset.",
                ephemeral=True,
            )
            return

        # 3. Retrieve app configuration
        app_cfg = next((a for a in self.cfg.applications if a.key == selected_key), None)
        if not app_cfg:
            from config import ApplicationTypeConfig
            app_cfg = ApplicationTypeConfig(key=selected_key)

        # 4. Initiate DM interview
        try:
            session = application_manager.start_session(
                user=interaction.user,
                guild=interaction.guild,
                app_config=app_cfg,
            )

            total_q = len(app_cfg.questions)
            dm_embed = discord.Embed(
                title=f"{app_cfg.emoji} Darklands SMP — {app_cfg.name}",
                description=(
                    f"Welcome, **{interaction.user.display_name}**!\n"
                    f"{app_cfg.description}\n\n"
                    f"• I will ask you **{total_q} questions** one at a time.\n"
                    f"• Reply directly to this DM with your answer.\n"
                    f"• Type `cancel` at any time to abort.\n\n"
                    f"─────────────────────────────\n"
                    f"**Question 1 of {total_q}:**\n"
                    f"**{app_cfg.questions[0] if app_cfg.questions else 'Why do you want to apply?'}**"
                ),
                colour=0x5865F2,
            )
            dm_embed.set_footer(text=f"Darklands SMP • Question 1 of {total_q}")
            await interaction.user.send(embed=dm_embed)

            await interaction.followup.send(
                f"📩 **I have sent you a Direct Message to begin your {app_cfg.name}!**\n"
                "Please check your DMs to answer the questions.",
                ephemeral=True,
            )
        except discord.Forbidden:
            application_manager.cancel_session(interaction.user.id)
            await interaction.followup.send(
                "❌ **Could not send you a Direct Message!**\n"
                "Please enable **'Allow direct messages from server members'** in your Discord Privacy Settings "
                "(User Settings → Privacy & Safety) and try selecting the application again.",
                ephemeral=True,
            )
        except Exception as exc:
            application_manager.cancel_session(interaction.user.id)
            log.exception("Error starting application session: %s", exc)
            await interaction.followup.send(
                "⚠️ An unexpected error occurred while starting your application. Please try again later.",
                ephemeral=True,
            )


class ApplicationLaunchView(ui.View):
    """Persistent view attached to the Application Launcher panel in server."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg
        self.add_item(ApplicationTypeSelect(cfg))


# ==============================================================================
# 3. Application Staff Review View
# ==============================================================================

class ApplicationReviewView(ui.View):
    """Persistent view attached to application review embeds in staff channels."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # Accept Button
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Accept",
        style=discord.ButtonStyle.success,
        emoji="✅",
        custom_id="app:action:accept",
    )
    async def accept_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can review applications.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordApplication).where(
                DiscordApplication.message_id == interaction.message.id
            )
            res = await session.execute(stmt)
            app = res.scalar_one_or_none()

            if not app:
                await interaction.followup.send("❌ Application not found in database.", ephemeral=True)
                return

            if app.status != "Pending":
                await interaction.followup.send(
                    f"⚠️ This application has already been marked as **{app.status}**.",
                    ephemeral=True,
                )
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            app.status = "Accepted"
            app.reviewer_id = interaction.user.id
            app.reviewer_name = interaction.user.display_name
            app.reviewed_at = now

            # Refresh embed
            embed = build_application_embed(app, interaction.guild)

            # Update buttons: Disable Accept/Deny
            for child in self.children:
                if isinstance(child, ui.Button) and child.custom_id in {"app:action:accept", "app:action:deny"}:
                    child.disabled = True

            await interaction.message.edit(embed=embed, view=self)

            # Assign role if configured
            app_cfg = next((a for a in self.cfg.applications if a.key == app.type_key), None)
            role_assigned = False
            if app_cfg and app_cfg.accepted_role_id:
                member = interaction.guild.get_member(app.applicant_id)
                if member:
                    role = interaction.guild.get_role(app_cfg.accepted_role_id)
                    if role:
                        try:
                            await member.add_roles(role, reason=f"Application #{app.application_number} accepted")
                            role_assigned = True
                        except Exception as exc:
                            log.warning("Failed to assign role %s to member %s: %s", role.id, member.id, exc)

            # DM Applicant
            try:
                applicant_user = await interaction.client.fetch_user(app.applicant_id)
                dm_embed = discord.Embed(
                    title=f"🎉 {app.type_name} Accepted!",
                    description=(
                        f"Congratulations, **{applicant_user.display_name}**!\n\n"
                        f"Your **{app.type_name}** for Darklands SMP has been **ACCEPTED** by {interaction.user.mention}!\n"
                        + ("• Your in-game/Discord tester role has been granted.\n" if role_assigned else "")
                        + "\nWelcome to the team! Keep an eye on announcements for testing phases and access."
                    ),
                    colour=0x57F287,
                    timestamp=now,
                )
                dm_embed.set_footer(text="Darklands SMP • Applications")
                await applicant_user.send(embed=dm_embed)
            except Exception as exc:
                log.info("Could not DM applicant %s about acceptance: %s", app.applicant_id, exc)

            await interaction.followup.send(
                f"✅ **Application #{app.application_number} has been accepted!**",
                ephemeral=True,
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Deny Button
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Deny",
        style=discord.ButtonStyle.danger,
        emoji="❌",
        custom_id="app:action:deny",
    )
    async def deny_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can review applications.", ephemeral=True
            )
            return

        async with get_session() as session:
            stmt = select(DiscordApplication).where(
                DiscordApplication.message_id == interaction.message.id
            )
            res = await session.execute(stmt)
            app = res.scalar_one_or_none()

            if not app:
                await interaction.response.send_message(
                    "❌ Application not found in database.", ephemeral=True
                )
                return

            if app.status != "Pending":
                await interaction.response.send_message(
                    f"⚠️ This application has already been marked as **{app.status}**.",
                    ephemeral=True,
                )
                return

            modal = ApplicationDenyModal(app.id, self.cfg, interaction.message.id)
            await interaction.response.send_modal(modal)

    # ──────────────────────────────────────────────────────────────────────────
    # Open Ticket Button
    # ──────────────────────────────────────────────────────────────────────────
    @ui.button(
        label="Open Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🎫",
        custom_id="app:action:ticket",
    )
    async def ticket_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can open application tickets.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordApplication).where(
                DiscordApplication.message_id == interaction.message.id
            )
            res = await session.execute(stmt)
            app = res.scalar_one_or_none()

            if not app:
                await interaction.followup.send("❌ Application not found in database.", ephemeral=True)
                return

            # Check if ticket channel is already open
            if app.ticket_channel_id:
                existing_channel = interaction.guild.get_channel(app.ticket_channel_id)
                if existing_channel:
                    await interaction.followup.send(
                        f"ℹ️ A ticket is already open for this application: {existing_channel.mention}",
                        ephemeral=True,
                    )
                    return

            applicant_member = interaction.guild.get_member(app.applicant_id)
            if not applicant_member:
                try:
                    applicant_member = await interaction.guild.fetch_member(app.applicant_id)
                except Exception:
                    pass

            if not applicant_member:
                await interaction.followup.send(
                    "❌ The applicant is no longer in this Discord server.", ephemeral=True
                )
                return

            # Spawn ticket using ticket infrastructure
            channel = await open_ticket_for_application(
                guild=interaction.guild,
                applicant=applicant_member,
                staff_user=interaction.user,
                app=app,
                cfg=self.cfg,
            )

            if channel is None:
                await interaction.followup.send(
                    "❌ Failed to create ticket channel. Check bot permissions and category settings.",
                    ephemeral=True,
                )
                return

            app.ticket_channel_id = channel.id
            # Refresh message embed to link to ticket
            embed = build_application_embed(app, interaction.guild)
            await interaction.message.edit(embed=embed, view=self)

            await interaction.followup.send(
                f"✅ Ticket created successfully: {channel.mention}",
                ephemeral=True,
            )


# ==============================================================================
# 4. Application Deny Modal
# ==============================================================================

class ApplicationDenyModal(ui.Modal, title="Deny Application"):
    """Modal for staff to specify why an application was rejected."""

    reason_input: ui.TextInput = ui.TextInput(
        label="Reason for Denial",
        style=discord.TextStyle.paragraph,
        placeholder="Explain why this application was denied (sent to the applicant)...",
        required=False,
        max_length=500,
    )

    def __init__(self, app_id: int, cfg: BotConfig, message_id: int) -> None:
        super().__init__()
        self.app_id = app_id
        self.cfg = cfg
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordApplication).where(DiscordApplication.id == self.app_id)
            res = await session.execute(stmt)
            app = res.scalar_one_or_none()

            if not app:
                await interaction.followup.send("❌ Application not found.", ephemeral=True)
                return

            now = datetime.datetime.now(datetime.timezone.utc)
            reason = self.reason_input.value.strip() or "Does not meet current requirements."

            app.status = "Denied"
            app.reviewer_id = interaction.user.id
            app.reviewer_name = interaction.user.display_name
            app.review_reason = reason
            app.reviewed_at = now

            # Refresh review embed in staff channel
            embed = build_application_embed(app, interaction.guild)
            view = ApplicationReviewView(self.cfg)
            for child in view.children:
                if isinstance(child, ui.Button) and child.custom_id in {"app:action:accept", "app:action:deny"}:
                    child.disabled = True

            try:
                msg = await interaction.channel.fetch_message(self.message_id)
                await msg.edit(embed=embed, view=view)
            except Exception as exc:
                log.warning("Could not edit review message %s: %s", self.message_id, exc)

            # DM Applicant
            try:
                applicant_user = await interaction.client.fetch_user(app.applicant_id)
                dm_embed = discord.Embed(
                    title=f"Update on your {app.type_name}",
                    description=(
                        f"Hello **{applicant_user.display_name}**,\n\n"
                        f"Your **{app.type_name}** for Darklands SMP was unfortunately **DENIED** by staff.\n\n"
                        f"**Reason:** {reason}\n\n"
                        "Thank you for your interest and support. You are welcome to reapply in the future!"
                    ),
                    colour=0xED4245,
                    timestamp=now,
                )
                dm_embed.set_footer(text="Darklands SMP • Applications")
                await applicant_user.send(embed=dm_embed)
            except Exception as exc:
                log.info("Could not DM applicant %s about denial: %s", app.applicant_id, exc)

            await interaction.followup.send(
                f"❌ **Application #{app.application_number} has been denied.**",
                ephemeral=True,
            )


# ==============================================================================
# 5. Open Ticket Helper (Uses Ticket Infrastructure)
# ==============================================================================

async def open_ticket_for_application(
    guild: discord.Guild,
    applicant: discord.Member,
    staff_user: discord.Member | discord.User,
    app: DiscordApplication,
    cfg: BotConfig,
) -> Optional[discord.TextChannel]:
    """Create a private Discord ticket for an applicant using the ticket system."""
    try:
        async with get_session() as session:
            # 1. Determine next ticket number
            max_num_res = await session.execute(
                select(func.max(DiscordTicket.ticket_number))
            )
            max_num = max_num_res.scalar_one_or_none()
            ticket_number = (max_num or 0) + 1
            ticket_code = f"ticket-{ticket_number:04d}"

            # 2. Pick a ticket category (prefer 'other', or first configured category)
            ticket_type = next((t for t in cfg.ticket_types if t.key == "other"), None)
            if not ticket_type and cfg.ticket_types:
                ticket_type = cfg.ticket_types[0]

            category_id = ticket_type.category_id if ticket_type else None
            category: Optional[discord.CategoryChannel] = (
                guild.get_channel(category_id) if category_id else None
            )

            # 3. Staff roles
            staff_role_ids = cfg.role_ids.staff + cfg.role_ids.admin
            staff_roles = [r for rid in staff_role_ids if (r := guild.get_role(rid)) is not None]

            # 4. Overwrites
            overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
                guild.default_role: discord.PermissionOverwrite(view_channel=False),
                applicant: discord.PermissionOverwrite(
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
                ),
            }
            if isinstance(staff_user, discord.Member):
                overwrites[staff_user] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    read_message_history=True,
                )
            for role in staff_roles:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    manage_messages=True,
                    read_message_history=True,
                )

            # 5. Create Channel
            channel = await guild.create_text_channel(
                name=f"app-{app.application_number:04d}",
                category=category,
                overwrites=overwrites,
                topic=f"Interview for Application #{app.application_number} | {app.type_name} | {applicant}",
                reason=f"Application ticket opened by {staff_user} for {applicant}",
            )

            # 6. Create Ticket ORM Record
            ticket = DiscordTicket(
                ticket_number=ticket_number,
                ticket_code=ticket_code,
                type_key="application",
                type_name=f"App #{app.application_number:04d} Interview",
                category_id=category.id if category else 0,
                channel_id=channel.id,
                channel_name=channel.name,
                creator_id=applicant.id,
                creator_name=str(applicant),
                creator_avatar=applicant.display_avatar.url if applicant.display_avatar else None,
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

            # 8. Post welcome & application reference message
            intro_embed = discord.Embed(
                title=f"📋 Application Review — #{app.application_number:04d}",
                description=(
                    f"Welcome {applicant.mention}!\n\n"
                    f"Staff member {staff_user.mention} has opened this private channel with you regarding your **{app.type_name}**.\n\n"
                    "• Use this channel to ask any questions or provide additional details.\n"
                    "• When the discussion is complete, staff can close the ticket using the controls above."
                ),
                colour=0x5865F2,
            )
            await channel.send(content=f"{applicant.mention} {staff_user.mention}", embed=intro_embed)

            app.ticket_id = ticket.id
            app.ticket_channel_id = channel.id
            log.info(
                "Created application ticket %s (channel %d) for App #%d",
                ticket_code,
                channel.id,
                app.application_number,
            )
            return channel
    except Exception as exc:
        log.exception("Error opening application ticket: %s", exc)
        return None
