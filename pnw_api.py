"""Thin async wrapper around the Politics and War GraphQL and REST APIs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

PNW_GRAPHQL_URL = "https://api.politicsandwar.com/graphql"
PNW_REST_URL = "https://politicsandwar.com/api/nation/"

# Maximum military units per city (used for capacity percentage calculations)
MAX_SOLDIERS_PER_CITY = 15_000
MAX_TANKS_PER_CITY = 1_250
MAX_AIRCRAFT_PER_CITY = 75
MAX_SHIPS_PER_CITY = 15

# Maximum concurrent defensive wars per nation
MAX_DEFENSIVE_SLOTS = 3


@dataclass
class Nation:
    nation_id: int
    nation_name: str
    leader_name: str
    discord_tag: str  # the Discord handle stored on the nation (may be empty)
    num_cities: int = 0
    score: float = 0.0
    # GraphQL sets this to a raw timestamp; REST sets it to "X minutes ago"
    last_active: str = ""
    # Only populated by get_nation_rest; 0 when sourced from GraphQL
    minutes_since_active: int = 0
    # Military units
    soldiers: int = 0
    tanks: int = 0
    aircraft: int = 0
    ships: int = 0
    # Alliance info
    alliance_id: int = 0
    alliance_name: str = ""


_NATION_FIELDS = """
    id
    nation_name
    leader_name
    discord
    num_cities
    score
    last_active
    soldiers
    tanks
    aircraft
    ships
    alliance_id
    alliance {
        name
    }
"""


class PnWClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    # ------------------------------------------------------------------
    # GraphQL helpers
    # ------------------------------------------------------------------

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = f"{PNW_GRAPHQL_URL}?api_key={self._api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"query": query, "variables": variables},
                headers={"Content-Type": "application/json"},
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        if "errors" in data:
            raise RuntimeError(
                "PnW API returned errors: "
                + "; ".join(e.get("message", "") for e in data["errors"])
            )
        return data

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nation(n: dict[str, Any]) -> Nation:
        alliance = n.get("alliance") or {}
        return Nation(
            nation_id=int(n["id"]),
            nation_name=n.get("nation_name", ""),
            leader_name=n.get("leader_name", ""),
            discord_tag=n.get("discord", "") or "",
            num_cities=int(n.get("num_cities") or 0),
            score=float(n.get("score") or 0.0),
            last_active=n.get("last_active", "") or "",
            soldiers=int(n.get("soldiers") or 0),
            tanks=int(n.get("tanks") or 0),
            aircraft=int(n.get("aircraft") or 0),
            ships=int(n.get("ships") or 0),
            alliance_id=int(n.get("alliance_id") or 0),
            alliance_name=alliance.get("name", "") or "",
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def get_nation(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID. Returns None if not found."""
        query = f"""
        query GetNation($id: [Int]) {{
            nations(id: $id, first: 1) {{
                data {{
                    {_NATION_FIELDS}
                }}
            }}
        }}
        """
        data = await self._query(query, {"id": [nation_id]})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        if not nations:
            return None
        return self._parse_nation(nations[0])

    async def get_nation_by_name(self, name: str) -> Optional[Nation]:
        """Search for a nation by name. Returns the first match or None."""
        query = f"""
        query GetNationByName($name: String) {{
            nations(nation_name: $name, first: 1) {{
                data {{
                    {_NATION_FIELDS}
                }}
            }}
        }}
        """
        data = await self._query(query, {"name": name})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        if not nations:
            return None
        return self._parse_nation(nations[0])

    async def get_alliance_members(self, alliance_ids: list[int]) -> list[Nation]:
        """Fetch all non-vacation-mode members of the given alliances."""
        query = f"""
        query GetAllianceMembers($alliance_id: [Int]) {{
            nations(alliance_id: $alliance_id, vmode: false, first: 500) {{
                data {{
                    {_NATION_FIELDS}
                }}
            }}
        }}
        """
        data = await self._query(query, {"alliance_id": alliance_ids})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        return [self._parse_nation(n) for n in nations]

    async def get_active_war_counts(self, nation_ids: list[int]) -> dict[int, int]:
        """Return a mapping of nation_id -> active defensive war count.

        Nations not present in the returned dict have zero active defensive wars.
        """
        if not nation_ids:
            return {}
        query = """
        query GetActiveWars($defid: [Int]) {
            wars(defid: $defid, active: true, first: 500) {
                data {
                    def_id
                }
            }
        }
        """
        data = await self._query(query, {"defid": nation_ids})
        wars = data.get("data", {}).get("wars", {}).get("data", [])
        counts: dict[int, int] = {}
        for war in wars:
            def_id = int(war["def_id"])
            counts[def_id] = counts.get(def_id, 0) + 1
        return counts

    async def get_nation_rest(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID using the PnW v1 REST API.

        Returns ``None`` if the nation is not found or the API reports failure.
        """
        url = f"{PNW_REST_URL}?id={nation_id}&key={self._api_key}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        if not data.get("success"):
            return None
        minutes = int(data.get("minutessinceactive") or 0)
        return Nation(
            nation_id=int(data.get("nationid") or nation_id),
            nation_name=data.get("name", ""),
            leader_name=data.get("leadername", ""),
            discord_tag="",  # v1 REST API does not expose the Discord field
            num_cities=int(data.get("cities") or 0),
            score=float(data.get("score") or 0.0),
            last_active=f"{minutes} minutes ago" if minutes else "",
            minutes_since_active=minutes,
        )

    @staticmethod
    def discord_matches(discord_tag: str, username: str) -> bool:
        """
        Return True if the nation's stored Discord tag matches the given
        Discord username.

        Comparison is case-insensitive and strips surrounding whitespace.
        Both the bare ``username`` and the legacy ``username#discriminator``
        format are accepted.
        """
        stored = discord_tag.strip().lower()
        if not stored:
            return False
        check = username.strip().lower()
        # Accept "username" or "username#0000" stored in the nation field
        return stored == check or stored.startswith(check + "#")
