"""
tickets/commands.py
===================
Implements the `/ticket` slash-command group for the Darklands SMP ticket bot.

Each command is a member of :class:`TicketCommands`, which is registered as an
``app_commands.Group``.  All commands respond ephemerally by default and
perform the relevant permission checks before mutating any state.
"""

import datetime
import logging

import discord
from discord import app_commands
from sqlalchemy import select

from config import BotConfig, CLOSE_DELAYS, VALID_PRIORITIES
from database.engine import get_session
from database.models import DiscordTicket
from tickets.permissions import (
    can_claim,
    can_manage,
    can_close_request,
    can_close_cancel,
    is_admin,
)
from tickets.views import (
    TicketPanelView,
    TicketControlView,
    TicketClosePendingView,
    build_control_embed,
)

log = logging.getLogger("darklands.commands")

# ---------------------------------------------------------------------------
# Shared UTC timezone reference
# ---------------------------------------------------------------------------
_UTC = datetime.timezone.utc


# ---------------------------------------------------------------------------
# Slash-command group
# ---------------------------------------------------------------------------


class TicketCommands(app_commands.Group, name="ticket", description="Ticket management commands."):
    """
    The ``/ticket`` slash-command group.

    All sub-commands live here.  The group is added to the bot's command tree
    in the bot's ``setup_hook`` (or equivalent) via::

        bot.tree.add_command(TicketCommands(cfg))
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    # -----------------------------------------------------------------------
    # Private helpers
    # -----------------------------------------------------------------------

    async def _fetch_ticket(
        self,
        interaction: discord.Interaction,
        session,
    ) -> "DiscordTicket | None":
        """
        Fetch a :class:`DiscordTicket` whose ``channel_id`` matches the
        current interaction channel.

        If no ticket is found an ephemeral error is sent to the user and
        ``None`` is returned so the caller can bail out immediately.

        Parameters
        ----------
        interaction:
            The active Discord interaction.
        session:
            An async SQLAlchemy session (already open).

        Returns
        -------
        DiscordTicket | None
            The ticket row, or ``None`` if the channel is not a ticket channel.
        """
        result = await session.execute(
            select(DiscordTicket).where(
                DiscordTicket.channel_id == interaction.channel_id
            )
        )
        ticket = result.scalar_one_or_none()
        if ticket is None:
            await interaction.followup.send(
                "❌ This is not a ticket channel.", ephemeral=True
            )
        return ticket

    # -----------------------------------------------------------------------
    # /ticket panel [channel]
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="panel",
        description="Post the ticket-creation panel (admin only).",
    )
    @app_commands.describe(channel="Channel to post the panel in (defaults to current channel).")
    async def panel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel = None,
    ) -> None:
        """
        Post a :class:`TicketPanelView` embed in *channel* (or the current
        channel if omitted).  Only server administrators may use this command.
        """
        await interaction.response.defer(ephemeral=True)

        if not is_admin(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You must be an administrator to post a ticket panel.",
                ephemeral=True,
            )
            return

        target_channel: discord.TextChannel = channel or interaction.channel

        try:
            await TicketPanelView.send_panel(target_channel, self.cfg)
        except Exception as exc:
            log.exception("Failed to post ticket panel in #%s: %s", target_channel.name, exc)
            await interaction.followup.send(
                f"❌ Failed to post the panel: {exc}", ephemeral=True
            )
            return

        log.info(
            "Ticket panel posted in #%s by %s (%d).",
            target_channel.name,
            interaction.user,
            interaction.user.id,
        )
        await interaction.followup.send("✅ Ticket panel posted.", ephemeral=True)

    # -----------------------------------------------------------------------
    # /ticket claim
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="claim",
        description="Claim this ticket as your own.",
    )
    async def claim(self, interaction: discord.Interaction) -> None:
        """
        Assign the current ticket to the invoking staff member.

        * Fails if the user lacks the claim permission.
        * Fails if the ticket is already claimed (with an informative message).
        * Records a history event and refreshes the control embed on success.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_claim(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to claim tickets.", ephemeral=True
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            # Guard: already claimed?
            if ticket.claimed_by_id is not None:
                if ticket.claimed_by_id == interaction.user.id:
                    await interaction.followup.send(
                        "⚠️ You already claimed this ticket.", ephemeral=True
                    )
                    return
                await interaction.followup.send(
                    f"⚠️ This ticket is already claimed by **{ticket.claimed_by_name}**.",
                    ephemeral=True,
                )
                return

            # Claim it
            ticket.claimed_by_id = interaction.user.id
            ticket.claimed_by_name = str(interaction.user)
            ticket.claimed_at = datetime.datetime.now(_UTC)
            ticket.add_history_event(
                actor=str(interaction.user),
                action="claimed",
            )
            await session.commit()
            await session.refresh(ticket)

        # Refresh control embed outside the session to avoid holding it open
        await self._refresh_control_embed(interaction, ticket)

        log.info("Ticket #%d claimed by %s.", ticket.id, interaction.user)
        await interaction.followup.send("✅ You claimed this ticket.", ephemeral=True)

    # -----------------------------------------------------------------------
    # /ticket claim-release
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="claim-release",
        description="Release your claim on this ticket.",
    )
    async def claim_release(self, interaction: discord.Interaction) -> None:
        """
        Remove the claim assignment from the current ticket.

        Staff members may only release their *own* claim unless they hold the
        admin role, in which case they can release any claim.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_claim(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to manage ticket claims.", ephemeral=True
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            if ticket.claimed_by_id is None:
                await interaction.followup.send(
                    "⚠️ This ticket is not claimed.", ephemeral=True
                )
                return

            if ticket.claimed_by_id != interaction.user.id and not is_admin(
                interaction.user, self.cfg
            ):
                await interaction.followup.send(
                    "❌ You didn't claim this ticket.", ephemeral=True
                )
                return

            previous_claimer = ticket.claimed_by_name

            # Clear claim fields
            ticket.claimed_by_id = None
            ticket.claimed_by_name = None
            ticket.claimed_at = None
            ticket.add_history_event(
                actor=str(interaction.user),
                action="claim_released",
                detail=f"(was claimed by {previous_claimer})",
            )
            await session.commit()
            await session.refresh(ticket)

        await self._refresh_control_embed(interaction, ticket)

        log.info("Claim on ticket #%d released by %s.", ticket.id, interaction.user)
        await interaction.followup.send("✅ Claim released.", ephemeral=True)

    # -----------------------------------------------------------------------
    # /ticket add <member>
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="add",
        description="Add a member to this ticket channel.",
    )
    @app_commands.describe(member="The Discord member to add.")
    async def add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """
        Grant *member* read/write access to the ticket channel and record
        them in the ``added_members`` list stored on the ticket row.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_manage(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to manage ticket members.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            added: list[int] = ticket.added_members or []

            if member.id in added:
                await interaction.followup.send(
                    f"⚠️ {member.mention} is already added to this ticket.",
                    ephemeral=True,
                )
                return

            # Grant channel permission overwrite
            channel: discord.TextChannel = interaction.channel
            await channel.set_permissions(
                member,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )

            # Persist
            ticket.added_members = [*added, member.id]
            ticket.add_history_event(
                actor=str(interaction.user),
                action="member_added",
                detail=f"{member} ({member.id})",
            )
            await session.commit()

        log.info(
            "Member %s added to ticket #%d by %s.",
            member,
            ticket.id,
            interaction.user,
        )
        await interaction.followup.send(
            f"✅ {member.mention} added to the ticket.", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # /ticket remove <member>
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="remove",
        description="Remove a previously added member from this ticket channel.",
    )
    @app_commands.describe(member="The Discord member to remove.")
    async def remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        """
        Revoke *member*'s channel overwrite and remove them from
        ``added_members``.  Only members who were explicitly added via
        ``/ticket add`` can be removed this way.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_manage(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to manage ticket members.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            added: list[int] = ticket.added_members or []

            if member.id not in added:
                await interaction.followup.send(
                    "⚠️ That member was not added to this ticket.",
                    ephemeral=True,
                )
                return

            # Revoke channel permission overwrite
            channel: discord.TextChannel = interaction.channel
            await channel.set_permissions(member, overwrite=None)

            # Persist
            ticket.added_members = [uid for uid in added if uid != member.id]
            ticket.add_history_event(
                actor=str(interaction.user),
                action="member_removed",
                detail=f"{member} ({member.id})",
            )
            await session.commit()

        log.info(
            "Member %s removed from ticket #%d by %s.",
            member,
            ticket.id,
            interaction.user,
        )
        await interaction.followup.send(
            f"✅ {member.mention} removed from the ticket.", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # /ticket set-priority <priority>
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="set-priority",
        description="Change the priority of this ticket.",
    )
    @app_commands.describe(priority="New priority level.")
    @app_commands.choices(
        priority=[
            app_commands.Choice(name="Low", value="Low"),
            app_commands.Choice(name="Medium", value="Medium"),
            app_commands.Choice(name="High", value="High"),
            app_commands.Choice(name="Critical", value="Critical"),
        ]
    )
    async def set_priority(
        self,
        interaction: discord.Interaction,
        priority: str,
    ) -> None:
        """
        Update ``ticket.priority`` and refresh the control embed so the
        colour change is immediately visible in the channel.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_manage(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to change ticket priority.",
                ephemeral=True,
            )
            return

        # Validate against the config-level list as a safety net
        if priority not in VALID_PRIORITIES:
            await interaction.followup.send(
                f"❌ Invalid priority `{priority}`. "
                f"Valid options: {', '.join(VALID_PRIORITIES)}.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            old_priority = ticket.priority
            ticket.priority = priority
            ticket.add_history_event(
                actor=str(interaction.user),
                action="priority_changed",
                detail=f"{old_priority} → {priority}",
            )
            await session.commit()
            await session.refresh(ticket)

        await self._refresh_control_embed(interaction, ticket)

        log.info(
            "Ticket #%d priority changed from %s to %s by %s.",
            ticket.id,
            old_priority,
            priority,
            interaction.user,
        )
        await interaction.followup.send(
            f"✅ Priority set to **{priority}**.", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # /ticket close-request <delay> [reason]
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="close-request",
        description="Schedule this ticket for closure after a delay.",
    )
    @app_commands.describe(
        delay="How long until the ticket closes automatically.",
        reason="Optional reason for closure.",
    )
    @app_commands.choices(
        delay=[
            app_commands.Choice(name="15 minutes", value="15m"),
            app_commands.Choice(name="1 hour", value="1h"),
            app_commands.Choice(name="6 hours", value="6h"),
            app_commands.Choice(name="12 hours", value="12h"),
            app_commands.Choice(name="24 hours", value="24h"),
            app_commands.Choice(name="3 days", value="3d"),
        ]
    )
    async def close_request(
        self,
        interaction: discord.Interaction,
        delay: str,
        reason: str = None,
    ) -> None:
        """
        Mark the ticket as ``close_pending``, compute the closure timestamp,
        send a :class:`TicketClosePendingView` countdown message, and store
        its message ID on the ticket so it can be cleaned up later.
        """
        await interaction.response.defer(ephemeral=True)

        # Validate delay key
        if delay not in CLOSE_DELAYS:
            await interaction.followup.send(
                f"❌ Invalid delay `{delay}`. "
                f"Valid options: {', '.join(CLOSE_DELAYS.keys())}.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            if not can_close_request(interaction.user, self.cfg, ticket):
                await interaction.followup.send(
                    "❌ You do not have permission to request closure of this ticket.",
                    ephemeral=True,
                )
                return

            if ticket.status != "open":
                await interaction.followup.send(
                    "⚠️ Ticket is not open.", ephemeral=True
                )
                return

            # Calculate closure timestamp
            delta: datetime.timedelta = CLOSE_DELAYS[delay]
            close_at = datetime.datetime.now(_UTC) + delta

            ticket.status = "close_pending"
            ticket.close_at = close_at
            ticket.close_reason = reason
            ticket.close_requested_by_id = interaction.user.id
            ticket.close_requested_by_name = str(interaction.user)
            ticket.add_history_event(
                actor=str(interaction.user),
                action="close_requested",
                detail=f"delay={delay}" + (f", reason={reason}" if reason else ""),
            )
            await session.commit()
            await session.refresh(ticket)

        # Refresh control embed first so the status colour updates
        await self._refresh_control_embed(interaction, ticket)

        # Send the close-pending notice with the countdown view
        channel: discord.TextChannel = interaction.channel
        close_view = TicketClosePendingView(self.cfg)
        close_msg = await channel.send(
            embed=_build_close_pending_embed(ticket, delay, reason),
            view=close_view,
        )

        # Store the message ID for later deletion / editing
        async with get_session() as session:
            result = await session.execute(
                select(DiscordTicket).where(
                    DiscordTicket.channel_id == interaction.channel_id
                )
            )
            ticket_row = result.scalar_one_or_none()
            if ticket_row is not None:
                ticket_row.close_message_id = close_msg.id
                await session.commit()

        log.info(
            "Closure of ticket #%d scheduled in %s by %s.",
            ticket.id,
            delay,
            interaction.user,
        )
        await interaction.followup.send(
            f"✅ Closure scheduled in **{delay}**.", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # /ticket close-cancel
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="close-cancel",
        description="Cancel a pending closure for this ticket.",
    )
    async def close_cancel(self, interaction: discord.Interaction) -> None:
        """
        Revert a ``close_pending`` ticket back to ``open``, clear scheduling
        fields, and clean up the countdown message posted by ``close-request``.
        """
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            if not can_close_cancel(interaction.user, self.cfg, ticket):
                await interaction.followup.send(
                    "❌ You do not have permission to cancel the closure of this ticket.",
                    ephemeral=True,
                )
                return

            if ticket.status != "close_pending":
                await interaction.followup.send(
                    "⚠️ There is no pending closure for this ticket.", ephemeral=True
                )
                return

            close_message_id: int | None = ticket.close_message_id

            # Reset status
            ticket.status = "open"
            ticket.close_at = None
            ticket.close_reason = None
            ticket.close_requested_by_id = None
            ticket.close_requested_by_name = None
            ticket.close_message_id = None
            ticket.add_history_event(
                actor=str(interaction.user),
                action="close_cancelled",
            )
            await session.commit()
            await session.refresh(ticket)

        # Attempt to delete the close-pending countdown message
        if close_message_id is not None:
            channel: discord.TextChannel = interaction.channel
            try:
                close_msg = await channel.fetch_message(close_message_id)
                await close_msg.delete()
            except (discord.NotFound, discord.HTTPException) as exc:
                log.warning(
                    "Could not delete close-pending message %d: %s",
                    close_message_id,
                    exc,
                )

        await self._refresh_control_embed(interaction, ticket)

        log.info(
            "Closure of ticket #%d cancelled by %s.",
            ticket.id,
            interaction.user,
        )
        await interaction.followup.send("✅ Closure cancelled.", ephemeral=True)

    # -----------------------------------------------------------------------
    # /ticket close-now
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="close-now",
        description="Close this ticket immediately.",
    )
    async def close_now(self, interaction: discord.Interaction) -> None:
        """
        Force-close the ticket by setting ``close_at`` to *now* and ensuring
        the status is ``close_pending`` so the background closer picks it up
        on its very next tick.
        """
        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            if not can_close_request(interaction.user, self.cfg, ticket):
                await interaction.followup.send(
                    "❌ You do not have permission to close this ticket.",
                    ephemeral=True,
                )
                return

            if ticket.status == "closed":
                await interaction.followup.send(
                    "⚠️ This ticket is already closed.", ephemeral=True
                )
                return

            # Set close_at to right now and ensure status is close_pending so
            # the closer task processes it on the next run.
            ticket.status = "close_pending"
            ticket.close_at = datetime.datetime.now(_UTC)
            ticket.close_requested_by_id = ticket.close_requested_by_id or interaction.user.id
            ticket.close_requested_by_name = ticket.close_requested_by_name or str(interaction.user)
            ticket.add_history_event(
                actor=str(interaction.user),
                action="close_now",
            )
            await session.commit()

        log.info(
            "Immediate closure of ticket #%d requested by %s.",
            ticket.id,
            interaction.user,
        )
        await interaction.followup.send(
            "⏳ Ticket will close momentarily.", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # /ticket notes
    # -----------------------------------------------------------------------

    @app_commands.command(
        name="notes",
        description="Open or jump to the private staff notes thread for this ticket.",
    )
    async def notes(self, interaction: discord.Interaction) -> None:
        """
        Provide staff with a private thread for internal discussion.

        * If a notes thread already exists, sends the user a clickable mention.
        * Otherwise, creates a new **private** thread, adds all configured
          staff roles, and stores the thread ID on the ticket row.

        This mirrors the logic of the *Staff Notes* button in
        :mod:`tickets.views`.
        """
        await interaction.response.defer(ephemeral=True)

        if not can_manage(interaction.user, self.cfg):
            await interaction.followup.send(
                "❌ You do not have permission to access staff notes.",
                ephemeral=True,
            )
            return

        async with get_session() as session:
            ticket = await self._fetch_ticket(interaction, session)
            if ticket is None:
                return

            channel: discord.TextChannel = interaction.channel

            # ------------------------------------------------------------------
            # Case 1: Thread already exists — just surface the mention
            # ------------------------------------------------------------------
            if ticket.notes_thread_id is not None:
                thread = channel.guild.get_channel_or_thread(ticket.notes_thread_id)
                if thread is not None:
                    await interaction.followup.send(
                        f"📝 Staff notes thread: {thread.mention}", ephemeral=True
                    )
                    return
                # Thread ID stored but thread is gone — fall through to recreate
                log.warning(
                    "Notes thread %d for ticket #%d no longer exists; recreating.",
                    ticket.notes_thread_id,
                    ticket.id,
                )

            # ------------------------------------------------------------------
            # Case 2: Create a new private thread
            # ------------------------------------------------------------------
            thread_name = f"notes-ticket-{ticket.ticket_number}"
            try:
                thread: discord.Thread = await channel.create_thread(
                    name=thread_name,
                    type=discord.ChannelType.private_thread,
                    reason=f"Staff notes for ticket #{ticket.ticket_number}",
                    invitable=False,  # only staff with manage_threads can add members
                )
            except discord.HTTPException as exc:
                log.exception(
                    "Failed to create notes thread for ticket #%d: %s",
                    ticket.id,
                    exc,
                )
                await interaction.followup.send(
                    f"❌ Could not create notes thread: {exc}", ephemeral=True
                )
                return

            # Add all configured staff roles to the thread
            guild: discord.Guild = interaction.guild
            for role_id in self.cfg.staff_role_ids:
                role = guild.get_role(role_id)
                if role is None:
                    log.warning("Staff role %d not found in guild.", role_id)
                    continue
                for member in role.members:
                    try:
                        await thread.add_user(member)
                    except discord.HTTPException as exc:
                        log.warning(
                            "Could not add %s to notes thread: %s", member, exc
                        )

            # Persist the thread ID
            ticket.notes_thread_id = thread.id
            ticket.add_history_event(
                actor=str(interaction.user),
                action="notes_thread_created",
                detail=f"thread_id={thread.id}",
            )
            await session.commit()

        log.info(
            "Notes thread %d created for ticket #%d by %s.",
            thread.id,
            ticket.id,
            interaction.user,
        )
        await interaction.followup.send(
            f"📝 Staff notes thread created: {thread.mention}", ephemeral=True
        )

    # -----------------------------------------------------------------------
    # Internal utility — refresh the control embed
    # -----------------------------------------------------------------------

    async def _refresh_control_embed(
        self,
        interaction: discord.Interaction,
        ticket: "DiscordTicket",
    ) -> None:
        """
        Fetch the control message stored on *ticket* and edit it with a freshly
        built embed + a new :class:`TicketControlView`.

        Silently swallows ``NotFound`` / ``HTTPException`` so that a missing
        control message never breaks the parent command.
        """
        if ticket.control_message_id is None:
            return

        channel: discord.TextChannel = interaction.channel
        try:
            control_msg = await channel.fetch_message(ticket.control_message_id)
            new_embed = build_control_embed(ticket, self.cfg)
            new_view = TicketControlView(self.cfg)
            await control_msg.edit(embed=new_embed, view=new_view)
        except (discord.NotFound, discord.HTTPException) as exc:
            log.warning(
                "Could not refresh control embed for ticket #%d: %s",
                ticket.id,
                exc,
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _build_close_pending_embed(
    ticket: "DiscordTicket",
    delay_key: str,
    reason: str | None,
) -> discord.Embed:
    """
    Build a simple informational embed that announces the pending closure.

    Parameters
    ----------
    ticket:
        The ticket being scheduled for closure.
    delay_key:
        Human-readable delay key, e.g. ``"15m"`` or ``"24h"``.
    reason:
        Optional closure reason supplied by staff.

    Returns
    -------
    discord.Embed
        A yellow-coloured embed announcing the scheduled closure.
    """
    embed = discord.Embed(
        title="🕐 Ticket Closure Scheduled",
        colour=discord.Colour.yellow(),
        timestamp=datetime.datetime.now(_UTC),
    )
    embed.add_field(name="Closes in", value=delay_key, inline=True)
    embed.add_field(
        name="Requested by",
        value=ticket.close_requested_by_name or "Unknown",
        inline=True,
    )
    if reason:
        embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text="Use /ticket close-cancel to cancel this closure.")
    return embed
