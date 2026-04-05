"""Tests for database.py and pnw_api.py (no Discord or network calls)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from database import Database
from pnw_api import PnWClient, _parse_resource_loot


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return an isolated in-memory Database backed by mongomock."""
    return Database("mongodb://irrelevant", _client=mongomock.MongoClient())


class TestDatabase:
    def test_register_and_retrieve_by_discord_id(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_register_and_retrieve_by_nation_id(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_nation_id(456)
        assert row is not None
        assert row["discord_id"] == "123"

    def test_register_updates_existing_entry(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        db.register(discord_id=123, nation_id=789, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert int(row["nation_id"]) == 789

    def test_get_missing_discord_id_returns_none(self):
        db = _make_db()
        assert db.get_by_discord_id(999) is None

    def test_get_missing_nation_id_returns_none(self):
        db = _make_db()
        assert db.get_by_nation_id(999) is None

    def test_delete_returns_true_when_deleted(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.delete(123) is True
        assert db.get_by_discord_id(123) is None

    def test_delete_returns_false_when_not_found(self):
        db = _make_db()
        assert db.delete(999) is False

    def test_nation_id_unique_across_users(self):
        db = _make_db()
        db.register(discord_id=111, nation_id=456, discord_username="alice")
        with pytest.raises(DuplicateKeyError):
            db.register(discord_id=222, nation_id=456, discord_username="bob")

    def test_get_by_discord_username(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_username("alice")
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_get_by_discord_username_case_insensitive(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="Alice")
        assert db.get_by_discord_username("alice") is not None
        assert db.get_by_discord_username("ALICE") is not None

    def test_get_by_discord_username_strips_whitespace(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.get_by_discord_username("  alice  ") is not None

    def test_get_by_discord_username_returns_none_when_missing(self):
        db = _make_db()
        assert db.get_by_discord_username("nobody") is None

    # ------------------------------------------------------------------
    # Slots config tests
    # ------------------------------------------------------------------

    def test_get_slots_alliances_returns_empty_by_default(self):
        db = _make_db()
        assert db.get_slots_alliances(1) == []

    def test_set_and_get_slots_alliances(self):
        db = _make_db()
        db.set_slots_alliances(1, [100, 200, 300])
        assert db.get_slots_alliances(1) == [100, 200, 300]

    def test_set_slots_alliances_overwrites(self):
        db = _make_db()
        db.set_slots_alliances(1, [100])
        db.set_slots_alliances(1, [200, 300])
        assert db.get_slots_alliances(1) == [200, 300]

    def test_slots_alliances_isolated_per_guild(self):
        db = _make_db()
        db.set_slots_alliances(1, [100])
        db.set_slots_alliances(2, [999])
        assert db.get_slots_alliances(1) == [100]
        assert db.get_slots_alliances(2) == [999]


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
# Shared mock nation data
# ---------------------------------------------------------------------------

_NATION_DATA = {
    "id": "676593",
    "nation_name": "Testland",
    "leader_name": "TestLeader",
    "discord": "testuser",
    "num_cities": 10,
    "score": 1234.56,
    "last_active": "2024-03-20 12:00:00",
    "soldiers": 50000,
    "tanks": 5000,
    "aircraft": 400,
    "ships": 80,
    "alliance_id": "42",
    "alliance": {"name": "Test Alliance"},
    "alliance_position": "MEMBER",
    "alliance_seniority": 45,
}


# ---------------------------------------------------------------------------
# PnWClient.get_nation tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetNation:
    @pytest.mark.asyncio
    async def test_returns_nation_on_success(self):
        mock_response_data = {"data": {"nations": {"data": [_NATION_DATA]}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation(676593)

        assert nation is not None
        assert nation.nation_id == 676593
        assert nation.nation_name == "Testland"
        assert nation.leader_name == "TestLeader"
        assert nation.discord_tag == "testuser"
        assert nation.num_cities == 10
        assert nation.score == 1234.56
        assert nation.last_active == "2024-03-20 12:00:00"
        assert nation.soldiers == 50000
        assert nation.tanks == 5000
        assert nation.aircraft == 400
        assert nation.ships == 80
        assert nation.alliance_id == 42
        assert nation.alliance_name == "Test Alliance"
        assert nation.alliance_position == "MEMBER"
        assert nation.alliance_seniority == 45
        assert nation.last_active_unix != 0  # ISO string was parsed to a timestamp

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


# ---------------------------------------------------------------------------
# PnWClient.get_nation_by_name tests (mocked)
# ---------------------------------------------------------------------------


class TestGetNationByName:
    @pytest.mark.asyncio
    async def test_returns_nation_on_match(self):
        mock_response_data = {"data": {"nations": {"data": [_NATION_DATA]}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation_by_name("Testland")

        assert nation is not None
        assert nation.nation_name == "Testland"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self):
        mock_response_data = {"data": {"nations": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation_by_name("Nobody")

        assert nation is None


# ---------------------------------------------------------------------------
# PnWClient.get_alliance_members tests (mocked)
# ---------------------------------------------------------------------------


class TestGetAllianceMembers:
    @pytest.mark.asyncio
    async def test_returns_member_list(self):
        mock_response_data = {
            "data": {"nations": {"data": [_NATION_DATA, {**_NATION_DATA, "id": "111", "nation_name": "Second"}]}}
        }

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            members = await client.get_alliance_members([42])

        assert len(members) == 2
        assert members[0].nation_name == "Testland"
        assert members[1].nation_name == "Second"

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_members(self):
        mock_response_data = {"data": {"nations": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            members = await client.get_alliance_members([99])

        assert members == []


# ---------------------------------------------------------------------------
# PnWClient.get_active_war_counts tests (mocked)
# ---------------------------------------------------------------------------


class TestGetActiveWarCounts:
    @pytest.mark.asyncio
    async def test_counts_wars_per_nation(self):
        mock_response_data = {
            "data": {
                "wars": {
                    "data": [
                        {"def_id": "10"},
                        {"def_id": "10"},
                        {"def_id": "20"},
                    ]
                }
            }
        }

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            counts = await client.get_active_war_counts([10, 20, 30])

        assert counts == {10: 2, 20: 1}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_for_empty_input(self):
        client = PnWClient(api_key="dummy")
        counts = await client.get_active_war_counts([])
        assert counts == {}

    @pytest.mark.asyncio
    async def test_returns_empty_dict_when_no_wars(self):
        mock_response_data = {"data": {"wars": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            counts = await client.get_active_war_counts([1, 2, 3])

        assert counts == {}


# ---------------------------------------------------------------------------
# PnWClient.get_nation_rest tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetNationRest:
    # Minimal REST response shape matching the PnW v1 API
    _REST_RESPONSE = {
        "success": True,
        "nationid": "676593",
        "name": "Testland",
        "leadername": "TestLeader",
        "cities": 10,
        "score": "1234.56",
        "minutessinceactive": 52,
        "continent": "North America",
        "color": "gray",
        "alliance": "None",
        "allianceid": "0",
    }

    def _make_mock_get(self, data: dict) -> MagicMock:
        """Build a mock aiohttp GET context manager returning *data*."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()  # sync in aiohttp
        mock_resp.json = AsyncMock(return_value=data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @pytest.mark.asyncio
    async def test_returns_nation_on_success(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        mock_session = self._make_mock_get(self._REST_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(676593)

        assert nation is not None
        assert nation.nation_id == 676593
        assert nation.nation_name == "Testland"
        assert nation.leader_name == "TestLeader"
        assert nation.num_cities == 10
        assert nation.score == 1234.56
        assert nation.minutes_since_active == 52
        assert nation.last_active == "52 minutes ago"
        assert nation.discord_tag == ""

    @pytest.mark.asyncio
    async def test_returns_none_when_success_false(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        mock_session = self._make_mock_get({"success": False, "error": "Nation not found."})

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(999999)

        assert nation is None

    @pytest.mark.asyncio
    async def test_zero_minutes_active_gives_empty_last_active(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        data = {**self._REST_RESPONSE, "minutessinceactive": 0}
        mock_session = self._make_mock_get(data)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(676593)

        assert nation is not None
        assert nation.minutes_since_active == 0
        assert nation.last_active == ""


# ---------------------------------------------------------------------------
# PnWClient REST alliance tests (mocked HTTP) — used by /test commands
# ---------------------------------------------------------------------------

_REST_ALLIANCES_RESPONSE = {
    "alliances": [
        {
            "id": "100",
            "name": "Test Alliance",
            "acronym": "TA",
            "color": "green",
            "score": 50000.0,
            "avgscore": 1000.0,
            "members": 42,
            "flagurl": "https://example.com/flag.png",
            "forumurl": "",
            "ircchan": "",
        },
        {
            "id": "200",
            "name": "Another Alliance",
            "acronym": "AA",
            "color": "blue",
            "score": 20000.0,
            "avgscore": 500.0,
            "members": 10,
            "flagurl": "",
            "forumurl": "",
            "ircchan": "",
        },
    ]
}


def _make_mock_get_alliances(data: dict) -> MagicMock:
    mock_resp = AsyncMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    return mock_session


class TestGetAllianceRestById:
    @pytest.mark.asyncio
    async def test_returns_alliance_matching_id(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_id(100)

        assert info is not None
        assert info.alliance_id == 100
        assert info.name == "Test Alliance"
        assert info.acronym == "TA"
        assert info.score == 50000.0
        assert info.average_score == 1000.0
        assert info.color == "green"
        assert info.flag == "https://example.com/flag.png"
        assert info.num_members == 42

    @pytest.mark.asyncio
    async def test_returns_second_alliance_by_id(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_id(200)

        assert info is not None
        assert info.alliance_id == 200
        assert info.name == "Another Alliance"

    @pytest.mark.asyncio
    async def test_returns_none_when_id_not_found(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_id(9999)

        assert info is None

    @pytest.mark.asyncio
    async def test_returns_none_when_alliances_list_empty(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances({"alliances": []})

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_id(100)

        assert info is None


class TestGetAllianceRestByName:
    @pytest.mark.asyncio
    async def test_returns_alliance_matching_name(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_name("Test Alliance")

        assert info is not None
        assert info.name == "Test Alliance"
        assert info.alliance_id == 100

    @pytest.mark.asyncio
    async def test_name_match_is_case_insensitive(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_name("test alliance")

        assert info is not None
        assert info.alliance_id == 100

    @pytest.mark.asyncio
    async def test_returns_none_when_name_not_found(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances(_REST_ALLIANCES_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_name("Nobody")

        assert info is None

    @pytest.mark.asyncio
    async def test_returns_none_when_alliances_list_empty(self):
        import aiohttp

        client = PnWClient(api_key="dummy", rest_url="https://test.example.com/api/")
        mock_session = _make_mock_get_alliances({"alliances": []})

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            info = await client.get_alliance_by_name("Test Alliance")

        assert info is None


# ---------------------------------------------------------------------------
# Database tests
# ---------------------------------------------------------------------------


def _make_db() -> Database:
    """Return an isolated in-memory Database backed by mongomock."""
    return Database("mongodb://irrelevant", _client=mongomock.MongoClient())


class TestDatabase:
    def test_register_and_retrieve_by_discord_id(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_register_and_retrieve_by_nation_id(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_nation_id(456)
        assert row is not None
        assert row["discord_id"] == "123"

    def test_register_updates_existing_entry(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        db.register(discord_id=123, nation_id=789, discord_username="alice")
        row = db.get_by_discord_id(123)
        assert int(row["nation_id"]) == 789

    def test_get_missing_discord_id_returns_none(self):
        db = _make_db()
        assert db.get_by_discord_id(999) is None

    def test_get_missing_nation_id_returns_none(self):
        db = _make_db()
        assert db.get_by_nation_id(999) is None

    def test_delete_returns_true_when_deleted(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.delete(123) is True
        assert db.get_by_discord_id(123) is None

    def test_delete_returns_false_when_not_found(self):
        db = _make_db()
        assert db.delete(999) is False

    def test_nation_id_unique_across_users(self):
        db = _make_db()
        db.register(discord_id=111, nation_id=456, discord_username="alice")
        with pytest.raises(DuplicateKeyError):
            db.register(discord_id=222, nation_id=456, discord_username="bob")

    def test_get_by_discord_username(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        row = db.get_by_discord_username("alice")
        assert row is not None
        assert int(row["nation_id"]) == 456

    def test_get_by_discord_username_case_insensitive(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="Alice")
        assert db.get_by_discord_username("alice") is not None
        assert db.get_by_discord_username("ALICE") is not None

    def test_get_by_discord_username_strips_whitespace(self):
        db = _make_db()
        db.register(discord_id=123, nation_id=456, discord_username="alice")
        assert db.get_by_discord_username("  alice  ") is not None

    def test_get_by_discord_username_returns_none_when_missing(self):
        db = _make_db()
        assert db.get_by_discord_username("nobody") is None


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
                            "id": "676593",
                            "nation_name": "Testland",
                            "leader_name": "TestLeader",
                            "discord": "testuser",
                            "num_cities": 10,
                            "score": 1234.56,
                            "last_active": "2024-03-20 12:00:00",
                        }
                    ]
                }
            }
        }

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response_data)):
            nation = await client.get_nation(676593)

        assert nation is not None
        assert nation.nation_id == 676593
        assert nation.nation_name == "Testland"
        assert nation.leader_name == "TestLeader"
        assert nation.discord_tag == "testuser"
        assert nation.num_cities == 10
        assert nation.score == 1234.56
        assert nation.last_active == "2024-03-20 12:00:00"

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


# ---------------------------------------------------------------------------
# PnWClient.get_nation_rest tests (mocked HTTP)
# ---------------------------------------------------------------------------


class TestGetNationRest:
    # Minimal REST response shape matching the PnW v1 API
    _REST_RESPONSE = {
        "success": True,
        "nationid": "676593",
        "name": "Testland",
        "leadername": "TestLeader",
        "cities": 10,
        "score": "1234.56",
        "minutessinceactive": 52,
        "continent": "North America",
        "color": "gray",
        "alliance": "None",
        "allianceid": "0",
    }

    def _make_mock_get(self, data: dict) -> MagicMock:
        """Build a mock aiohttp GET context manager returning *data*."""
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()  # sync in aiohttp
        mock_resp.json = AsyncMock(return_value=data)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_resp)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        return mock_session

    @pytest.mark.asyncio
    async def test_returns_nation_on_success(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        mock_session = self._make_mock_get(self._REST_RESPONSE)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(676593)

        assert nation is not None
        assert nation.nation_id == 676593
        assert nation.nation_name == "Testland"
        assert nation.leader_name == "TestLeader"
        assert nation.num_cities == 10
        assert nation.score == 1234.56
        assert nation.minutes_since_active == 52
        assert nation.last_active == "52 minutes ago"
        assert nation.discord_tag == ""

    @pytest.mark.asyncio
    async def test_returns_none_when_success_false(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        mock_session = self._make_mock_get({"success": False, "error": "Nation not found."})

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(999999)

        assert nation is None

    @pytest.mark.asyncio
    async def test_zero_minutes_active_gives_empty_last_active(self):
        import aiohttp

        client = PnWClient(api_key="dummy")
        data = {**self._REST_RESPONSE, "minutessinceactive": 0}
        mock_session = self._make_mock_get(data)

        with patch.object(aiohttp, "ClientSession", return_value=mock_session):
            nation = await client.get_nation_rest(676593)

        assert nation is not None
        assert nation.minutes_since_active == 0
        assert nation.last_active == ""


# ---------------------------------------------------------------------------
# PnWClient._parse_alliance / get_alliance_by_id tests
# ---------------------------------------------------------------------------

_ALLIANCE_DATA = {
    "id": "7",
    "name": "The Rose",
    "acronym": "Rose",
    "score": 250000.0,
    "average_score": 1250.0,
    "color": "pink",
    "flag": "https://example.com/flag.png",
    "discord_link": "https://discord.gg/example",
    "nations": [
        {"id": "1", "num_cities": 20, "alliance_position": "MEMBER", "vacation_mode_turns": 0},
        {"id": "2", "num_cities": 15, "alliance_position": "OFFICER", "vacation_mode_turns": 0},
        {"id": "3", "num_cities": 10, "alliance_position": "APPLICANT", "vacation_mode_turns": 0},
        {"id": "4", "num_cities": 25, "alliance_position": "MEMBER", "vacation_mode_turns": 5},
    ],
}


class TestParseAlliance:
    def test_member_and_applicant_counts(self):
        info = PnWClient._parse_alliance(_ALLIANCE_DATA)
        # id=4 is in vacation mode, id=3 is applicant → 2 active members
        assert info.num_members == 2
        assert info.num_applicants == 1

    def test_vacation_mode_members_excluded_from_active_count(self):
        data = {
            **_ALLIANCE_DATA,
            "nations": [
                {"id": "1", "num_cities": 20, "alliance_position": "MEMBER", "vacation_mode_turns": 0},
                {"id": "2", "num_cities": 10, "alliance_position": "MEMBER", "vacation_mode_turns": 3},
                {"id": "3", "num_cities": 5,  "alliance_position": "MEMBER", "vacation_mode_turns": 99},
            ],
        }
        info = PnWClient._parse_alliance(data)
        # Only id=1 is active; ids 2 and 3 are in vmode
        assert info.num_members == 1
        assert info.total_cities == 20

    def test_total_and_avg_cities(self):
        info = PnWClient._parse_alliance(_ALLIANCE_DATA)
        # Active members: id=1 (20 cities) + id=2 (15 cities) = 35 total, avg 17.5
        assert info.total_cities == 35
        assert info.avg_cities == 17.5

    def test_basic_fields(self):
        info = PnWClient._parse_alliance(_ALLIANCE_DATA)
        assert info.alliance_id == 7
        assert info.name == "The Rose"
        assert info.acronym == "Rose"
        assert info.score == 250000.0
        assert info.color == "pink"
        assert info.discord_link == "https://discord.gg/example"

    def test_empty_nations_list(self):
        data = {**_ALLIANCE_DATA, "nations": []}
        info = PnWClient._parse_alliance(data)
        assert info.num_members == 0
        assert info.num_applicants == 0
        assert info.total_cities == 0
        assert info.avg_cities == 0.0


class TestGetAllianceById:
    @pytest.mark.asyncio
    async def test_returns_alliance_on_success(self):
        mock_response = {"data": {"alliances": {"data": [_ALLIANCE_DATA]}}}
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_id(7)
        assert info is not None
        assert info.alliance_id == 7
        assert info.name == "The Rose"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        mock_response = {"data": {"alliances": {"data": []}}}
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_id(9999)
        assert info is None


class TestGetAllianceByName:
    @pytest.mark.asyncio
    async def test_returns_alliance_on_match(self):
        mock_response = {"data": {"alliances": {"data": [_ALLIANCE_DATA]}}}
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_name("The Rose")
        assert info is not None
        assert info.name == "The Rose"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        mock_response = {"data": {"alliances": {"data": []}}}
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_name("Nobody")
        assert info is None


class TestParseLastActiveUnix:
    def test_iso_with_timezone(self):
        from pnw_api import _parse_last_active_unix
        ts = _parse_last_active_unix("2024-03-20T12:00:00+00:00")
        assert ts > 0

    def test_iso_without_timezone_treated_as_utc(self):
        from pnw_api import _parse_last_active_unix
        ts_with = _parse_last_active_unix("2024-03-20T12:00:00+00:00")
        ts_without = _parse_last_active_unix("2024-03-20T12:00:00")
        assert ts_with == ts_without

    def test_empty_string_returns_zero(self):
        from pnw_api import _parse_last_active_unix
        assert _parse_last_active_unix("") == 0

    def test_invalid_string_returns_zero(self):
        from pnw_api import _parse_last_active_unix
        assert _parse_last_active_unix("not a date") == 0


# ---------------------------------------------------------------------------
# Shared mock alliance data (for get_alliance_by_id / get_alliance_by_name tests)
# ---------------------------------------------------------------------------

_ALLIANCE_API_DATA = {
    "id": "100",
    "name": "Test Alliance",
    "acronym": "TA",
    "score": 50000.0,
    "average_score": 1000.0,
    "color": "green",
    "flag": "https://example.com/flag.png",
    "discord_link": "https://discord.gg/test",
    "nations": [
        # active member
        {"id": "1", "num_cities": 10, "alliance_position": "MEMBER", "vacation_mode_turns": 0},
        # active officer
        {"id": "2", "num_cities": 15, "alliance_position": "OFFICER", "vacation_mode_turns": 0},
        # applicant — excluded from active count
        {"id": "3", "num_cities": 5, "alliance_position": "APPLICANT", "vacation_mode_turns": 0},
        # vacation mode — excluded from active count
        {"id": "4", "num_cities": 8, "alliance_position": "MEMBER", "vacation_mode_turns": 12},
    ],
}


# ---------------------------------------------------------------------------
# PnWClient.get_alliance_by_id tests
# ---------------------------------------------------------------------------


class TestGetAllianceById:
    @pytest.mark.asyncio
    async def test_returns_alliance_on_success(self):
        mock_response = {"data": {"alliances": {"data": [_ALLIANCE_API_DATA]}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_id(100)

        assert info is not None
        assert info.alliance_id == 100
        assert info.name == "Test Alliance"
        assert info.acronym == "TA"
        assert info.score == 50000.0
        assert info.average_score == 1000.0
        assert info.color == "green"
        assert info.flag == "https://example.com/flag.png"
        assert info.discord_link == "https://discord.gg/test"
        # only the 2 non-vmode, non-applicant members count as active
        assert info.num_members == 2
        assert info.num_applicants == 1
        assert info.total_cities == 25   # 10 + 15
        assert info.avg_cities == 12.5

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        mock_response = {"data": {"alliances": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_id(9999)

        assert info is None

    @pytest.mark.asyncio
    async def test_raises_on_api_error(self):
        client = PnWClient(api_key="dummy")

        with patch.object(
            client,
            "_query",
            new=AsyncMock(side_effect=RuntimeError("PnW API returned errors: Unauthorized")),
        ):
            with pytest.raises(RuntimeError, match="Unauthorized"):
                await client.get_alliance_by_id(1)


# ---------------------------------------------------------------------------
# PnWClient.get_alliance_by_name tests
# ---------------------------------------------------------------------------


class TestGetAllianceByName:
    @pytest.mark.asyncio
    async def test_returns_alliance_on_match(self):
        mock_response = {"data": {"alliances": {"data": [_ALLIANCE_API_DATA]}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_name("Test Alliance")

        assert info is not None
        assert info.name == "Test Alliance"
        assert info.num_members == 2

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self):
        mock_response = {"data": {"alliances": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            info = await client.get_alliance_by_name("Nobody")

        assert info is None

    @pytest.mark.asyncio
    async def test_passes_name_as_list_variable(self):
        """The query must send name as a list so the [String] variable type is satisfied."""
        mock_response = {"data": {"alliances": {"data": []}}}
        captured: list = []

        async def capturing_query(query_str, variables):
            captured.append(variables)
            return mock_response

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=capturing_query):
            await client.get_alliance_by_name("Some Alliance")

        assert captured, "expected _query to be called"
        assert captured[0]["name"] == ["Some Alliance"]


# ---------------------------------------------------------------------------
# PnWClient.get_nation_by_discord_tag tests
# ---------------------------------------------------------------------------


class TestGetNationByDiscordTag:
    @pytest.mark.asyncio
    async def test_returns_nation_on_match(self):
        mock_response = {"data": {"nations": {"data": [_NATION_DATA]}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            nation = await client.get_nation_by_discord_tag("testuser")

        assert nation is not None
        assert nation.discord_tag == "testuser"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_match(self):
        mock_response = {"data": {"nations": {"data": []}}}

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=AsyncMock(return_value=mock_response)):
            nation = await client.get_nation_by_discord_tag("ghost")

        assert nation is None

    @pytest.mark.asyncio
    async def test_passes_discord_tag_as_list_variable(self):
        """The query must send discord as a list so the [String] variable type is satisfied."""
        mock_response = {"data": {"nations": {"data": []}}}
        captured: list = []

        async def capturing_query(query_str, variables):
            captured.append(variables)
            return mock_response

        client = PnWClient(api_key="dummy")

        with patch.object(client, "_query", new=capturing_query):
            await client.get_nation_by_discord_tag("someuser")

        assert captured, "expected _query to be called"
        assert captured[0]["discord"] == ["someuser"]


# ---------------------------------------------------------------------------
# Database gov-role config tests
# ---------------------------------------------------------------------------


class TestGovRoles:
    def test_returns_all_none_by_default(self):
        db = _make_db()
        roles = db.get_gov_roles(1)
        assert roles == {"econ": None, "milcom": None, "ia": None, "gov": None}

    def test_set_and_get_all_roles(self):
        db = _make_db()
        db.set_gov_roles(1, {"econ": 111, "milcom": 222, "ia": 333, "gov": 444})
        roles = db.get_gov_roles(1)
        assert roles["econ"] == 111
        assert roles["milcom"] == 222
        assert roles["ia"] == 333
        assert roles["gov"] == 444

    def test_partial_roles_preserved(self):
        db = _make_db()
        db.set_gov_roles(1, {"econ": 111, "milcom": None, "ia": None, "gov": None})
        roles = db.get_gov_roles(1)
        assert roles["econ"] == 111
        assert roles["milcom"] is None

    def test_set_gov_roles_overwrites(self):
        db = _make_db()
        db.set_gov_roles(1, {"econ": 111, "milcom": None, "ia": None, "gov": None})
        db.set_gov_roles(1, {"econ": 222, "milcom": None, "ia": None, "gov": None})
        assert db.get_gov_roles(1)["econ"] == 222

    def test_gov_roles_isolated_per_guild(self):
        db = _make_db()
        db.set_gov_roles(1, {"econ": 111, "milcom": None, "ia": None, "gov": None})
        db.set_gov_roles(2, {"econ": 999, "milcom": None, "ia": None, "gov": None})
        assert db.get_gov_roles(1)["econ"] == 111
        assert db.get_gov_roles(2)["econ"] == 999

    def test_role_ids_returned_as_int(self):
        db = _make_db()
        db.set_gov_roles(1, {"econ": 123456789012345678, "milcom": None, "ia": None, "gov": None})
        roles = db.get_gov_roles(1)
        assert isinstance(roles["econ"], int)
        assert roles["econ"] == 123456789012345678


# ---------------------------------------------------------------------------
# _parse_resource_loot tests
# ---------------------------------------------------------------------------


class TestParseResourceLoot:
    def test_standard_format_all_resources(self):
        loot_info = (
            "The attacking forces looted 3 Gasoline, 2 Munitions, 1 Aluminum, 1 Steel from the nation."
        )
        _, gas, mun, alum, steel = _parse_resource_loot(loot_info)
        assert gas == 3.0
        assert mun == 2.0
        assert alum == 1.0
        assert steel == 1.0

    def test_comma_formatted_numbers(self):
        loot_info = "looted 1,234 Gasoline, 5,678 Munitions, 900 Aluminum, 100 Steel."
        _, gas, mun, alum, steel = _parse_resource_loot(loot_info)
        assert gas == 1234.0
        assert mun == 5678.0
        assert alum == 900.0
        assert steel == 100.0

    def test_partial_resources_missing_values_default_to_zero(self):
        loot_info = "The attacking forces looted 5 Steel from the nation."
        _, gas, mun, alum, steel = _parse_resource_loot(loot_info)
        assert gas == 0.0
        assert mun == 0.0
        assert alum == 0.0
        assert steel == 5.0

    def test_case_insensitive(self):
        _, gas, _, _, _ = _parse_resource_loot("looted 10 GASOLINE")
        assert gas == 10.0
        _, _, mun, _, _ = _parse_resource_loot("looted 7 munitions")
        assert mun == 7.0

    def test_empty_string_returns_all_zeros(self):
        _, gas, mun, alum, steel = _parse_resource_loot("")
        assert gas == mun == alum == steel == 0.0

    def test_no_match_returns_zeros(self):
        _, gas, mun, alum, steel = _parse_resource_loot("The forces attacked but looted nothing.")
        assert gas == mun == alum == steel == 0.0

    def test_decimal_quantities(self):
        loot_info = "looted 3.5 Gasoline, 2.25 Munitions."
        _, gas, mun, alum, steel = _parse_resource_loot(loot_info)
        assert gas == 3.5
        assert mun == 2.25
        assert alum == 0.0
        assert steel == 0.0

    def test_zero_quantity(self):
        loot_info = "looted 0 Gasoline, 0 Munitions, 0 Aluminum, 0 Steel."
        _, gas, mun, alum, steel = _parse_resource_loot(loot_info)
        assert gas == 0.0
        assert mun == 0.0
        assert alum == 0.0
        assert steel == 0.0


# ---------------------------------------------------------------------------
# get_alliance_damage tests
# ---------------------------------------------------------------------------

_CUTOFF = datetime(2024, 1, 1, tzinfo=timezone.utc)
_RECENT_DATE = "2024-01-15T00:00:00+00:00"
_OLD_DATE = "2023-12-01T00:00:00+00:00"

_NO_MORE_PAGES = {"hasMorePages": False}
_EMPTY_WARATTACKS = {
    "data": {
        "warattacks": {
            "data": [],
            "paginatorInfo": _NO_MORE_PAGES,
        }
    }
}


def _wars_response(wars: list, has_more: bool = False) -> dict:
    return {
        "data": {
            "wars": {
                "data": wars,
                "paginatorInfo": {"hasMorePages": has_more},
            }
        }
    }


def _warattacks_response(attacks: list, has_more: bool = False) -> dict:
    return {
        "data": {
            "warattacks": {
                "data": attacks,
                "paginatorInfo": {"hasMorePages": has_more},
            }
        }
    }


def _make_war(
    war_id: str,
    att_id: str,
    def_id: str,
    att_alliance_id: str,
    def_alliance_id: str,
    att_name: str = "Attacker",
    def_name: str = "Defender",
    att_cities: int = 10,
    def_cities: int = 8,
    date: str = _RECENT_DATE,
) -> dict:
    """Build a minimal Phase-1 war dict (no damage totals — those live in Phase 2)."""
    return {
        "id": war_id,
        "att_id": att_id,
        "def_id": def_id,
        "att_alliance_id": att_alliance_id,
        "def_alliance_id": def_alliance_id,
        "date": date,
        "attacker": {"nation_name": att_name, "num_cities": att_cities},
        "defender": {"nation_name": def_name, "num_cities": def_cities},
    }


def _make_attack(
    att_id: str,
    attack_type: str = "AIRVINFRA",
    victor: str | None = None,
    money_stolen: float = 0.0,
    money_looted: float = 0.0,
    infra_destroyed_value: float = 0.0,
    def_gas_used: float = 0.0,
    def_mun_used: float = 0.0,
    gasoline_looted: float = 0.0,
    munitions_looted: float = 0.0,
    aluminum_looted: float = 0.0,
    steel_looted: float = 0.0,
) -> dict:
    """Build a per-attack dict matching the Phase-2 warattacks schema."""
    return {
        "att_id": att_id,
        "type": attack_type,
        "victor": victor if victor is not None else att_id,
        "money_stolen": money_stolen,
        "money_looted": money_looted,
        "infra_destroyed_value": infra_destroyed_value,
        "def_gas_used": def_gas_used,
        "def_mun_used": def_mun_used,
        "gasoline_looted": gasoline_looted,
        "munitions_looted": munitions_looted,
        "aluminum_looted": aluminum_looted,
        "steel_looted": steel_looted,
    }


class TestGetAllianceDamage:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_wars(self):
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=_wars_response([]))):
            result = await client.get_alliance_damage(42, _CUTOFF)
        assert result == {}

    @pytest.mark.asyncio
    async def test_offensive_war_accumulates_infra_and_money(self):
        war = _make_war("1", "100", "200", "42", "99", "Attacker", "Defender", 10, 8)
        # Per-attack data provides infra/money/consumption (Locutus model).
        attack = _make_attack(
            "100",
            attack_type="AIRVINFRA",
            infra_destroyed_value=1_000_000.0,
            money_stolen=500_000.0,
            def_gas_used=80.0,
            def_mun_used=40.0,
        )
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(side_effect=[_wars_response([war]), _warattacks_response([attack])]),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        assert 100 in result
        entry = result[100]
        assert entry["nation_name"] == "Attacker"
        assert entry["num_cities"] == 10
        assert entry["infra_value"] == 1_000_000.0
        assert entry["money_looted"] == 500_000.0
        assert entry["def_gas_used"] == 80.0
        assert entry["def_mun_used"] == 40.0
        assert entry["def_alum_used"] == 0.0
        assert entry["def_steel_used"] == 0.0
        # Enemy not in result
        assert 200 not in result

    @pytest.mark.asyncio
    async def test_defensive_war_accumulates_infra_and_enemy_resources(self):
        # Enemy (200) attacked our member (100).  Our member counterattacks,
        # so att_id=100 appears in Phase-2 attacks even though 100 is the war's
        # defender.  This is the core of Locutus's per-attack attribution model.
        war = _make_war("2", "200", "100", "99", "42", "Enemy", "Defender", 12, 9)
        counterattack = _make_attack(
            "100",  # our member counterattacking
            attack_type="GROUND",
            infra_destroyed_value=800_000.0,
            money_stolen=200_000.0,
            def_gas_used=60.0,
            def_mun_used=30.0,
        )
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(side_effect=[_wars_response([war]), _warattacks_response([counterattack])]),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        assert 100 in result
        entry = result[100]
        assert entry["nation_name"] == "Defender"
        assert entry["infra_value"] == 800_000.0
        assert entry["money_looted"] == 200_000.0
        # def_*_used = enemy's resource cost defending against our counterattack
        assert entry["def_gas_used"] == 60.0
        assert entry["def_mun_used"] == 30.0
        assert entry["def_alum_used"] == 0.0
        assert entry["def_steel_used"] == 0.0
        # Enemy attacker not in result
        assert 200 not in result

    @pytest.mark.asyncio
    async def test_ground_attack_resource_loot_phase2(self):
        war = _make_war("1", "100", "200", "42", "99")
        ground_attack = {
            "att_id": "100",
            "type": "GROUND",
            "victor": "100",
            "money_stolen": 0.0,
            "money_looted": 0.0,
            "infra_destroyed_value": 0.0,
            "def_gas_used": 0.0,
            "def_mun_used": 0.0,
            "gasoline_looted": 5.0,
            "munitions_looted": 3.0,
            "aluminum_looted": 2.0,
            "steel_looted": 1.0,
        }
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[
                    _wars_response([war]),
                    _warattacks_response([ground_attack]),
                ]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        entry = result[100]
        assert entry["gas_looted"] == 5.0
        assert entry["mun_looted"] == 3.0
        assert entry["alum_looted"] == 2.0
        assert entry["steel_looted"] == 1.0
        # GROUND loot does not go into vict_* fields
        assert entry["vict_gas_looted"] == 0.0
        assert entry["vict_mun_looted"] == 0.0
        assert entry["vict_alum_looted"] == 0.0
        assert entry["vict_steel_looted"] == 0.0

    @pytest.mark.asyncio
    async def test_victory_attack_adds_money_and_resources_to_both_columns(self):
        war = _make_war("1", "100", "200", "42", "99")
        victory_attack = {
            "att_id": "100",
            "type": "VICTORY",
            "victor": "100",
            "money_stolen": 0.0,
            "money_looted": 1_000_000.0,
            "infra_destroyed_value": 0.0,
            "def_gas_used": 0.0,
            "def_mun_used": 0.0,
            "gasoline_looted": 10.0,
            "munitions_looted": 5.0,
            "aluminum_looted": 3.0,
            "steel_looted": 2.0,
        }
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[
                    _wars_response([war]),
                    _warattacks_response([victory_attack]),
                ]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        entry = result[100]
        # VICTORY money_stolen is added to money_looted
        assert entry["money_looted"] == 1_000_000.0
        # VICTORY resources appear in both gas_looted and vict_gas_looted
        assert entry["gas_looted"] == 10.0
        assert entry["mun_looted"] == 5.0
        assert entry["alum_looted"] == 3.0
        assert entry["steel_looted"] == 2.0
        assert entry["vict_gas_looted"] == 10.0
        assert entry["vict_mun_looted"] == 5.0
        assert entry["vict_alum_looted"] == 3.0
        assert entry["vict_steel_looted"] == 2.0

    @pytest.mark.asyncio
    async def test_failed_exchange_yields_no_loot(self):
        war = _make_war("1", "100", "200", "42", "99")
        # Attacker lost this ground exchange (victor=200, not 100)
        failed_attack = {
            "att_id": "100",
            "type": "GROUND",
            "victor": "200",
            "money_stolen": 0.0,
            "money_looted": 0.0,
            "infra_destroyed_value": 0.0,
            "def_gas_used": 0.0,
            "def_mun_used": 0.0,
            "gasoline_looted": 0.0,
            "munitions_looted": 0.0,
            "aluminum_looted": 0.0,
            "steel_looted": 0.0,
        }
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[
                    _wars_response([war]),
                    _warattacks_response([failed_attack]),
                ]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        entry = result[100]
        assert entry["gas_looted"] == 0.0
        assert entry["money_looted"] == 0.0

    @pytest.mark.asyncio
    async def test_wars_older_than_cutoff_are_excluded(self):
        old_war = _make_war("1", "100", "200", "42", "99", date=_OLD_DATE)
        client = PnWClient(api_key="dummy")
        with patch.object(client, "_query", new=AsyncMock(return_value=_wars_response([old_war]))):
            result = await client.get_alliance_damage(42, _CUTOFF)
        assert result == {}

    @pytest.mark.asyncio
    async def test_intra_alliance_defensive_war_skipped(self):
        """Defensive block is skipped for intra-alliance wars to avoid double-counting."""
        war = _make_war("1", "100", "101", "42", "42", "Attacker", "Defender", 10, 8)
        # Infra credited to attacker (100) via per-attack data.
        attack = _make_attack("100", attack_type="AIRVINFRA", infra_destroyed_value=500_000.0)
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(side_effect=[_wars_response([war]), _warattacks_response([attack])]),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        # Attacker IS counted (offensive block runs)
        assert 100 in result
        assert result[100]["infra_value"] == 500_000.0
        # Defender is NOT counted (defensive block skipped for intra-alliance)
        assert 101 not in result

    @pytest.mark.asyncio
    async def test_war_id_deduplicated_in_offensive_block(self):
        """The same war_id must appear only once in Phase 2 even if the API returns
        the same war entry twice (e.g. a pagination edge case)."""
        war = _make_war("1", "100", "200", "42", "99")
        ground_attack = {
            "att_id": "100",
            "type": "GROUND",
            "victor": "100",
            "money_stolen": 0.0,
            "money_looted": 0.0,
            "infra_destroyed_value": 0.0,
            "def_gas_used": 0.0,
            "def_mun_used": 0.0,
            "gasoline_looted": 5.0,
            "munitions_looted": 0.0,
            "aluminum_looted": 0.0,
            "steel_looted": 0.0,
        }
        # Simulate the same war returned on two pages (API edge case)
        page1 = _wars_response([war], has_more=True)
        page2 = _wars_response([war], has_more=False)
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[page1, page2, _warattacks_response([ground_attack])],
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        # Without deduplication the resource loot would be 10 (processed twice).
        # With deduplication it is 5.
        assert result[100]["gas_looted"] == 5.0

    @pytest.mark.asyncio
    async def test_multiple_wars_accumulate_per_nation(self):
        war1 = _make_war("1", "100", "201", "42", "99", "Our Nation", "Enemy1", 10, 5)
        war2 = _make_war("2", "100", "202", "42", "99", "Our Nation", "Enemy2", 10, 6)
        # Both wars are in the same Phase-2 batch; attacks provide all damage data.
        attack1 = _make_attack(
            "100", infra_destroyed_value=1_000_000.0, money_stolen=200_000.0, def_gas_used=50.0
        )
        attack2 = _make_attack(
            "100", infra_destroyed_value=500_000.0, money_stolen=100_000.0, def_gas_used=30.0
        )
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[_wars_response([war1, war2]), _warattacks_response([attack1, attack2])]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        entry = result[100]
        assert entry["infra_value"] == 1_500_000.0
        assert entry["money_looted"] == 300_000.0
        assert entry["def_gas_used"] == 80.0

    @pytest.mark.asyncio
    async def test_non_alliance_attacker_skipped_in_phase2(self):
        """Attacks by nations NOT in the alliance should be ignored in Phase 2."""
        war = _make_war("1", "100", "200", "42", "99")
        # Counterattack by the enemy (nation 200), who is NOT in our alliance
        enemy_attack = {
            "att_id": "200",
            "type": "GROUND",
            "victor": "200",
            "money_stolen": 999_999.0,
            "money_looted": 0.0,
            "infra_destroyed_value": 0.0,
            "def_gas_used": 0.0,
            "def_mun_used": 0.0,
            "gasoline_looted": 999.0,
            "munitions_looted": 0.0,
            "aluminum_looted": 0.0,
            "steel_looted": 0.0,
        }
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[
                    _wars_response([war]),
                    _warattacks_response([enemy_attack]),
                ]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        # Nation 200 is NOT in our alliance, so their loot must not appear
        assert 200 not in result
        # Nation 100's stats remain at zero (no winning attacks from our side)
        assert result[100]["gas_looted"] == 0.0
        assert result[100]["money_looted"] == 0.0

    @pytest.mark.asyncio
    async def test_per_attack_def_consumption_excludes_enemy_counterattack_gas(self):
        """Per-attack tracking (Locutus model) only counts enemy gas consumed
        when defending against OUR attacks, not when the enemy counterattacks us.

        With war-level totals the enemy's counterattack gas would be incorrectly
        included in our member's 'def_gas_used' stat.
        """
        war = _make_war("1", "100", "200", "42", "99")
        # Our member (100) attacks once; enemy spends 30 gas defending.
        our_attack = _make_attack("100", attack_type="AIRVINFRA", def_gas_used=30.0)
        # Enemy (200) counterattacks; our member spends 20 gas defending.
        # This 20 gas is the ENEMY's consumption when THEY attack, so it should
        # NOT appear in our member's def_gas_used (enemy 200 not in results).
        enemy_counter = _make_attack(
            "200", attack_type="AIRVINFRA", def_gas_used=20.0
        )
        client = PnWClient(api_key="dummy")
        with patch.object(
            client,
            "_query",
            new=AsyncMock(
                side_effect=[
                    _wars_response([war]),
                    _warattacks_response([our_attack, enemy_counter]),
                ]
            ),
        ):
            result = await client.get_alliance_damage(42, _CUTOFF)

        # Only the 30 gas from our member's attack is counted; the enemy
        # counterattack gas (20) is excluded because att_id=200 is not in results.
        assert result[100]["def_gas_used"] == 30.0

