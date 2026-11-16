"""
views.py – Persistent button view and modal for promo code redemption.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import ui

from promo.service import redeem_promo_code

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.promo.views")


class PromoRedeemModal(ui.Modal, title="Redeem Promo Code"):
    """Modal for players to input their promo code and Minecraft username."""

    code_input: ui.TextInput = ui.TextInput(
        label="Promo Code",
        placeholder="Enter your promo code, e.g. d3rkl3nds",
        default="d3rkl3nds",
        max_length=32,
        required=True,
    )

    ign_input: ui.TextInput = ui.TextInput(
        label="Minecraft In-Game Name (IGN)",
        placeholder="Your exact Minecraft username (Java or Bedrock)",
        max_length=32,
        required=True,
    )

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__()
        self.cfg = cfg

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        code_val = self.code_input.value.strip()
        ign_val = self.ign_input.value.strip()

        success, message, receipt_embed = await redeem_promo_code(
            bot=interaction.client,
            user=interaction.user,
            code_raw=code_val,
            ign_raw=ign_val,
            cfg=self.cfg,
        )

        if success and receipt_embed:
            await interaction.followup.send(embed=receipt_embed, ephemeral=True)
        else:
            await interaction.followup.send(message, ephemeral=True)


class PromoRedeemLaunchView(ui.View):
    """Persistent view attached to the Promo Code Redemption panel."""

    def __init__(self, cfg: BotConfig) -> None:
        super().__init__(timeout=None)
        self.cfg = cfg

    @ui.button(
        label="Redeem Promo Code",
        style=discord.ButtonStyle.success,
        emoji="🎁",
        custom_id="promo:redeem_button",
    )
    async def redeem_button(
        self, interaction: discord.Interaction, button: ui.Button
    ) -> None:
        modal = PromoRedeemModal(self.cfg)
        await interaction.response.send_modal(modal)
