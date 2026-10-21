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
        super().__init__(command_prefix="!", intents=intents)
        self._worker = None             # Will hold the ClosureWorker instance

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
            BugCommands,
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
        log.info("Persistent views registered.")

        # ── Slash commands ────────────────────────────────────────────────────
        # guild= scopes commands to a specific server for instant propagation.
        # Remove guild= for global commands (takes up to 1 hour to propagate).
        self.tree.add_command(TicketCommands(cfg), guild=GUILD)
        self.tree.add_command(SuggestionCommands(cfg), guild=GUILD)
        self.tree.add_command(QuestionCommands(cfg), guild=GUILD)
        self.tree.add_command(BugCommands(cfg), guild=GUILD)

        synced = await self.tree.sync(guild=GUILD)
        log.info(
            "Synced %d slash command(s) to guild %s.",
            len(synced),
            GUILD_ID,
        )

        # ── Background worker ─────────────────────────────────────────────────
        # ClosureWorker is a discord.ext.tasks loop that polls for tickets
        # whose auto-close deadline has passed and closes them.
        self._worker = ClosureWorker(self, cfg)
        self._worker.start()
        log.info("Closure worker started.")

    async def on_ready(self):
        """Fired once the bot has fully connected and cached guild data."""
        log.info("🟢  Logged in as %s (ID: %s)", self.user, self.user.id)

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
