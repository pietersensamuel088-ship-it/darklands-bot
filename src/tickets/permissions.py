"""
permissions.py – Role-based access control for the ticket system.

All check functions return True/False. They can also raise
discord.app_commands.CheckFailure with a user-friendly message.

Permission levels (configured in BotConfig.permissions):
    - "everyone"        – Any guild member may perform the action.
    - "roles"           – Only members with specific role IDs may act.
    - "owner_and_staff" – The ticket creator or any staff/admin member.
    - "staff_and_admin" – Any member that holds a staff or admin role.
    - "admin_only"      – Only members with an admin role.
"""

from __future__ import annotations

import discord

from config import BotConfig


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _has_role(member: discord.Member, role_ids: list[int]) -> bool:
    """Return True if *member* holds at least one of the given role IDs.

    Args:
        member:   The guild member whose roles are inspected.
        role_ids: An iterable of Discord role ID integers to check against.

    Returns:
        ``True`` when the intersection of the member's role IDs and the
        supplied list is non-empty; ``False`` otherwise.
    """
    member_role_ids = {r.id for r in member.roles}
    return bool(member_role_ids & set(role_ids))


# ---------------------------------------------------------------------------
# Basic role predicates
# ---------------------------------------------------------------------------


def is_staff(member: discord.Member, cfg: BotConfig) -> bool:
    """Return True if *member* holds any staff **or** admin role.

    Args:
        member: The guild member to evaluate.
        cfg:    The bot configuration instance providing role ID lists.

    Returns:
        ``True`` when the member qualifies as staff or admin.
    """
    return _has_role(member, cfg.role_ids.staff + cfg.role_ids.admin)


def is_admin(member: discord.Member, cfg: BotConfig) -> bool:
    """Return True if *member* holds any admin role.

    Args:
        member: The guild member to evaluate.
        cfg:    The bot configuration instance providing role ID lists.

    Returns:
        ``True`` when the member qualifies as admin.
    """
    return _has_role(member, cfg.role_ids.admin)


# ---------------------------------------------------------------------------
# Action-level permission checks
# ---------------------------------------------------------------------------


def can_create_ticket(member: discord.Member, cfg: BotConfig) -> bool:
    """Check whether *member* is permitted to open a new ticket.

    Reads ``cfg.permissions.create``:
        - ``"everyone"`` – all guild members may create tickets.
        - ``"roles"``    – only members with a role in
                           ``cfg.permissions.create_role_ids`` may create.

    Args:
        member: The guild member requesting ticket creation.
        cfg:    The bot configuration instance.

    Returns:
        ``True`` when the member is allowed to create a ticket.
    """
    perm = cfg.permissions.create
    if perm == "everyone":
        return True
    if perm == "roles":
        return _has_role(member, cfg.permissions.create_role_ids)
    return False


def can_claim(member: discord.Member, cfg: BotConfig) -> bool:
    """Check whether *member* is permitted to claim an open ticket.

    Reads ``cfg.permissions.claim``:
        - ``"staff_and_admin"`` – staff or admin may claim.
        - ``"admin_only"``      – only admins may claim.

    Args:
        member: The guild member attempting to claim a ticket.
        cfg:    The bot configuration instance.

    Returns:
        ``True`` when the member is allowed to claim tickets.
    """
    perm = cfg.permissions.claim
    if perm == "staff_and_admin":
        return is_staff(member, cfg)
    if perm == "admin_only":
        return is_admin(member, cfg)
    return False


def can_manage(member: discord.Member, cfg: BotConfig) -> bool:
    """Check whether *member* can perform general ticket management actions.

    Management covers operations such as adding/removing participants,
    renaming the channel, or changing priority.

    Reads ``cfg.permissions.manage``:
        - ``"staff_and_admin"`` – staff or admin may manage.
        - ``"admin_only"``      – only admins may manage.

    Args:
        member: The guild member attempting a management action.
        cfg:    The bot configuration instance.

    Returns:
        ``True`` when the member is allowed to manage tickets.
    """
    perm = cfg.permissions.manage
    if perm == "staff_and_admin":
        return is_staff(member, cfg)
    if perm == "admin_only":
        return is_admin(member, cfg)
    return False


def can_close_request(
    member: discord.Member, cfg: BotConfig, ticket
) -> bool:
    """Check whether *member* may **request** that a ticket be closed.

    A close-request initiates a confirmation flow; the actual closure is
    subject to ``can_manage`` or ``can_close_cancel``.

    Reads ``cfg.permissions.close_request``:
        - ``"owner_and_staff"`` – the ticket creator, staff, or admin.
        - ``"staff_and_admin"`` – staff or admin only.
        - ``"admin_only"``      – admin only.

    Args:
        member: The guild member requesting closure.
        cfg:    The bot configuration instance.
        ticket: A ``DiscordTicket`` ORM instance; ``ticket.creator_id``
                is compared against ``member.id``.

    Returns:
        ``True`` when the member is allowed to request ticket closure.
    """
    perm = cfg.permissions.close_request
    if perm == "owner_and_staff":
        return member.id == ticket.creator_id or is_staff(member, cfg)
    if perm == "staff_and_admin":
        return is_staff(member, cfg)
    if perm == "admin_only":
        return is_admin(member, cfg)
    return False


def can_close_cancel(
    member: discord.Member, cfg: BotConfig, ticket
) -> bool:
    """Check whether *member* may **cancel** a pending close-request.

    Reads ``cfg.permissions.close_cancel``:
        - ``"owner_and_staff"`` – the ticket creator, staff, or admin.
        - ``"staff_and_admin"`` – staff or admin only.
        - ``"admin_only"``      – admin only.

    Args:
        member: The guild member attempting to cancel the close request.
        cfg:    The bot configuration instance.
        ticket: A ``DiscordTicket`` ORM instance; ``ticket.creator_id``
                is compared against ``member.id``.

    Returns:
        ``True`` when the member is allowed to cancel the close request.
    """
    perm = cfg.permissions.close_cancel
    if perm == "owner_and_staff":
        return member.id == ticket.creator_id or is_staff(member, cfg)
    if perm == "staff_and_admin":
        return is_staff(member, cfg)
    if perm == "admin_only":
        return is_admin(member, cfg)
    return False


# ---------------------------------------------------------------------------
# Interaction-level guard helpers (async)
# ---------------------------------------------------------------------------


async def require_can_create(
    interaction: discord.Interaction, cfg: BotConfig
) -> bool:
    """Guard helper: ensure the interacting user may create tickets.

    Sends an ephemeral error message and returns ``False`` if the user is
    not a guild member or does not satisfy ``can_create_ticket``.

    Args:
        interaction: The Discord interaction to respond to on failure.
        cfg:         The bot configuration instance.

    Returns:
        ``True`` when the user is permitted to create a ticket, ``False``
        (with an ephemeral error already sent) otherwise.
    """
    if not isinstance(interaction.user, discord.Member):
        # Interaction originated outside a guild (e.g. DM) – deny silently.
        return False

    if not can_create_ticket(interaction.user, cfg):
        await interaction.response.send_message(
            "❌ You don't have permission to open a ticket.",
            ephemeral=True,
        )
        return False

    return True


async def require_can_manage(
    interaction: discord.Interaction, cfg: BotConfig
) -> bool:
    """Guard helper: ensure the interacting user may manage tickets.

    Sends an ephemeral error message and returns ``False`` if the user is
    not a guild member or does not satisfy ``can_manage``.

    Args:
        interaction: The Discord interaction to respond to on failure.
        cfg:         The bot configuration instance.

    Returns:
        ``True`` when the user is permitted to manage tickets, ``False``
        (with an ephemeral error already sent) otherwise.
    """
    if not isinstance(interaction.user, discord.Member):
        return False

    if not can_manage(interaction.user, cfg):
        await interaction.response.send_message(
            "❌ Only staff can perform this action.",
            ephemeral=True,
        )
        return False

    return True


async def require_can_claim(
    interaction: discord.Interaction, cfg: BotConfig
) -> bool:
    """Guard helper: ensure the interacting user may claim tickets.

    Sends an ephemeral error message and returns ``False`` if the user is
    not a guild member or does not satisfy ``can_claim``.

    Args:
        interaction: The Discord interaction to respond to on failure.
        cfg:         The bot configuration instance.

    Returns:
        ``True`` when the user is permitted to claim a ticket, ``False``
        (with an ephemeral error already sent) otherwise.
    """
    if not isinstance(interaction.user, discord.Member):
        return False

    if not can_claim(interaction.user, cfg):
        await interaction.response.send_message(
            "❌ Only staff can claim tickets.",
            ephemeral=True,
        )
        return False

    return True
