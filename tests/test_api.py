"""Tests for the bar3 HTTP API (api.py)."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from api import RoleConfig, create_app

API_KEY = "test-secret-key"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_guild(
    *,
    discord_id: int | None = None,
    role_ids: list[int] | None = None,
) -> MagicMock:
    """Return a mock discord.Guild.

    If *discord_id* is given, the guild will have a member with that ID
    who holds the roles whose IDs are listed in *role_ids*.
    """
    guild = MagicMock()
    if discord_id is None:
        guild.get_member.return_value = None
        return guild

    member = MagicMock()
    member.roles = [MagicMock(id=rid) for rid in (role_ids or [])]
    guild.get_member.side_effect = lambda mid: member if mid == discord_id else None
    return guild


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestIndexEndpoint:
    @pytest.mark.asyncio
    async def test_returns_200_with_expected_text(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/")
            assert resp.status == 200
            text = await resp.text()
            assert text == "would you kindly begone"


class TestRolesEndpoint:
    @pytest.mark.asyncio
    async def test_missing_api_key_returns_401(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/roles/123456789")
            assert resp.status == 401
            data = await resp.json()
            assert data["error"] == "Unauthorized"

    @pytest.mark.asyncio
    async def test_wrong_api_key_returns_401(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/123456789", headers={"X-API-Key": "wrong"}
            )
            assert resp.status == 401

    @pytest.mark.asyncio
    async def test_invalid_discord_id_returns_400(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/not-a-number", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 400
            data = await resp.json()
            assert data["error"] == "Invalid discord_id"

    @pytest.mark.asyncio
    async def test_user_not_in_guild(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/999", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["discord_id"] == "999"
            assert data["roles"] == {
                "verified": False,
                "bar3_client": False,
                "bar3_server": False,
            }

    @pytest.mark.asyncio
    async def test_user_not_in_guild_cache(self):
        """Guild is not available (bot not ready) — roles stay False."""
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/111", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["roles"]["verified"] is False
            assert data["roles"]["bar3_client"] is False

    @pytest.mark.asyncio
    async def test_user_with_bar3_client_role(self):
        bar3_client_role_id = 500

        guild = _make_guild(discord_id=222, role_ids=[bar3_client_role_id])

        role_config = RoleConfig(bar3_client_role_id=bar3_client_role_id)
        app = create_app(lambda: guild, API_KEY, role_config=role_config)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/222", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["roles"]["bar3_client"] is True
            assert data["roles"]["bar3_server"] is False

    @pytest.mark.asyncio
    async def test_user_with_all_roles(self):
        verified_id, client_id, server_id = 10, 20, 30

        guild = _make_guild(discord_id=333, role_ids=[verified_id, client_id, server_id])

        role_config = RoleConfig(
            verified_role_id=verified_id,
            bar3_client_role_id=client_id,
            bar3_server_role_id=server_id,
        )
        app = create_app(lambda: guild, API_KEY, role_config=role_config)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/333", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["roles"] == {
                "verified": True,
                "bar3_client": True,
                "bar3_server": True,
            }

    @pytest.mark.asyncio
    async def test_correct_api_key_returns_200(self):
        app = create_app(lambda: None, API_KEY)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/1", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200

    @pytest.mark.asyncio
    async def test_member_without_matching_roles(self):
        """Member is in guild but has none of the bar3 roles."""
        guild = _make_guild(discord_id=444, role_ids=[999])  # irrelevant role

        role_config = RoleConfig(
            verified_role_id=10,
            bar3_client_role_id=20,
            bar3_server_role_id=30,
        )
        app = create_app(lambda: guild, API_KEY, role_config=role_config)
        async with TestClient(TestServer(app)) as client:
            resp = await client.get(
                "/api/roles/444", headers={"X-API-Key": API_KEY}
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["roles"] == {
                "verified": False,
                "bar3_client": False,
                "bar3_server": False,
            }

