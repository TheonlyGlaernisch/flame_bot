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
