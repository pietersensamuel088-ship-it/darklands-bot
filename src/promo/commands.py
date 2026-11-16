"""
commands.py – Slash commands for promo code redemption and admin management.
Includes:
- /redeem <code> <minecraft_username>
- /claim <code> <minecraft_username>
- /promocode list
- /promocode claims [code]
- /promocode panel [channel]
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord
from discord import app_commands

from promo.service import (
    build_promo_panel_embed,
    load_promo_claims,
    load_promo_codes,
    redeem_promo_code,
)
from promo.views import PromoRedeemLaunchView
from tickets.permissions import is_admin

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.promo.commands")


class PromoCommands(app_commands.Group, name="promocode", description="Darklands SMP promo code commands."):
    """Group of promo code management commands under /promocode."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    # ──────────────────────────────────────────────────────────────────────────
    # /promocode list (Admin Only)
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="list",
        description="View all active promo codes and claim statistics (Admin only).",
    )
    async def list_codes(self, interaction: discord.Interaction) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can view promo code configurations.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        codes = load_promo_codes()
        claims = load_promo_claims()

        claim_counts: dict[str, int] = {}
        for c in claims:
            k = str(c.get("code", "")).upper()
            claim_counts[k] = claim_counts.get(k, 0) + 1

        embed = discord.Embed(
            title="🏷️ Darklands SMP — Configured Promo Codes",
            description=f"Total Registered Claims: **{len(claims)}**",
            colour=0x5865F2,
        )

        for code, info in codes.items():
            status_emoji = "🟢" if info.get("enabled", True) else "🔴"
            total = claim_counts.get(code.upper(), 0)
            reward = info.get("reward", "No reward defined")
            cmd = info.get("command", "eco give {ign} 20000")
            desc = info.get("description", "")

            embed.add_field(
                name=f"{status_emoji} `{code}` — {reward}",
                value=(
                    f"**Total Claims:** {total}\n"
                    f"**Command:** `/{cmd}`\n"
                    f"*{desc}*" if desc else f"**Total Claims:** {total}\n**Command:** `/{cmd}`"
                ),
                inline=False,
            )

        embed.set_footer(text=f"Showing {len(codes)} promo codes")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────────────────────────────────────
    # /promocode claims [code] (Admin Only)
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="claims",
        description="View recent promo code redemption records (Admin only).",
    )
    @app_commands.describe(code="Filter claims by a specific promo code")
    async def view_claims(
        self, interaction: discord.Interaction, code: Optional[str] = None
    ) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can view promo code claims.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        claims = load_promo_claims()
        if code:
            code_norm = code.strip().upper()
            claims = [c for c in claims if c.get("code") == code_norm]

        if not claims:
            msg = f"ℹ️ No claims found for code `{code}`." if code else "ℹ️ No promo code claims recorded yet."
            await interaction.followup.send(msg, ephemeral=True)
            return

        recent = claims[-15:][::-1]
        lines = []
        for c in recent:
            ts = f"<t:{c.get('claimed_unix', 0)}:R>" if c.get("claimed_unix") else "N/A"
            lines.append(
                f"• `{c.get('code')}` | IGN: **`{c.get('ign')}`** | User: <@{c.get('user_id')}> ({ts})"
            )

        embed = discord.Embed(
            title="📋 Recent Promo Code Claims",
            description="\n".join(lines),
            colour=0x5865F2,
        )
        embed.set_footer(text=f"Showing latest {len(recent)} of {len(claims)} total claims")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ──────────────────────────────────────────────────────────────────────────
    # /promocode panel [channel] (Admin Only)
    # ──────────────────────────────────────────────────────────────────────────
    @app_commands.command(
        name="panel",
        description="Post the interactive Promo Code Redemption panel in a channel (Admin only).",
    )
    @app_commands.describe(channel="Target channel to post the panel in (defaults to current)")
    async def post_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        if not is_admin(interaction.user, self.cfg):
            await interaction.response.send_message(
                "❌ Only administrators can post promo code panels.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Target channel must be a text channel.", ephemeral=True
            )
            return

        embed = build_promo_panel_embed()
        view = PromoRedeemLaunchView(self.cfg)
        await target_channel.send(embed=embed, view=view)
        await interaction.response.send_message(
            f"✅ Promo code redemption panel posted in {target_channel.mention}!",
            ephemeral=True,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Top-Level /redeem and /claim commands
# ──────────────────────────────────────────────────────────────────────────────

@app_commands.command(
    name="redeem",
    description="Redeem an in-game promo code for rewards on Darklands SMP.",
)
@app_commands.describe(
    code="The promo code to redeem (e.g. d3rkl3nds)",
    minecraft_username="Your exact Minecraft username (Java or Bedrock)",
)
async def redeem_slash_command(
    interaction: discord.Interaction,
    code: str,
    minecraft_username: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    from config import load_config
    cfg = load_config()

    success, message, receipt_embed = await redeem_promo_code(
        bot=interaction.client,
        user=interaction.user,
        code_raw=code,
        ign_raw=minecraft_username,
        cfg=cfg,
    )

    if success and receipt_embed:
        await interaction.followup.send(embed=receipt_embed, ephemeral=True)
    else:
        await interaction.followup.send(message, ephemeral=True)


@app_commands.command(
    name="claim",
    description="Claim an in-game promo code for rewards on Darklands SMP (alias for /redeem).",
)
@app_commands.describe(
    code="The promo code to redeem (e.g. d3rkl3nds)",
    minecraft_username="Your exact Minecraft username (Java or Bedrock)",
)
async def claim_slash_command(
    interaction: discord.Interaction,
    code: str,
    minecraft_username: str,
) -> None:
    await interaction.response.defer(ephemeral=True)
    from config import load_config
    cfg = load_config()

    success, message, receipt_embed = await redeem_promo_code(
        bot=interaction.client,
        user=interaction.user,
        code_raw=code,
        ign_raw=minecraft_username,
        cfg=cfg,
    )

    if success and receipt_embed:
        await interaction.followup.send(embed=receipt_embed, ephemeral=True)
    else:
        await interaction.followup.send(message, ephemeral=True)
