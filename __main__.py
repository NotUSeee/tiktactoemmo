"""
MMO Maid Tic-Tac-Toe — plugin entry point.

This module owns:
  - /tictactoe slash command (root menu with buttons)
  - Button handlers (Play, My Stats, Leaderboard, How to play)
  - One dashboard handler — ``dashboard.get_play_iframe_url`` — that mints
    a personalised JWT-signed lobby URL for the iframe-mode Play page.

The dashboard runs in iframe mode: a single bundled HTML file (play.html)
embeds the game server's lobby in an iframe. Leaderboard / Stats /
Settings UIs all live on the game server itself rather than being rebuilt
as widget-mode pages here.

The plugin never holds game state — every button or dashboard call goes
through gs_client.GameServerClient, which signs the request with the
shared HMAC secret. The game server is the single source of truth for
boards, ELO, and match history.
"""
from __future__ import annotations

from typing import Any, Dict

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


def _i(d: dict, key: str, default: int = 0) -> int:
    """``int(d.get(key, default))`` but None-safe.

    The game server may return ``{"elo": null}`` for new players or hot
    columns; ``int(None)`` raises TypeError. Coerce explicitly.
    """
    v = d.get(key)
    if v is None:
        return int(default)
    try:
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _viewer_id(params: dict) -> str:
    """Extract the viewing dashboard user's Discord ID from RPC params.

    The platform injects both ``discord_user_id`` and ``viewer_user_id``
    into dashboard RPC params; we accept either for robustness.
    """
    for key in ("discord_user_id", "viewer_user_id", "user_id"):
        v = params.get(key)
        if v:
            return str(v)
    return ""


def _viewer_username(params: dict) -> str:
    """Extract the viewing user's display name if available."""
    for key in ("username", "viewer_username", "display_name"):
        v = params.get(key)
        if v:
            return str(v)
    return ""


def _event_user_id(event: dict) -> str:
    """Pull the Discord user ID out of an interaction_create event.

    The SDK's canonical shape puts ``user_id`` at the top level of ``event``.
    Older or alternate hosts may nest it under ``event["user"]["id"]``;
    we accept both for forward-compatibility.
    """
    uid = event.get("user_id")
    if uid:
        return str(uid)
    nested = event.get("user")
    if isinstance(nested, dict):
        v = nested.get("id") or nested.get("user_id")
        if v:
            return str(v)
    return ""


def _event_username(ctx, event: dict, user_id: str = "") -> str:
    """Best-effort display name for an interaction event.

    Interaction events don't carry a username, so we check optional keys
    first, then fall back to ctx.discord.get_member (requires discord:read)
    to pull the real Discord name, and finally to a "Player XXXX" handle.
    """
    for key in ("username", "user_name", "display_name", "global_name"):
        v = event.get(key)
        if v:
            return str(v)
    nested = event.get("user")
    if isinstance(nested, dict):
        for key in ("username", "global_name", "display_name", "name"):
            v = nested.get(key)
            if v:
                return str(v)
    if user_id:
        try:
            member = ctx.discord.get_member(user_id=user_id) or {}
            for key in ("nick", "display_name", "username"):
                v = member.get(key)
                if v:
                    return str(v)
        except Exception:
            pass
        return f"Player {user_id[-4:]}"
    return "Player"


# ── Slash command ──────────────────────────────────────────────────────────


@plugin.on_slash_command("tictactoe")
def handle_tictactoe(ctx: Context, event: dict) -> None:
    """Root slash command — opens the menu in Discord with action buttons.

    Defers the interaction immediately so the 3-second Discord timeout
    doesn't fire during the (slow-ish) stats fetch. After defer, we have 15
    minutes to send the followup.
    """
    user_id = _event_user_id(event)
    username = _event_username(ctx, event, user_id=user_id)

    ctx.interaction.defer(ephemeral=True)

    headline = f"**{username}**, welcome to MMO Maid Tic-Tac-Toe."
    if user_id:
        try:
            stats = _client(ctx).stats(user_id=user_id)
            elo = _i(stats, "elo", 1200)
            wins = _i(stats, "wins", 0)
            losses = _i(stats, "losses", 0)
            draws = _i(stats, "draws", 0)
            headline += (
                f"\nCurrent ELO: **{elo}**  ·  "
                f"Record: **{wins}W / {losses}L / {draws}D**"
            )
        except GameServerError as e:
            ctx.log(f"stats fetch failed: {e}", level="warning")
            headline += "\n_Stats temporarily unavailable._"
    else:
        ctx.log(
            f"tictactoe slash: missing user_id; event keys={sorted(event.keys())}",
            level="warning",
        )
        headline += "\n_Stats temporarily unavailable._"

    body = (
        f"{headline}\n\n"
        "Tap **Play** to open the live lobby in your dashboard. Create your "
        "own table or jump into one already waiting for a challenger."
    )

    ctx.interaction.followup(
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
    user_id = _event_user_id(event)
    username = _event_username(ctx, event, user_id=user_id)

    if not user_id:
        ctx.log(
            f"ttt_btn_play: missing user_id; event keys={sorted(event.keys())}",
            level="warning",
        )
        ctx.interaction.respond(
            content="Couldn't identify your Discord account. Try again in a moment.",
            ephemeral=True,
        )
        return

    ctx.interaction.defer(ephemeral=True)

    try:
        info = _client(ctx).lobby_url(
            user_id=user_id,
            username=username,
            guild_id=ctx.server_id,
        )
        url = info.get("url") or ""
    except GameServerError as e:
        ctx.log(f"lobby_url failed: {e}", level="error")
        ctx.interaction.followup(
            content="The tic-tac-toe server isn't reachable right now. Try again shortly.",
            ephemeral=True,
        )
        return

    if not url:
        ctx.interaction.followup(
            content="The server didn't return a play URL. Tell a server admin.",
            ephemeral=True,
        )
        return

    ctx.interaction.followup(
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
    user_id = _event_user_id(event)
    username = _event_username(ctx, event, user_id=user_id)

    if not user_id:
        ctx.log(
            f"ttt_btn_stats: missing user_id; event keys={sorted(event.keys())}",
            level="warning",
        )
        ctx.interaction.respond(
            content="Couldn't identify your Discord account. Try again in a moment.",
            ephemeral=True,
        )
        return

    ctx.interaction.defer(ephemeral=True)

    try:
        stats = _client(ctx).stats(user_id=user_id)
    except GameServerError as e:
        ctx.log(f"stats failed: {e}", level="error")
        ctx.interaction.followup(
            content="Stats are temporarily unavailable.",
            ephemeral=True,
        )
        return

    elo = _i(stats, "elo", 1200)
    wins = _i(stats, "wins", 0)
    losses = _i(stats, "losses", 0)
    draws = _i(stats, "draws", 0)
    streak = _i(stats, "current_streak", 0)
    best = _i(stats, "best_streak", 0)
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
    ctx.interaction.followup(content=body, ephemeral=True)


@plugin.on_component("ttt_btn_leaderboard")
def handle_leaderboard(ctx: Context, event: dict) -> None:
    ctx.interaction.defer(ephemeral=True)

    try:
        result = _client(ctx).leaderboard(limit=10)
    except GameServerError as e:
        ctx.log(f"leaderboard failed: {e}", level="error")
        ctx.interaction.followup(
            content="The leaderboard is temporarily unavailable.",
            ephemeral=True,
        )
        return

    rows = result.get("rows") or []
    if not rows:
        ctx.interaction.followup(
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
        elo = _i(r, "elo", 1200)
        wins = _i(r, "wins", 0)
        losses = _i(r, "losses", 0)
        draws = _i(r, "draws", 0)
        record = f"{wins}-{losses}-{draws}"
        lines.append(f"{rank:<3} {name:<22} {elo:>5} {record:>10}")
    lines.append("```")
    ctx.interaction.followup(content="\n".join(lines), ephemeral=True)


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


# ── Dashboard handler ──────────────────────────────────────────────────────
#
# The dashboard manifest is `mode: "iframe"` with a single `play.html` page.
# That page asks us (via the SDK postMessage bridge) for a personalised lobby
# URL bound to the viewing user's Discord identity, then sets it as the
# iframe src. All other UIs (leaderboard, stats, settings) live on the game
# server itself and are reachable via tabs in the embedded lobby.


@plugin.on_dashboard("get_play_iframe_url")
def dash_get_play_iframe_url(ctx: Context, params: dict) -> Dict[str, Any]:
    """Mint a JWT-signed lobby URL bound to the viewing user.

    Falls back to the public lobby (anonymous, read-only) if the platform
    didn't pass a viewer user_id — the lobby's Discord OAuth flow can pick
    them up from there.
    """
    user_id = _viewer_id(params)
    username = _viewer_username(params)

    if not user_id:
        ctx.log(
            "dashboard.get_play_iframe_url called without a viewer user_id; "
            f"params keys: {list(params.keys())}",
            level="warning",
        )
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


# ── Lifecycle hooks ────────────────────────────────────────────────────────


@plugin.on_install
def on_install(ctx: Context) -> None:
    ctx.log("Tic-Tac-Toe plugin installed", tags=["lifecycle"])


@plugin.on_uninstall
def on_uninstall(ctx: Context) -> None:
    ctx.log("Tic-Tac-Toe plugin uninstalled", tags=["lifecycle"])


# Note: @plugin.on_ready and @plugin.schedule(...) are silently skipped when
# the platform runs this plugin in pool mode (the current default). Any
# background work that needs to run on a timer should live on the game server
# (tictactoe.mmomaid.cloud), not here.


# ── Run ────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    plugin.run()
