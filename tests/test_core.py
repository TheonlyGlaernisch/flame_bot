"""Tests for database.py and pnw_api.py (no Discord or network calls)."""
import sqlite3
import tempfile
from unittest.mock import AsyncMock, patch

import pytest

from database import Database
from pnw_api import PnWClient


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------


class TestDatabase:
    def _make_db(self) -> Database:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        return Database(tmp.name)

    def test_register_and_retrieve_by_discord_id(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_register_and_retrieve_by_nation_id(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_nation_id(456)
        assert row is not None
        assert row["discord_id"] == "123"

    def test_register_updates_existing_entry(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        db.register(discord_id=123, nation_id=789, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert int(row["nation_id"]) == 789

    def test_get_missing_discord_id_returns_none(self):
        db = self._make_db()
        assert db.get_by_discord_id(999) is None

    def test_get_missing_nation_id_returns_none(self):
        db = self._make_db()
        assert db.get_by_nation_id(999) is None

    def test_delete_returns_true_when_deleted(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.delete(123) is True
        assert db.get_by_discord_id(123) is None

    def test_delete_returns_false_when_not_found(self):
        db = self._make_db()
        assert db.delete(999) is False

    def test_nation_id_unique_across_users(self):
        db = self._make_db()
        db.register(discord_id=111, nation_id=456, discord_username="alice")
        with pytest.raises(sqlite3.IntegrityError):
            with db._connect() as conn:
                conn.execute(
                    "INSERT INTO registrations (discord_id, nation_id, registered_at, discord_username) "
                    "VALUES (?, ?, ?, ?)",
                    ("222", 456, "2024-01-01T00:00:00+00:00", "bob"),
                )

    def test_get_by_discord_username(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_username("alice")
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_get_by_discord_username_case_insensitive(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="Alice")
        assert db.get_by_discord_username("alice") is not None
        assert db.get_by_discord_username("ALICE") is not None

    def test_get_by_discord_username_strips_whitespace(self):
        db = self._make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.get_by_discord_username("  alice  ") is not None

    def test_get_by_discord_username_returns_none_when_missing(self):
        db = self._make_db()
        assert db.get_by_discord_username("nobody") is None

    def test_migration_adds_column_to_existing_db(self):
        """A DB created without discord_username column must be migrated on open."""
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        # Create old-style DB without the discord_username column
        conn = sqlite3.connect(tmp.name)
        conn.execute(
            "CREATE TABLE registrations "
            "(discord_id TEXT PRIMARY KEY, nation_id INTEGER NOT NULL UNIQUE, registered_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO registrations VALUES ('999', 1, '2024-01-01T00:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        # Opening the Database should run the migration without error
        db = Database(tmp.name)
        row = db.get_by_discord_id(999)
        assert row is not None
        assert row["discord_username"] == ""


# ---------------------------------------------------------------------------
# PnWClient.discord_matches tests
# ---------------------------------------------------------------------------


class TestDiscordMatches:
    def test_exact_match(self):
        assert PnWClient.discord_matches("alice", "alice")

    def test_case_insensitive(self):
        assert PnWClient.discord_matches("Alice", "alice")
        assert PnWClient.discord_matches("alice", "ALICE")

    def test_legacy_discriminator_format(self):
        assert PnWClient.discord_matches("alice#1234", "alice")

    def test_mismatch(self):
        assert not PnWClient.discord_matches("bob", "alice")

    def test_empty_stored(self):
        assert not PnWClient.discord_matches("", "alice")

    def test_whitespace_stripped(self):
        assert PnWClient.discord_matches("  alice  ", "alice")
        assert PnWClient.discord_matches("alice", "  alice  ")

    def test_partial_prefix_does_not_match(self):
        # "alic" should NOT match "alice"
        assert not PnWClient.discord_matches("alic", "alice")


# ---------------------------------------------------------------------------
# PnWClient.get_nation tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetNation:
    @pytest.mark.asyncio
    async def test_returns_nation_on_success(self):
        mock_response_data = {
            "data": {
                "nations": {
                    "data": [
                        {
                            "id": "42",
                            "nation_name": "Testland",
                            "leader_name": "TestLeader",
                            "discord": "testuser",
                        }
                    ]
                }
            }
        }

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation(42)

        assert nation is not None
        assert nation.nation_id == 42
        assert nation.nation_name == "Testland"
        assert nation.leader_name == "TestLeader"
        assert nation.discord_tag == "testuser"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_data(self):
        mock_response_data = {"data": {"nations": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation(9999)

        assert nation is None

    @pytest.mark.asyncio
    async def test_raises_on_api_errors(self):
        client = PnWClient(api_key="bad_key")

        with patch.object(
            client,
            "_query",
            new=AsyncMock(side_effect=RuntimeError("PnW API returned errors: Unauthorized")),
        ):
            with pytest.raises(RuntimeError, match="Unauthorized"):
                await client.get_nation(1)
