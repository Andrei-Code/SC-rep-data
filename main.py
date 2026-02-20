import discord
from discord.ext import commands
from discord import app_commands

import os
import random
import glob
import time
import itertools
import io
import datetime
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

import shutil

from config import (
    TOKEN,
    REPLAY_FOLDER,
    AUTO_REPLAY_MAX_AGE_SECONDS,
    DISCORD_ATTACHMENT_LIMIT_MB,
    SESSION_GAP_SECONDS,
    MY_DISCORD_ID,
    CUSTOM_MU,
    CUSTOM_SIGMA,
    TEST_DATA_FILE,
    MATCH_EMBED_STYLE,
)
from ratings import (
    model,
    load_data,
    update_ratings,
    display_mmr,
    get_player_rating,
    backup_data,
    restore_backup,
)


class FakeMember:
    """Minimal member-like object for test-mode simulated matches (has .id and .display_name)."""

    def __init__(self, id: int, display_name: str) -> None:
        self.id = id
        self.display_name = display_name


# --- SETUP --------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_message(message: discord.Message):
    # 1. Ignore the bot's own messages so it doesn't talk to itself
    if message.author == bot.user:
        return

    # 2. Check if the message was sent in a Direct Message channel
    if isinstance(message.channel, discord.DMChannel):
        # Print it nicely to your terminal
        print(f"\n📩 [DM from {message.author.display_name}]: {message.content}")

    # 3. Let command processing continue
    await bot.process_commands(message)


bot.remove_command("help")


# --- STATE MANAGEMENT ---------------------------------------------------------

current_match = {
    "active": False,
    "team_1": [],
    "team_2": [],
    "lobby_channel": None,
    "test_mode": False,
}

current_observers: set[int] = set()

bot_settings = {
    "auto_replay": True,  # Starts ON by default
    "last_lobby_time": 0.0,
}


class MatchView(discord.ui.View):
    def __init__(self, lobby_started: bool = False) -> None:
        super().__init__(timeout=None)
        self._lobby_started = lobby_started
        self._set_win_buttons_enabled(lobby_started)

    def _set_win_buttons_enabled(self, enabled: bool) -> None:
        for child in self.children:
            if getattr(child, "custom_id", None) == "btn_red":
                child.disabled = not enabled
            elif getattr(child, "custom_id", None) == "btn_blue":
                child.disabled = not enabled
            elif getattr(child, "custom_id", None) == "btn_start_lobby":
                child.disabled = enabled

    # ▶️ START LOBBY BUTTON
    @discord.ui.button(
        label="Start Lobby",
        style=discord.ButtonStyle.success,
        custom_id="btn_start_lobby",
        row=0,
    )
    async def start_lobby(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not current_match.get("active"):
            await interaction.response.send_message(
                "❌ This match is no longer active.", ephemeral=True
            )
            return
        await interaction.response.defer()

        team_1 = current_match["team_1"]
        team_2 = current_match["team_2"]
        lobby_channel = current_match["lobby_channel"]
        guild = interaction.guild

        category = discord.utils.get(guild.categories, name="Scrims")
        if not category:
            category = await guild.create_category("Scrims")

        vc_red = discord.utils.get(guild.voice_channels, name="Team Red") or await guild.create_voice_channel(
            "Team Red", category=category
        )
        vc_blue = (
            discord.utils.get(guild.voice_channels, name="Team Blue")
            or await guild.create_voice_channel("Team Blue", category=category)
        )

        # Move players to their respective team channels
        for m in team_1:
            if getattr(m, "voice", None) and m.voice:
                try:
                    await m.move_to(vc_red)
                except discord.HTTPException:
                    pass
        for m in team_2:
            if getattr(m, "voice", None) and m.voice:
                try:
                    await m.move_to(vc_blue)
                except discord.HTTPException:
                    pass

        # Move observers semi-randomly into the two team channels
        if lobby_channel:
            observers_in_lobby = [
                m for m in lobby_channel.members
                if m.id in current_observers and not m.bot
            ]
            for i, obs in enumerate(observers_in_lobby):
                if getattr(obs, "voice", None) and obs.voice:
                    target_vc = vc_red if i % 2 == 0 else vc_blue
                    try:
                        await obs.move_to(target_vc)
                    except discord.HTTPException:
                        pass

        self._lobby_started = True
        self._set_win_buttons_enabled(True)
        await interaction.message.edit(view=self)
        await interaction.followup.send(
            "✅ **Lobby started.** Everyone has been moved to team channels. Use Red Wins / Blue Wins to report the result.",
            ephemeral=True,
        )

    # 🔴 RED BUTTON
    @discord.ui.button(
        label="Red Wins",
        style=discord.ButtonStyle.danger,
        custom_id="btn_red",
        disabled=True,
        row=0,
    )
    async def red_wins(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.resolve_match(interaction, "red")

    # 🔵 BLUE BUTTON
    @discord.ui.button(
        label="Blue Wins",
        style=discord.ButtonStyle.primary,
        custom_id="btn_blue",
        disabled=True,
        row=0,
    )
    async def blue_wins(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.resolve_match(interaction, "blue")

    # ❌ CANCEL BUTTON
    @discord.ui.button(
        label="Cancel Match",
        style=discord.ButtonStyle.secondary,
        custom_id="btn_cancel",
        row=0,
    )
    async def cancel_match(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer()

        if not current_match["active"]:
            await interaction.followup.send(
                "❌ No match is currently active.", ephemeral=True
            )
            return

        current_match["active"] = False
        current_match["test_mode"] = False

        # If lobby was started, move everyone back and delete team channels
        if getattr(self, "_lobby_started", False) and current_match.get("lobby_channel"):
            lobby = current_match["lobby_channel"]
            all_players = current_match.get("team_1", []) + current_match.get("team_2", [])
            guild = interaction.guild
            
            # Find any observers that might have been pulled into the team channels
            vc_red = discord.utils.get(guild.voice_channels, name="Team Red")
            vc_blue = discord.utils.get(guild.voice_channels, name="Team Blue")
            observers = []
            if vc_red:
                observers.extend([m for m in vc_red.members if m.id in current_observers])
            if vc_blue:
                observers.extend([m for m in vc_blue.members if m.id in current_observers])
                
            for player in all_players + observers:
                if getattr(player, "voice", None) and player.voice:
                    try:
                        await player.move_to(lobby)
                    except discord.HTTPException:
                        pass
                        
            category = discord.utils.get(guild.categories, name="Scrims")
            if vc_red:
                await vc_red.delete()
            if vc_blue:
                await vc_blue.delete()
            if category and len(category.channels) == 0:
                await category.delete()

        # Disable all buttons so they turn grey
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        await interaction.followup.send("🛑 **Match cancelled.** No MMR was changed.")

    # 🧠 THE LOGIC ROUTER
    async def resolve_match(
        self, interaction: discord.Interaction, winner: str
    ) -> None:
        # 1. THE INSTANT LOCK: Do this BEFORE any 'await' network calls!
        if not current_match.get("active"):
            await interaction.response.send_message(
                "❌ This match is already processing!", ephemeral=True
            )
            return

        # 2. Instantly flip the switch so no other clicks can get past this point
        current_match["active"] = False

        # 3. Now it is safe to talk to Discord's servers
        await interaction.response.defer()

        # 4. Disable all buttons on the message visually
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        # 5. Pass it to your rating / match completion function
        await handle_victory_slash(interaction, winner)


# --- ANALYTICS HELPERS --------------------------------------------------------

def generate_session_history_text(
    guild: discord.Guild,
    data_file: str | None = None,
    name_overrides: dict[str, str] | None = None,
) -> tuple[str, str] | None:
    data = load_data(data_file=data_file)
    name_overrides = name_overrides or {}

    cutoff_time = time.time() - SESSION_GAP_SECONDS

    # 1. Grab the exact timestamps for this session's matches
    lobby_matches = sorted(
        list(
            {
                match["timestamp"]
                for p_data in data.values()
                if "history" in p_data
                for match in p_data["history"]
                if match["timestamp"] >= cutoff_time
            }
        )
    )

    if not lobby_matches:
        return None

    player_data_extracted = []

    for pid, p_data in data.items():
        if "history" not in p_data:
            continue

        history = p_data["history"]

        # Check if this player actually played in this session
        played_session = any(m["timestamp"] >= cutoff_time for m in history)
        if not played_session:
            continue

        user = guild.get_member(int(pid))
        name = name_overrides.get(
            pid, user.display_name if user else f"User {pid[-4:]}"
        )

        match_results = []
        for lobby_ts in lobby_matches:
            played_this_game = next(
                (m for m in history if m["timestamp"] == lobby_ts), None
            )
            if played_this_game:
                if played_this_game["result"] == "Win":
                    match_results.append("🟩")
                else:
                    match_results.append("🟥")
            else:
                match_results.append("⬛")

        player_data_extracted.append({"name": name, "results": match_results})

    if not player_data_extracted:
        return None

    names_column = []
    emojis_column = []
    for p in player_data_extracted:
        names_column.append(f"**{p['name']}**")
        spaced_emojis = " ".join(p["results"])
        emojis_column.append(spaced_emojis)

    if not names_column:
        return None

    return "\n".join(names_column), "\n".join(emojis_column)


def generate_session_graph(
    guild: discord.Guild,
    data_file: str | None = None,
    name_overrides: dict[str, str] | None = None,
) -> discord.File | None:
    data = load_data(data_file=data_file)
    name_overrides = name_overrides or {}

    plt.figure(figsize=(8, 4))
    ax = plt.gca()
    ax.set_facecolor("#2b2d31")
    plt.gcf().patch.set_facecolor("#2b2d31")
    ax.tick_params(colors="lightgrey")
    for spine in ax.spines.values():
        spine.set_color("#1e1f22")
    plt.grid(True, color="#1e1f22", linestyle="-", linewidth=1)

    cutoff_time = time.time() - SESSION_GAP_SECONDS

    # 1. Grab the exact timestamps for this session's matches
    lobby_matches = sorted(
        list(
            {
                match["timestamp"]
                for p_data in data.values()
                if "history" in p_data
                for match in p_data["history"]
                if match["timestamp"] >= cutoff_time
            }
        )
    )

    if not lobby_matches:
        plt.close()
        return None

    lines_plotted = 0

    for pid, p_data in data.items():
        if "history" not in p_data:
            continue

        history = p_data["history"]

        # Check if this player actually played in this session
        played_session = any(m["timestamp"] >= cutoff_time for m in history)
        if not played_session:
            continue

        # Baseline MMR: last game before session, or default if new
        past_matches = [m for m in history if m["timestamp"] < cutoff_time]
        if past_matches:
            baseline_mmr = int(past_matches[-1]["mmr"])
        else:
            baseline_mmr = int(
                model.rating(mu=CUSTOM_MU, sigma=CUSTOM_SIGMA).ordinal()
            )

        x_plot = [0]
        y_plot = [baseline_mmr]
        current_mmr = baseline_mmr

        # Loop through this session's games: 1, 2, 3...
        for i, lobby_ts in enumerate(lobby_matches, start=1):
            played_this_game = next(
                (m for m in history if m["timestamp"] == lobby_ts), None
            )
            if played_this_game:
                current_mmr = int(played_this_game["mmr"])

            x_plot.append(i)
            y_plot.append(current_mmr)

        user = guild.get_member(int(pid))
        name = name_overrides.get(
            pid, user.display_name if user else f"User {pid[-4:]}"
        )

        plt.plot(
            x_plot,
            y_plot,
            marker="o",
            linestyle="-",
            linewidth=2,
            markersize=5,
            label=name,
        )
        lines_plotted += 1

    if lines_plotted == 0:
        plt.close()
        return None

    plt.title("Lobby Session Progress", fontsize=12, fontweight="bold", color="white")
    plt.xlabel("Total Lobby Matches", fontsize=10, color="lightgrey")

    ax.xaxis.set_major_locator(plt.MaxNLocator(integer=True))
    plt.xlim(left=0)

    plt.legend(
        loc="center left",
        bbox_to_anchor=(1, 0.5),
        facecolor="#2b2d31",
        edgecolor="#1e1f22",
        labelcolor="lightgrey",
    )

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close()

    unique_filename = f"session_graph_{int(time.time())}.png"
    return discord.File(buf, filename=unique_filename)


# --- BOT EVENTS ---------------------------------------------------------------

async def clear_all_observers() -> None:
    """Clears the active observers list and removes the Observer role from everyone."""
    current_observers.clear()
    for guild in bot.guilds:
        obs_role = discord.utils.get(guild.roles, name="Observer")
        for member in guild.members:
            if obs_role and obs_role in member.roles:
                try:
                    await member.remove_roles(obs_role)
                except discord.Forbidden:
                    pass

@bot.event
async def on_ready():
    # This sets the status to "Playing /help | Matchmaking"
    activity = discord.Activity(
        type=discord.ActivityType.playing,
        name="/match /help | Matchmaking",
    )
    await bot.change_presence(activity=activity)
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Failed to sync commands: {e}")

    # Ensure no one is stuck as an observer if the bot crashed/restarted
    await clear_all_observers()

    print(f"Logged in as {bot.user.name}")


# --- MATCH RESOLUTION ---------------------------------------------------------


async def handle_victory_slash(
    interaction: discord.Interaction, team_name: str
) -> None:
    # Capture the teams BEFORE we reset the match state
    team_red = current_match["team_1"]
    team_blue = current_match["team_2"]

    if team_name == "red":
        winners = team_red
        losers = team_blue
        embed_color = discord.Color.red()
    elif team_name == "blue":
        winners = team_blue
        losers = team_red
        embed_color = discord.Color.blue()
    else:
        await interaction.followup.send("❌ Unknown team name.", ephemeral=True)
        return

    test_mode = current_match.get("test_mode", False)
    data_file = TEST_DATA_FILE if test_mode else None

    # --- 🛡️ SAVE STATE BACKUP ---
    backup_data(data_file=data_file)

    # 📸 SNAPSHOT: Grab everyone's MMR BEFORE the math happens
    old_mmrs = {
        p.id: get_player_rating(p.id, data_file=data_file).ordinal()
        for p in winners + losers
    }

    # Run the OpenSkill math and save to database
    update_ratings(winners, losers, data_file=data_file)

    # Build winner / loser lines
    win_strings = []
    for p in winners:
        new_mmr = get_player_rating(p.id, data_file=data_file).ordinal()
        diff = int(new_mmr - old_mmrs[p.id])
        win_strings.append(f"{p.display_name}: **{int(new_mmr)}** (🟩 +{diff})")

    lose_strings = []
    for p in losers:
        new_mmr = get_player_rating(p.id, data_file=data_file).ordinal()
        diff = int(new_mmr - old_mmrs[p.id])
        lose_strings.append(f"{p.display_name}: **{int(new_mmr)}** (🟥 {diff})")

    embed = discord.Embed(
        title=f"🎉 {team_name.capitalize()} Team Wins!",
        color=embed_color,
    )
    embed.add_field(name="🏆 Winners", value="\n".join(win_strings), inline=True)
    embed.add_field(name="💀 Losers", value="\n".join(lose_strings), inline=True)

    # Move players back (only real Discord members have .voice; fake players are skipped)
    lobby = current_match["lobby_channel"]
    all_players = team_red + team_blue

    if lobby:
        # Find any observers that might have been pulled into the team channels
        guild = interaction.guild
        vc_red = discord.utils.get(guild.voice_channels, name="Team Red")
        vc_blue = discord.utils.get(guild.voice_channels, name="Team Blue")
        observers = []
        if vc_red:
            observers.extend([m for m in vc_red.members if m.id in current_observers])
        if vc_blue:
            observers.extend([m for m in vc_blue.members if m.id in current_observers])
            
        for player in all_players + observers:
            if getattr(player, "voice", None) and player.voice:
                try:
                    await player.move_to(lobby)
                except discord.HTTPException:
                    pass

    # Delete channels
    guild = interaction.guild
    vc_red = discord.utils.get(guild.voice_channels, name="Team Red")
    vc_blue = discord.utils.get(guild.voice_channels, name="Team Blue")
    category = discord.utils.get(guild.categories, name="Scrims")

    if vc_red:
        await vc_red.delete()
    if vc_blue:
        await vc_blue.delete()
    if category and len(category.channels) == 0:
        await category.delete()

    # Reset state
    test_mode = current_match.get("test_mode", False)
    current_match["active"] = False
    current_match["team_1"] = []
    current_match["team_2"] = []
    current_match["lobby_channel"] = None
    current_match["test_mode"] = False

    # Generate session visual (graph or history text)
    name_overrides = None
    if test_mode:
        name_overrides = {
            str(p.id): p.display_name for p in team_red + team_blue
        }
        
    if MATCH_EMBED_STYLE == "history":
        history_data = generate_session_history_text(
            guild,
            data_file=TEST_DATA_FILE if test_mode else None,
            name_overrides=name_overrides,
        )
        if history_data:
            names_col, emojis_col = history_data
            # Add an invisible spacer to force the columns below onto a new row
            embed.add_field(name="\u200b", value="\u200b", inline=False)
            embed.add_field(name="Player", value=names_col, inline=True)
            embed.add_field(name="Recent Matches", value=emojis_col, inline=True)
        await interaction.followup.send(embed=embed)
    else:
        graph_file = generate_session_graph(
            guild,
            data_file=TEST_DATA_FILE if test_mode else None,
            name_overrides=name_overrides,
        )
        if graph_file:
            embed.set_image(url=f"attachment://{graph_file.filename}")
            await interaction.followup.send(embed=embed, file=graph_file)
        else:
            await interaction.followup.send(embed=embed)

    # --- 📼 AUTO-REPLAY INTEGRATION (skip in test mode) ---
    if not test_mode and bot_settings.get("auto_replay"):
        replay_channel = discord.utils.get(guild.text_channels, name="replayz")
        if not replay_channel:
            category = discord.utils.get(guild.categories, name="Scrims")
            replay_channel = await guild.create_text_channel(
                "replayz", category=category
            )

        status_msg = await replay_channel.send(
            "⏳ *Attempting to auto-grab the latest replay...*"
        )

        if not REPLAY_FOLDER or not os.path.exists(REPLAY_FOLDER):
            await status_msg.edit(
                content=f"❌ Could not find the folder: `{REPLAY_FOLDER}`. Check your path!"
            )
            return

        list_of_files = glob.glob(f"{REPLAY_FOLDER}/*")
        if not list_of_files:
            await status_msg.edit(
                content="❌ The replay folder is completely empty!"
            )
            return

        latest_file = max(list_of_files, key=os.path.getmtime)
        file_name = os.path.basename(latest_file)

        file_age_seconds = time.time() - os.path.getmtime(latest_file)
        file_max_age_seconds = AUTO_REPLAY_MAX_AGE_SECONDS

        if file_age_seconds > file_max_age_seconds:
            minutes_old = int(file_age_seconds // 60)
            await status_msg.edit(
                content=(
                    f"❌ **Upload aborted.** The newest file (`{file_name}`) is "
                    f"**{minutes_old} minutes old**. Replays must be under "
                    f"{file_max_age_seconds // 60} minutes old to auto-upload!"
                )
            )
            return

        file_size_mb = os.path.getsize(latest_file) / (1024 * 1024)
        if file_size_mb > DISCORD_ATTACHMENT_LIMIT_MB:
            await status_msg.edit(
                content=(
                    f"❌ The newest replay (`{file_name}`) is **{file_size_mb:.1f}MB**, "
                    f"exceeding Discord's {DISCORD_ATTACHMENT_LIMIT_MB}MB limit!"
                )
            )
            return

        t1_names = (
            "\n".join(
                f"{m.display_name} ({display_mmr(m.id)})" for m in team_red
            )
            or "None"
        )
        t2_names = (
            "\n".join(
                f"{m.display_name} ({display_mmr(m.id)})" for m in team_blue
            )
            or "None"
        )

        embed_replay = discord.Embed(
            title="📼 Match Replay",
            description=(
                f"**Match Result:** ||Team {team_name.capitalize()} won!||\n"
                f"**File:** `{file_name}`"
            ),
            color=discord.Color.dark_grey(),
        )
        embed_replay.add_field(name="🔴 Team Red", value=t1_names, inline=True)
        embed_replay.add_field(name="🔵 Team Blue", value=t2_names, inline=True)

        try:
            discord_file = discord.File(latest_file)
            await replay_channel.send(embed=embed_replay, file=discord_file)
            await status_msg.delete()
            # Fixed: use interaction.followup instead of undefined ctx
            await interaction.followup.send(
                f"✅ Replay successfully auto-saved to {replay_channel.mention}!"
            )
        except discord.HTTPException:
            await status_msg.edit(
                content="❌ **Upload failed.** Discord rejected the file."
            )


# --- COMMANDS -----------------------------------------------------------------


@bot.tree.command(
    name="ladder", description="🏆 View the server's MMR standings 🏆"
)
async def ladder(interaction: discord.Interaction):
    data = load_data()

    active_players = []
    for pid, p_data in data.items():
        if "history" in p_data and len(p_data["history"]) > 0:
            current_mmr = int(p_data["history"][-1]["mmr"])
            active_players.append((pid, current_mmr))

    if not active_players:
        await interaction.response.send_message(
            "❌ No matches have been played yet!", ephemeral=True
        )
        return

    active_players.sort(key=lambda x: x[1], reverse=True)

    players_column = "\u200b\n"
    mmr_column = "\u200b\n"

    for rank, (pid, mmr) in enumerate(active_players, 1):
        user = interaction.guild.get_member(int(pid))
        name = user.display_name if user else f"User {pid[-4:]}"

        if rank == 1:
            medal = "🥇"
        elif rank == 2:
            medal = "🥈"
        elif rank == 3:
            medal = "🥉"
        else:
            medal = f"-{rank}- "

        players_column += f"{medal} **{name}**\n\n"
        mmr_column += f"**{mmr}** MMR\n\n"

    embed = discord.Embed(
        title="🏆 LADDER STANDINGS 🏆",
        description="──────────────────────────────",
        color=discord.Color.gold(),
    )

    embed.add_field(name="Player", value=players_column, inline=True)
    embed.add_field(name="Rating", value=mmr_column, inline=True)

    await interaction.response.send_message(embed=embed)


async def _run_match_lobby(
    interaction: discord.Interaction,
    players: list,
    lobby_channel: discord.VoiceChannel,
    test_mode: bool,
    balanced: bool,
) -> None:
    """Shared logic: build teams, store state, create channels, move players, send embed + view."""
    data_file_for_match = TEST_DATA_FILE if test_mode else None

    if balanced:
        match_type_title = "⚖️ Balanced Match Started!"
        best_diff = 1.0
        best_t1, best_t2 = [], []
        best_prob = 0.5
        team_size = len(players) // 2

        for t1_combo in itertools.combinations(players, team_size):
            t1 = list(t1_combo)
            t2 = [p for p in players if p not in t1]

            t1_ratings = [
                get_player_rating(p.id, data_file=data_file_for_match) for p in t1
            ]
            t2_ratings = [
                get_player_rating(p.id, data_file=data_file_for_match) for p in t2
            ]

            prob = model.predict_win([t1_ratings, t2_ratings])[0]
            diff = abs(prob - 0.5)

            if diff < best_diff:
                best_diff = diff
                best_t1 = t1
                best_t2 = t2
                best_prob = prob

        team_1, team_2 = best_t1, best_t2
        win_percentage = round(best_prob * 100, 1)
    else:
        match_type_title = "🎲 Random Match Started!"
        random.shuffle(players)
        mid = len(players) // 2
        team_1 = players[:mid]
        team_2 = players[mid:]

        t1_ratings = [
            get_player_rating(p.id, data_file=data_file_for_match) for p in team_1
        ]
        t2_ratings = [
            get_player_rating(p.id, data_file=data_file_for_match) for p in team_2
        ]
        win_chance = model.predict_win([t1_ratings, t2_ratings])[0]
        win_percentage = round(win_chance * 100, 1)

    current_match["active"] = True
    current_match["team_1"] = team_1
    current_match["team_2"] = team_2
    current_match["lobby_channel"] = lobby_channel
    current_match["test_mode"] = test_mode

    desc = (
        f"Teams from **{lobby_channel.name}**. "
        "Click **Start Lobby** to create team voice channels and move everyone. "
        "Then use Red Wins / Blue Wins to report the result."
    )
    if test_mode:
        desc += f"\n\n🧪 **Test:** MMR updates go to `{TEST_DATA_FILE}`."

    embed = discord.Embed(
        title=match_type_title,
        description=desc,
        color=discord.Color.blurple(),
    )

    t1_names = "\n".join(
        f"**{p.display_name}** ({int(get_player_rating(p.id, data_file=data_file_for_match).ordinal())})"
        for p in team_1
    )
    t2_names = "\n".join(
        f"**{p.display_name}** ({int(get_player_rating(p.id, data_file=data_file_for_match).ordinal())})"
        for p in team_2
    )

    embed.add_field(name="🔴 Team Red", value=t1_names, inline=True)
    embed.add_field(name="🔵 Team Blue", value=t2_names, inline=True)
    embed.add_field(
        name="📊 Match Odds",
        value=f"Team Red has a **{win_percentage}%** chance to win.",
        inline=False,
    )
    footer = "First click Start Lobby, then Red or Blue to report the winner. Cancel to abort."
    if test_mode:
        footer += " (Test data only.)"
    embed.set_footer(text=footer)

    view = MatchView(lobby_started=False)
    await interaction.followup.send(embed=embed, view=view)


@bot.tree.command(
    name="match",
    description="🎲 Start a new match from your current voice channel.",
)
@app_commands.describe(
    balanced="Create balanced teams based on MMR? (Default: False)",
)
async def match(interaction: discord.Interaction, balanced: bool = False):
    await interaction.response.defer()

    # If it's been over an hour since the last match started, clear all observers first
    current_time = time.time()
    if current_time - bot_settings["last_lobby_time"] > 3600:
        await clear_all_observers()
        
    bot_settings["last_lobby_time"] = current_time

    if current_match["active"]:
        await interaction.followup.send(
            "⚠️ Match already in progress! Use the win buttons first."
        )
        return

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send(
            "❌ You must be sitting in a voice channel to start a match!"
        )
        return

    lobby_channel = interaction.user.voice.channel
    players = [
        member
        for member in lobby_channel.members
        if not member.bot and member.id not in current_observers
    ]

    if len(players) < 2:
        await interaction.followup.send(
            f"❌ Not enough players in **{lobby_channel.name}**! Need at least 2."
        )
        return

    await _run_match_lobby(
        interaction, players, lobby_channel, test_mode=False, balanced=balanced
    )


@bot.tree.command(
    name="obs",
    description="👀 Toggles your observer status. Observers are excluded from matchmaking.",
)
async def obs(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or not interaction.guild:
        await interaction.response.send_message("❌ This command must be used in a server.", ephemeral=True)
        return
        
    obs_role = discord.utils.get(interaction.guild.roles, name="Observer")
    if not obs_role:
        try:
            obs_role = await interaction.guild.create_role(name="Observer", reason="Created by bot for observer tracking")
        except discord.Forbidden:
            pass
    
    if member.id in current_observers:
        # User is observing, turn it OFF
        current_observers.discard(member.id)
        
        # Remove the Observer role
        if obs_role and obs_role in member.roles:
            try:
                await member.remove_roles(obs_role)
            except discord.Forbidden:
                pass
                
        await interaction.response.send_message(
            f"⚔️ **{member.display_name}** is no longer observing ⚔️\n*(They will be included in matches)*"
        )
    else:
        # User is playing, turn it ON
        current_observers.add(member.id)
        
        # Add the Observer role
        if obs_role:
            try:
                await member.add_roles(obs_role)
            except discord.Forbidden:
                pass
                
        await interaction.response.send_message(
            f"👁️ **{member.display_name}** is observing 👁️\n*(They will be excluded from matchmaking)*"
        )


@bot.tree.command(
    name="test",
    description="🧪 Start a test match (you + 3 fake players). Uses test data file only.",
)
async def test(interaction: discord.Interaction):
    await interaction.response.defer()

    if current_match["active"]:
        await interaction.followup.send(
            "⚠️ Match already in progress! Use the win buttons first."
        )
        return

    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.followup.send(
            "❌ You must be sitting in a voice channel to start a test match!"
        )
        return

    lobby_channel = interaction.user.voice.channel
    
    players = []
    if interaction.user.id not in current_observers:
        players.append(interaction.user)
    else:
        players.append(FakeMember(9004, "TestPlayer4"))
        
    players.extend([
        FakeMember(9001, "TestPlayer1"),
        FakeMember(9002, "TestPlayer2"),
        FakeMember(9003, "TestPlayer3"),
    ])

    await _run_match_lobby(
        interaction, players, lobby_channel, test_mode=True, balanced=False
    )


@bot.tree.command(
    name="stats", description="📊 Check a player's current MMR and Win Rate."
)
@app_commands.describe(member="The player to check (leave blank for yourself)")
async def stats(
    interaction: discord.Interaction, member: discord.Member | None = None
):
    member = member or interaction.user

    data = load_data()
    pid = str(member.id)

    if pid not in data or "history" not in data[pid] or len(data[pid]["history"]) == 0:
        await interaction.response.send_message(
            f"❌ **{member.display_name}** hasn't played any matches yet!",
            ephemeral=True,
        )
        return

    history = data[pid]["history"]
    current_mmr = int(history[-1]["mmr"])

    wins = sum(1 for match in history if match["result"] == "Win")
    losses = len(history) - wins
    win_rate = round((wins / len(history)) * 100, 1)

    embed = discord.Embed(
        title=f"📊 Stats for {member.display_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Current MMR", value=f"**{current_mmr}**", inline=False)
    embed.add_field(
        name="Record",
        value=f"{wins}W - {losses}L ({win_rate}%)",
        inline=False,
    )
    embed.add_field(
        name="Total Matches", value=f"{len(history)}", inline=False
    )

    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.command()
async def replay(ctx: commands.Context, state: str | None = None):
    if state is None:
        current_state = "ON" if bot_settings["auto_replay"] else "OFF"
        await ctx.send(
            f"⚙️ Auto-Replay is currently **{current_state}**.\n"
            "Type `!replay on` or `!replay off` to change it."
        )
        return

    state = state.lower()
    if state == "on":
        bot_settings["auto_replay"] = True
        await ctx.send(
            "✅ **Auto-Replay ENABLED.** The bot will now automatically upload the "
            "newest replay to the `#replayz` channel when a match ends."
        )
    elif state == "off":
        bot_settings["auto_replay"] = False
        await ctx.send("🛑 **Auto-Replay DISABLED.**")
    else:
        await ctx.send("❌ Invalid option. Use `!replay on` or `!replay off`.")


@bot.tree.command(
    name="graph", description="📈 Graph a player's MMR progression."
)
@app_commands.describe(member="The player to graph (leave blank for yourself)")
async def graph(
    interaction: discord.Interaction, member: discord.Member | None = None
):
    await interaction.response.defer(ephemeral=True)

    data = load_data()
    member = member or interaction.user
    pid = str(member.id)

    if pid not in data or "history" not in data[pid] or len(data[pid]["history"]) < 1:
        await interaction.followup.send(
            f"❌ **{member.display_name}** doesn't have enough match history to graph yet!"  # noqa: E501
        )
        return

    plt.figure(figsize=(10, 6))
    ax = plt.gca()
    ax.set_facecolor("#2b2d31")
    plt.gcf().patch.set_facecolor("#2b2d31")
    ax.tick_params(colors="lightgrey")
    for spine in ax.spines.values():
        spine.set_color("#1e1f22")
    plt.grid(True, color="#1e1f22", linestyle="-", linewidth=1)

    history = data[pid]["history"]

    baseline_mmr = int(model.rating(mu=CUSTOM_MU, sigma=CUSTOM_SIGMA).ordinal())
    y_mmr = [baseline_mmr] + [int(entry["mmr"]) for entry in history]
    x_matches = list(range(0, len(history) + 1))

    plt.plot(
        x_matches,
        y_mmr,
        marker="o",
        linestyle="-",
        color="dodgerblue",
        linewidth=2,
        markersize=6,
    )
    plt.title(
        f"MMR History for {member.display_name}",
        fontsize=14,
        fontweight="bold",
        color="white",
    )
    plt.xlabel("Matches Played", fontsize=12, color="lightgrey")
    plt.ylabel("MMR (Integer Rating)", fontsize=12, color="lightgrey")

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close()

    file = discord.File(buf, filename="graph.png")
    embed = discord.Embed(
        title=f"📈 {member.display_name}'s MMR Progress",
        color=discord.Color.blue(),
    )
    embed.set_image(url="attachment://graph.png")

    await interaction.followup.send(embed=embed, file=file)


@bot.tree.command(
    name="graphall",
    description="🌍 Graph the entire server's MMR progression by session.",
)
async def graphall(interaction: discord.Interaction):
    await interaction.response.defer()

    data = load_data()

    plt.figure(figsize=(10, 6))
    ax = plt.gca()
    ax.set_facecolor("#2b2d31")
    plt.gcf().patch.set_facecolor("#2b2d31")
    ax.tick_params(colors="lightgrey")
    for spine in ax.spines.values():
        spine.set_color("#1e1f22")
    plt.grid(True, color="#1e1f22", linestyle="-", linewidth=1)

    lines_plotted = 0

    for pid, p_data in data.items():
        if "history" not in p_data or len(p_data["history"]) == 0:
            continue

        history = p_data["history"]

        session_data = []
        current_time = history[0]["timestamp"]
        current_mmr = int(history[0]["mmr"])

        for entry in history[1:]:
            if entry["timestamp"] - current_time > SESSION_GAP_SECONDS:
                session_data.append({"time": current_time, "mmr": current_mmr})

            current_time = entry["timestamp"]
            current_mmr = int(entry["mmr"])

        session_data.append({"time": current_time, "mmr": current_mmr})

        x_times = [datetime.datetime.fromtimestamp(s["time"]) for s in session_data]
        y_mmr = [s["mmr"] for s in session_data]

        x_times.append(datetime.datetime.now())
        y_mmr.append(y_mmr[-1])

        user = interaction.guild.get_member(int(pid))
        name = user.display_name if user else f"User {pid[-4:]}"

        plt.plot(
            x_times,
            y_mmr,
            marker="o",
            linestyle="-",
            linewidth=2,
            markersize=5,
            label=name,
        )
        lines_plotted += 1

    if lines_plotted == 0:
        await interaction.followup.send("❌ No matches have been played yet!")
        plt.close()
        return

    plt.title(
        "📈 MMR Session Aggregated",
        fontsize=14,
        fontweight="bold",
        color="white",
    )
    plt.xlabel("Date", fontsize=12, color="lightgrey")
    plt.ylabel("MMR (Integer Rating)", fontsize=12, color="lightgrey")

    ax.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
    plt.xticks(rotation=45)
    plt.legend(
        loc="best",
        facecolor="#2b2d31",
        edgecolor="#1e1f22",
        labelcolor="lightgrey",
    )

    buf = io.BytesIO()
    plt.savefig(buf, format="png", bbox_inches="tight")
    buf.seek(0)
    plt.close()

    file = discord.File(buf, filename="race.png")
    embed = discord.Embed(
        title="📈 MMR Graph",
        description="Showing final MMR at the end of each play session.",
        color=discord.Color.blue(),
    )
    embed.set_image(url="attachment://race.png")

    await interaction.followup.send(embed=embed, file=file)


@bot.tree.command(
    name="history", description="🕒 View a player's recent match history."
)
@app_commands.describe(
    member="The player whose history you want to see (leave blank for yourself)"
)
async def history(
    interaction: discord.Interaction, member: discord.Member | None = None
):
    member = member or interaction.user

    data = load_data()
    pid = str(member.id)

    if pid not in data or "history" not in data[pid] or len(data[pid]["history"]) == 0:
        await interaction.response.send_message(
            f"❌ **{member.display_name}** hasn't played any matches yet!",
            ephemeral=True,
        )
        return

    history_list = data[pid]["history"]
    current_time = int(time.time())
    cutoff_time = current_time - SESSION_GAP_SECONDS
    recent_matches: list[str] = []

    for i, match in enumerate(history_list):
        if match["timestamp"] >= cutoff_time:
            current_mmr = int(match["mmr"])
            prev_mmr = (
                int(history_list[i - 1]["mmr"])
                if i > 0
                else int(model.rating(mu=CUSTOM_MU, sigma=CUSTOM_SIGMA).ordinal())
            )

            diff = current_mmr - prev_mmr
            sign = "+" if diff >= 0 else ""

            result_text = (
                "🟩 **WIN** " if match["result"] == "Win" else "🟥 **LOSS**"
            )
            time_tag = f"<t:{match['timestamp']}:R>"

            row = (
                f"{result_text} | **{sign}{diff}** "
                f"*(Total: {current_mmr})* •  {time_tag}"
            )
            recent_matches.append(row)

    if not recent_matches:
        await interaction.response.send_message(
            f"🕰️ **{member.display_name}** hasn't played any matches in the current session.",  # noqa: E501
            ephemeral=True,
        )
        return

    recent_matches.reverse()

    if len(recent_matches) > 15:
        display_text = (
            "\n".join(recent_matches[:15])
            + f"\n\n*...and {len(recent_matches) - 15} older matches.*"
        )
    else:
        display_text = "\n".join(recent_matches)

    embed = discord.Embed(
        title="🕒 Session Recap",
        description=display_text,
        color=discord.Color.gold(),
    )
    if member.avatar:
        embed.set_author(name=member.display_name, icon_url=member.avatar.url)
    else:
        embed.set_author(name=member.display_name)

    embed.set_footer(text=f"Total matches this session: {len(recent_matches)}")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="undo",
    description="⏪ Revert the last match and restore everyone's MMR.",
)
async def undo(interaction: discord.Interaction):
    restored = restore_backup()
    if not restored:
        await interaction.response.send_message(
            "❌ There is no previous match to undo, or a backup hasn't been created yet!",  # noqa: E501
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="⏪ Match Undone!",
        description=(
            "The last match results have been completely wiped. Everyone's MMR, "
            "ladder rank, and graph history has been perfectly restored to what it "
            "was right before the game."
        ),
        color=discord.Color.orange(),
    )
    embed.set_footer(
        text="You will need to run /match again to re-host the lobby."
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="dm", description="🕵️‍♂️ Secretly send a DM to a user."
)
async def dm(
    interaction: discord.Interaction, member: discord.Member, message: str
):
    if interaction.user.id != MY_DISCORD_ID:
        await interaction.response.send_message(
            "❌ You do not have permission to use this.", ephemeral=True
        )
        return

    try:
        await member.send(message)
        print(f"📤 [Sent to {member.display_name}]: {message}")

        await interaction.response.send_message(
            f"✅ Message secretly delivered to {member.display_name}.",
            ephemeral=True,
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"❌ Cannot send message to {member.display_name}. DMs are closed!",
            ephemeral=True,
        )


@bot.tree.command(
    name="help", description="🆘 Show the list of me.Tică+ commands."
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 me.Tică+ Command Guide",
        description=(
            "Here is everything I can do! *Note: Match reporting is now fully "
            "automated via clickable buttons.*\n"
        ),
        color=discord.Color.blurple(),
    )

    embed.add_field(
        name="🎮 Matchmaking",
        value=(
            "**`/match`** - Pulls players from your voice channel and builds teams.\n"
            "↳ *Optional: Set `balanced=True` to auto-balance teams based on MMR.*\n"
            "**`/test`** - Start a test match (you + 3 fake players). Uses `test_player_data.json` only.\n"
            "** Click 🔴Team Red or 🔵Team Blue to close active lobby, report match "
            "winner and update everyone's MMR.\n"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 Stats & Leaderboards",
        value=(
            "**`/stats`** - Check your current MMR, Record, and Win Rate.\n"
            "↳ *Optional: Tag a `member` to scout their stats.*\n"
            "**`/ladder`** - 🏆 View the server's official MMR standings.\n"
        ),
        inline=False,
    )

    embed.add_field(
        name="📈 Analytics",
        value=(
            "**`/graph`** - Generate a visual chart of your personal MMR history.\n"
            "**`/graphall`** - 🌍 Graph everyone's MMR progression timeline.\n"
        ),
        inline=False,
    )

    embed.set_footer(
        text=(
            "Pro Tip: You don't need commands to report a win anymore. "
            "Just click the Red or Blue buttons under the /match post!"
        )
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set. Make sure it is present in your .env file."
    )

bot.run(TOKEN)

