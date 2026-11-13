"""
commands.py – Slash commands for Applications under /apply.
Includes:
- /apply panel [channel]: Post the application launcher panel (Admin).
- /apply view <number>: View full application details (Staff).
- /apply list [status]: View list of recent applications (Staff).
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal, Optional

import discord
from discord import app_commands
from sqlalchemy import desc, select

from applications.views import ApplicationLaunchView, build_application_embed
from database.engine import get_session
from database.models import DiscordApplication
from tickets.permissions import is_admin, is_staff

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.applications.commands")


class ApplyCommands(app_commands.Group, name="apply", description="Darklands SMP Application commands."):
    """Group of application commands under /apply."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # /apply panel [channel]
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="panel",
        description="Post the permanent Application launcher panel in a channel.",
    )
    @app_commands.describe(channel="Target channel to post the panel in (defaults to current)")
    async def post_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post application panels.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Target channel must be a standard text channel.", ephemeral=True
            )
            return

        app_list_lines = []
        for app in self.cfg.applications:
            emoji = app.emoji or "📝"
            desc = f" — {app.description}" if app.description else ""
            app_list_lines.append(f"• **{emoji} {app.name}**{desc}")
        apps_desc = "\n".join(app_list_lines) if app_list_lines else "• **🧪 Beta Tester Application** — Apply to test Darklands SMP before public launch!"

        embed = discord.Embed(
            title="Darklands Applications",
            description=(
                "**Darklands Applications: Click below to select an application and start applying**\n\n"
                f"{apps_desc}\n\n"
                "**How it works:**\n"
                "1. Choose your application from the dropdown menu below.\n"
                "2. The bot will send you a **Direct Message** with your interview questions.\n"
                "3. Answer each question directly in DMs at your own pace (type `cancel` to quit anytime).\n"
                "4. Once submitted, our staff team will review your application and update you directly in your DMs!\n\n"
                "⚠️ *Please ensure you have **Allow Direct Messages from Server Members** enabled in Discord Settings!*"
            ),
            colour=0x5865F2,
        )
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else None)
        embed.set_footer(text="Darklands SMP Network • Official Applications")

        view = ApplicationLaunchView(self.cfg)
        await target_channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"✅ Application panel posted successfully in {target_channel.mention}!",
            ephemeral=True,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # /apply view <number>
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="view",
        description="View details of a specific application by number.",
    )
    @app_commands.describe(number="The application number (e.g. 1)")
    async def view_app(self, interaction: discord.Interaction, number: int) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can view applications.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordApplication).where(DiscordApplication.application_number == number)
            res = await session.execute(stmt)
            app = res.scalar_one_or_none()

            if not app:
                await interaction.followup.send(
                    f"❌ Application **#{number}** was not found.", ephemeral=True
                )
                return

            embed = build_application_embed(app, interaction.guild)
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────────────────────────────────────
    # /apply list [status]
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="list",
        description="List recent applications.",
    )
    @app_commands.describe(status="Filter applications by status")
    async def list_apps(
        self,
        interaction: discord.Interaction,
        status: Optional[Literal["Pending", "Accepted", "Denied", "All"]] = "Pending",
    ) -> None:
        if not is_staff(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only staff members can view applications.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            stmt = select(DiscordApplication).order_by(desc(DiscordApplication.created_at)).limit(20)
            if status and status != "All":
                stmt = stmt.where(DiscordApplication.status == status)

            res = await session.execute(stmt)
            apps = res.scalars().all()

            if not apps:
                await interaction.followup.send(
                    f"ℹ️ No applications found with status **{status}**.", ephemeral=True
                )
                return

            embed = discord.Embed(
                title=f"📋 Darklands Applications — {status or 'All'}",
                colour=0x5865F2,
            )

            status_emojis = {
                "Pending": "⏳",
                "Accepted": "✅",
                "Denied": "❌",
                "Cancelled": "⚠️",
            }

            lines = []
            for a in apps:
                emoji = status_emojis.get(a.status, "❓")
                submitted = f"<t:{int(a.created_at.timestamp())}:d>" if a.created_at else "N/A"
                lines.append(
                    f"{emoji} **#{a.application_number:04d}** | <@{a.applicant_id}> (`{a.applicant_name}`) — *{a.status}* ({submitted})"
                )

            embed.description = "\n".join(lines)
            embed.set_footer(text=f"Showing latest {len(apps)} applications")
            await interaction.followup.send(embed=embed, ephemeral=True)
