"""
worker.py – Background closure worker.
Runs every 15 seconds; closes any ticket whose close_at has passed.
"""
import asyncio
import datetime
import io
import logging
import pathlib

import discord
from discord.ext import tasks
from sqlalchemy import select

from config import BotConfig, PRIORITY_COLOURS
from database.engine import get_session
from database.models import DiscordTicket
from tickets.transcript import generate_html_transcript, scrape_channel_messages

log = logging.getLogger("darklands.worker")

DATA_DIR = pathlib.Path(__file__).parent.parent.parent / "data"
TRANSCRIPT_DIR = DATA_DIR / "transcripts"
TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


async def _execute_closure(
    bot: discord.Client,
    ticket: DiscordTicket,
    session,
    cfg: BotConfig,
) -> None:
    """
    Perform every step required to fully close a single ticket.

    Steps
    -----
    1. Fetch the Discord channel (may be None if already deleted).
    2. Scrape ticket messages and staff-notes thread messages.
    3. Generate and save an HTML transcript to disk.
    4. Build a rich closure embed.
    5. Post the transcript to the transcript-log channel.
    6. Post a summary to the closed-log channel.
    7. DM the ticket creator with the transcript attached.
    8. Update all relevant DB fields and mark the ticket as closed.
    9. Delete the Discord channel.

    Parameters
    ----------
    bot:
        The running discord.Client (or Bot) instance used to make API calls.
    ticket:
        The DiscordTicket ORM object that should be closed. Must already be
        attached to *session*.
    session:
        The active async SQLAlchemy session. Commits are the caller's
        responsibility.
    cfg:
        Loaded BotConfig containing channel IDs and other settings.
    """

    # ------------------------------------------------------------------
    # 1. Fetch the Discord channel
    # ------------------------------------------------------------------
    channel = bot.get_channel(ticket.channel_id)
    if channel is None:
        log.warning(
            "Channel %s for ticket %s not found in cache; "
            "channel-dependent steps will be skipped.",
            ticket.channel_id,
            ticket.ticket_code,
        )

    # ------------------------------------------------------------------
    # 2. Scrape messages from the ticket channel and staff-notes thread
    # ------------------------------------------------------------------
    messages: list = await scrape_channel_messages(channel) if channel else []

    staff_notes: list = []
    if ticket.notes_thread_id:
        thread = bot.get_channel(ticket.notes_thread_id)
        if thread:
            staff_notes = await scrape_channel_messages(thread)

    # ------------------------------------------------------------------
    # 3. Generate HTML transcript and write it to disk
    # ------------------------------------------------------------------
    html: str = generate_html_transcript(ticket, messages, staff_notes, cfg)
    filename: str = f"{ticket.ticket_code}-transcript.html"
    transcript_path: pathlib.Path = TRANSCRIPT_DIR / filename
    transcript_path.write_text(html, encoding="utf-8")

    log.debug("Transcript for %s written to %s", ticket.ticket_code, transcript_path)

    # ------------------------------------------------------------------
    # 4. Build the shared closure embed
    # ------------------------------------------------------------------
    colour: int = PRIORITY_COLOURS.get(ticket.priority, 0x99AAB5)

    close_embed = discord.Embed(
        title=f"🔒 Ticket Closed — {ticket.ticket_code}",
        colour=colour,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    close_embed.add_field(name="Type",       value=ticket.type_name,                          inline=True)
    close_embed.add_field(name="Priority",   value=ticket.priority,                           inline=True)
    close_embed.add_field(name="Creator",    value=f"<@{ticket.creator_id}>",                 inline=True)
    close_embed.add_field(name="Claimed By", value=ticket.claimed_by_name or "Nobody",        inline=True)
    close_embed.add_field(name="Closed By",  value=ticket.closed_by_name or "Auto-close",     inline=True)
    close_embed.add_field(name="Messages",   value=str(ticket.message_count),                 inline=True)
    if ticket.close_reason:
        close_embed.add_field(name="Reason", value=ticket.close_reason, inline=False)
    close_embed.set_footer(text="Darklands SMP Network")

    # ------------------------------------------------------------------
    # 5. Post transcript to the transcript-log channel
    # ------------------------------------------------------------------
    transcript_log_ch = bot.get_channel(cfg.channels.transcript_log_channel_id)
    if transcript_log_ch is not None:
        # A fresh BytesIO is required for each send because discord.py
        # reads and closes the stream internally.
        transcript_file = discord.File(
            io.BytesIO(html.encode("utf-8")),
            filename=filename,
        )
        try:
            await transcript_log_ch.send(embed=close_embed, file=transcript_file)
        except Exception as exc:
            log.warning(
                "Failed to post transcript for %s to transcript-log channel: %s",
                ticket.ticket_code,
                exc,
            )
    else:
        log.warning(
            "Transcript-log channel %s not found; skipping transcript post for %s.",
            cfg.channels.transcript_log_channel_id,
            ticket.ticket_code,
        )

    # ------------------------------------------------------------------
    # 6. Post summary to the closed-log channel
    # ------------------------------------------------------------------
    closed_log_ch = bot.get_channel(cfg.channels.log_channel_closed_id)
    if closed_log_ch is not None:
        try:
            await closed_log_ch.send(embed=close_embed)
        except Exception as exc:
            log.warning(
                "Failed to post summary for %s to closed-log channel: %s",
                ticket.ticket_code,
                exc,
            )
    else:
        log.warning(
            "Closed-log channel %s not found; skipping summary post for %s.",
            cfg.channels.log_channel_closed_id,
            ticket.ticket_code,
        )

    # ------------------------------------------------------------------
    # 7. DM the ticket creator with the transcript attached
    # ------------------------------------------------------------------
    try:
        user = await bot.fetch_user(ticket.creator_id)
        dm_embed = discord.Embed(
            title=f"Your ticket {ticket.ticket_code} has been closed",
            description=(
                f"**Server:** Darklands SMP Network\n"
                f"**Type:** {ticket.type_name}\n"
                f"**Reason:** {ticket.close_reason or 'No reason provided'}\n\n"
                f"A full transcript is attached."
            ),
            colour=colour,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        dm_embed.set_footer(text="Darklands SMP Network")

        transcript_bytes = io.BytesIO(html.encode("utf-8"))
        await user.send(
            embed=dm_embed,
            file=discord.File(transcript_bytes, filename=filename),
        )
    except discord.Forbidden:
        log.info(
            "Cannot DM user %s (DMs disabled or bot blocked).",
            ticket.creator_id,
        )
    except Exception as exc:
        log.warning("Failed to DM user %s: %s", ticket.creator_id, exc)

    # ------------------------------------------------------------------
    # 8. Update the DB record
    # ------------------------------------------------------------------
    now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)

    ticket.status = "closed"
    ticket.closed_at = now
    ticket.message_count = len(messages)
    ticket.messages = messages
    ticket.staff_notes_messages = staff_notes
    ticket.transcript_saved = True
    ticket.add_history_event(
        "closed",
        ticket.close_requested_by_id or 0,
        ticket.close_requested_by_name or "System",
        "Auto-closed by worker",
    )

    log.info("DB record updated for ticket %s.", ticket.ticket_code)

    # ------------------------------------------------------------------
    # 9. Delete the Discord channel
    # ------------------------------------------------------------------
    if channel is not None:
        try:
            await channel.delete(reason=f"Ticket {ticket.ticket_code} closed.")
            log.info("Channel %s deleted for ticket %s.", channel.id, ticket.ticket_code)
        except Exception as exc:
            log.warning(
                "Failed to delete channel for %s: %s",
                ticket.ticket_code,
                exc,
            )


# ---------------------------------------------------------------------------
# Public worker class
# ---------------------------------------------------------------------------


class ClosureWorker:
    """
    Schedules and runs the background ticket-closure loop.

    The worker polls the database every 15 seconds for any ticket with
    ``status == "close_pending"`` whose ``close_at`` timestamp is in the
    past, then closes each such ticket via :func:`_execute_closure`.

    Usage
    -----
    .. code-block:: python

        worker = ClosureWorker(bot, cfg)
        worker.start()   # begin the loop (call after bot is ready)
        # …
        worker.stop()    # gracefully stop the loop on shutdown
    """

    def __init__(self, bot: discord.Client, cfg: BotConfig) -> None:
        """
        Parameters
        ----------
        bot:
            The running discord.Client (or Bot) instance.
        cfg:
            Loaded application configuration (BotConfig).
        """
        self.bot: discord.Client = bot
        self.cfg: BotConfig = cfg
        # Kept for potential future use (e.g. manual cancellation).
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Public control methods
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the :py:meth:`_loop` task."""
        self._loop.start()

    def stop(self) -> None:
        """Stop the :py:meth:`_loop` task gracefully."""
        self._loop.stop()

    # ------------------------------------------------------------------
    # The main loop
    # ------------------------------------------------------------------

    @tasks.loop(seconds=15)
    async def _loop(self) -> None:
        """
        Query for expired ``close_pending`` tickets and close them one by one.

        A separate database session is used per ticket so that a failure on
        one ticket does not roll back mutations from previously processed
        tickets.
        """
        now: datetime.datetime = datetime.datetime.now(datetime.timezone.utc)

        try:
            # First pass: collect IDs of tickets that need closing.
            async with get_session() as session:
                result = await session.execute(
                    select(DiscordTicket).where(
                        DiscordTicket.status == "close_pending",
                        DiscordTicket.close_at <= now,
                    )
                )
                tickets: list[DiscordTicket] = result.scalars().all()

            if not tickets:
                # Nothing to do this cycle.
                return

            log.debug("Worker found %d ticket(s) to close.", len(tickets))

            # Second pass: close each ticket in its own session.
            for ticket in tickets:
                log.info(
                    "Closing ticket %s (close_at=%s).",
                    ticket.ticket_code,
                    ticket.close_at,
                )
                try:
                    async with get_session() as session:
                        # Re-fetch inside its own session for safe mutation.
                        result = await session.execute(
                            select(DiscordTicket).where(DiscordTicket.id == ticket.id)
                        )
                        t: DiscordTicket = result.scalar_one()
                        await _execute_closure(self.bot, t, session, self.cfg)
                        # Commit is handled by the context manager on clean exit.
                except Exception as exc:
                    log.exception(
                        "Error closing ticket %s: %s",
                        ticket.ticket_code,
                        exc,
                    )

        except Exception as exc:
            # Catch-all so the loop is never silently killed by an unexpected
            # error (e.g. a transient database connection problem).
            log.exception("Worker loop error: %s", exc)

    # ------------------------------------------------------------------
    # Before-loop hook
    # ------------------------------------------------------------------

    @_loop.before_loop
    async def _before_loop(self) -> None:
        """
        Wait until the bot is fully connected before starting the loop.

        This prevents the worker from firing before the internal channel
        cache has been populated, which would cause every ``get_channel``
        call to return ``None``.
        """
        await self.bot.wait_until_ready()
        log.info("Closure worker is ready and will run every 15 seconds.")
