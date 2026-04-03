"""Thin async wrapper around the Politics and War GraphQL and REST APIs."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

PNW_GRAPHQL_URL = "https://api.politicsandwar.com/graphql"
PNW_TEST_GRAPHQL_URL = "https://test.politicsandwar.com/graphql"
PNW_REST_URL = "https://politicsandwar.com/api/nation/"
PNW_TEST_REST_URL = "https://test.politicsandwar.com/api/"

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
    # GraphQL sets this to a raw ISO timestamp; REST sets it to "X minutes ago"
    last_active: str = ""
    # Unix timestamp derived from last_active (0 when unavailable, e.g. REST path)
    last_active_unix: int = 0
    # Only populated by get_nation_rest; 0 when sourced from GraphQL
    minutes_since_active: int = 0
    # Military units
    soldiers: int = 0
    tanks: int = 0
    aircraft: int = 0
    ships: int = 0
    missiles: int = 0
    nukes: int = 0
    # National projects — list of short abbreviations for built projects
    projects_built: list[str] = field(default_factory=list)

    @property
    def num_projects(self) -> int:
        return len(self.projects_built)

    # Alliance info
    alliance_id: int = 0
    alliance_name: str = ""
    # Alliance role and tenure
    alliance_position: str = ""  # "MEMBER", "OFFICER", "HEIR", "LEADER", …
    alliance_seniority: int = 0  # days in current alliance


@dataclass
class AllianceInfo:
    """Aggregated statistics for a Politics and War alliance."""

    alliance_id: int
    name: str
    acronym: str
    score: float
    average_score: float
    color: str
    flag: str
    discord_link: str
    num_members: int    # active (non-vmode, non-applicant) members
    num_applicants: int
    total_cities: int   # sum of cities across active members
    avg_cities: float


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
    missiles
    nukes
    iron_works
    bauxite_works
    arms_stockpile
    emergency_gasoline_reserve
    mass_irrigation
    international_trade_center
    missile_launch_pad
    nuclear_research_facility
    iron_dome
    vital_defense_system
    space_program
    uranium_enrichment_program
    advanced_urban_planning
    government_support_agency
    research_and_development_center
    propaganda_bureau
    telecommunications_satellite
    green_technologies
    arable_land_agency
    clinical_research_center
    urban_planning
    advanced_engineering_corps
    pirate_economy
    recycling_initiative
    specialized_police_training_program
    metropolitan_planning
    moon_landing
    surveillance_network
    nuclear_launch_facility
    alliance_id
    alliance_position
    alliance_seniority
    alliance {
        name
    }
"""

# Mapping of GraphQL field name → short abbreviation shown in /whois.
_PROJECT_ABBREVS: dict[str, str] = {
    "iron_works": "IW",
    "bauxite_works": "BW",
    "arms_stockpile": "AS",
    "emergency_gasoline_reserve": "EGR",
    "mass_irrigation": "MI",
    "international_trade_center": "ITC",
    "missile_launch_pad": "MLP",
    "nuclear_research_facility": "NRF",
    "iron_dome": "ID",
    "vital_defense_system": "VDS",
    "space_program": "SP",
    "uranium_enrichment_program": "UEP",
    "advanced_urban_planning": "ACP",
    "government_support_agency": "GSA",
    "research_and_development_center": "RDC",
    "propaganda_bureau": "PB",
    "telecommunications_satellite": "TS",
    "green_technologies": "GT",
    "arable_land_agency": "ALA",
    "clinical_research_center": "CRC",
    "urban_planning": "UP",
    "advanced_engineering_corps": "AEC",
    "pirate_economy": "PE",
    "recycling_initiative": "RI",
    "specialized_police_training_program": "SPTP",
    "metropolitan_planning": "MP",
    "moon_landing": "ML",
    "surveillance_network": "SN",
    "nuclear_launch_facility": "NLF",
}

_ALLIANCE_MEMBER_FIELDS = """
    id
    num_cities
    alliance_position
    vacation_mode_turns
"""


def _parse_last_active_unix(value: str) -> int:
    """Convert an ISO datetime string to a Unix timestamp, or return 0."""
    if not value:
        return 0
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, OSError):
        return 0


class PnWClient:
    def __init__(
        self,
        api_key: str,
        graphql_url: str = PNW_GRAPHQL_URL,
        rest_url: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._graphql_url = graphql_url
        # When set, all nation/alliance lookups use this REST base URL instead
        # of GraphQL (e.g. "https://test.politicsandwar.com/api/").
        self._rest_url = rest_url
        self._session: aiohttp.ClientSession | None = None

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_session(self) -> aiohttp.ClientSession:
        """Return the shared HTTP session, creating it lazily if needed."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # GraphQL helpers
    # ------------------------------------------------------------------

    async def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._graphql_url}?api_key={self._api_key}"
        session = self._get_session()
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
        la_str = n.get("last_active", "") or ""
        projects_built = sorted(abbr for field_name, abbr in _PROJECT_ABBREVS.items() if n.get(field_name))
        return Nation(
            nation_id=int(n["id"]),
            nation_name=n.get("nation_name", ""),
            leader_name=n.get("leader_name", ""),
            discord_tag=n.get("discord", "") or "",
            num_cities=int(n.get("num_cities") or 0),
            score=float(n.get("score") or 0.0),
            last_active=la_str,
            last_active_unix=_parse_last_active_unix(la_str),
            soldiers=int(n.get("soldiers") or 0),
            tanks=int(n.get("tanks") or 0),
            aircraft=int(n.get("aircraft") or 0),
            ships=int(n.get("ships") or 0),
            missiles=int(n.get("missiles") or 0),
            nukes=int(n.get("nukes") or 0),
            projects_built=projects_built,
            alliance_id=int(n.get("alliance_id") or 0),
            alliance_name=alliance.get("name", "") or "",
            alliance_position=n.get("alliance_position", "") or "",
            alliance_seniority=int(n.get("alliance_seniority") or 0),
        )

    @staticmethod
    def _parse_alliance(a: dict[str, Any]) -> AllianceInfo:
        nations = a.get("nations") or []
        active = [
            n for n in nations
            if n.get("alliance_position", "") not in ("", "APPLICANT", "NOALLIANCE")
            and int(n.get("vacation_mode_turns") or 0) == 0
        ]
        applicants = [n for n in nations if n.get("alliance_position", "") == "APPLICANT"]
        total_cities = sum(int(n.get("num_cities") or 0) for n in active)
        avg_cities = total_cities / len(active) if active else 0.0
        return AllianceInfo(
            alliance_id=int(a["id"]),
            name=a.get("name", ""),
            acronym=a.get("acronym", "") or "",
            score=float(a.get("score") or 0.0),
            average_score=float(a.get("average_score") or 0.0),
            color=a.get("color", "") or "",
            flag=a.get("flag", "") or "",
            discord_link=a.get("discord_link", "") or "",
            num_members=len(active),
            num_applicants=len(applicants),
            total_cities=total_cities,
            avg_cities=avg_cities,
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    async def get_nation(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID. Returns None if not found."""
        if self._rest_url is not None:
            return await self.get_nation_rest(nation_id)
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
        """Search for a nation by name (case-insensitive). Returns the first match or None."""
        if self._rest_url is not None:
            url = f"{self._rest_url}nation/?nation_name={name}&key={self._api_key}"
            session = self._get_session()
            async with session.get(url) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
            if not data.get("success"):
                return None
            minutes = int(data.get("minutessinceactive") or 0)
            return Nation(
                nation_id=int(data.get("nationid") or 0),
                nation_name=data.get("name", ""),
                leader_name=data.get("leadername", ""),
                discord_tag="",
                num_cities=int(data.get("cities") or 0),
                score=float(data.get("score") or 0.0),
                last_active=f"{minutes} minutes ago" if minutes else "",
                minutes_since_active=minutes,
            )
        query = f"""
        query GetNationByName($name: [String]) {{
            nations(nation_name: $name, first: 1) {{
                data {{
                    {_NATION_FIELDS}
                }}
            }}
        }}
        """
        data = await self._query(query, {"name": [name]})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        if not nations:
            return None
        return self._parse_nation(nations[0])

    async def get_nation_by_discord_tag(self, discord_tag: str) -> Optional[Nation]:
        """Search for a nation whose PnW discord field matches *discord_tag*.

        Returns the first match or None.  The comparison is done server-side
        by the PnW API; the caller should verify with :meth:`discord_matches`
        when an exact match is required.

        Not supported by the v1 REST API; always returns None in REST mode.
        """
        if self._rest_url is not None:
            return None
        query = f"""
        query GetNationByDiscord($discord: [String]) {{
            nations(discord: $discord, first: 1) {{
                data {{
                    {_NATION_FIELDS}
                }}
            }}
        }}
        """
        data = await self._query(query, {"discord": [discord_tag]})
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

    async def get_alliance_by_id(self, alliance_id: int) -> Optional[AllianceInfo]:
        """Fetch alliance statistics by numeric ID. Returns None if not found."""
        if self._rest_url is not None:
            return await self._get_alliance_rest(alliance_id)
        query = f"""
        query GetAlliance($id: [Int]) {{
            alliances(id: $id, first: 1) {{
                data {{
                    id
                    name
                    acronym
                    score
                    average_score
                    color
                    flag
                    discord_link
                    nations {{
                        {_ALLIANCE_MEMBER_FIELDS}
                    }}
                }}
            }}
        }}
        """
        data = await self._query(query, {"id": [alliance_id]})
        alliances = data.get("data", {}).get("alliances", {}).get("data", [])
        if not alliances:
            return None
        return self._parse_alliance(alliances[0])

    async def get_alliance_by_name(self, name: str) -> Optional[AllianceInfo]:
        """Search for an alliance by name (case-insensitive). Returns the first match or None.

        Not supported by the v1 REST API; always returns None in REST mode.
        """
        if self._rest_url is not None:
            return None
        query = f"""
        query GetAllianceByName($name: [String]) {{
            alliances(name: $name, first: 1) {{
                data {{
                    id
                    name
                    acronym
                    score
                    average_score
                    color
                    flag
                    discord_link
                    nations {{
                        {_ALLIANCE_MEMBER_FIELDS}
                    }}
                }}
            }}
        }}
        """
        data = await self._query(query, {"name": [name]})
        alliances = data.get("data", {}).get("alliances", {}).get("data", [])
        if not alliances:
            return None
        return self._parse_alliance(alliances[0])

    async def get_nation_rest(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID using the PnW v1 REST API.

        Uses ``self._rest_url`` as the base when set; falls back to the
        production REST URL otherwise.

        Returns ``None`` if the nation is not found or the API reports failure.
        """
        base = self._rest_url if self._rest_url is not None else "https://politicsandwar.com/api/"
        url = f"{base}nation/?id={nation_id}&key={self._api_key}"
        session = self._get_session()
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

    async def _get_alliance_rest(self, alliance_id: int) -> Optional[AllianceInfo]:
        """Fetch alliance info by numeric ID using the PnW v1 REST API.

        Returns ``None`` if the alliance is not found or the API reports failure.
        """
        base = self._rest_url if self._rest_url is not None else "https://politicsandwar.com/api/"
        url = f"{base}alliance/?allianceid={alliance_id}&key={self._api_key}"
        session = self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        if not data.get("success"):
            return None
        return AllianceInfo(
            alliance_id=int(data.get("allianceid") or alliance_id),
            name=data.get("name", ""),
            acronym=data.get("acronym", "") or "",
            score=float(data.get("score") or 0.0),
            average_score=0.0,
            color=data.get("color", "") or "",
            flag=data.get("flagurl", "") or "",
            discord_link=data.get("discord", "") or "",
            num_members=int(data.get("members") or 0),
            num_applicants=int(data.get("applicants") or 0),
            total_cities=0,
            avg_cities=0.0,
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
        # Accept "username" or "username#0000" stored in the nation field,
        # and also accept the reverse (check includes a discriminator suffix).
        return (
            stored == check
            or stored.startswith(check + "#")
            or check.startswith(stored + "#")
        )
