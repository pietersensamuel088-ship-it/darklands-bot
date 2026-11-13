"""
session.py – DM Interview State Machine and Manager for Applications.
Handles asking questions one-by-one via DMs, collecting answers,
and submitting completed applications to the staff review channel.
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import discord
from sqlalchemy import func, select

from database.engine import get_session
from database.models import DiscordApplication

if TYPE_CHECKING:
    from config import ApplicationTypeConfig, BotConfig

log = logging.getLogger("darklands.applications.session")


@dataclass
class ApplicationSession:
    """Represents an ongoing interactive application session in user DMs."""
    user_id: int
    guild_id: int
    app_key: str
    app_name: str
    user_name: str
    avatar_url: Optional[str]
    questions: list[str]
    current_index: int = 0
    answers: list[dict[str, str]] = field(default_factory=list)
    started_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
    )
    expires_at: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=30)
    )

    @property
    def is_expired(self) -> bool:
        return datetime.datetime.now(datetime.timezone.utc) > self.expires_at

    @property
    def total_questions(self) -> int:
        return len(self.questions)


class ApplicationManager:
    """Manages active DM application sessions across users."""

    def __init__(self) -> None:
        self._sessions: dict[int, ApplicationSession] = {}

    def has_session(self, user_id: int) -> bool:
        session = self._sessions.get(user_id)
        if session is None:
            return False
        if session.is_expired:
            self._sessions.pop(user_id, None)
            return False
        return True

    def get_session(self, user_id: int) -> Optional[ApplicationSession]:
        if not self.has_session(user_id):
            return None
        return self._sessions.get(user_id)

    def start_session(
        self,
        user: discord.User | discord.Member,
        guild: discord.Guild,
        app_config: ApplicationTypeConfig,
    ) -> ApplicationSession:
        """Create and store a new active application session."""
        session = ApplicationSession(
            user_id=user.id,
            guild_id=guild.id,
            app_key=app_config.key,
            app_name=app_config.name,
            user_name=str(user),
            avatar_url=user.display_avatar.url if user.display_avatar else None,
            questions=list(app_config.questions),
        )
        self._sessions[user.id] = session
        log.info(
            "Started %s session for user %s (%d)",
            app_config.name,
            user,
            user.id,
        )
        return session

    def cancel_session(self, user_id: int) -> bool:
        """Cancel an in-progress session."""
        return self._sessions.pop(user_id, None) is not None

    async def handle_dm(
        self,
        bot: discord.Client,
        message: discord.Message,
        cfg: BotConfig,
    ) -> bool:
        """Process an incoming DM message from a potential applicant.

        Returns True if the message was handled as part of an active session.
        """
        session = self.get_session(message.author.id)
        if session is None:
            return False

        # Check expiration
        if session.is_expired:
            self.cancel_session(message.author.id)
            embed = discord.Embed(
                title="⏱️ Application Expired",
                description=(
                    "Your application session has expired due to 30 minutes of inactivity.\n"
                    "You can restart it anytime by clicking **Apply** in the Discord server!"
                ),
                colour=0xED4245,
            )
            await message.channel.send(embed=embed)
            return True

        content = message.content.strip()

        # Check for cancel command
        if content.lower() in {"cancel", "quit", "abort", "exit"}:
            self.cancel_session(message.author.id)
            embed = discord.Embed(
                title="❌ Application Cancelled",
                description=(
                    f"Your **{session.app_name}** has been cancelled.\n"
                    "None of your answers were submitted. You can apply again anytime in the server!"
                ),
                colour=0x99AAB5,
            )
            await message.channel.send(embed=embed)
            log.info("User %s (%d) cancelled their application.", message.author, message.author.id)
            return True

        # Process attachments if any
        answer_text = content
        if message.attachments:
            attach_lines = [
                f"[Attachment: {a.filename}]({a.url})" for a in message.attachments
            ]
            attach_str = "\n".join(attach_lines)
            answer_text = f"{answer_text}\n{attach_str}".strip() if answer_text else attach_str

        if not answer_text:
            await message.channel.send(
                "⚠️ Please provide a written answer, send an attachment, or type `cancel` to quit."
            )
            return True

        # Save current answer
        current_q = session.questions[session.current_index]
        session.answers.append({"question": current_q, "answer": answer_text})
        session.current_index += 1
        # Refresh timeout
        session.expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=30)

        # Are there more questions?
        if session.current_index < session.total_questions:
            next_q = session.questions[session.current_index]
            embed = discord.Embed(
                title=f"{session.app_name} • Step {session.current_index + 1}/{session.total_questions}",
                description=f"**{next_q}**\n\n*Type your answer below, or type `cancel` to abort.*",
                colour=0x5865F2,
            )
            embed.set_footer(text=f"Darklands SMP • Question {session.current_index + 1} of {session.total_questions}")
            await message.channel.send(embed=embed)
            return True

        # All questions answered — finalize application!
        self._sessions.pop(message.author.id, None)

        # 1. Inform applicant in DM
        complete_embed = discord.Embed(
            title="✅ Application Submitted!",
            description=(
                f"Thank you, **{message.author.display_name}**! Your **{session.app_name}** has been successfully "
                "submitted to the Darklands SMP staff team.\n\n"
                "• Our staff will review your responses shortly.\n"
                "• You will be notified directly in this DM once a decision has been made.\n\n"
                "Good luck, and thank you for supporting the network!"
            ),
            colour=0x57F287,
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        complete_embed.set_footer(text="Darklands SMP • Applications")
        await message.channel.send(embed=complete_embed)

        # 2. Persist to Database & Post to Staff Review Channel
        await self._submit_to_channel(bot, message.author, session, cfg)
        return True

    async def _submit_to_channel(
        self,
        bot: discord.Client,
        applicant: discord.User | discord.Member,
        session: ApplicationSession,
        cfg: BotConfig,
    ) -> None:
        """Store the application in the database and post the review embed to the staff channel."""
        from applications.views import ApplicationReviewView, build_application_embed

        guild = bot.get_guild(session.guild_id)
        if guild is None:
            log.error("Could not find guild %s for application submission", session.guild_id)
            return

        # Lookup app config
        app_cfg = next(
            (a for a in cfg.applications if a.key == session.app_key),
            None,
        )

        # Determine target review channel
        channel_id = None
        if app_cfg and app_cfg.channel_id:
            channel_id = app_cfg.channel_id
        elif cfg.channels.application_channel_id:
            channel_id = cfg.channels.application_channel_id
        elif cfg.channels.transcript_log_channel_id:
            channel_id = cfg.channels.transcript_log_channel_id

        target_channel: Optional[discord.TextChannel] = None
        if channel_id:
            target_channel = guild.get_channel(channel_id)

        try:
            async with get_session() as db_session:
                # Next application number
                max_num_result = await db_session.execute(
                    select(func.max(DiscordApplication.application_number))
                )
                max_num = max_num_result.scalar_one_or_none()
                app_number = (max_num or 0) + 1

                app_record = DiscordApplication(
                    application_number=app_number,
                    type_key=session.app_key,
                    type_name=session.app_name,
                    applicant_id=applicant.id,
                    applicant_name=str(applicant),
                    applicant_avatar=applicant.display_avatar.url if applicant.display_avatar else None,
                    status="Pending",
                    answers=session.answers,
                )
                db_session.add(app_record)
                await db_session.flush()

                # Post embed to channel if available
                if target_channel:
                    embed = build_application_embed(app_record, guild)
                    view = ApplicationReviewView(cfg)
                    sent_msg = await target_channel.send(embed=embed, view=view)

                    app_record.channel_id = target_channel.id
                    app_record.message_id = sent_msg.id
                    log.info(
                        "Posted Application #%d to channel %s (message %d)",
                        app_number,
                        target_channel.name,
                        sent_msg.id,
                    )
                else:
                    log.warning(
                        "No valid review channel found for Application #%d! Channel ID: %s",
                        app_number,
                        channel_id,
                    )

                log.info(
                    "Successfully registered Application #%d for %s (%d)",
                    app_number,
                    applicant,
                    applicant.id,
                )
        except Exception as exc:
            log.exception("Error saving application to DB/channel: %s", exc)


# Global singleton manager instance
application_manager = ApplicationManager()
