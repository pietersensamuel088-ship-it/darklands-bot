# Darklands SMP Bot

Official Discord bot for the **Darklands SMP Network**.

---

## Setup

### 1. Install Python
Make sure you have **Python 3.10+** installed → [python.org](https://www.python.org/downloads/)

### 2. Create a virtual environment *(recommended)*
```powershell
cd c:\darklands\bot
python -m venv .venv
.venv\Scripts\activate
```

### 3. Install dependencies
```powershell
pip install -r requirements.txt
```

### 4. Configure your `.env`
```powershell
copy .env.example .env
```
Open `.env` and fill in:
```env
DISCORD_TOKEN=your_bot_token_here
GUILD_ID=your_guild_id_here
```
Get these from [discord.com/developers/applications](https://discord.com/developers/applications).

### 5. Run the bot
```powershell
python src/bot.py
```

Slash commands are synced to your guild automatically on startup — no separate deploy step needed.

---

## Project structure

```
bot/
├── src/
│   ├── cogs/
│   │   └── ping.py     # /ping command
│   └── bot.py          # Entry point
├── .env.example
├── .gitignore
└── requirements.txt
```

## Adding more commands

1. Create a new file in `src/cogs/`, e.g. `src/cogs/help.py`.
2. Write a `commands.Cog` subclass with your `app_commands` and an `async def setup(bot)` at the bottom.
3. Restart the bot — it auto-loads and syncs everything.
