"""Thin async wrapper around the Politics and War GraphQL API."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import aiohttp

PNW_GRAPHQL_URL = "https://api.politicsandwar.com/graphql"


@dataclass
class Nation:
    nation_id: int
    nation_name: str
    leader_name: str
    discord_tag: str  # the Discord handle stored on the nation (may be empty)


class PnWClient:
    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    # ------------------------------------------------------------------
    # GraphQL helpers
    # ------------------------------------------------------------------

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = PNW_GRAPHQL_URL
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"query": query, "variables": variables},
                headers={
                    "Content-Type": "application/json",
                    "X-Api-Key": self._api_key,
                },
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
