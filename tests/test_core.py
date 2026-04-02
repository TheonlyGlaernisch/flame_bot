"""Tests for database.py and pnw_api.py (no Discord or network calls)."""
from unittest.mock import AsyncMock, MagicMock, patch

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from database import Database
from pnw_api import PnWClient


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
