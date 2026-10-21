"""
models.py – SQLAlchemy ORM model for the discord_tickets table.
"""
import datetime
from sqlalchemy import (
    BigInteger, Boolean, DateTime, Integer, JSON, String, Text, func
)
from sqlalchemy.orm import Mapped, mapped_column
from database.engine import Base

class DiscordTicket(Base):
    __tablename__ = "discord_tickets"

    id:                       Mapped[int]            = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_number:            Mapped[int]            = mapped_column(Integer, unique=True, nullable=False)
    ticket_code:              Mapped[str]            = mapped_column(String(32), nullable=False)
    type_key:                 Mapped[str]            = mapped_column(String(64), nullable=False)
    type_name:                Mapped[str]            = mapped_column(String(128), nullable=False)
    category_id:              Mapped[int]            = mapped_column(BigInteger, nullable=False)
    channel_id:               Mapped[int | None]     = mapped_column(BigInteger, unique=True, nullable=True)
    channel_name:             Mapped[str]            = mapped_column(String(128), nullable=False)
    control_message_id:       Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    close_message_id:         Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    creator_id:               Mapped[int]            = mapped_column(BigInteger, nullable=False)
    creator_name:             Mapped[str]            = mapped_column(String(128), nullable=False)
    creator_avatar:           Mapped[str | None]     = mapped_column(String(256), nullable=True)
    claimed_by_id:            Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    claimed_by_name:          Mapped[str | None]     = mapped_column(String(128), nullable=True)
    claimed_at:               Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    priority:                 Mapped[str]            = mapped_column(String(16), nullable=False, default="Medium")
    status:                   Mapped[str]            = mapped_column(String(32), nullable=False, default="open")
    close_reason:             Mapped[str | None]     = mapped_column(Text, nullable=True)
    close_requested_by_id:    Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    close_requested_by_name:  Mapped[str | None]     = mapped_column(String(128), nullable=True)
    close_delay_key:          Mapped[str | None]     = mapped_column(String(8), nullable=True)
    close_delay_seconds:      Mapped[int | None]     = mapped_column(Integer, nullable=True)
    close_at:                 Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at:                Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_by_id:             Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    closed_by_name:           Mapped[str | None]     = mapped_column(String(128), nullable=True)
    notes_thread_id:          Mapped[int | None]     = mapped_column(BigInteger, nullable=True)
    added_members:            Mapped[list]           = mapped_column(JSON, nullable=False, default=list)
    history:                  Mapped[list]           = mapped_column(JSON, nullable=False, default=list)
    messages:                 Mapped[list]           = mapped_column(JSON, nullable=False, default=list)
    staff_notes_messages:     Mapped[list]           = mapped_column(JSON, nullable=False, default=list)
    message_count:            Mapped[int]            = mapped_column(Integer, nullable=False, default=0)
    transcript_saved:         Mapped[bool]           = mapped_column(Boolean, nullable=False, default=False)
    created_at:               Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:               Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def add_history_event(
        self,
        action: str = "",
        actor_id: int | None = None,
        actor_name: str | None = None,
        details: str = "",
        **extra
    ) -> None:
        """Append an audit event to the history JSON list."""
        import datetime as dt

        # Map keyword argument aliases from different callers
        if not actor_name and "actor" in extra:
            actor_name = str(extra.pop("actor"))
        if not details and "detail" in extra:
            details = str(extra.pop("detail"))
        if not action and "action" in extra:
            action = str(extra.pop("action"))

        timestamp = extra.pop("timestamp", None)
        if not timestamp:
            timestamp = dt.datetime.now(dt.timezone.utc).isoformat()
        elif hasattr(timestamp, "isoformat"):
            timestamp = timestamp.isoformat()
        else:
            timestamp = str(timestamp)

        event = {
            "timestamp":  timestamp,
            "action":     action,
            "actor_id":   actor_id,
            "actor_name": actor_name,
            "details":    details,
            **extra
        }
        # JSON columns must be reassigned (not mutated in place) for SQLAlchemy to detect changes
        self.history = [*self.history, event]

    def __repr__(self) -> str:
        return f"<DiscordTicket {self.ticket_code} status={self.status}>"


class DiscordSuggestion(Base):
    __tablename__ = "discord_suggestions"

    id:                 Mapped[int]               = mapped_column(Integer, primary_key=True, autoincrement=True)
    suggestion_number:  Mapped[int]               = mapped_column(Integer, unique=True, nullable=False)
    author_id:          Mapped[int]               = mapped_column(BigInteger, nullable=False)
    author_name:        Mapped[str]               = mapped_column(String(128), nullable=False)
    author_avatar:      Mapped[str | None]        = mapped_column(String(256), nullable=True)
    title:              Mapped[str]               = mapped_column(String(256), nullable=False)
    suggestion:         Mapped[str]               = mapped_column(Text, nullable=False)
    reason:             Mapped[str]               = mapped_column(Text, nullable=False)
    status:             Mapped[str]               = mapped_column(String(32), nullable=False, default="Pending")
    status_reason:      Mapped[str | None]        = mapped_column(Text, nullable=True)
    status_updated_by:  Mapped[str | None]        = mapped_column(String(128), nullable=True)
    upvotes:            Mapped[list]              = mapped_column(JSON, nullable=False, default=list)
    downvotes:          Mapped[list]              = mapped_column(JSON, nullable=False, default=list)
    message_id:         Mapped[int | None]        = mapped_column(BigInteger, unique=True, nullable=True)
    channel_id:         Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    thread_id:          Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    created_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def calculate_votes(self) -> tuple[int, int, float]:
        """Return (upvotes_count, downvotes_count, percent_in_favour)."""
        up = len(self.upvotes or [])
        down = len(self.downvotes or [])
        total = up + down
        pct = (up / total * 100.0) if total > 0 else 0.0
        return up, down, pct

    def __repr__(self) -> str:
        return f"<DiscordSuggestion #{self.suggestion_number} status={self.status}>"


class DiscordQuestion(Base):
    __tablename__ = "discord_questions"

    id:                 Mapped[int]               = mapped_column(Integer, primary_key=True, autoincrement=True)
    question_number:    Mapped[int]               = mapped_column(Integer, unique=True, nullable=False)
    author_id:          Mapped[int]               = mapped_column(BigInteger, nullable=False)
    author_name:        Mapped[str]               = mapped_column(String(128), nullable=False)
    author_avatar:      Mapped[str | None]        = mapped_column(String(256), nullable=True)
    title:              Mapped[str]               = mapped_column(String(256), nullable=False)
    question:           Mapped[str]               = mapped_column(Text, nullable=False)
    status:             Mapped[str]               = mapped_column(String(32), nullable=False, default="Open")
    status_reason:      Mapped[str | None]        = mapped_column(Text, nullable=True)
    answered_by_id:     Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    answered_by_name:   Mapped[str | None]        = mapped_column(String(128), nullable=True)
    message_id:         Mapped[int | None]        = mapped_column(BigInteger, unique=True, nullable=True)
    channel_id:         Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    thread_id:          Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    created_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DiscordQuestion #{self.question_number} status={self.status}>"


class DiscordBugReport(Base):
    __tablename__ = "discord_bug_reports"

    id:                 Mapped[int]               = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_number:      Mapped[int]               = mapped_column(Integer, unique=True, nullable=False)
    author_id:          Mapped[int]               = mapped_column(BigInteger, nullable=False)
    author_name:        Mapped[str]               = mapped_column(String(128), nullable=False)
    author_avatar:      Mapped[str | None]        = mapped_column(String(256), nullable=True)
    title:              Mapped[str]               = mapped_column(String(256), nullable=False)
    description:        Mapped[str]               = mapped_column(Text, nullable=False)
    reproduce_steps:    Mapped[str]               = mapped_column(Text, nullable=False)
    status:             Mapped[str]               = mapped_column(String(32), nullable=False, default="Investigating")
    status_reason:      Mapped[str | None]        = mapped_column(Text, nullable=True)
    status_updated_by:  Mapped[str | None]        = mapped_column(String(128), nullable=True)
    message_id:         Mapped[int | None]        = mapped_column(BigInteger, unique=True, nullable=True)
    channel_id:         Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    thread_id:          Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    created_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DiscordBugReport #{self.report_number} status={self.status}>"


class DiscordGangRecruitment(Base):
    __tablename__ = "discord_gang_recruitments"

    id:                 Mapped[int]               = mapped_column(Integer, primary_key=True, autoincrement=True)
    recruitment_number: Mapped[int]               = mapped_column(Integer, unique=True, nullable=False)
    gang_name:          Mapped[str]               = mapped_column(String(128), nullable=False)
    leader_id:          Mapped[int]               = mapped_column(BigInteger, nullable=False)
    leader_name:        Mapped[str]               = mapped_column(String(128), nullable=False)
    playstyle:          Mapped[str]               = mapped_column(String(128), nullable=False)
    members_wanted:     Mapped[str]               = mapped_column(String(32), nullable=False)
    about:              Mapped[str]               = mapped_column(Text, nullable=False)
    requirements:       Mapped[str]               = mapped_column(Text, nullable=False)
    contact:            Mapped[str]               = mapped_column(String(128), nullable=False, default="Discord")
    status:             Mapped[str]               = mapped_column(String(32), nullable=False, default="Recruiting")
    message_id:         Mapped[int | None]        = mapped_column(BigInteger, unique=True, nullable=True)
    channel_id:         Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    thread_id:          Mapped[int | None]        = mapped_column(BigInteger, nullable=True)
    created_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:         Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:
        return f"<DiscordGangRecruitment #{self.recruitment_number} gang={self.gang_name}>"
