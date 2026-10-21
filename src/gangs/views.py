"""
views.py – UI components, embeds, modals, and persistent views for Gang Recruitment.
Follows the exact layout from the reference design:
- Header: ⚔ Gang Recruitment — {GangName}
- Posted relative timestamp
- Inline fields: Leader, Playstyle, Members wanted
- Text blocks: About, Requirements, Contact
- Buttons: Apply (green), Contact Leader (grey), View Gang (grey), Recruit (blue)
- Public thread under each post for applications and discussion
"""
from __future__ import annotations

import datetime
import logging
from typing import TYPE_CHECKING, Optional

import discord
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordGangRecruitment

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.gangs")

GANG_COLOUR = 0xC1121F  # Rich criminal syndicate burgundy/red


def build_recruitment_embed(recruitment: DiscordGangRecruitment) -> discord.Embed:
    """Construct the exact embed shown in the reference design."""
    embed = discord.Embed(
        title=f"⚔ Gang Recruitment — {recruitment.gang_name}",
        colour=GANG_COLOUR,
    )

    # Relative timestamp line: "Posted <t:TIMESTAMP:R>"
    if recruitment.created_at:
        ts = int(recruitment.created_at.timestamp())
        embed.description = f"Posted <t:{ts}:R>"

    # Inline fields
    embed.add_field(
        name="Leader",
        value=f"<@{recruitment.leader_id}>",
        inline=True,
    )
    embed.add_field(
        name="Playstyle",
        value=recruitment.playstyle or "PvP and Raiding",
        inline=True,
    )
    embed.add_field(
        name="Members wanted",
        value=str(recruitment.members_wanted or "Open"),
        inline=True,
    )

    # Text blocks
    embed.add_field(
        name="About",
        value=recruitment.about or "No description provided.",
        inline=False,
    )
    embed.add_field(
        name="Requirements",
        value=recruitment.requirements or "• Active daily\n• Good team player",
        inline=False,
    )
    embed.add_field(
        name="Contact",
        value=recruitment.contact or "Discord",
        inline=False,
    )

    # Footer matching: "DarkLands Network • Gang #16 • 18/10/2026 20:38"
    date_str = recruitment.created_at.strftime("%d/%m/%Y %H:%M") if recruitment.created_at else ""
    embed.set_footer(
        text=f"DarkLands Network • Gang #{recruitment.recruitment_number} • {date_str}"
    )

    return embed


class GangRecruitModal(discord.ui.Modal, title="⚔ Post Gang Recruitment"):
    """Modal for gang leaders to submit a new recruitment post."""

    gang_name = discord.ui.TextInput(
        label="Gang Name",
        placeholder="e.g. QuietHollow",
        min_length=2,
        max_length=64,
        required=True,
    )
    playstyle = discord.ui.TextInput(
        label="Playstyle",
        placeholder="e.g. PvP and farming / Raiding & Siphoning",
        min_length=2,
        max_length=64,
        required=True,
    )
    members_wanted = discord.ui.TextInput(
        label="Members Wanted",
        placeholder="e.g. 3 or Open",
        min_length=1,
        max_length=16,
        required=True,
    )
    about = discord.ui.TextInput(
        label="About Your Gang",
        style=discord.TextStyle.paragraph,
        placeholder="Describe your syndicate's vibe, history, and goals...",
        min_length=10,
        max_length=1000,
        required=True,
    )
    requirements = discord.ui.TextInput(
        label="Requirements",
        style=discord.TextStyle.paragraph,
        placeholder="• Active daily\n• Working microphone\n• Ready for syndicate turf wars",
        min_length=5,
        max_length=1000,
        required=True,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel_id = self.cfg.channels.gang_recruitment_channel_id
        if not channel_id:
            await interaction.response.send_message(
                "❌ Gang recruitment channel is not configured.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                f"❌ Channel <#{channel_id}> not found or is not a text channel.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with get_session() as session:
            # Generate next recruitment number
            res = await session.execute(func.max(DiscordGangRecruitment.recruitment_number))
            max_num = res.scalar() or 0
            recruitment_number = max_num + 1

            recruitment = DiscordGangRecruitment(
                recruitment_number=recruitment_number,
                gang_name=self.gang_name.value.strip(),
                leader_id=interaction.user.id,
                leader_name=str(interaction.user),
                playstyle=self.playstyle.value.strip(),
                members_wanted=self.members_wanted.value.strip(),
                about=self.about.value.strip(),
                requirements=self.requirements.value.strip(),
                contact="Discord",
                status="Recruiting",
                channel_id=channel.id,
                created_at=datetime.datetime.now(datetime.timezone.utc),
            )
            session.add(recruitment)
            await session.commit()
            await session.refresh(recruitment)

            # Send embed with interactive buttons
            embed = build_recruitment_embed(recruitment)
            view = GangItemView(self.cfg)
            msg = await channel.send(embed=embed, view=view)

            # Create thread underneath the post for applications & discussion
            thread = None
            try:
                thread = await msg.create_thread(
                    name=f"⚔ {recruitment.gang_name} Applications",
                    auto_archive_duration=1440,
                )
            except Exception as e:
                log.warning("Could not create thread for gang recruitment: %s", e)

            # Update database with message and thread IDs
            recruitment.message_id = msg.id
            if thread:
                recruitment.thread_id = thread.id
            await session.commit()

        await interaction.followup.send(
            f"✅ Your recruitment ad for **{self.gang_name.value}** is live in {channel.mention}!\n"
            f"Thread: {thread.mention if thread else 'Created'}",
            ephemeral=True,
        )


class GangApplicationModal(discord.ui.Modal):
    """Modal for prospective members to apply to a gang."""

    ign = discord.ui.TextInput(
        label="Minecraft In-Game Name",
        placeholder="e.g. ItzFex",
        min_length=2,
        max_length=16,
        required=True,
    )
    role = discord.ui.TextInput(
        label="Role / Speciality",
        placeholder="e.g. PvP, Base Builder, Miner, Printer Manager",
        min_length=2,
        max_length=64,
        required=True,
    )
    why_join = discord.ui.TextInput(
        label="Why do you want to join?",
        style=discord.TextStyle.paragraph,
        placeholder="Tell the leader why you'd be a great addition to their syndicate...",
        min_length=10,
        max_length=500,
        required=True,
    )
    experience = discord.ui.TextInput(
        label="Activity & Experience",
        style=discord.TextStyle.paragraph,
        placeholder="Hours per day, previous servers/gangs, timezone...",
        min_length=5,
        max_length=500,
        required=True,
    )

    def __init__(self, gang_name: str, leader_id: int, thread_id: Optional[int]) -> None:
        super().__init__(title=f"Apply to {gang_name[:35]}")
        self.gang_name = gang_name
        self.leader_id = leader_id
        self.thread_id = thread_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        app_embed = discord.Embed(
            title=f"📩 New Gang Application — {self.gang_name}",
            colour=0x57F287,
            timestamp=discord.utils.utcnow(),
        )
        app_embed.add_field(name="Applicant", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=True)
        app_embed.add_field(name="Minecraft IGN", value=self.ign.value, inline=True)
        app_embed.add_field(name="Role / Speciality", value=self.role.value, inline=True)
        app_embed.add_field(name="Why Join", value=self.why_join.value, inline=False)
        app_embed.add_field(name="Experience & Activity", value=self.experience.value, inline=False)
        app_embed.set_footer(text="DarkLands Syndicate Recruitment • Review with /gang invite in-game")

        sent_to_thread = False
        if self.thread_id:
            thread = interaction.guild.get_thread(self.thread_id)
            if thread:
                try:
                    await thread.send(
                        content=f"🔔 <@{self.leader_id}> — New application from {interaction.user.mention}!",
                        embed=app_embed,
                    )
                    sent_to_thread = True
                except Exception as e:
                    log.warning("Could not send application to thread: %s", e)

        # Also DM leader as backup
        try:
            leader = interaction.guild.get_member(self.leader_id)
            if leader and not leader.bot:
                await leader.send(
                    content=f"🔔 New applicant for your gang **{self.gang_name}**!",
                    embed=app_embed,
                )
        except Exception:
            pass

        msg = f"✅ Your application to **{self.gang_name}** has been sent to <@{self.leader_id}>!"
        if sent_to_thread:
            msg += f"\nCheck out the thread: <#{self.thread_id}>"

        await interaction.followup.send(msg, ephemeral=True)


class GangItemView(discord.ui.View):
    """Persistent view attached to each gang recruitment post.

    Buttons:
      1. Apply (green) -> Opens GangApplicationModal
      2. Contact Leader (grey) -> Ephemeral contact info
      3. View Gang (grey) -> Ephemeral full details
      4. Recruit (blue) -> Opens GangRecruitModal for anyone to post
    """

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    async def _fetch_recruitment(self, message_id: int) -> Optional[DiscordGangRecruitment]:
        async with get_session() as session:
            stmt = select(DiscordGangRecruitment).where(DiscordGangRecruitment.message_id == message_id)
            res = await session.execute(stmt)
            return res.scalar_one_or_none()

    @discord.ui.button(
        label="Apply",
        emoji="✉",
        style=discord.ButtonStyle.success,
        custom_id="gang:apply",
    )
    async def apply_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        rec = await self._fetch_recruitment(interaction.message.id)
        if not rec:
            await interaction.response.send_message("❌ Gang recruitment record not found in database.", ephemeral=True)
            return

        if interaction.user.id == rec.leader_id:
            await interaction.response.send_message("❌ You are the leader of this gang!", ephemeral=True)
            return

        modal = GangApplicationModal(
            gang_name=rec.gang_name,
            leader_id=rec.leader_id,
            thread_id=rec.thread_id,
        )
        await interaction.response.send_modal(modal)

    @discord.ui.button(
        label="Contact Leader",
        emoji="💬",
        style=discord.ButtonStyle.secondary,
        custom_id="gang:contact",
    )
    async def contact_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        rec = await self._fetch_recruitment(interaction.message.id)
        if not rec:
            await interaction.response.send_message("❌ Gang recruitment record not found.", ephemeral=True)
            return

        # Matches exact ephemeral reference: "QuietHollow — contact @ItzFex . | Discord"
        now_str = discord.utils.utcnow().strftime("%H:%M")
        embed = discord.Embed(
            description=f"**{rec.gang_name}** — contact <@{rec.leader_id}> .\n| {rec.contact}",
            colour=0x5865F2,
        )
        embed.set_footer(text=f"DarkLands Network • Today at {now_str}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="View Gang",
        emoji="🔍",
        style=discord.ButtonStyle.secondary,
        custom_id="gang:view",
    )
    async def view_gang_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        rec = await self._fetch_recruitment(interaction.message.id)
        if not rec:
            await interaction.response.send_message("❌ Gang record not found.", ephemeral=True)
            return

        embed = build_recruitment_embed(rec)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(
        label="Recruit",
        emoji="⚔",
        style=discord.ButtonStyle.primary,
        custom_id="gang:recruit",
    )
    async def recruit_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = GangRecruitModal(self.cfg)
        await interaction.response.send_modal(modal)


class GangLaunchView(discord.ui.View):
    """Permanent standalone panel view with a '⚔ Post Recruitment Ad' button."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @discord.ui.button(
        label="Post Recruitment Ad",
        emoji="⚔",
        style=discord.ButtonStyle.primary,
        custom_id="gang:panel_recruit",
    )
    async def post_recruitment_button(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        modal = GangRecruitModal(self.cfg)
        await interaction.response.send_modal(modal)
