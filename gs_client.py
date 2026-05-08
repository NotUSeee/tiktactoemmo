"""
gs_client.py — HMAC-signed HTTP client to the tic-tac-toe game server.

Every call from the plugin sandbox to the game server is signed with the
shared secret so the server can authenticate the call without reusing
Discord OAuth tokens or exposing user-facing JWTs to the plugin.

Signature format (header `X-MMO-Signature`):
    <timestamp>.<hex(hmac_sha256(secret, timestamp + "\\n" + method + "\\n" + path + "\\n" + body))>

The server rejects requests with timestamps older than 60 seconds (replay
protection). Both sides use UTC seconds.

──────────────────────────────────────────────────────────────────────
SECURITY NOTE — global shared secret
──────────────────────────────────────────────────────────────────────
``GAME_SERVER_URL`` and ``SHARED_SECRET`` below are global constants
shared by every install of this plugin. The trade-off is documented:

  + Simpler than per-install settings — no Settings UI, no KV storage,
    no admin onboarding step.
  - The secret is visible in the source on GitHub. Anyone who reads
    the repo can mint forged HMAC requests to the game server, which
    means they can forge user_id values and impersonate anyone for
    leaderboard / stats purposes.

This is acceptable for tic-tac-toe (worst case = fake leaderboard
ranks). DO NOT copy this pattern to a plugin that handles money,
private data, or anything destructive. For those, use a per-install
secret entered by the server admin (KV-backed, edited via a dashboard
Settings page) or a platform-managed credential system.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Dict


# ── Game server config (global, shared by all installs) ────────────────────
GAME_SERVER_URL = "https://tictactoe.mmomaid.cloud"
SHARED_SECRET = "aea29c5ba1034b7ecf4936e0429c79da8cfdb10a74f6eed40c0301e1a8313a83"


class GameServerError(Exception):
    """Raised when the game server returns a non-2xx response."""


class GameServerClient:
    """Thin wrapper around ctx.http that signs every request.

    Usage::

        client = GameServerClient(ctx)
        info = client.lobby_url(user_id="123", username="Aerilyn")
        print(info["url"])  # signed iframe URL
    """

    def __init__(self, ctx) -> None:
        self._ctx = ctx
        self._base_url = GAME_SERVER_URL.rstrip("/")
        self._secret = SHARED_SECRET

    def _sign(self, method: str, path: str, body: str) -> str:
        """Build the X-MMO-Signature header value."""
        ts = str(int(time.time()))
        payload = f"{ts}\n{method.upper()}\n{path}\n{body}".encode("utf-8")
        digest = hmac.new(
            self._secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest()
        return f"{ts}.{digest}"

    # ── Low-level call ─────────────────────────────────────────────────────

    @staticmethod
    def _read_response(resp: Dict[str, Any]) -> tuple[int, str]:
        """Pull (status, body_text) out of a proxy.request response.

        The MMO Maid SDK returns ``{status, headers, body_bytes, truncated}``
        — the body field is ``body_bytes`` (a UTF-8 decoded string despite the
        name). We fall back to ``body`` / ``text`` for any older SDK build that
        used those names.
        """
        status = int(
            resp.get("status")
            or resp.get("status_code")
            or 0
        )
        text = (
            resp.get("body_bytes")
            or resp.get("body")
            or resp.get("text")
            or ""
        )
        if not isinstance(text, str):
            try:
                text = text.decode("utf-8", errors="replace")
            except Exception:
                text = str(text)
        return status, text

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        body_str = json.dumps(body, separators=(",", ":"), sort_keys=True)
        sig = self._sign("POST", path, body_str)
        url = f"{self._base_url}{path}"
        try:
            resp = self._ctx.http.post(
                url,
                body=body_str,
                headers={
                    "Content-Type": "application/json",
                    "X-MMO-Signature": sig,
                },
            )
        except Exception as e:
            raise GameServerError(f"network error calling {path}: {e}")

        status, text = self._read_response(resp)
        if not (200 <= status < 300):
            raise GameServerError(f"{path} returned {status}: {text[:300]}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError as e:
            raise GameServerError(f"{path} returned non-JSON body: {e}")

    def _get(self, path: str) -> Dict[str, Any]:
        sig = self._sign("GET", path, "")
        url = f"{self._base_url}{path}"
        try:
            resp = self._ctx.http.get(
                url,
                headers={"X-MMO-Signature": sig},
            )
        except Exception as e:
            raise GameServerError(f"network error calling {path}: {e}")

        status, text = self._read_response(resp)
        if not (200 <= status < 300):
            raise GameServerError(f"{path} returned {status}: {text[:300]}")
        try:
            return json.loads(text) if text else {}
        except json.JSONDecodeError as e:
            raise GameServerError(f"{path} returned non-JSON body: {e}")

    # ── High-level RPCs ────────────────────────────────────────────────────

    def lobby_url(self, *, user_id: str, username: str = "", guild_id: str = "") -> Dict[str, Any]:
        """Mint a short-lived signed lobby URL bound to a Discord user.

        Returns ``{"url": "https://.../?token=<jwt>"}``.
        """
        return self._post(
            "/api/internal/lobby_url",
            {"user_id": str(user_id), "username": str(username), "guild_id": str(guild_id)},
        )

    def stats(self, *, user_id: str) -> Dict[str, Any]:
        """Get cross-server stats for a single Discord user.

        Returns ``{"elo": int, "wins": int, "losses": int, "draws": int,
        "current_streak": int, "best_streak": int, "win_pct": float}``. If the
        user has never played, all fields default to zero (ELO defaults to 1200).
        """
        return self._post(
            "/api/internal/stats",
            {"user_id": str(user_id)},
        )

    def leaderboard(self, *, limit: int = 25) -> Dict[str, Any]:
        """Top-N players by ELO across all servers.

        Returns ``{"rows": [{"rank", "user_id", "username", "elo", "wins",
        "losses", "draws", "win_pct", "best_streak"}, ...]}``.
        """
        limit = max(1, min(int(limit), 100))
        return self._post(
            "/api/internal/leaderboard",
            {"limit": limit},
        )

    def metrics(self) -> Dict[str, Any]:
        """Quick counts for the dashboard Overview page.

        Returns ``{"open_tables": int, "active_players": int,
        "games_24h": int, "trend_7d": {"labels": [...], "data": [...]}}``.
        """
        return self._post("/api/internal/metrics", {})

