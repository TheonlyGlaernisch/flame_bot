"""
flame_bot – HTTP API server for bar3 integration.

bar3 (the website) calls this API after a user logs in via Discord OAuth to
decide whether the user should be granted access.

Endpoints
---------
GET /
    Returns plain text "would you kindly begone".  Useful as a health-check
    when the bot is hosted on a platform like Render.

GET /health
    Returns ``{"status": "ok"}`` (200 OK).  Intended as a lightweight
    liveness probe for uptime monitors and deployment platforms.

GET /ping
    Returns ``{"ping": "pong"}`` (200 OK).  Simple round-trip check.

GET /api/roles/{discord_id}
    Returns the bar3 role status for the given Discord user ID.

    Roles are manually assigned and stripped in Discord; no bot registration
    is required.  The endpoint simply reflects the member's current roles.

    Requires the ``X-API-Key`` request header to match the ``API_KEY``
    environment variable.

    Response (200 OK):
    {
        "discord_id": "123456789",
        "roles": {
            "verified":    true,
            "bar3_client": false,
            "bar3_server": false
        }
    }

    Error responses:
    • 401  { "error": "Unauthorized" }   — missing or wrong API key
    • 400  { "error": "Invalid discord_id" }
    • 503  { "error": "Bot not ready" }  — guild cache not populated yet
                                            (safe to retry after a short delay)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from aiohttp import web

log = logging.getLogger("flame_bot.api")


@dataclass
class RoleConfig:
    """Holds the Discord role IDs that the API checks against."""

    verified_role_id: int | None = None
    bar3_client_role_id: int | None = None
    bar3_server_role_id: int | None = None


def _check_api_key(request: web.Request, api_key: str) -> bool:
    return request.headers.get("X-API-Key") == api_key


def create_app(
    guild_getter,         # callable() -> discord.Guild | None
    api_key: str,
    role_config: RoleConfig | None = None,
) -> web.Application:
    """Return an aiohttp Application.

    Parameters
    ----------
    guild_getter:
        Zero-argument callable that returns the live ``discord.Guild`` object
        (or ``None`` if the bot isn't ready yet).  Keeping it as a callable
        rather than a direct reference makes the app easy to test without a
        real Discord connection.
    api_key:
        The secret that callers must supply via the ``X-API-Key`` header.
    role_config:
        The Discord role IDs to check.  Defaults to an empty ``RoleConfig``
        (all role checks will return ``False``).
    """
    if role_config is None:
        role_config = RoleConfig()

    async def index(request: web.Request) -> web.Response:
        return web.Response(text="would you kindly begone")

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ping(request: web.Request) -> web.Response:
        return web.json_response({"ping": "pong"})

    async def get_roles(request: web.Request) -> web.Response:
        if not _check_api_key(request, api_key):
            return web.json_response({"error": "Unauthorized"}, status=401)

        discord_id_str = request.match_info["discord_id"]
        if not discord_id_str.isdigit():
            return web.json_response({"error": "Invalid discord_id"}, status=400)

        discord_id = int(discord_id_str)

        roles: dict[str, bool] = {
            "verified": False,
            "bar3_client": False,
            "bar3_server": False,
        }

        guild: discord.Guild | None = guild_getter()
        if guild is None:
            return web.json_response(
                {"error": "Bot not ready"},
                status=503,
            )

        member = guild.get_member(discord_id)
        if member is not None:
            member_role_ids = {r.id for r in member.roles}
            if role_config.verified_role_id and role_config.verified_role_id in member_role_ids:
                roles["verified"] = True
            if role_config.bar3_client_role_id and role_config.bar3_client_role_id in member_role_ids:
                roles["bar3_client"] = True
            if role_config.bar3_server_role_id and role_config.bar3_server_role_id in member_role_ids:
                roles["bar3_server"] = True

        return web.json_response(
            {
                "discord_id": str(discord_id),
                "roles": roles,
            }
        )

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/ping", ping)
    app.router.add_get("/api/roles/{discord_id}", get_roles)
    return app
