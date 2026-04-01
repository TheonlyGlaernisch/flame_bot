"""Thin async wrapper around the Politics and War GraphQL and REST APIs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

PNW_GRAPHQL_URL = "https://api.politicsandwar.com/graphql"
PNW_REST_URL = "https://politicsandwar.com/api/nation/"


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
    # Public helpers
    # ------------------------------------------------------------------

    async def get_nation(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID. Returns None if not found."""
        query = """
        query GetNation($id: [Int]) {
            nations(id: $id, first: 1) {
                data {
                    id
                    nation_name
                    leader_name
                    discord
                    num_cities
                    score
                    last_active
                }
            }
        }
        """
        data = await self._query(query, {"id": [nation_id]})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        if not nations:
            return None
        n = nations[0]
        return Nation(
            nation_id=int(n["id"]),
            nation_name=n.get("nation_name", ""),
            leader_name=n.get("leader_name", ""),
            discord_tag=n.get("discord", "") or "",
            num_cities=int(n.get("num_cities") or 0),
            score=float(n.get("score") or 0.0),
            last_active=n.get("last_active", "") or "",
        )

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
