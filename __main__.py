"""
MMO Maid Tic-Tac-Toe — plugin entry point.

This module owns:
  - /tictactoe slash command (root menu with buttons)
  - Button handlers (Play, My Stats, Leaderboard, How to play)
  - Dashboard data handlers (iframe URL, overview, leaderboard, settings)
  - Scheduled announce sweep for big matches

The plugin never holds game state — every button or dashboard call goes
through gs_client.GameServerClient, which signs the request with the
shared HMAC secret. The game server is the single source of truth for
boards, ELO, and match history.
"""
from __future__ import annotations

import time
import traceback
from typing import Any, Dict, List

from mmo_maid_sdk import (
    Plugin,
    Context,
    ActionRow,
    Button,
)

from gs_client import GameServerClient, GameServerError

plugin = Plugin()

# ── Helpers ────────────────────────────────────────────────────────────────


def _client(ctx: Context) -> GameServerClient:
    """Build a fresh game-server client bound to this ctx."""
    return GameServerClient(ctx)


def _viewer_id(params: dict) -> str:
    """Extract the viewing dashboard user's Discord ID from RPC params.

    The platform passes the viewer's user_id under one of three field names
    depending on which renderer version is in use. We try all three and fall
    back to empty string (which the game server treats as a tokenless
    read-only request).
    """
    for key in ("user_id", "caller_user_id", "viewer_user_id"):
        v = params.get(key)
        if v:
            return str(v)
    return ""


def _viewer_username(params: dict) -> str:
    """Extract the viewing user's display name if available."""
    for key in ("username", "caller_username", "viewer_username", "display_name"):
        v = params.get(key)
        if v:
            return str(v)
    return ""


def _format_elo_delta(delta: int) -> str:
    if delta > 0:
        return f"+{delta}"
    return str(delta)


# ── Slash command ──────────────────────────────────────────────────────────


@plugin.on_slash_command("tictactoe")
def handle_tictactoe(ctx: Context, event: dict) -> None:
    """Root slash command — opens the menu in Discord with action buttons."""
    user = event.get("user") or {}
    username = user.get("username") or user.get("global_name") or "Player"

    # Try to grab the user's current ELO so we can show it inline.
    headline = f"**{username}**, welcome to MMO Maid Tic-Tac-Toe."
    try:
        stats = _client(ctx).stats(user_id=str(user.get("id") or ""))
        elo = int(stats.get("elo", 1200))
        record = (
            f"Record: **{stats.get('wins', 0)}W / "
            f"{stats.get('losses', 0)}L / {stats.get('draws', 0)}D**"
        )
        headline += f"\nCurrent ELO: **{elo}**  ·  {record}"
    except GameServerError as e:
        ctx.log(f"stats fetch failed: {e}", level="warning")
        headline += "\n_Stats temporarily unavailable._"

    body = (
        f"{headline}\n\n"
        "Tap **Play** to open the live lobby in your dashboard. Create your "
        "own table or jump into one already waiting for a challenger."
    )

    ctx.interaction.respond(
        content=body,
        components=[
            ActionRow(
                Button("Play",         "ttt_btn_play",         style="success", emoji="🎮"),
                Button("My Stats",     "ttt_btn_stats",        style="secondary", emoji="📊"),
                Button("Leaderboard",  "ttt_btn_leaderboard",  style="secondary", emoji="🏆"),
                Button("How to Play",  "ttt_btn_help",         style="secondary", emoji="❔"),
            ),
        ],
        ephemeral=True,
    )


# ── Button handlers ────────────────────────────────────────────────────────


@plugin.on_component("ttt_btn_play")
def handle_play(ctx: Context, event: dict) -> None:
    """Send the user a personal signed link into the lobby."""
    user = event.get("user") or {}
    user_id = str(user.get("id") or "")
    username = user.get("username") or user.get("global_name") or "Player"

    if not user_id:
        ctx.interaction.respond(
            content="Couldn't identify your Discord account. Try again in a moment.",
            ephemeral=True,
        )
        return

    try:
        info = _client(ctx).lobby_url(
            user_id=user_id,
            username=username,
            guild_id=ctx.server_id,
        )
        url = info.get("url") or ""
    except GameServerError as e:
        ctx.log(f"lobby_url failed: {e}", level="error")
        ctx.interaction.respond(
            content="The tic-tac-toe server isn't reachable right now. Try again shortly.",
            ephemeral=True,
        )
        return

    if not url:
        ctx.interaction.respond(
            content="The server didn't return a play URL. Tell a server admin.",
            ephemeral=True,
        )
        return

    ctx.interaction.respond(
        content=(
            "Your private match link is ready below. It's tied to your Discord "
            "account and expires in **15 minutes** — open it now."
        ),
        components=[
            ActionRow(
                Button("Open Lobby", style="link", url=url, emoji="🎮"),
            ),
        ],
        ephemeral=True,
    )


@plugin.on_component("ttt_btn_stats")
def handle_stats(ctx: Context, event: dict) -> None:
    user = event.get("user") or {}
    user_id = str(user.get("id") or "")
    username = user.get("username") or user.get("global_name") or "Player"

    try:
        stats = _client(ctx).stats(user_id=user_id)
    except GameServerError as e:
        ctx.log(f"stats failed: {e}", level="error")
        ctx.interaction.respond(
            content="Stats are temporarily unavailable.",
            ephemeral=True,
        )
        return

    elo = int(stats.get("elo", 1200))
    wins = int(stats.get("wins", 0))
    losses = int(stats.get("losses", 0))
    draws = int(stats.get("draws", 0))
    streak = int(stats.get("current_streak", 0))
    best = int(stats.get("best_streak", 0))
    total = wins + losses + draws
    win_pct = (wins / total * 100) if total else 0.0

    streak_label = "🔥 on a roll" if streak >= 3 else ("🔥" if streak >= 1 else "")
    body = (
        f"**{username}** — Cross-Server Stats\n"
        f"```\n"
        f"ELO              {elo}\n"
        f"Record (W/L/D)   {wins}/{losses}/{draws}   ({win_pct:.1f}%)\n"
        f"Current streak   {streak} {streak_label}\n"
        f"Best streak      {best}\n"
        f"Games played     {total}\n"
        f"```"
    )
    ctx.interaction.respond(content=body, ephemeral=True)


@plugin.on_component("ttt_btn_leaderboard")
def handle_leaderboard(ctx: Context, event: dict) -> None:
    try:
        result = _client(ctx).leaderboard(limit=10)
    except GameServerError as e:
        ctx.log(f"leaderboard failed: {e}", level="error")
        ctx.interaction.respond(
            content="The leaderboard is temporarily unavailable.",
            ephemeral=True,
        )
        return

    rows = result.get("rows") or []
    if not rows:
        ctx.interaction.respond(
            content="No games have been played yet — be the first!",
            ephemeral=True,
        )
        return

    lines = ["**🏆 Tic-Tac-Toe — Top 10 (Cross-Server)**", "```"]
    lines.append(f"{'#':<3} {'Player':<22} {'ELO':>5} {'W-L-D':>10}")
    lines.append("-" * 44)
    for r in rows[:10]:
        rank = r.get("rank", "?")
        name = (r.get("username") or "Unknown")[:22]
        elo = r.get("elo", 1200)
        record = f"{r.get('wins', 0)}-{r.get('losses', 0)}-{r.get('draws', 0)}"
        lines.append(f"{rank:<3} {name:<22} {elo:>5} {record:>10}")
    lines.append("```")
    ctx.interaction.respond(content="\n".join(lines), ephemeral=True)


@plugin.on_component("ttt_btn_help")
def handle_help(ctx: Context, event: dict) -> None:
    body = (
        "**How to Play**\n"
        "1. Tap **Play** — a private link opens the lobby in your dashboard\n"
        "2. **Create a table** (you'll be X) or **Join** an open table (you'll be O)\n"
        "3. Tap an empty cell on your turn — three in a row wins\n"
        "4. You have **30 seconds** per move; running out forfeits the match\n\n"
        "**ELO**\n"
        "Everyone starts at **1200**. Wins earn ELO, losses cost ELO, and the "
        "amount depends on the rating gap — beating a higher-rated opponent "
        "earns more. Draws nudge ratings toward each other. Stats are global "
        "across every server with the plugin installed."
    )
    ctx.interaction.respond(content=body, ephemeral=True)


# ── Dashboard handlers ─────────────────────────────────────────────────────


@plugin.on_dashboard("get_play_iframe_url")
def dash_get_play_iframe_url(ctx: Context, params: dict) -> Dict[str, Any]:
    """Mint a personalised iframe URL for the dashboard Play page.

    Falls back to the public lobby (read-only) if the dashboard renderer
    didn't pass the viewer's Discord user_id.
    """
    user_id = _viewer_id(params)
    username = _viewer_username(params)

    if not user_id:
        ctx.log(
            "dashboard.get_play_iframe_url called without a viewer user_id; "
            f"params keys: {list(params.keys())}",
            level="warning",
        )
        # Return the public lobby URL so the iframe still loads (read-only).
        base = ctx.kv.get("settings:game_server_url") or "https://tictactoe.mmomaid.cloud"
        return {"url": str(base).rstrip("/") + "/"}

    try:
        info = _client(ctx).lobby_url(
            user_id=user_id, username=username, guild_id=ctx.server_id,
        )
        return {"url": info.get("url", "")}
    except GameServerError as e:
        ctx.log(f"dashboard lobby_url failed: {e}", level="error")
        return {"url": "", "error": "Game server unreachable"}


@plugin.on_dashboard("get_open_tables_count")
def dash_open_tables(ctx: Context, params: dict) -> Dict[str, Any]:
    try:
        m = _client(ctx).metrics()
        return {"value": int(m.get("open_tables", 0))}
    except GameServerError:
        return {"value": 0}


@plugin.on_dashboard("get_active_players_count")
def dash_active_players(ctx: Context, params: dict) -> Dict[str, Any]:
    try:
        m = _client(ctx).metrics()
        return {"value": int(m.get("active_players", 0))}
    except GameServerError:
        return {"value": 0}


@plugin.on_dashboard("get_games_today")
def dash_games_today(ctx: Context, params: dict) -> Dict[str, Any]:
    try:
        m = _client(ctx).metrics()
        return {"value": int(m.get("games_24h", 0))}
    except GameServerError:
        return {"value": 0}


@plugin.on_dashboard("get_games_trend")
def dash_games_trend(ctx: Context, params: dict) -> Dict[str, Any]:
    try:
        m = _client(ctx).metrics()
        trend = m.get("trend_7d") or {}
        labels = trend.get("labels") or []
        data = trend.get("data") or []
    except GameServerError:
        labels, data = [], []
    return {
        "labels": labels,
        "series": [{"name": "Games", "data": data}],
    }


@plugin.on_dashboard("get_leaderboard")
def dash_leaderboard(ctx: Context, params: dict) -> Dict[str, Any]:
    limit = int(params.get("limit") or 25)
    try:
        result = _client(ctx).leaderboard(limit=limit)
    except GameServerError:
        return {"rows": [], "total": 0}

    rows = result.get("rows") or []
    # Format win_pct as a percentage string for the table widget.
    formatted = []
    for r in rows:
        wins = int(r.get("wins", 0))
        losses = int(r.get("losses", 0))
        draws = int(r.get("draws", 0))
        total = wins + losses + draws
        pct = (wins / total * 100) if total else 0.0
        formatted.append({
            "rank": r.get("rank"),
            "username": r.get("username") or "Unknown",
            "elo": int(r.get("elo", 1200)),
            "wins": wins,
            "losses": losses,
            "draws": draws,
            "win_pct": f"{pct:.1f}%",
            "best_streak": int(r.get("best_streak", 0)),
        })
    return {"rows": formatted, "total": len(formatted)}


@plugin.on_dashboard("get_settings")
def dash_get_settings(ctx: Context, params: dict) -> Dict[str, Any]:
    return {
        "values": {
            "game_server_url": ctx.kv.get("settings:game_server_url") or "https://tictactoe.mmomaid.cloud",
            # Never echo the secret back to the client.
            "shared_secret": "",
            "announce_channel_id": ctx.kv.get("settings:announce_channel_id") or "",
            "turn_timer_seconds": int(ctx.kv.get("settings:turn_timer_seconds") or 30),
        },
    }


@plugin.on_dashboard("save_settings")
def dash_save_settings(ctx: Context, params: dict) -> Dict[str, Any]:
    values = params.get("values") or params
    if not isinstance(values, dict):
        return {"ok": False, "error": "invalid payload"}

    if "game_server_url" in values:
        url = str(values["game_server_url"] or "").strip()
        if url:
            ctx.kv.set("settings:game_server_url", url)
    if "shared_secret" in values:
        sec = str(values["shared_secret"] or "")
        # Only overwrite if a non-empty value was supplied (blank = keep current).
        if sec:
            ctx.kv.set("settings:shared_secret", sec)
    if "announce_channel_id" in values:
        ctx.kv.set("settings:announce_channel_id", str(values["announce_channel_id"] or ""))
    if "turn_timer_seconds" in values:
        try:
            t = int(values["turn_timer_seconds"])
            if 10 <= t <= 300:
                ctx.kv.set("settings:turn_timer_seconds", t)
        except (ValueError, TypeError):
            pass
    return {"ok": True}


# ── Lifecycle hooks ────────────────────────────────────────────────────────


@plugin.on_install
def on_install(ctx: Context) -> None:
    ctx.log("Tic-Tac-Toe plugin installed", tags=["lifecycle"])
    # Seed last-announce-sweep cursor so we don't replay old matches.
    ctx.kv.set("announce:last_ts", float(time.time()))


@plugin.on_uninstall
def on_uninstall(ctx: Context) -> None:
    ctx.log("Tic-Tac-Toe plugin uninstalled", tags=["lifecycle"])


@plugin.on_ready
def on_ready(ctx: Context) -> None:
    ctx.log(f"Tic-Tac-Toe plugin ready (server={ctx.server_id})")


# ── Background announce sweep (every 60s) ──────────────────────────────────


@plugin.schedule(60)
def announce_sweep(ctx: Context) -> None:
    """Pull queued match announcements from the game server and post them
    to the configured channel — only if announce_channel_id is set."""
    channel_id = ctx.kv.get("settings:announce_channel_id") or ""
    if not channel_id:
        return  # disabled

    last_ts = float(ctx.kv.get("announce:last_ts") or time.time())
    try:
        result = _client(ctx).announce_payload(since_ts=last_ts)
    except GameServerError as e:
        ctx.log(f"announce sweep failed: {e}", level="warning")
        return

    announcements = result.get("announcements") or []
    if not announcements:
        return

    newest_ts = last_ts
    for a in announcements:
        ts = float(a.get("completed_at") or 0)
        if ts > newest_ts:
            newest_ts = ts

        winner = a.get("winner_name") or "Unknown"
        loser = a.get("loser_name") or "Unknown"
        delta = int(a.get("elo_delta") or 0)
        winner_elo = int(a.get("winner_elo") or 0)

        try:
            ctx.discord.send_message(
                channel_id=str(channel_id),
                content=(
                    f"🏆 **{winner}** ({winner_elo} ELO, {_format_elo_delta(delta)}) "
                    f"defeated **{loser}** at tic-tac-toe!"
                ),
            )
        except Exception as e:
            ctx.log(f"announce send failed: {e}", level="warning")

    # Advance the cursor so we don't re-announce the same matches.
    ctx.kv.set("announce:last_ts", newest_ts)


# ── Run ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    plugin.run()
