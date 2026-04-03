"""MongoDB-backed storage for nation-to-Discord registrations."""
import re
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError  # noqa: F401 – re-exported for callers


class Database:
    def __init__(self, uri: str, *, _client=None) -> None:
        self._client = _client if _client is not None else MongoClient(uri)
        self._col = self._client["TRF"]["registrations"]
        self._col.create_index("discord_id", unique=True)
        self._col.create_index("nation_id", unique=True)
        self._guild_config = self._client["TRF"]["guild_config"]

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def register(self, discord_id: int, nation_id: int, discord_username: str = "") -> None:
        """Insert or replace a registration entry."""
        now = datetime.now(timezone.utc).isoformat()
        self._col.update_one(
            {"discord_id": str(discord_id)},
            {
                "$set": {
                    "discord_id": str(discord_id),
                    "nation_id": nation_id,
                    "registered_at": now,
                    "discord_username": discord_username,
                }
            },
            upsert=True,
        )

    def get_by_discord_id(self, discord_id: int) -> Optional[dict]:
        """Return the registration document for a Discord user, or None."""
        return self._col.find_one({"discord_id": str(discord_id)}, {"_id": 0})

    def get_by_nation_id(self, nation_id: int) -> Optional[dict]:
        """Return the registration document for a nation ID, or None."""
        return self._col.find_one({"nation_id": nation_id}, {"_id": 0})

    def get_by_discord_username(self, username: str) -> Optional[dict]:
        """Return the registration document for a Discord username (case-insensitive), or None."""
        pattern = re.compile(f"^{re.escape(username.strip())}$", re.IGNORECASE)
        return self._col.find_one({"discord_username": pattern}, {"_id": 0})

    def delete(self, discord_id: int) -> bool:
        """Remove a registration. Returns True if a row was deleted."""
        result = self._col.delete_one({"discord_id": str(discord_id)})
        return result.deleted_count > 0

    # ------------------------------------------------------------------
    # Guild config helpers
    # ------------------------------------------------------------------

    def get_slots_alliances(self, guild_id: int) -> list[int]:
        """Return the list of alliance IDs configured for /slots in this guild."""
        doc = self._guild_config.find_one({"guild_id": str(guild_id)}, {"_id": 0})
        if doc is None:
            return []
        return [int(a) for a in doc.get("slots_alliances", [])]

    def set_slots_alliances(self, guild_id: int, alliance_ids: list[int]) -> None:
        """Set the alliance IDs used by /slots for this guild."""
        self._guild_config.update_one(
            {"guild_id": str(guild_id)},
            {"$set": {"guild_id": str(guild_id), "slots_alliances": alliance_ids}},
            upsert=True,
        )

    # Gov-role config helpers ---------------------------------------------------

    _GOV_ROLE_KEYS = ("leader", "econ", "milcom", "ia", "gov")

    def get_gov_roles(self, guild_id: int) -> dict[str, int | None]:
        """Return the Discord role IDs configured for each gov department, or None if unset."""
        doc = self._guild_config.find_one({"guild_id": str(guild_id)}, {"_id": 0})
        stored = (doc or {}).get("gov_roles", {})
        return {k: (int(stored[k]) if stored.get(k) else None) for k in self._GOV_ROLE_KEYS}

    def set_gov_roles(self, guild_id: int, roles: dict[str, int | None]) -> None:
        """Persist the gov-department role mapping for a guild."""
        self._guild_config.update_one(
            {"guild_id": str(guild_id)},
            {"$set": {"guild_id": str(guild_id), "gov_roles": roles}},
            upsert=True,
        )

    # Grant channel config helpers -------------------------------------------

    def get_grant_channel(self, guild_id: int) -> int | None:
        """Return the channel ID configured for grant request posts, or None if unset."""
        doc = self._guild_config.find_one({"guild_id": str(guild_id)}, {"_id": 0})
        if doc is None:
            return None
        raw = doc.get("grant_channel_id")
        return int(raw) if raw else None

    def set_grant_channel(self, guild_id: int, channel_id: int | None) -> None:
        """Set (or clear) the grant request channel for a guild."""
        self._guild_config.update_one(
            {"guild_id": str(guild_id)},
            {"$set": {"guild_id": str(guild_id), "grant_channel_id": channel_id}},
            upsert=True,
        )
