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
    transcript_log_channel_id:      int
    log_channel_closed_id:          int
    welcome_channel_id:             Optional[int] = None
    suggestion_channel_id:          Optional[int] = None
    question_channel_id:            Optional[int] = None
    bug_report_channel_id:          Optional[int] = None
    gang_recruitment_channel_id:    Optional[int] = None
    application_channel_id:         Optional[int] = None
    application_pending_channel_id: Optional[int] = None
    application_accepted_channel_id: Optional[int] = None
    application_denied_channel_id:  Optional[int] = None
    application_ticket_category_id: Optional[int] = None
    beta_tester_role_id:            Optional[int] = None
    beta_tester_lounge_channel_id:  Optional[int] = None
    tester_info_channel_id:         Optional[int] = None
    owner_user_id:                  Optional[int] = None
    server_info_channel_id:         Optional[int] = None
    promo_redeem_channel_id:        Optional[int] = None
    bug_staff_channel_id:           Optional[int] = None

class MinecraftServerConfig(BaseModel):
    host:                     str = "darklands.sa1nt.xyz"
    port:                     int = 25565
    bedrock_port:             int = 19132
    display_name:             str = "Darklands SMP"
    update_interval_seconds:  int = 60

class ApplicationTypeConfig(BaseModel):
    key:                  str = "beta_tester"
    name:                 str = "Beta Tester Application"
    description:          str = "Apply to become an official Beta Tester for Darklands SMP!"
    emoji:                str = "🧪"
    channel_id:           Optional[int] = None
    pending_channel_id:   Optional[int] = None
    accepted_channel_id:  Optional[int] = None
    denied_channel_id:    Optional[int] = None
    accepted_role_id:     Optional[int] = None
    ticket_category_id:   Optional[int] = None
    lounge_channel_id:    Optional[int] = None
    questions:            list[str] = [
        "What is your Minecraft username? (Short answer)",
        "What is your Minecraft UUID? (Short answer — check mcuuid.net if needed)",
        "How old are you? (Short answer)",
        "What is your Discord username? (Short answer)",
        "How long have you been playing Minecraft, and what experience do you have with SMP servers? (Paragraph)",
        "Why would you like to become a beta tester for our SMP? (Paragraph)",
        "Have you ever beta tested or helped test a Minecraft server before? (Yes/No + explanation if yes)",
        "What would you do if you found a bug, glitch, or exploit during testing? (Paragraph)",
        "How active would you be during the beta testing phase? (Choose one: Daily / A few times a week / Once a week / Only occasionally)",
        "Is there anything else you would like us to know? (Optional — reply 'none' if nothing)",
    ]

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
    applications:               list[ApplicationTypeConfig] = []
    minecraft_server:           MinecraftServerConfig = Field(default_factory=MinecraftServerConfig)

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
