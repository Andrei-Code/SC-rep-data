import json
import os
import shutil
import time
from typing import Any, Dict, List

from openskill.models import PlackettLuce

from config import (
    CUSTOM_MU,
    CUSTOM_SIGMA,
    CUSTOM_BETA,
    CUSTOM_TAU,
    DATA_FILE,
    BACKUP_DATA_FILE,
    TEST_DATA_FILE,
    TEST_BACKUP_DATA_FILE,
)


"""
Rating engine and persistence for me.Tică+.

This module is intentionally Discord-agnostic so it can be reused
from other tools (for example, a StarCraft replay analyzer) that
want to use the same OpenSkill configuration and JSON schema.
"""


# --- OpenSkill model ----------------------------------------------------------

model = PlackettLuce(
    mu=CUSTOM_MU,
    sigma=CUSTOM_SIGMA,
    beta=CUSTOM_BETA,
    tau=CUSTOM_TAU,
)


# --- In-memory cache for player data -----------------------------------------

_data_cache: Dict[str, Any] | None = None


def _read_from_disk(path: str | None = None) -> Dict[str, Any]:
    """Internal helper: read the JSON database from disk or return an empty dict."""
    file_path = path or DATA_FILE
    if not os.path.exists(file_path):
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_data(data_file: str | None = None) -> Dict[str, Any]:
    """
    Load the player rating database into memory.

    If data_file is None, subsequent calls are served from an in-memory cache.
    If data_file is set (e.g. TEST_DATA_FILE), reads from that path and returns
    without using or updating the main cache.
    """
    global _data_cache
    if data_file is not None:
        return _read_from_disk(data_file)
    if _data_cache is None:
        _data_cache = _read_from_disk()
    return _data_cache


def save_data(data: Dict[str, Any] | None = None, data_file: str | None = None) -> None:
    """
    Persist the player rating database to disk.

    If data_file is set, writes to that path (data must be provided).
    Otherwise uses the main cache and DATA_FILE; if data is provided it becomes
    the new cache. Writes are done atomically via a temporary file.
    """
    global _data_cache

    if data_file is not None:
        if data is None:
            data = {}
        tmp_path = data_file + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, data_file)
        return

    if data is not None:
        _data_cache = data
    if _data_cache is None:
        _data_cache = {}
    tmp_path = DATA_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(_data_cache, f, indent=4)
    os.replace(tmp_path, DATA_FILE)


def backup_data(data_file: str | None = None) -> None:
    """
    Create a backup of the current database for /undo.

    If data_file is set, backs up that file to its corresponding backup path
    (TEST_BACKUP_DATA_FILE when data_file is TEST_DATA_FILE).
    """
    if data_file is not None:
        backup_path = TEST_BACKUP_DATA_FILE if data_file == TEST_DATA_FILE else (data_file + ".backup")
        if not os.path.exists(data_file):
            return
        shutil.copy(data_file, backup_path)
        return

    data = load_data()
    if not data and not os.path.exists(DATA_FILE):
        return
    if not os.path.exists(DATA_FILE):
        save_data(data)
    shutil.copy(DATA_FILE, BACKUP_DATA_FILE)


def restore_backup(data_file: str | None = None) -> bool:
    """
    Restore the database from the backup created by backup_data().

    If data_file is set, restores that file from its backup (e.g. TEST_DATA_FILE).
    Returns True if a backup was restored, False if no backup was found.
    """
    global _data_cache

    if data_file is not None:
        backup_path = TEST_BACKUP_DATA_FILE if data_file == TEST_DATA_FILE else (data_file + ".backup")
        if not os.path.exists(backup_path):
            return False
        shutil.copy(backup_path, data_file)
        os.remove(backup_path)
        return True

    if not os.path.exists(BACKUP_DATA_FILE):
        return False
    shutil.copy(BACKUP_DATA_FILE, DATA_FILE)
    os.remove(BACKUP_DATA_FILE)
    _data_cache = None
    return True


# --- Rating helpers -----------------------------------------------------------

def get_player_rating(user_id: int, data_file: str | None = None):
    """
    Return an OpenSkill rating object for the given player ID.

    If the player has no history, they are initialized with the
    configured CUSTOM_MU and CUSTOM_SIGMA.
    If data_file is set, ratings are read from that file (e.g. test data).
    """
    data = load_data(data_file=data_file)
    pid_str = str(user_id)

    if pid_str in data:
        return model.rating(mu=data[pid_str]["mu"], sigma=data[pid_str]["sigma"])

    return model.rating(mu=CUSTOM_MU, sigma=CUSTOM_SIGMA)


def get_player_ordinal(user_id: int, data_file: str | None = None) -> int:
    """Convenience helper: return the integer ladder rating for a player."""
    return int(get_player_rating(user_id, data_file=data_file).ordinal())


def update_ratings(
    winners: List[Any], losers: List[Any], data_file: str | None = None
) -> None:
    """
    Apply an OpenSkill update for a single match.

    `winners` and `losers` are expected to be Discord Member-like objects
    with an `.id` attribute. If data_file is set, read/write that file (e.g. test).
    """
    data = load_data(data_file=data_file)
    match_time = int(time.time())

    # 1. Build Team 1 (Winners)
    team1_ratings = []
    for p in winners:
        pid_str = str(p.id)
        if pid_str not in data:
            data[pid_str] = {"mu": CUSTOM_MU, "sigma": CUSTOM_SIGMA, "history": []}
        team1_ratings.append(
            model.rating(mu=data[pid_str]["mu"], sigma=data[pid_str]["sigma"])
        )

    # 2. Build Team 2 (Losers)
    team2_ratings = []
    for p in losers:
        pid_str = str(p.id)
        if pid_str not in data:
            data[pid_str] = {"mu": CUSTOM_MU, "sigma": CUSTOM_SIGMA, "history": []}
        team2_ratings.append(
            model.rating(mu=data[pid_str]["mu"], sigma=data[pid_str]["sigma"])
        )

    # 3. Calculate the new OpenSkill ratings (team1 beat team2)
    updated_teams = model.rate([team1_ratings, team2_ratings])
    updated_winners_team = updated_teams[0]
    updated_losers_team = updated_teams[1]

    # 4. Save the Winners
    for i, p in enumerate(winners):
        new_r = updated_winners_team[i]
        pid_str = str(p.id)

        data[pid_str]["mu"] = new_r.mu
        data[pid_str]["sigma"] = new_r.sigma

        data[pid_str]["history"].append(
            {"timestamp": match_time, "mmr": int(new_r.ordinal()), "result": "Win"}
        )

    # 5. Save the Losers
    for i, p in enumerate(losers):
        new_r = updated_losers_team[i]
        pid_str = str(p.id)

        data[pid_str]["mu"] = new_r.mu
        data[pid_str]["sigma"] = new_r.sigma

        data[pid_str]["history"].append(
            {"timestamp": match_time, "mmr": int(new_r.ordinal()), "result": "Loss"}
        )

    save_data(data, data_file=data_file)


def display_mmr(pid: int, data_file: str | None = None) -> str:
    """
    Return the display-ready integer rating string for a player ID.
    If data_file is set, read from that file (e.g. test data).
    """
    data = load_data(data_file=data_file)
    key = str(pid)

    if key in data:
        r = model.rating(mu=data[key]["mu"], sigma=data[key]["sigma"])
        return f"{int(r.ordinal())}"

    return "0"

