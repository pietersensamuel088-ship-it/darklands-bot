"""
bot.py – Darklands SMP Bot entry point.
Initialises the database, registers all persistent views, mounts the /ticket
command group, starts the background closure worker, and logs in.
"""

import asyncio
import logging
import os
import pathlib

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("darklands")

# ── Config ────────────────────────────────────────────────────────────────────
from config import load_config, PRIORITY_COLOURS

cfg = load_config()

TOKEN              = os.getenv("DISCORD_TOKEN")
GUILD_ID           = os.getenv("GUILD_ID")
WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set in your .env file.")
if not GUILD_ID:
    raise RuntimeError("GUILD_ID is not set in your .env file.")

GUILD = discord.Object(id=int(GUILD_ID))


# ── Bot class ─────────────────────────────────────────────────────────────────

class DarklansBot(commands.Bot):
    """Main bot class for Darklands SMP.

    Responsibilities:
    - Initialise the async database on startup.
    - Register all persistent UI views so buttons survive bot restarts.
    - Mount and sync the /ticket slash-command group to the guild.
    - Launch the background ticket-closure worker task.
    - Post welcome embeds when new members join.
    """

    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # Required for on_member_join and member lookups
        intents.message_content = True  # Required for reading application answers in DMs
        super().__init__(command_prefix="!", intents=intents)
        self._worker = None             # Will hold the ClosureWorker instance
        self._status_worker = None      # Will hold the ServerStatusWorker instance

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def setup_hook(self):
        """Called by discord.py once the bot is logged in but before on_ready.

        This is the correct place to:
          • create database tables
          • add persistent views
          • register and sync slash commands
          • start background tasks
        """
        from database.engine import init_db
        from tickets.views import (
            TicketPanelView,
            TicketControlView,
            TicketClosePendingView,
            TicketTypeSelect,
        )
        from tickets.commands import TicketCommands
        from tickets.worker import ClosureWorker
        from suggestions import (
            SuggestionItemView,
            SuggestionLaunchView,
            SuggestionCommands,
        )
        from questions import (
            QuestionItemView,
            QuestionLaunchView,
            QuestionCommands,
        )
        from bugs import (
            BugItemView,
            BugLaunchView,
            BugStaffActionView,
            BugCommands,
        )
        from gangs import (
            GangItemView,
            GangLaunchView,
            GangCommands,
        )
        from applications import (
            ApplicationLaunchView,
            ApplicationReviewView,
            ApplyCommands,
        )

        # ── Database ──────────────────────────────────────────────────────────
        await init_db()
        log.info("Database initialised.")

        # ── Persistent views ──────────────────────────────────────────────────
        # Persistent views must be added here (before on_ready) so that any
        # existing messages with buttons are immediately interactive after restart.
        self.add_view(TicketPanelView(cfg))
        self.add_view(TicketControlView(cfg))
        self.add_view(TicketClosePendingView(cfg))
        self.add_view(SuggestionLaunchView(cfg))
        self.add_view(SuggestionItemView(cfg))
        self.add_view(QuestionLaunchView(cfg))
        self.add_view(QuestionItemView(cfg))
        self.add_view(BugLaunchView(cfg))
        self.add_view(BugItemView(cfg))
        self.add_view(BugStaffActionView(cfg))
        self.add_view(GangLaunchView(cfg))
        self.add_view(GangItemView(cfg))
        self.add_view(ApplicationLaunchView(cfg))
        self.add_view(ApplicationReviewView(cfg))

        from promo import PromoRedeemLaunchView, PromoCommands, redeem_slash_command, claim_slash_command
        self.add_view(PromoRedeemLaunchView(cfg))
        log.info("Persistent views registered.")

        # ── Slash commands ────────────────────────────────────────────────────
        # guild= scopes commands to a specific server for instant propagation.
        # Remove guild= for global commands (takes up to 1 hour to propagate).
        self.tree.add_command(TicketCommands(cfg), guild=GUILD)
        self.tree.add_command(SuggestionCommands(cfg), guild=GUILD)
        self.tree.add_command(QuestionCommands(cfg), guild=GUILD)
        self.tree.add_command(BugCommands(cfg), guild=GUILD)
        self.tree.add_command(GangCommands(cfg), guild=GUILD)
        self.tree.add_command(ApplyCommands(cfg), guild=GUILD)
        self.tree.add_command(PromoCommands(cfg), guild=GUILD)
        self.tree.add_command(redeem_slash_command, guild=GUILD)
        self.tree.add_command(claim_slash_command, guild=GUILD)

        from server_status import ServerStatusCommands, ServerStatusWorker
        self.tree.add_command(ServerStatusCommands(cfg), guild=GUILD)

        synced = await self.tree.sync(guild=GUILD)
        log.info(
            "Synced %d slash command(s) to guild %s.",
            len(synced),
            GUILD_ID,
        )

        # ── Background workers ────────────────────────────────────────────────
        # ClosureWorker is a discord.ext.tasks loop that polls for tickets
        # whose auto-close deadline has passed and closes them.
        self._worker = ClosureWorker(self, cfg)
        self._worker.start()
        log.info("Closure worker started.")

        # ServerStatusWorker tracks live Minecraft status & updates #server-info
        self._status_worker = ServerStatusWorker(self, cfg)
        self._status_worker.start()
        log.info("ServerStatusWorker started.")

    async def on_ready(self):
        """Fired once the bot has fully connected and cached guild data."""
        log.info("🟢  Logged in as %s (ID: %s)", self.user, self.user.id)
        await self._ensure_startup_panels()

    async def _ensure_startup_panels(self):
        """Ensure the Application Panel and Tester Info Guide are posted if not already present."""
        try:
            # 1. Application Panel (1562431588355280896)
            app_chan_id = cfg.channels.application_channel_id
            if app_chan_id:
                channel = self.get_channel(app_chan_id)
                if channel and isinstance(channel, discord.TextChannel):
                    already_posted = False
                    try:
                        async for msg in channel.history(limit=30):
                            if msg.author.id == self.user.id and msg.embeds:
                                if any("Darklands Applications" in (e.title or "") for e in msg.embeds):
                                    already_posted = True
                                    break
                    except Exception as exc:
                        log.warning("Could not read history for application channel: %s", exc)

                    if not already_posted:
                        from applications.views import ApplicationLaunchView
                        embed = discord.Embed(
                            title="Darklands Applications",
                            description=(
                                "**Darklands Applications: Click below to select an application and start applying**\n\n"
                                "• **🧪 Beta Tester Application** — Apply to test Darklands SMP before public launch!\n\n"
                                "**How it works:**\n"
                                "1. Select your application category from the dropdown menu below.\n"
                                "2. The bot will send you a **Direct Message** with your interview questions.\n"
                                "3. Answer each question directly in DMs at your own pace (type `cancel` to quit anytime).\n"
                                "4. Once submitted, our staff team will review your application and update you directly in your DMs!\n\n"
                                "⚠️ *Please ensure you have **Allow Direct Messages from Server Members** enabled in Discord Settings!*"
                            ),
                            colour=0x5865F2,
                        )
                        embed.set_footer(text="Darklands SMP Network • Official Applications")
                        view = ApplicationLaunchView(cfg)
                        await channel.send(embed=embed, view=view)
                        log.info("✅ Auto-posted Application Launch Panel to channel %d", app_chan_id)

            # 2. Beta Tester Info Channel (1570846617366560928)
            info_chan_id = cfg.channels.tester_info_channel_id
            if info_chan_id:
                info_channel = self.get_channel(info_chan_id)
                if info_channel and isinstance(info_channel, discord.TextChannel):
                    already_posted_info = False
                    try:
                        async for msg in info_channel.history(limit=30):
                            if msg.author.id == self.user.id and msg.embeds:
                                if any("Beta Tester Information & Guidelines" in (e.title or "") for e in msg.embeds):
                                    already_posted_info = True
                                    break
                    except Exception as exc:
                        log.warning("Could not read history for tester info channel: %s", exc)

                    if not already_posted_info:
                        bug_ref = f"<#{cfg.channels.bug_report_channel_id}>" if cfg.channels.bug_report_channel_id else "#bug-reports"
                        sug_ref = f"<#{cfg.channels.suggestion_channel_id}>" if cfg.channels.suggestion_channel_id else "#suggestions"
                        lounge_ref = f"<#{cfg.channels.beta_tester_lounge_channel_id}>" if cfg.channels.beta_tester_lounge_channel_id else "#tester-lounge"
                        owner_ref = f"<@{cfg.channels.owner_user_id}>" if cfg.channels.owner_user_id else "Server Owner"

                        info_embed = discord.Embed(
                            title="🧪 Darklands SMP — Beta Tester Information & Guidelines",
                            description=(
                                "Welcome to the **Darklands SMP Beta Tester Team**! As a beta tester, you play a vital role "
                                "in polishing server mechanics, balancing gameplay, testing features, and discovering bugs "
                                "before our public launch.\n\n"
                                "─────────────────────────────\n\n"
                                f"🐛 **Found a Bug or Glitch?**\n"
                                f"• Head over to {bug_ref} and click **Report a Bug** or use `/bug report`.\n"
                                "• Please include clear reproduction steps, coordinates (if in-game), and any screenshots/videos.\n\n"
                                f"💡 **Have an Idea or Feedback?**\n"
                                f"• Post your ideas in {sug_ref} using `/suggestion submit`.\n"
                                "• Other testers and community members can upvote, downvote, and discuss.\n\n"
                                f"🚨 **Critical or Game-Breaking Exploit?**\n"
                                "• If you discover a severe duplication exploit, economy breaking bug, or crash glitch, **DO NOT post it publicly**.\n"
                                f"• Open a private staff ticket in the server or send a direct DM to {owner_ref}.\n\n"
                                f"💬 **Tester Chat & Discussion:**\n"
                                f"• Chat with other testers and staff in {lounge_ref}.\n\n"
                                "Thank you for supporting Darklands SMP and helping us create the best experience possible!"
                            ),
                            colour=0x5865F2,
                            timestamp=discord.utils.utcnow(),
                        )
                        info_embed.set_footer(text="Darklands SMP • Beta Testing Program")
                        await info_channel.send(embed=info_embed)
                        log.info("✅ Auto-posted Beta Tester Info Guide to channel %d", info_chan_id)

            # 3. Promo Code Redemption Channel (1571924571853037754)
            promo_chan_id = cfg.channels.promo_redeem_channel_id
            if promo_chan_id:
                promo_channel = self.get_channel(promo_chan_id)
                if promo_channel and isinstance(promo_channel, discord.TextChannel):
                    already_posted_promo = False
                    try:
                        async for msg in promo_channel.history(limit=30):
                            if msg.author.id == self.user.id and msg.embeds:
                                if any("Redeem Promo Codes" in (e.title or "") for e in msg.embeds):
                                    already_posted_promo = True
                                    break
                    except Exception as exc:
                        log.warning("Could not read history for promo redeem channel: %s", exc)

                    if not already_posted_promo:
                        from promo.service import build_promo_panel_embed
                        from promo.views import PromoRedeemLaunchView

                        embed = build_promo_panel_embed()
                        view = PromoRedeemLaunchView(cfg)
                        await promo_channel.send(embed=embed, view=view)
                        log.info("✅ Auto-posted Promo Code Redemption Panel to channel %d", promo_chan_id)

            # 4. Bug Report Public Channel (1562455946335232120)
            bug_chan_id = cfg.channels.bug_report_channel_id
            if bug_chan_id:
                bug_channel = self.get_channel(bug_chan_id)
                if bug_channel and isinstance(bug_channel, discord.TextChannel):
                    already_posted_bug = False
                    try:
                        async for msg in bug_channel.history(limit=30):
                            if msg.author.id == self.user.id and msg.embeds:
                                if any("Bug & Exploit Reports" in (e.title or "") for e in msg.embeds):
                                    already_posted_bug = True
                                    break
                    except Exception as exc:
                        log.warning("Could not read history for bug report channel: %s", exc)

                    if not already_posted_bug:
                        from bugs.views import BugLaunchView
                        embed = discord.Embed(
                            title="🐛 Darklands SMP — Bug & Exploit Reports",
                            description=(
                                "Found a bug, glitch, duplication issue, or error in-game or on Discord?\n\n"
                                "Click the button below to **Report a Bug**!\n\n"
                                "🔒 **Private Review:** All reports are submitted directly to the developer and staff team for investigation.\n"
                                "• Please include clear reproduction steps and your Minecraft username.\n"
                                "• When staff reviews or fixes your report, you will receive an automatic DM update!"
                            ),
                            colour=0xED4245,
                        )
                        embed.set_footer(text="Darklands SMP Network • Bug Tracker")
                        view = BugLaunchView(cfg)
                        await bug_channel.send(embed=embed, view=view)
                        log.info("✅ Auto-posted Bug Report Panel to channel %d", bug_chan_id)
        except Exception as exc:
            log.exception("Error checking/auto-posting startup panels: %s", exc)

    # ── Events ────────────────────────────────────────────────────────────────

    async def on_member_join(self, member: discord.Member):
        """Post a welcome embed when a new member joins the guild.

        The welcome channel is configured via the WELCOME_CHANNEL_ID environment
        variable.  If the variable is absent or the channel cannot be found the
        event is silently skipped (a warning is emitted to the log).

        Args:
            member: The :class:`discord.Member` who joined.
        """
        if not WELCOME_CHANNEL_ID:
            return

        channel = member.guild.get_channel(int(WELCOME_CHANNEL_ID))
        if channel is None:
            log.warning(
                "Welcome channel %s not found in guild %s.",
                WELCOME_CHANNEL_ID,
                member.guild.id,
            )
            return

        def ordinal(n: int) -> str:
            """Return an English ordinal string for *n* (e.g. 1 → '1st')."""
            suffix = ["th", "st", "nd", "rd"]
            v = n % 100
            # Special-case 11th, 12th, 13th which don't follow the normal rule
            return f"{n}{suffix[v - 20] if 20 <= v <= 23 else suffix[min(v, 3)]}"

        member_count = member.guild.member_count

        embed = (
            discord.Embed(
                title=f"Welcome to Darklands SMP, {member.display_name}! 🌑",
                description=(
                    f"Hey {member.mention}, glad you made it to the network!\n"
                    f"You are our **{ordinal(member_count)} member**. Enjoy your stay."
                ),
                colour=discord.Colour.dark_grey(),
                timestamp=discord.utils.utcnow(),
            )
            .set_thumbnail(url=member.display_avatar.url)
            .set_footer(text="Darklands SMP Network")
        )

        await channel.send(embed=embed)

    async def on_message(self, message: discord.Message):
        """Listen to incoming messages, routing DMs to the application session manager."""
        if message.author.bot:
            return

        # Direct messages routing for Application interviews
        if message.guild is None:
            from applications.session import application_manager
            if application_manager.has_session(message.author.id):
                handled = await application_manager.handle_dm(self, message, cfg)
                if handled:
                    return

        await self.process_commands(message)


# ── Entry point ───────────────────────────────────────────────────────────────

async def main():
    """Async entry point.  Uses the bot as an async context manager so that
    cleanup (cancelling tasks, closing the aiohttp session, etc.) always runs
    even if an exception is raised.
    """
    async with DarklansBot() as bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
