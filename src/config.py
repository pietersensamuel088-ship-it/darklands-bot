"""
config.py – Loads bot configuration from config.json (ticket types, roles,
channels, permissions) and exposes typed Pydantic models.
"""
import json
import pathlib
from typing import Optional
from pydantic import BaseModel, Field

# ── Shared constants ──────────────────────────────────────────────────────────
CLOSE_DELAYS: dict[str, int] = {
    "15m": 900,
    "1h":  3600,
    "6h":  21600,
    "12h": 43200,
    "24h": 86400,
    "3d":  259200,
}

PRIORITY_COLOURS: dict[str, int] = {
    "Low":      0x57f287,
    "Medium":   0xfee75c,
    "High":     0xe67e22,
    "Critical": 0xed4245,
}

VALID_PRIORITIES = list(PRIORITY_COLOURS.keys())
VALID_STATUSES   = ["open", "close_pending", "closed"]

# ── Pydantic models ───────────────────────────────────────────────────────────

class TicketTypeConfig(BaseModel):
    key:              str
    name:             str
    description:      str
    emoji:            str
    category_id:      int
    staff_role_ids:   list[int] = []
    default_priority: str = "Medium"

class ChannelConfig(BaseModel):
    transcript_log_channel_id: int
    log_channel_closed_id:     int
    welcome_channel_id:        Optional[int] = None
    suggestion_channel_id:     Optional[int] = None
    question_channel_id:       Optional[int] = None
    bug_report_channel_id:     Optional[int] = None
    gang_recruitment_channel_id: Optional[int] = None

class RoleIds(BaseModel):
    staff: list[int] = []
    admin: list[int] = []

class PermissionsConfig(BaseModel):
    # "everyone" | "roles"
    create: str = "everyone"
    # "staff_and_admin" | "admin_only"
    claim:  str = "staff_and_admin"
    manage: str = "staff_and_admin"
    # "owner_and_staff" | "staff_and_admin" | "admin_only"
    close_request: str = "owner_and_staff"
    close_cancel:  str = "owner_and_staff"
    # used when create == "roles"
    create_role_ids: list[int] = []

class BotConfig(BaseModel):
    ticket_types:               list[TicketTypeConfig]
    channels:                   ChannelConfig
    role_ids:                   RoleIds
    permissions:                PermissionsConfig
    max_open_tickets_per_user:  int = 1

# ── Loader ────────────────────────────────────────────────────────────────────
_CONFIG_PATH = pathlib.Path(__file__).parent.parent / "config.json"

def load_config() -> BotConfig:
    """Load and validate config.json."""
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"config.json not found at {_CONFIG_PATH}. "
            "Copy config.json.example and fill in your values."
        )
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        raw = json.load(f)
    return BotConfig(**raw)
