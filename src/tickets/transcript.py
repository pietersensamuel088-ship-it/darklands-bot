"""
transcript.py
=============
Generates a self-contained, dark-themed HTML transcript for a Darklands SMP
Discord ticket.  No external CDN references – all CSS is inlined so the file
remains readable after the server is long gone.

Public surface
--------------
    generate_html_transcript(ticket, messages, staff_notes, cfg) -> str
    async scrape_channel_messages(channel)                        -> list[dict]
"""

from __future__ import annotations

import html
import re
import textwrap
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    # Avoid circular imports; only used for type hints.
    from sqlalchemy.orm import DeclarativeBase  # noqa: F401

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

# Palette used for username colouring (rotated by a hash of the user id).
_USERNAME_PALETTE: list[str] = [
    "#7289da",  # blurple
    "#43b581",  # green
    "#faa61a",  # yellow
    "#f47fff",  # pink
    "#ed4245",  # red
    "#00b0f4",  # cyan
    "#ff6b6b",  # coral
    "#a3e635",  # lime
    "#c084fc",  # violet
    "#fb923c",  # orange
]

_PRIORITY_COLOURS: dict[str, str] = {
    "low":      "#57f287",
    "medium":   "#fee75c",
    "high":     "#e67e22",
    "critical": "#ed4245",
}

_PRIORITY_TEXT_DARK: set[str] = {"low", "medium"}  # needs dark text on badge


def _username_colour(user_id: int) -> str:
    """Return a stable hex colour for *user_id* drawn from the palette."""
    return _USERNAME_PALETTE[user_id % len(_USERNAME_PALETTE)]


def _priority_badge(priority: str | None) -> str:
    """Return an HTML ``<span>`` badge for the given priority string."""
    if not priority:
        return "<span>—</span>"
    key = priority.lower()
    bg  = _PRIORITY_COLOURS.get(key, "#72767d")
    fg  = "#23272a" if key in _PRIORITY_TEXT_DARK else "#ffffff"
    label = html.escape(priority.title())
    return (
        f'<span style="'
        f'background:{bg};color:{fg};'
        f'padding:2px 8px;border-radius:4px;font-weight:700;font-size:0.8rem;'
        f'">{label}</span>'
    )


# ---------------------------------------------------------------------------
# Initials avatar (SVG data-URI fallback when no avatar URL is available)
# ---------------------------------------------------------------------------

def _initials_avatar(name: str, user_id: int) -> str:
    """Return a ``data:image/svg+xml`` URI for a coloured-circle initials avatar."""
    colour  = _username_colour(user_id)
    letter  = (name[0] if name else "?").upper()
    # Minimal inline SVG – no external resources.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
        f'<circle cx="20" cy="20" r="20" fill="{colour}"/>'
        f'<text x="20" y="26" font-family="sans-serif" font-size="18" '
        f'font-weight="bold" fill="#ffffff" text-anchor="middle">{html.escape(letter)}</text>'
        f'</svg>'
    )
    import base64
    encoded = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{encoded}"


# ---------------------------------------------------------------------------
# Markdown → HTML renderer (subset relevant to Discord messages)
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(
    r"```(?P<lang>[a-zA-Z0-9_+-]*)?\n?(?P<code>.*?)```",
    re.DOTALL,
)
_INLINE_CODE_RE = re.compile(r"`(?P<code>[^`]+)`")
_BOLD_RE        = re.compile(r"\*\*(?P<text>.+?)\*\*", re.DOTALL)
_ITALIC_RE      = re.compile(r"\*(?P<text>.+?)\*", re.DOTALL)
_UNDERLINE_RE   = re.compile(r"__(?P<text>.+?)__", re.DOTALL)
_STRIKE_RE      = re.compile(r"~~(?P<text>.+?)~~", re.DOTALL)
_SPOILER_RE     = re.compile(r"\|\|(?P<text>.+?)\|\|", re.DOTALL)
_URL_RE         = re.compile(
    r"(https?://[^\s<>\"']+)",
    re.IGNORECASE,
)


def _render_markdown(text: str) -> str:
    """
    Convert a subset of Discord-flavoured markdown to safe HTML.

    Processing order matters: code blocks are extracted first so their
    contents are never mangled by the other rules.
    """
    if not text:
        return ""

    # ---- 1. Extract code blocks to protect them from further processing ----
    placeholders: dict[str, str] = {}
    counter = [0]

    def _store(rendered: str) -> str:
        key = f"\x00PLACEHOLDER_{counter[0]}\x00"
        placeholders[key] = rendered
        counter[0] += 1
        return key

    def _replace_code_block(m: re.Match) -> str:
        lang = html.escape(m.group("lang") or "")
        code = html.escape(m.group("code"))
        label = f'<span class="lang-label">{lang}</span>' if lang else ""
        rendered = (
            f'<div class="code-block">{label}'
            f'<pre><code>{code}</code></pre></div>'
        )
        return _store(rendered)

    def _replace_inline_code(m: re.Match) -> str:
        code = html.escape(m.group("code"))
        return _store(f'<code class="inline-code">{code}</code>')

    text = _CODE_BLOCK_RE.sub(_replace_code_block, text)
    text = _INLINE_CODE_RE.sub(_replace_inline_code, text)

    # ---- 2. Escape all remaining HTML special characters ----
    text = html.escape(text, quote=False)

    # ---- 3. Apply inline formatting ----
    text = _BOLD_RE.sub(r"<strong>\g<text></strong>", text)
    text = _ITALIC_RE.sub(r"<em>\g<text></em>", text)
    text = _UNDERLINE_RE.sub(r"<u>\g<text></u>", text)
    text = _STRIKE_RE.sub(r"<del>\g<text></del>", text)
    text = _SPOILER_RE.sub(
        r'<span class="spoiler">\g<text></span>', text
    )

    # ---- 4. Auto-link plain URLs (only outside placeholders) ----
    def _linkify(m: re.Match) -> str:
        url = m.group(1)
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>'

    text = _URL_RE.sub(_linkify, text)

    # ---- 5. Newlines → <br> ----
    text = text.replace("\n", "<br>")

    # ---- 6. Restore placeholders ----
    for key, rendered in placeholders.items():
        text = text.replace(key, rendered)

    return text


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------

def _fmt_ts(iso: str | None) -> str:
    """Return a human-friendly UTC timestamp string, or '—' if absent."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%d %b %Y %H:%M UTC")
    except ValueError:
        return html.escape(iso)


# ---------------------------------------------------------------------------
# Attachment rendering
# ---------------------------------------------------------------------------

_IMAGE_TYPES: frozenset[str] = frozenset({
    "image/png", "image/jpeg", "image/gif",
    "image/webp", "image/svg+xml", "image/bmp",
})


def _render_attachments(attachments: list[dict]) -> str:
    """Return HTML for a list of attachment dicts."""
    if not attachments:
        return ""
    parts: list[str] = []
    for att in attachments:
        url      = html.escape(att.get("url", ""), quote=True)
        filename = html.escape(att.get("filename", "file"))
        ctype    = (att.get("content_type") or "").lower().split(";")[0].strip()

        if ctype in _IMAGE_TYPES:
            parts.append(
                f'<div class="attachment image-attachment">'
                f'<a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'<img src="{url}" alt="{filename}" '
                f'style="max-width:300px;max-height:300px;border-radius:4px;">'
                f'</a></div>'
            )
        else:
            parts.append(
                f'<div class="attachment file-attachment">'
                f'📎 <a href="{url}" target="_blank" rel="noopener noreferrer">'
                f'{filename}</a>'
                f'</div>'
            )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Single message block
# ---------------------------------------------------------------------------

def _render_message(msg: dict) -> str:
    """Render one message dict as an HTML block."""
    user_id   = int(msg.get("author_id") or 0)
    name      = msg.get("author_name") or "Unknown"
    avatar    = msg.get("author_avatar") or ""
    content   = msg.get("content") or ""
    timestamp = msg.get("timestamp") or ""
    atts      = msg.get("attachments") or []
    is_bot    = bool(msg.get("is_bot"))

    colour    = _username_colour(user_id)
    ts_str    = _fmt_ts(timestamp)

    # Avatar: prefer remote URL, fall back to SVG initials.
    if avatar:
        avatar_tag = (
            f'<img class="avatar" src="{html.escape(avatar, quote=True)}" '
            f'alt="{html.escape(name)}" '
            f'onerror="this.src=\'{_initials_avatar(name, user_id)}\'">'
        )
    else:
        avatar_tag = (
            f'<img class="avatar" src="{_initials_avatar(name, user_id)}" '
            f'alt="{html.escape(name)}">'
        )

    bot_badge = (
        '<span class="bot-badge">BOT</span>' if is_bot else ""
    )

    rendered_content    = _render_markdown(content)
    rendered_atts       = _render_attachments(atts)

    return textwrap.dedent(f"""\
        <div class="message">
          <div class="avatar-col">{avatar_tag}</div>
          <div class="message-body">
            <div class="message-header">
              <span class="username" style="color:{colour};">{html.escape(name)}</span>
              {bot_badge}
              <span class="timestamp">{ts_str}</span>
            </div>
            <div class="message-content">{rendered_content}</div>
            {rendered_atts}
          </div>
        </div>
    """)


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = """\
/* ===== Reset & base ===== */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 16px; }
body {
    font-family: 'Whitney', 'Helvetica Neue', Helvetica, Arial, sans-serif;
    background: #36393f;
    color: #dcddde;
    line-height: 1.5;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
}
a { color: #00b0f4; text-decoration: none; }
a:hover { text-decoration: underline; }

/* ===== Layout ===== */
.wrapper {
    max-width: 960px;
    margin: 0 auto;
    padding: 0 16px 40px;
    flex: 1;
}

/* ===== Header / info card ===== */
.ticket-header {
    background: #202225;
    border-radius: 8px;
    padding: 24px 28px;
    margin: 24px 0 20px;
    display: grid;
    grid-template-columns: auto 1fr;
    gap: 16px 24px;
    align-items: start;
}
.ticket-header .creator-avatar {
    width: 72px;
    height: 72px;
    border-radius: 50%;
    object-fit: cover;
    grid-row: 1 / span 3;
}
.ticket-title {
    font-size: 1.5rem;
    font-weight: 700;
    color: #ffffff;
    word-break: break-all;
}
.ticket-meta {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
    gap: 8px 20px;
    margin-top: 10px;
    grid-column: 1 / -1;
}
.meta-item { display: flex; flex-direction: column; }
.meta-label {
    font-size: 0.7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: #72767d;
}
.meta-value {
    font-size: 0.9rem;
    color: #dcddde;
    margin-top: 2px;
}
.close-reason {
    grid-column: 1 / -1;
    background: #2f3136;
    border-left: 4px solid #ed4245;
    padding: 8px 12px;
    border-radius: 0 4px 4px 0;
    font-size: 0.85rem;
    color: #b9bbbe;
    margin-top: 4px;
}

/* ===== Section heading ===== */
.section-heading {
    font-size: 0.75rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.06em;
    color: #72767d;
    border-bottom: 1px solid #40444b;
    padding-bottom: 6px;
    margin: 20px 0 12px;
}

/* ===== Messages ===== */
.messages-area {
    background: #2f3136;
    border-radius: 8px;
    padding: 16px;
}
.message {
    display: flex;
    gap: 12px;
    padding: 6px 0;
    border-radius: 4px;
    transition: background 0.1s;
}
.message:hover { background: #32353b; }
.avatar-col { flex-shrink: 0; }
.avatar {
    width: 40px;
    height: 40px;
    border-radius: 50%;
    object-fit: cover;
}
.message-body { flex: 1; min-width: 0; }
.message-header {
    display: flex;
    align-items: baseline;
    gap: 8px;
    flex-wrap: wrap;
    margin-bottom: 2px;
}
.username {
    font-weight: 700;
    font-size: 1rem;
}
.bot-badge {
    background: #5865f2;
    color: #ffffff;
    font-size: 0.6rem;
    font-weight: 700;
    padding: 1px 5px;
    border-radius: 3px;
    letter-spacing: 0.04em;
    vertical-align: middle;
}
.timestamp {
    font-size: 0.72rem;
    color: #72767d;
}
.message-content {
    font-size: 0.94rem;
    color: #dcddde;
    word-break: break-word;
}

/* ===== Inline & block code ===== */
.inline-code {
    background: #2d2f32;
    border: 1px solid #40444b;
    border-radius: 3px;
    padding: 0 4px;
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 0.85em;
    color: #e3e5e8;
}
.code-block {
    background: #1e1f22;
    border: 1px solid #40444b;
    border-radius: 6px;
    margin: 6px 0;
    overflow: hidden;
}
.lang-label {
    display: block;
    background: #2b2d31;
    font-size: 0.72rem;
    color: #72767d;
    font-family: 'Consolas', monospace;
    padding: 4px 12px;
    border-bottom: 1px solid #40444b;
    text-transform: lowercase;
}
.code-block pre {
    margin: 0;
    padding: 12px 14px;
    overflow-x: auto;
}
.code-block code {
    font-family: 'Consolas', 'Courier New', monospace;
    font-size: 0.88rem;
    color: #e3e5e8;
    white-space: pre;
}

/* ===== Spoilers ===== */
.spoiler {
    background: #202225;
    color: transparent;
    border-radius: 3px;
    cursor: pointer;
    user-select: none;
    padding: 0 3px;
    transition: color 0.15s, background 0.15s;
}
.spoiler.revealed {
    color: #dcddde;
    background: #40444b;
}

/* ===== Attachments ===== */
.attachment { margin-top: 6px; }
.image-attachment img { display: block; border-radius: 4px; }
.file-attachment {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: #202225;
    border: 1px solid #40444b;
    padding: 6px 12px;
    border-radius: 6px;
    font-size: 0.88rem;
}

/* ===== Staff notes collapsible ===== */
details.staff-notes {
    margin-top: 24px;
    background: #202225;
    border: 1px solid #40444b;
    border-radius: 8px;
    overflow: hidden;
}
details.staff-notes summary {
    cursor: pointer;
    padding: 12px 16px;
    font-weight: 700;
    font-size: 0.9rem;
    color: #b9bbbe;
    list-style: none;
    user-select: none;
    border-bottom: 1px solid transparent;
    transition: background 0.1s;
}
details.staff-notes summary::-webkit-details-marker { display: none; }
details.staff-notes[open] summary {
    border-bottom-color: #40444b;
    background: #2b2d31;
}
details.staff-notes summary::before {
    content: '▶ ';
    font-size: 0.7rem;
    color: #72767d;
    transition: transform 0.2s;
}
details.staff-notes[open] summary::before { content: '▼ '; }
.staff-notes-inner {
    padding: 12px 16px;
    background: #2f3136;
}
.notes-empty {
    color: #72767d;
    font-style: italic;
    font-size: 0.88rem;
}

/* ===== Footer ===== */
footer {
    background: #202225;
    border-top: 1px solid #40444b;
    text-align: center;
    padding: 14px;
    font-size: 0.78rem;
    color: #4f545c;
}
footer strong { color: #72767d; }
"""


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def generate_html_transcript(
    ticket,
    messages: list[dict],
    staff_notes: list[dict],
    cfg,
) -> str:
    """
    Generate a complete, self-contained HTML transcript for *ticket*.

    Parameters
    ----------
    ticket:
        A DiscordTicket SQLAlchemy model instance.  Expected attributes:
        ``ticket_code``, ``type_name``, ``creator_name``, ``creator_avatar``,
        ``priority``, ``claimed_by_name``, ``closed_by_name``, ``close_reason``,
        ``created_at``, ``closed_at``.
    messages:
        List of message dicts (see module docstring for schema).
    staff_notes:
        List of staff-note dicts (same schema as *messages*).
    cfg:
        Bot configuration object (reserved for future use, e.g. guild name).

    Returns
    -------
    str
        A standalone HTML document as a string.
    """

    # ---- Ticket header values ------------------------------------------------
    ticket_code    = html.escape(str(getattr(ticket, "ticket_code", "UNKNOWN")))
    type_name      = html.escape(str(getattr(ticket, "type_name", "—") or "—"))
    creator_name   = html.escape(str(getattr(ticket, "creator_name", "—") or "—"))
    creator_avatar = getattr(ticket, "creator_avatar", None) or ""
    priority       = getattr(ticket, "priority", None)
    claimed_by     = html.escape(str(getattr(ticket, "claimed_by_name", "—") or "—"))
    closed_by      = html.escape(str(getattr(ticket, "closed_by_name", "—") or "—"))
    close_reason   = getattr(ticket, "close_reason", None) or ""
    created_at_raw = getattr(ticket, "created_at", None)
    closed_at_raw  = getattr(ticket, "closed_at", None)

    # Convert datetime objects to ISO strings if necessary.
    def _to_iso(val) -> str | None:
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.isoformat()
        return str(val)

    created_at = _fmt_ts(_to_iso(created_at_raw))
    closed_at  = _fmt_ts(_to_iso(closed_at_raw))

    msg_count = len(messages)

    # Creator avatar tag
    if creator_avatar:
        creator_avatar_html = (
            f'<img class="creator-avatar" '
            f'src="{html.escape(creator_avatar, quote=True)}" '
            f'alt="{creator_name}">'
        )
    else:
        # No avatar – render initials SVG in an <img> tag.
        creator_id = getattr(ticket, "creator_id", 0) or 0
        creator_avatar_html = (
            f'<img class="creator-avatar" '
            f'src="{_initials_avatar(creator_name, int(creator_id))}" '
            f'alt="{creator_name}">'
        )

    close_reason_html = ""
    if close_reason:
        close_reason_html = (
            f'<div class="close-reason">'
            f'<strong>Close reason:</strong> {html.escape(close_reason)}'
            f'</div>'
        )

    # ---- Render messages -----------------------------------------------------
    messages_html = "\n".join(_render_message(m) for m in messages) if messages else (
        '<p style="color:#72767d;font-style:italic;padding:8px 0;">No messages.</p>'
    )

    # ---- Render staff notes --------------------------------------------------
    if staff_notes:
        notes_html = "\n".join(_render_message(n) for n in staff_notes)
    else:
        notes_html = '<p class="notes-empty">No staff notes recorded.</p>'

    # ---- Page title ----------------------------------------------------------
    guild_name = html.escape(str(getattr(cfg, "guild_name", "Darklands SMP")))
    page_title = f"{ticket_code} – Ticket Transcript | {guild_name}"

    # ---- Assemble document ---------------------------------------------------
    document = f"""\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{page_title}</title>
  <style>
{_CSS}
  </style>
</head>
<body>
  <div class="wrapper">

    <!-- ===== Ticket Header ===== -->
    <div class="ticket-header">
      {creator_avatar_html}
      <div>
        <div class="ticket-title">#{ticket_code}</div>
        <div style="color:#72767d;font-size:0.85rem;margin-top:4px;">{type_name}</div>
      </div>

      <div class="ticket-meta">

        <div class="meta-item">
          <span class="meta-label">Created by</span>
          <span class="meta-value">{creator_name}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Priority</span>
          <span class="meta-value">{_priority_badge(priority)}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Claimed by</span>
          <span class="meta-value">{claimed_by}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Closed by</span>
          <span class="meta-value">{closed_by}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Opened</span>
          <span class="meta-value">{created_at}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Closed</span>
          <span class="meta-value">{closed_at}</span>
        </div>

        <div class="meta-item">
          <span class="meta-label">Messages</span>
          <span class="meta-value">{msg_count:,}</span>
        </div>

        {close_reason_html}

      </div>
    </div><!-- /.ticket-header -->

    <!-- ===== Message Log ===== -->
    <div class="section-heading">Message Log</div>
    <div class="messages-area">
{messages_html}
    </div>

    <!-- ===== Staff Notes (collapsible) ===== -->
    <details class="staff-notes">
      <summary>🔒 Staff Notes ({len(staff_notes)})</summary>
      <div class="staff-notes-inner">
{notes_html}
      </div>
    </details>

  </div><!-- /.wrapper -->

  <footer>
    Generated by <strong>Darklands SMP Bot</strong>
    &nbsp;·&nbsp;
    Transcript for ticket <strong>#{ticket_code}</strong>
    &nbsp;·&nbsp;
    {html.escape(datetime.now(timezone.utc).strftime("%d %b %Y %H:%M UTC"))}
  </footer>

  <!-- Spoiler reveal script (no external dependencies) -->
  <script>
    document.querySelectorAll('.spoiler').forEach(function(el) {{
      el.addEventListener('click', function() {{
        this.classList.toggle('revealed');
      }});
    }});
  </script>

</body>
</html>
"""
    return document


# ---------------------------------------------------------------------------
# Channel scraper
# ---------------------------------------------------------------------------

async def scrape_channel_messages(channel: discord.TextChannel) -> list[dict]:
    """
    Iterate the full history of *channel* and return a list of message dicts
    suitable for passing to :func:`generate_html_transcript`.

    Messages are returned in chronological order (oldest first).

    Parameters
    ----------
    channel:
        A ``discord.TextChannel`` (or compatible) object.

    Returns
    -------
    list[dict]
        Each element has the following keys:

        .. code-block:: python

            {
                "id":            int,          # Discord snowflake
                "author_id":     int,
                "author_name":   str,
                "author_avatar": str | None,   # CDN URL or None
                "content":       str,
                "timestamp":     str,          # ISO 8601, UTC
                "attachments":   [
                    {
                        "url":          str,
                        "filename":     str,
                        "content_type": str,   # MIME type (may be empty)
                    }
                ],
                "is_bot":        bool,
            }
    """
    results: list[dict] = []

    async for message in channel.history(limit=None, oldest_first=True):
        # Build the attachment list.
        attachments: list[dict] = []
        for att in message.attachments:
            attachments.append(
                {
                    "url":          att.url,
                    "filename":     att.filename,
                    "content_type": att.content_type or "",
                }
            )

        # Resolve the avatar URL for this message's author.
        author: discord.User | discord.Member = message.author
        try:
            avatar_url: str | None = (
                author.display_avatar.url if author.display_avatar else None
            )
        except Exception:
            avatar_url = None

        results.append(
            {
                "id":            message.id,
                "author_id":     author.id,
                "author_name":   author.display_name,
                "author_avatar": avatar_url,
                "content":       message.content or "",
                "timestamp":     message.created_at.isoformat(),
                "attachments":   attachments,
                "is_bot":        author.bot,
            }
        )

    return results
