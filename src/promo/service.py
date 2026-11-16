"""
service.py – Promo code redemption logic, file-based persistence (JSON),
anti-abuse validation, owner DM notifications, and receipt generation.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import pathlib
from typing import TYPE_CHECKING, Any, Optional

import discord

if TYPE_CHECKING:
    from config import BotConfig

log = logging.getLogger("darklands.promo.service")

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "data"
PROMO_CODES_FILE = DATA_DIR / "promo_codes.json"
PROMO_CLAIMS_FILE = DATA_DIR / "promo_claims.json"

DEFAULT_PROMO_CODES = {
    "D3RKL3NDS": {
        "reward": "$20,000 In-Game Cash",
        "command": "eco give {ign} 20000",
        "description": "Special welcome promo reward granting $20,000 in-game currency on Darklands SMP.",
        "enabled": True,
    },
    "ASCENSION": {
        "reward": "$20,000 In-Game Cash",
        "command": "eco give {ign} 20000",
        "description": "Ascension SMP partner promo code granting $20,000 in-game currency.",
        "enabled": True,
    },
    "START": {
        "reward": "Starter Kit & $5,000",
        "command": "kit give starter {ign}",
        "description": "Welcome starter bundle.",
        "enabled": True,
    },
}


def load_promo_codes() -> dict[str, dict[str, Any]]:
    """Load promo codes from data/promo_codes.json with default fallback."""
    if not PROMO_CODES_FILE.exists():
        save_promo_codes(DEFAULT_PROMO_CODES)
        return dict(DEFAULT_PROMO_CODES)
    try:
        with PROMO_CODES_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return {k.upper(): v for k, v in data.items()}
    except Exception as exc:
        log.error("Error loading promo_codes.json: %s", exc)
        return dict(DEFAULT_PROMO_CODES)


def save_promo_codes(codes: dict[str, dict[str, Any]]) -> None:
    """Save promo codes dictionary to data/promo_codes.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with PROMO_CODES_FILE.open("w", encoding="utf-8") as f:
            json.dump(codes, f, indent=2)
    except Exception as exc:
        log.error("Error saving promo_codes.json: %s", exc)


def load_promo_claims() -> list[dict[str, Any]]:
    """Load promo claims history from data/promo_claims.json."""
    if not PROMO_CLAIMS_FILE.exists():
        save_promo_claims([])
        return []
    try:
        with PROMO_CLAIMS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.error("Error loading promo_claims.json: %s", exc)
        return []


def save_promo_claims(claims: list[dict[str, Any]]) -> None:
    """Save claims history list to data/promo_claims.json."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with PROMO_CLAIMS_FILE.open("w", encoding="utf-8") as f:
            json.dump(claims, f, indent=2)
    except Exception as exc:
        log.error("Error saving promo_claims.json: %s", exc)


async def redeem_promo_code(
    bot: discord.Client,
    user: discord.User | discord.Member,
    code_raw: str,
    ign_raw: str,
    cfg: BotConfig,
) -> tuple[bool, str, Optional[discord.Embed]]:
    """Process a promo code claim with strict anti-abuse and owner notifications.

    Returns:
        tuple(success: bool, message: str, receipt_embed: Optional[discord.Embed])
    """
    code = code_raw.strip().upper()
    ign = ign_raw.strip()

    if not code:
        return False, "❌ Please enter a promo code.", None
    if not ign:
        return False, "❌ Please enter your Minecraft in-game name (IGN).", None

    promo_codes = load_promo_codes()
    if code not in promo_codes or not promo_codes[code].get("enabled", True):
        return (
            False,
            f"❌ **Invalid Promo Code:** `{code_raw}` does not exist, has expired, or is currently disabled. "
            "Please check your spelling and try again!",
            None,
        )

    promo = promo_codes[code]
    claims = load_promo_claims()
    ign_norm = ign.lower()

    # 1. Anti-Abuse: One claim per Discord account per code
    for claim in claims:
        if claim.get("code") == code and claim.get("user_id") == user.id:
            return (
                False,
                f"❌ **Already Redeemed:** You have already redeemed the code `{code}` with this Discord account. "
                "Each code can only be claimed once per user.",
                None,
            )

    # 2. Anti-Abuse: One claim per Minecraft IGN per code
    for claim in claims:
        if claim.get("code") == code and str(claim.get("ign", "")).lower() == ign_norm:
            return (
                False,
                f"❌ **Already Redeemed:** The Minecraft account **`{ign}`** has already claimed the reward for code `{code}`. "
                "Rewards cannot be claimed multiple times for the same in-game account.",
                None,
            )

    reward_text = promo.get("reward", "Promo Reward")
    cmd_template = promo.get("command", "eco give {ign} 20000")
    formatted_cmd = cmd_template.replace("{ign}", ign).replace("{username}", ign)

    now = datetime.datetime.now(datetime.timezone.utc)
    now_unix = int(now.timestamp())
    claim_id = f"CLM-{len(claims) + 1:04d}"

    claim_record = {
        "claim_id": claim_id,
        "user_id": user.id,
        "user_tag": str(user),
        "ign": ign,
        "code": code,
        "reward": reward_text,
        "command": formatted_cmd,
        "claimed_at": now.isoformat(),
        "claimed_unix": now_unix,
        "status": "Queued",
    }
    claims.append(claim_record)
    save_promo_claims(claims)

    log.info(
        "Promo code %s claimed by %s (%d) for IGN %s: %s",
        code,
        user,
        user.id,
        ign,
        reward_text,
    )

    # 3. Automatic DM Notification to Owner
    await _notify_owner(bot, user, ign, code, reward_text, formatted_cmd, now_unix, cfg)

    # 4. Build Player Confirmation Receipt Embed
    receipt_embed = discord.Embed(
        title="🎉 Promo Code Successfully Redeemed!",
        description=f"Thank you for supporting **Darklands SMP**! Your promo code **`{code}`** has been verified.",
        colour=0x57F287,
        timestamp=now,
    )
    receipt_embed.add_field(name="🎮 Credited IGN", value=f"**`{ign}`**", inline=True)
    receipt_embed.add_field(name="🏷️ Code Used", value=f"**`{code}`**", inline=True)
    receipt_embed.add_field(name="🎁 Reward Earned", value=f"**{reward_text}**", inline=False)
    receipt_embed.add_field(
        name="📋 Delivery Status",
        value="✅ **Queued & Server Administration Notified**\n*The console command has been sent to the server owner.*",
        inline=False,
    )
    receipt_embed.set_footer(text=f"Darklands SMP • Receipt #{claim_id}")

    return True, "✅ Promo code successfully redeemed!", receipt_embed


async def _notify_owner(
    bot: discord.Client,
    user: discord.User | discord.Member,
    ign: str,
    code: str,
    reward: str,
    command: str,
    now_unix: int,
    cfg: BotConfig,
) -> None:
    """Send an immediate private DM embed to the server owner with 1-click console command."""
    owner_id_raw = os.getenv("OWNER_ID") or cfg.channels.owner_user_id or 1162838607220965377
    try:
        owner_id = int(owner_id_raw)
    except (TypeError, ValueError):
        owner_id = 1162838607220965377

    clean_cmd = f"/{command.lstrip('/')}"

    embed = discord.Embed(
        title="🎁 New Promo Code Redemption!",
        description="A player has just redeemed an in-game promo code on the Discord server.",
        colour=0x57F287,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(
        name="👤 Discord User",
        value=f"{user.mention} (`{user}` • `{user.id}`)",
        inline=False,
    )
    embed.add_field(name="🎮 Minecraft Username (IGN)", value=f"**`{ign}`**", inline=True)
    embed.add_field(name="🏷️ Promo Code Used", value=f"**`{code}`**", inline=True)
    embed.add_field(name="💎 Reward / Gift", value=f"**{reward}**", inline=False)
    embed.add_field(name="🕒 Timestamp", value=f"<t:{now_unix}:F> (<t:{now_unix}:R>)", inline=False)
    embed.add_field(
        name="💻 Minecraft Console Command (1-Click Copy)",
        value=f"```bash\n{clean_cmd}\n```\n*Copy and paste into your Minecraft server console to credit the player!*",
        inline=False,
    )
    embed.set_footer(text="Darklands SMP • Owner Promo Alert")

    try:
        owner = await bot.fetch_user(owner_id)
        if owner:
            await owner.send(embed=embed)
            log.info("Sent promo redemption DM notification to owner %d", owner_id)
        else:
            log.warning("Owner user %d could not be fetched", owner_id)
    except Exception as exc:
        log.warning("Failed to send promo DM to owner %d: %s", owner_id, exc)


def build_promo_panel_embed() -> discord.Embed:
    """Construct the public promo redemption embed for the designated channel."""
    embed = discord.Embed(
        title="🎁 Darklands SMP — Redeem Promo Codes",
        description=(
            "Welcome to the **Darklands SMP Promo Code Center**!\n\n"
            "Did you join from an advertisement, partner server, or special event? You can claim exclusive in-game rewards like cash, starter kits, and gear!\n\n"
            "✨ **Featured Welcome Code:**\n"
            "Use code **`d3rkl3nds`** to claim **$20,000 In-Game Cash**!\n\n"
            "**How to Redeem:**\n"
            "1. Click the **Redeem Promo Code** button below.\n"
            "2. Enter your promo code (e.g. `d3rkl3nds`).\n"
            "3. Enter your exact Minecraft in-game username (Java or Bedrock).\n"
            "4. Submit! The server console command is immediately dispatched to administration.\n\n"
            "⚠️ *Anti-Abuse Rule: Each promo code can only be claimed once per player and Discord account.*"
        ),
        colour=0xFEE75C,
    )
    embed.set_thumbnail(url="https://images-ext-1.discordapp.net/external/something.png" if False else None)
    embed.set_footer(text="Darklands SMP Network • Official Promo System")
    return embed
