import os
from dotenv import load_dotenv

"""
Central configuration for the me.Tică+ bot.

This module is Discord-agnostic so it can also be imported from
other tools (e.g. a StarCraft replay analyzer) to reuse the same
rating constants, data locations, and replay settings.
"""


# --- Environment & secrets ----------------------------------------------------

load_dotenv()

# Discord bot token
TOKEN: str | None = os.getenv("DISCORD_TOKEN")


# --- Rating / OpenSkill configuration ----------------------------------------

# These custom values mirror your existing bot settings.
CUSTOM_MU: float = 1200.0
CUSTOM_SIGMA: float = 200.0 / 3.0
CUSTOM_BETA: float = 100.0 / 3.0
CUSTOM_TAU: float = 8.0


# --- Data files ---------------------------------------------------------------

# Primary persisted rating database
DATA_FILE: str = "player_data.json"

# File used by /undo to restore the last match state
BACKUP_DATA_FILE: str = "player_data_backup.json"

# Test-mode data files (used when /match is run with test_mode=True)
TEST_DATA_FILE: str = "test_player_data.json"
TEST_BACKUP_DATA_FILE: str = "test_player_data_backup.json"


# --- Replay integration -------------------------------------------------------

# Default SC:BW replay folder (can be overridden via env var)
REPLAY_FOLDER: str = os.getenv(
    "REPLAY_FOLDER",
    r"C:\Users\Andrei\OneDrive\Documents\StarCraft\Maps\Replays",
)

# Maximum age (in seconds) for a replay to be considered valid for auto-upload
AUTO_REPLAY_MAX_AGE_SECONDS: int = 15 * 60  # 15 minutes

# Discord's typical upload limit for non-Nitro servers (approximate)
DISCORD_ATTACHMENT_LIMIT_MB: int = 25


# --- Session / analytics settings --------------------------------------------

# Gap that defines a new play session in /graphall and history views
SESSION_GAP_SECONDS: int = 8 * 60 * 60  # 8 hours

# What to show in the embed after a match. Can be "graph" or "history".
MATCH_EMBED_STYLE: str = "history"


# --- Permissions / owner config ----------------------------------------------

# Replace with your actual Discord user ID; used for /dm and similar commands
MY_DISCORD_ID: int = 351117400529436675

