"""
tickets/views.py
================
All Discord UI components (Views, Selects, Modals, Buttons) for the
Darklands SMP ticket system.

Components
----------
- build_control_embed        : Helper – builds the pinned control embed.
- TicketTypeSelect           : Select menu on the support panel.
- TicketPanelView            : View that hosts the panel select menu.
- TicketCloseRequestModal    : Modal that collects close-delay + reason.
- TicketControlView          : Persistent view pinned inside every ticket channel.
- TicketClosePendingView     : Transient view shown when a close is pending.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

import discord
from discord import ui
from sqlalchemy import select, func

from config import BotConfig, CLOSE_DELAYS, PRIORITY_COLOURS, VALID_PRIORITIES
from database.engine import get_session
from database.models import DiscordTicket
from tickets.permissions import (
    can_create_ticket,
    can_claim,
    can_manage,
    can_close_request,
    can_close_cancel,
)

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
log = logging.getLogger("darklands.tickets")


# ---------------------------------------------------------------------------
# Helper: status badge emoji
# ---------------------------------------------------------------------------
_STATUS_EMOJI: dict[str, str] = {
    "open": "🟢",
    "close_pending": "🟡",
    "closed": "🔴",
}


# ===========================================================================
# 1. build_control_embed
# ===========================================================================

def build_control_embed(ticket: DiscordTicket, cfg: BotConfig) -> discord.Embed:
    """
    Build the rich embed that is pinned as the control message inside a
    ticket channel.

    Parameters
    ----------
    ticket : DiscordTicket
        The ORM record for this ticket.
    cfg : BotConfig
        The active bot configuration (used for colour lookups, etc.).

    Returns
    -------
    discord.Embed
        A fully populated embed ready to send or edit.
    """
    colour_int: int = PRIORITY_COLOURS.get(ticket.priority, 0x5865F2)
    embed = discord.Embed(
        title=f"🎫 {ticket.type_name} — {ticket.ticket_code}",
        colour=colour_int,
    )

    # --- Status badge ---
    status_emoji = _STATUS_EMOJI.get(ticket.status, "❓")
    embed.add_field(
        name="Creator",
        value=f"<@{ticket.creator_id}>",
        inline=True,
    )
    embed.add_field(
        name="Status",
        value=f"{status_emoji} {ticket.status.replace('_', ' ').title()}",
        inline=True,
    )
    embed.add_field(
        name="Priority",
        value=ticket.priority.capitalize(),
        inline=True,
    )

    # --- Claimed by ---
    if ticket.claimed_by_id:
        claimed_value = f"<@{ticket.claimed_by_id}>"
    else:
        claimed_value = "*Unclaimed*"
    embed.add_field(name="Claimed By", value=claimed_value, inline=True)

    # --- Ticket number + created timestamp ---
    embed.add_field(
        name="Ticket #",
        value=str(ticket.ticket_number),
        inline=True,
    )
    created_unix = int(ticket.created_at.timestamp())
    embed.add_field(
        name="Created At",
        value=f"<t:{created_unix}:F>",
        inline=True,
    )

    # --- Scheduled close (only when close_pending) ---
    if ticket.status == "close_pending" and ticket.close_at is not None:
        close_unix = int(ticket.close_at.timestamp())
        embed.add_field(
            name="Scheduled Close",
            value=(
                f"<t:{close_unix}:F> (<t:{close_unix}:R>)\n"
                f"**Reason:** {ticket.close_reason or 'No reason provided.'}"
            ),
            inline=False,
        )

    embed.set_footer(
        text="Darklands SMP Network • Use the buttons below to manage this ticket"
    )
    return embed


# ===========================================================================
# 2. TicketTypeSelect
# ===========================================================================

class TicketTypeSelect(ui.Select):
    """
    A persistent select menu shown on the support panel that lets users
    choose which type of ticket they want to open.

    custom_id : ``"ticket:panel_select"``
    """

    def __init__(self, cfg: BotConfig) -> None:
        self.cfg = cfg

        options = [
            discord.SelectOption(
                label=t.name,
                value=t.key,
                description=t.description,
                emoji=t.emoji,
            )
            for t in cfg.ticket_types
        ]

        super().__init__(
            custom_id="ticket:panel_select",
            placeholder="Select a ticket type…",
            min_values=1,
            max_values=1,
            options=options,
        )

    # ------------------------------------------------------------------
    async def callback(self, interaction: discord.Interaction) -> None:
        """
        Handles the user selecting a ticket type.

        Flow
        ----
        1. Defer ephemeral.
        2. Permission check.
        3. Find the matching TicketTypeConfig.
        4. Check open ticket count for this user.
        5. Calculate next ticket_number.
        6. Resolve Discord category.
        7. Build permission overwrites.
        8. Create the channel.
        9. Persist the ORM record.
        10. Send welcome embed + control view into the channel.
        11. Pin the control message.
        12. Commit and inform the user.
        """
        await interaction.response.defer(ephemeral=True)

        try:
            # 2. Permission check -------------------------------------------
            if not can_create_ticket(interaction.user, self.cfg):
                await interaction.followup.send(
                    "❌ You do not have permission to open a ticket.",
                    ephemeral=True,
                )
                return

            # 3. Resolve ticket type ----------------------------------------
            selected_key: str = self.values[0]
            type_config = next(
                (t for t in self.cfg.ticket_types if t.key == selected_key),
                None,
            )
            if type_config is None:
                await interaction.followup.send(
                    "❌ Unknown ticket type. Please try again.",
                    ephemeral=True,
                )
                return

            guild: discord.Guild = interaction.guild

            async with get_session() as session:
                # 4. Count open tickets for this user -----------------------
                open_count_result = await session.execute(
                    select(func.count(DiscordTicket.id)).where(
                        DiscordTicket.creator_id == interaction.user.id,
                        DiscordTicket.status != "closed",
                    )
                )
                open_count: int = open_count_result.scalar_one()

                if open_count >= self.cfg.max_open_tickets_per_user:
                    await interaction.followup.send(
                        f"❌ You already have **{open_count}** open ticket(s). "
                        f"The maximum is **{self.cfg.max_open_tickets_per_user}**. "
                        "Please resolve your existing ticket(s) before opening a new one.",
                        ephemeral=True,
                    )
                    return

                # 5. Next ticket number -------------------------------------
                max_num_result = await session.execute(
                    select(func.max(DiscordTicket.ticket_number))
                )
                max_num: Optional[int] = max_num_result.scalar_one_or_none()
                ticket_number: int = (max_num or 0) + 1

                # 6. Build ticket code + channel name -----------------------
                ticket_code = f"ticket-{ticket_number:04d}"
                channel_name = ticket_code

                # 7. Resolve the Discord category ---------------------------
                category: Optional[discord.CategoryChannel] = guild.get_channel(
                    type_config.category_id
                )
                if category is None:
                    log.error(
                        "Category %s not found for ticket type %s",
                        type_config.category_id,
                        type_config.key,
                    )
                    await interaction.followup.send(
                        "❌ The ticket category could not be found. "
                        "Please contact an administrator.",
                        ephemeral=True,
                    )
                    return

                # 8. Collect all staff roles --------------------------------
                staff_role_ids = (
                    self.cfg.role_ids.staff + self.cfg.role_ids.admin
                )
                staff_roles: list[discord.Role] = [
                    r
                    for rid in staff_role_ids
                    if (r := guild.get_role(rid)) is not None
                ]

                # 9. Build permission overwrites ----------------------------
                overwrites: dict[
                    discord.abc.Snowflake, discord.PermissionOverwrite
                ] = {
                    guild.default_role: discord.PermissionOverwrite(
                        view_channel=False,
                    ),
                    interaction.user: discord.PermissionOverwrite(
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
                for role in staff_roles:
                    overwrites[role] = discord.PermissionOverwrite(
                        view_channel=True,
                        send_messages=True,
                        manage_messages=True,
                        read_message_history=True,
                    )

                # 10. Create the text channel ------------------------------
                channel: discord.TextChannel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites,
                    topic=(
                        f"Ticket {ticket_code} | {type_config.name} | "
                        f"{interaction.user}"
                    ),
                    reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
                )
                log.info(
                    "Created ticket channel %s (id=%s) for user %s",
                    ticket_code,
                    channel.id,
                    interaction.user,
                )

                # 11. Persist ORM record -----------------------------------
                now = datetime.datetime.now(datetime.timezone.utc)
                initial_event = {
                    "event": "opened",
                    "actor_id": interaction.user.id,
                    "actor_name": str(interaction.user),
                    "timestamp": now.isoformat(),
                }
                ticket = DiscordTicket(
                    ticket_number=ticket_number,
                    ticket_code=ticket_code,
                    type_key=type_config.key,
                    type_name=type_config.name,
                    category_id=type_config.category_id,
                    channel_id=channel.id,
                    channel_name=channel.name,
                    creator_id=interaction.user.id,
                    creator_name=str(interaction.user),
                    creator_avatar=str(interaction.user.display_avatar.url) if interaction.user.display_avatar else None,
                    status="open",
                    priority=type_config.default_priority,
                    history=[initial_event],
                    added_members=[],
                    created_at=now,
                )
                session.add(ticket)
                # Flush so the DB assigns ticket.id before we use it
                await session.flush()

                # 12. Build welcome embed ----------------------------------
                colour_int: int = PRIORITY_COLOURS.get(
                    type_config.default_priority, 0x5865F2
                )
                welcome_embed = discord.Embed(
                    title=f"👋 Welcome, {interaction.user.display_name}!",
                    description=(
                        f"Your **{type_config.name}** ticket has been opened. "
                        "A member of staff will be with you shortly.\n\n"
                        "Please describe your issue in as much detail as possible "
                        "so we can assist you efficiently."
                    ),
                    colour=colour_int,
                )
                welcome_embed.add_field(
                    name="Ticket Type",
                    value=f"{type_config.emoji} {type_config.name}",
                    inline=True,
                )
                welcome_embed.add_field(
                    name="Priority",
                    value=type_config.default_priority.capitalize(),
                    inline=True,
                )
                welcome_embed.add_field(
                    name="Instructions",
                    value=(
                        "• Use the buttons below to manage your ticket.\n"
                        "• Staff can claim, escalate, or close this ticket.\n"
                        "• Do **not** ping staff — they will respond as soon as possible."
                    ),
                    inline=False,
                )
                welcome_embed.set_footer(
                    text="Darklands SMP Network • Ticket System"
                )

                # 13. Send welcome embed + control view --------------------
                control_view = TicketControlView(self.cfg)
                control_msg: discord.Message = await channel.send(
                    embed=welcome_embed,
                    view=control_view,
                )

                # 14. Pin the control message ------------------------------
                await control_msg.pin(
                    reason="Ticket control message — do not unpin."
                )

                # 15. Store control_message_id and commit ------------------
                ticket.control_message_id = control_msg.id
                await session.commit()

            # 16. Acknowledge the user ------------------------------------
            await interaction.followup.send(
                f"✅ Your ticket has been created: {channel.mention}",
                ephemeral=True,
            )

        except discord.DiscordException as exc:
            log.exception("Discord error during ticket creation: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred while creating your ticket. "
                "Please try again or contact an administrator.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error during ticket creation: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred. Please try again.",
                ephemeral=True,
            )


# ===========================================================================
# 3. TicketPanelView
# ===========================================================================

class TicketPanelView(ui.View):
    """
    The persistent view sent to the support panel channel.
    Contains only a ``TicketTypeSelect`` menu.
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg
        self.add_item(TicketTypeSelect(cfg))

    # ------------------------------------------------------------------
    @classmethod
    async def send_panel(
        cls,
        channel: discord.TextChannel,
        cfg: BotConfig,
    ) -> discord.Message:
        """
        Send the support panel embed + view to *channel*.

        Parameters
        ----------
        channel : discord.TextChannel
            The channel to post the panel into.
        cfg : BotConfig
            The active bot configuration.

        Returns
        -------
        discord.Message
            The message that was sent.
        """
        # Build ticket-type list for the embed description
        type_lines = "\n".join(
            f"{t.emoji} **{t.name}** — {t.description}"
            for t in cfg.ticket_types
        )

        embed = discord.Embed(
            title="📩 Open a Support Ticket",
            description=(
                "Need help? Select the ticket type that best describes your issue "
                "from the menu below and a member of staff will assist you.\n\n"
                f"{type_lines}\n\n"
                "Please be patient — staff will respond as soon as possible."
            ),
            colour=0x5865F2,
        )
        embed.set_footer(text="Darklands SMP Network • Ticket System")

        view = cls(cfg)
        return await channel.send(embed=embed, view=view)


# ===========================================================================
# 4. TicketCloseRequestModal
# ===========================================================================

class TicketCloseRequestModal(ui.Modal, title="Request Ticket Closure"):
    """
    Modal that collects the close delay and an optional reason from the
    staff member requesting closure.
    """

    delay_input: ui.TextInput = ui.TextInput(
        label="Close Delay",
        placeholder="15m, 1h, 6h, 12h, 24h, 3d",
        required=True,
        max_length=4,
    )
    reason_input: ui.TextInput = ui.TextInput(
        label="Reason",
        style=discord.TextStyle.paragraph,
        placeholder="Why is this ticket being closed?",
        required=False,
        max_length=500,
    )

    def __init__(self, ticket_id: int, cfg: BotConfig) -> None:
        super().__init__()
        self.ticket_id = ticket_id
        self.cfg = cfg

    # ------------------------------------------------------------------
    async def on_submit(self, interaction: discord.Interaction) -> None:
        """
        Handles submission of the close-request modal.

        Flow
        ----
        1. Validate the delay key.
        2. Fetch ticket from DB.
        3. Permission + state checks.
        4. Calculate close_at timestamp.
        5. Update ticket fields.
        6. Append history event.
        7. Edit the pinned control embed.
        8. Post close-pending message with ``TicketClosePendingView``.
        9. Acknowledge the requester.
        """
        await interaction.response.defer(ephemeral=True)

        try:
            delay_key: str = self.delay_input.value.strip().lower()
            if delay_key not in CLOSE_DELAYS:
                valid = ", ".join(CLOSE_DELAYS.keys())
                await interaction.followup.send(
                    f"❌ **'{delay_key}'** is not a valid close delay. "
                    f"Valid options: `{valid}`",
                    ephemeral=True,
                )
                return

            reason: str = (self.reason_input.value or "").strip() or "No reason provided."

            async with get_session() as session:
                # Fetch ticket ------------------------------------------------
                result = await session.execute(
                    select(DiscordTicket).where(DiscordTicket.id == self.ticket_id)
                )
                ticket: Optional[DiscordTicket] = result.scalar_one_or_none()

                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket not found.", ephemeral=True
                    )
                    return

                # Permission check --------------------------------------------
                if not can_close_request(interaction.user, self.cfg, ticket):
                    await interaction.followup.send(
                        "❌ You do not have permission to request closure of this ticket.",
                        ephemeral=True,
                    )
                    return

                # State check -------------------------------------------------
                if ticket.status != "open":
                    await interaction.followup.send(
                        "❌ Ticket is already pending close or closed.",
                        ephemeral=True,
                    )
                    return

                # Calculate close timestamp -----------------------------------
                delay_seconds: int = CLOSE_DELAYS[delay_key]
                now = datetime.datetime.now(datetime.timezone.utc)
                close_at = now + datetime.timedelta(seconds=delay_seconds)

                # Update ticket fields ----------------------------------------
                ticket.status = "close_pending"
                ticket.close_at = close_at
                ticket.close_delay_key = delay_key
                ticket.close_delay_seconds = delay_seconds
                ticket.close_reason = reason
                ticket.close_requested_by_id = interaction.user.id
                ticket.close_requested_by_name = str(interaction.user)

                # History event -----------------------------------------------
                ticket.add_history_event(
                    "close_requested",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    delay_key=delay_key,
                    reason=reason,
                    close_at=close_at.isoformat(),
                )

                await session.flush()

                # Edit pinned control message ---------------------------------
                channel: discord.TextChannel = interaction.channel
                if ticket.control_message_id:
                    try:
                        ctrl_msg = await channel.fetch_message(
                            ticket.control_message_id
                        )
                        await ctrl_msg.edit(
                            embed=build_control_embed(ticket, self.cfg),
                            view=TicketControlView(self.cfg),
                        )
                    except discord.NotFound:
                        log.warning(
                            "Control message %s not found for ticket %s",
                            ticket.control_message_id,
                            ticket.ticket_code,
                        )

                # Send close-pending message ----------------------------------
                close_unix = int(close_at.timestamp())
                pending_embed = discord.Embed(
                    title="🕐 Ticket Closure Scheduled",
                    description=(
                        f"This ticket will be **automatically closed** "
                        f"<t:{close_unix}:R> (<t:{close_unix}:F>).\n\n"
                        f"**Reason:** {reason}\n\n"
                        "Use the buttons below to cancel or immediately close the ticket."
                    ),
                    colour=0xFFA500,
                )
                pending_embed.set_footer(
                    text=(
                        f"Closure requested by {interaction.user} • "
                        "Darklands SMP Network"
                    )
                )
                pending_view = TicketClosePendingView(self.cfg)
                close_msg: discord.Message = await channel.send(
                    embed=pending_embed,
                    view=pending_view,
                )
                ticket.close_message_id = close_msg.id

                await session.commit()

            log.info(
                "Close requested for ticket %s by %s (delay=%s)",
                ticket.ticket_code,
                interaction.user,
                delay_key,
            )
            await interaction.followup.send(
                "✅ Closure scheduled.", ephemeral=True
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in close-request modal: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in close-request modal: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred. Please try again.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        log.exception("TicketCloseRequestModal on_error: %s", error)
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "❌ An error occurred processing this modal.", ephemeral=True
            )


# ===========================================================================
# 5. TicketControlView
# ===========================================================================

class TicketControlView(ui.View):
    """
    Persistent view pinned inside every ticket channel.

    Buttons
    -------
    * 🙋 Claim Ticket       — staff claims ownership of the ticket.
    * 🔓 Release Claim      — staff releases an existing claim.
    * 📝 Staff Notes        — creates / links a private staff notes thread.
    * 🔒 Request Close      — opens TicketCloseRequestModal.
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Internal helper: fetch ticket by channel_id
    # ------------------------------------------------------------------
    @staticmethod
    async def _fetch_ticket(
        session, channel_id: int
    ) -> Optional[DiscordTicket]:
        result = await session.execute(
            select(DiscordTicket).where(
                DiscordTicket.channel_id == channel_id
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Button 1 — Claim Ticket
    # ------------------------------------------------------------------
    @ui.button(
        label="Claim Ticket",
        style=discord.ButtonStyle.primary,
        emoji="🙋",
        custom_id="ticket:ctrl:claim",
        row=0,
    )
    async def claim_ticket(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """Staff claims ownership of this ticket."""
        await interaction.response.defer(ephemeral=True)
        try:
            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                if not can_claim(interaction.user, self.cfg):
                    await interaction.followup.send(
                        "❌ You do not have permission to claim tickets.",
                        ephemeral=True,
                    )
                    return

                if ticket.claimed_by_id and ticket.claimed_by_id != interaction.user.id:
                    await interaction.followup.send(
                        f"❌ This ticket is already claimed by <@{ticket.claimed_by_id}>.",
                        ephemeral=True,
                    )
                    return

                if ticket.claimed_by_id == interaction.user.id:
                    await interaction.followup.send(
                        "❌ You have already claimed this ticket.",
                        ephemeral=True,
                    )
                    return

                now = datetime.datetime.now(datetime.timezone.utc)
                ticket.claimed_by_id = interaction.user.id
                ticket.claimed_by_name = str(interaction.user)
                ticket.claimed_at = now

                ticket.add_history_event(
                    "claimed",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    timestamp=now.isoformat(),
                )

                await session.flush()

                # Update control embed
                if ticket.control_message_id:
                    try:
                        ctrl_msg = await interaction.channel.fetch_message(
                            ticket.control_message_id
                        )
                        await ctrl_msg.edit(
                            embed=build_control_embed(ticket, self.cfg),
                            view=TicketControlView(self.cfg),
                        )
                    except discord.NotFound:
                        log.warning(
                            "Control message not found for ticket %s",
                            ticket.ticket_code,
                        )

                await session.commit()

            log.info(
                "Ticket %s claimed by %s",
                ticket.ticket_code,
                interaction.user,
            )
            await interaction.followup.send(
                f"✅ You have claimed **{ticket.ticket_code}**.",
                ephemeral=True,
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in claim_ticket: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in claim_ticket: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # Button 2 — Release Claim
    # ------------------------------------------------------------------
    @ui.button(
        label="Release Claim",
        style=discord.ButtonStyle.secondary,
        emoji="🔓",
        custom_id="ticket:ctrl:release",
        row=0,
    )
    async def release_claim(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """Staff releases their claim (or an admin force-releases a claim)."""
        await interaction.response.defer(ephemeral=True)
        try:
            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                if not can_claim(interaction.user, self.cfg):
                    await interaction.followup.send(
                        "❌ You do not have permission to release claims.",
                        ephemeral=True,
                    )
                    return

                if not ticket.claimed_by_id:
                    await interaction.followup.send(
                        "❌ This ticket is not currently claimed.",
                        ephemeral=True,
                    )
                    return

                # Only the claimer or an admin may release
                is_admin = any(
                    r.id in self.cfg.role_ids.admin
                    for r in interaction.user.roles
                )
                if (
                    ticket.claimed_by_id != interaction.user.id
                    and not is_admin
                ):
                    await interaction.followup.send(
                        f"❌ This ticket is claimed by <@{ticket.claimed_by_id}>. "
                        "Only an administrator can force-release it.",
                        ephemeral=True,
                    )
                    return

                previous_claimer_id = ticket.claimed_by_id
                ticket.claimed_by_id = None
                ticket.claimed_by_name = None
                ticket.claimed_at = None

                ticket.add_history_event(
                    "claim_released",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    previous_claimer_id=previous_claimer_id,
                    timestamp=datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                )

                await session.flush()

                # Update control embed
                if ticket.control_message_id:
                    try:
                        ctrl_msg = await interaction.channel.fetch_message(
                            ticket.control_message_id
                        )
                        await ctrl_msg.edit(
                            embed=build_control_embed(ticket, self.cfg),
                            view=TicketControlView(self.cfg),
                        )
                    except discord.NotFound:
                        log.warning(
                            "Control message not found for ticket %s",
                            ticket.ticket_code,
                        )

                await session.commit()

            log.info(
                "Claim released on ticket %s by %s",
                ticket.ticket_code,
                interaction.user,
            )
            await interaction.followup.send(
                f"✅ The claim on **{ticket.ticket_code}** has been released.",
                ephemeral=True,
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in release_claim: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in release_claim: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # Button 3 — Staff Notes
    # ------------------------------------------------------------------
    @ui.button(
        label="Staff Notes",
        style=discord.ButtonStyle.secondary,
        emoji="📝",
        custom_id="ticket:ctrl:notes",
        row=0,
    )
    async def staff_notes(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """
        Creates a private staff-only notes thread, or links the existing one.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            if not can_manage(interaction.user, self.cfg):
                await interaction.followup.send(
                    "❌ You do not have permission to access staff notes.",
                    ephemeral=True,
                )
                return

            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                channel: discord.TextChannel = interaction.channel

                # If a notes thread already exists, link it -----------------
                if ticket.notes_thread_id:
                    try:
                        thread = await channel.guild.fetch_channel(
                            ticket.notes_thread_id
                        )
                        await interaction.followup.send(
                            f"📝 Staff notes thread: {thread.mention}",
                            ephemeral=True,
                        )
                        return
                    except discord.NotFound:
                        log.warning(
                            "Notes thread %s not found; creating a new one.",
                            ticket.notes_thread_id,
                        )
                        ticket.notes_thread_id = None

                # Create a new private thread -------------------------------
                thread: discord.Thread = await channel.create_thread(
                    name=f"staff-notes-{ticket.ticket_code}",
                    type=discord.ChannelType.private_thread,
                    reason=f"Staff notes for {ticket.ticket_code}",
                    invitable=False,
                )

                # Add all staff members who currently have access
                staff_role_ids = (
                    self.cfg.role_ids.staff + self.cfg.role_ids.admin
                )
                for member in channel.guild.members:
                    if any(r.id in staff_role_ids for r in member.roles):
                        try:
                            await thread.add_user(member)
                        except discord.HTTPException:
                            pass  # Non-fatal — best effort

                ticket.notes_thread_id = thread.id
                ticket.add_history_event(
                    "notes_thread_created",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    thread_id=thread.id,
                    timestamp=datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                )

                await session.commit()

            log.info(
                "Staff notes thread created for ticket %s by %s (thread_id=%s)",
                ticket.ticket_code,
                interaction.user,
                thread.id,
            )
            await interaction.followup.send(
                f"📝 Staff notes thread created: {thread.mention}",
                ephemeral=True,
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in staff_notes: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in staff_notes: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # Button 4 — Request Close
    # ------------------------------------------------------------------
    @ui.button(
        label="Request Close",
        style=discord.ButtonStyle.danger,
        emoji="🔒",
        custom_id="ticket:ctrl:close_modal",
        row=0,
    )
    async def request_close(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """Opens the close-request modal after verifying permissions and state."""
        try:
            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.response.send_message(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                if not can_close_request(interaction.user, self.cfg, ticket):
                    await interaction.response.send_message(
                        "❌ You do not have permission to request closure of this ticket.",
                        ephemeral=True,
                    )
                    return

                if ticket.status != "open":
                    await interaction.response.send_message(
                        "❌ This ticket is already pending closure or has been closed.",
                        ephemeral=True,
                    )
                    return

                ticket_id: int = ticket.id

            # Send the modal — must NOT defer before send_modal
            await interaction.response.send_modal(
                TicketCloseRequestModal(ticket_id=ticket_id, cfg=self.cfg)
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in request_close: %s", exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ A Discord error occurred. Please try again.",
                    ephemeral=True,
                )
        except Exception as exc:
            log.exception("Unexpected error in request_close: %s", exc)
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "❌ An unexpected error occurred.", ephemeral=True
                )


# ===========================================================================
# 6. TicketClosePendingView
# ===========================================================================

class TicketClosePendingView(ui.View):
    """
    Persistent view attached to the close-pending message inside a ticket
    channel.

    Buttons
    -------
    * ↩️ Cancel Closure  — reverts the ticket back to "open".
    * ⚡ Close Now       — fast-tracks closure (sets close_at to now).
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    # ------------------------------------------------------------------
    # Internal helper: fetch ticket by channel_id
    # ------------------------------------------------------------------
    @staticmethod
    async def _fetch_ticket(
        session, channel_id: int
    ) -> Optional[DiscordTicket]:
        result = await session.execute(
            select(DiscordTicket).where(
                DiscordTicket.channel_id == channel_id
            )
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Button 1 — Cancel Closure
    # ------------------------------------------------------------------
    @ui.button(
        label="Cancel Closure",
        style=discord.ButtonStyle.secondary,
        emoji="↩️",
        custom_id="ticket:close:cancel",
        row=0,
    )
    async def cancel_closure(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """Reverts the ticket status from close_pending back to open."""
        await interaction.response.defer(ephemeral=True)
        try:
            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                if not can_close_cancel(interaction.user, self.cfg, ticket):
                    await interaction.followup.send(
                        "❌ You do not have permission to cancel closure of this ticket.",
                        ephemeral=True,
                    )
                    return

                if ticket.status != "close_pending":
                    await interaction.followup.send(
                        "❌ This ticket is not currently pending closure.",
                        ephemeral=True,
                    )
                    return

                # Revert to open -------------------------------------------
                ticket.status = "open"
                ticket.close_at = None
                ticket.close_delay_key = None
                ticket.close_delay_seconds = None
                ticket.close_reason = None
                ticket.close_requested_by_id = None
                ticket.close_requested_by_name = None

                ticket.add_history_event(
                    "close_cancelled",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    timestamp=datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                )

                await session.flush()

                channel: discord.TextChannel = interaction.channel

                # Edit control embed back to open state --------------------
                if ticket.control_message_id:
                    try:
                        ctrl_msg = await channel.fetch_message(
                            ticket.control_message_id
                        )
                        await ctrl_msg.edit(
                            embed=build_control_embed(ticket, self.cfg),
                            view=TicketControlView(self.cfg),
                        )
                    except discord.NotFound:
                        log.warning(
                            "Control message not found for ticket %s",
                            ticket.ticket_code,
                        )

                # Delete or disable the close-pending message --------------
                if ticket.close_message_id:
                    try:
                        close_msg = await channel.fetch_message(
                            ticket.close_message_id
                        )
                        await close_msg.delete()
                    except discord.NotFound:
                        pass
                    ticket.close_message_id = None

                await session.commit()

            log.info(
                "Closure cancelled for ticket %s by %s",
                ticket.ticket_code,
                interaction.user,
            )
            await interaction.followup.send(
                "✅ Closure cancelled.", ephemeral=True
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in cancel_closure: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in cancel_closure: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred.", ephemeral=True
            )

    # ------------------------------------------------------------------
    # Button 2 — Close Now
    # ------------------------------------------------------------------
    @ui.button(
        label="Close Now",
        style=discord.ButtonStyle.danger,
        emoji="⚡",
        custom_id="ticket:close:now",
        row=0,
    )
    async def close_now(
        self,
        interaction: discord.Interaction,
        button: ui.Button,
    ) -> None:
        """
        Fast-tracks closure by setting ``close_at`` to the current time so
        the background worker picks it up on the next tick.
        """
        await interaction.response.defer(ephemeral=True)
        try:
            async with get_session() as session:
                ticket = await self._fetch_ticket(
                    session, interaction.channel_id
                )
                if ticket is None:
                    await interaction.followup.send(
                        "❌ Ticket record not found.", ephemeral=True
                    )
                    return

                if not can_close_request(interaction.user, self.cfg, ticket):
                    await interaction.followup.send(
                        "❌ You do not have permission to close this ticket.",
                        ephemeral=True,
                    )
                    return

                if ticket.status == "closed":
                    await interaction.followup.send(
                        "❌ This ticket is already closed.", ephemeral=True
                    )
                    return

                now = datetime.datetime.now(datetime.timezone.utc)
                # Set close_at to now — the worker will process it immediately
                ticket.close_at = now
                # Ensure the ticket is in close_pending so the worker acts on it
                if ticket.status != "close_pending":
                    ticket.status = "close_pending"

                ticket.add_history_event(
                    "close_now_requested",
                    actor_id=interaction.user.id,
                    actor_name=str(interaction.user),
                    timestamp=now.isoformat(),
                )

                await session.commit()

            log.info(
                "Close-now requested for ticket %s by %s",
                ticket.ticket_code,
                interaction.user,
            )
            await interaction.followup.send(
                "⏳ Ticket will be closed momentarily.", ephemeral=True
            )

        except discord.DiscordException as exc:
            log.exception("Discord error in close_now: %s", exc)
            await interaction.followup.send(
                "❌ A Discord error occurred. Please try again.",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Unexpected error in close_now: %s", exc)
            await interaction.followup.send(
                "❌ An unexpected error occurred.", ephemeral=True
            )
