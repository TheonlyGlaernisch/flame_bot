"""Thin async wrapper around the Politics and War GraphQL and REST APIs."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Loot-info parsing helpers
# ---------------------------------------------------------------------------

# PnW loot_info is a human-readable string like:
#   "The attacking forces looted 3 Gasoline, 2 Munitions, 1 Aluminum, 1 Steel…"
# We extract the four war-relevant manufactured resources using case-insensitive
# patterns that tolerate comma-formatted numbers (e.g. "1,234") with any number
# of decimal places (PnW may use 2 or 3 decimal places depending on context).
_LOOT_MONEY_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+money", re.IGNORECASE)
_LOOT_GAS_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+gasoline", re.IGNORECASE)
_LOOT_MUN_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+munitions", re.IGNORECASE)
_LOOT_ALU_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+aluminum", re.IGNORECASE)
_LOOT_STL_RE = re.compile(r"([\d,]+(?:\.\d+)?)\s+steel", re.IGNORECASE)

# Attack types that yield resource loot when the attacker wins.
# GROUND: per-city loot on a successful ground battle.
# VICTORY: beige loot when the defender's resistance reaches 0.
_ATTACK_TYPES_WITH_LOOT: frozenset[str] = frozenset({"GROUND", "VICTORY"})

# Number of war IDs to include per warattacks API request.
_WARATTACKS_BATCH_SIZE = 50


def _parse_resource_loot(loot_info: str) -> tuple[float, float, float, float, float]:
    """Return ``(money, gasoline, munitions, aluminum, steel)`` looted from a *loot_info* string."""

    def _extract(pattern: re.Pattern) -> float:  # type: ignore[type-arg]
        m = pattern.search(loot_info)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return 0.0

    return (
        _extract(_LOOT_MONEY_RE),
        _extract(_LOOT_GAS_RE),
        _extract(_LOOT_MUN_RE),
        _extract(_LOOT_ALU_RE),
        _extract(_LOOT_STL_RE),
    )

PNW_GRAPHQL_URL = "https://api.politicsandwar.com/graphql"
PNW_TEST_GRAPHQL_URL = "https://test.politicsandwar.com/graphql"
PNW_REST_URL = "https://politicsandwar.com/api/"
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
    # Spy count — null for foreign nations (API restriction); -1 means unknown
    spies: int = -1
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

    # Extra fields populated from the bulk nations REST endpoint
    rank: int = 0
    continent: str = ""
    war_policy: str = ""
    color: str = ""
    offensive_wars: int = 0
    defensive_wars: int = 0

    # Turns remaining on beige (0 = not beiged); populated by GraphQL path
    beige_turns: int = 0

    # Extended fields populated by get_nation_with_cities()
    population: int = 0
    domestic_policy: str = ""


@dataclass
class TradePrice:
    """Current market prices for war-relevant resources."""

    gasoline: float = 0.0
    munitions: float = 0.0
    aluminum: float = 0.0
    steel: float = 0.0

    def resource_value(
        self,
        *,
        gasoline: float = 0.0,
        munitions: float = 0.0,
        aluminum: float = 0.0,
        steel: float = 0.0,
    ) -> float:
        """Return the market value of the given resource quantities."""
        return (
            gasoline * self.gasoline
            + munitions * self.munitions
            + aluminum * self.aluminum
            + steel * self.steel
        )


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
    rank: int = 0


@dataclass
class City:
    """All per-city improvement data fetched for revenue calculations."""

    city_id: int
    infrastructure: float = 0.0
    land: float = 0.0
    powered: bool = True
    # Power plants
    coal_power: int = 0
    oil_power: int = 0
    nuclear_power: int = 0
    wind_power: int = 0
    # Raw resource improvements
    coal_mine: int = 0
    oil_well: int = 0
    uranium_mine: int = 0
    iron_mine: int = 0
    bauxite_mine: int = 0
    lead_mine: int = 0
    farm: int = 0
    # Commerce improvements
    supermarket: int = 0
    bank: int = 0
    shopping_mall: int = 0
    stadium: int = 0
    subway: int = 0
    # Manufacturing improvements
    gasrefinery: int = 0      # produces gasoline
    aluminum_refinery: int = 0
    steel_mill: int = 0
    munitions_factory: int = 0


@dataclass
class GameInfo:
    """Live game metadata relevant to revenue and city-cost calculations."""

    city_average: float = 43.6   # average city count of top nations (dynamic)
    game_month: int = 6           # 1–12; used for seasonal food modifiers
    global_radiation: float = 0.0
    # Two-letter continent code (upper) → radiation level
    continent_radiation: dict[str, float] = field(default_factory=dict)
    # Color name (lower-case) → turn bonus (e.g. {"blue": 2, "red": 1, …})
    color_bonuses: dict[str, int] = field(default_factory=dict)

    def radiation_for(self, continent: str) -> float:
        return self.continent_radiation.get(continent.upper(), 0.0)


@dataclass
class NationRevenue:
    """Estimated gross daily revenue for a nation (all resources per day)."""

    money: float = 0.0
    food_production: float = 0.0
    food_consumption: float = 0.0
    coal: float = 0.0
    oil: float = 0.0
    uranium: float = 0.0
    iron: float = 0.0
    bauxite: float = 0.0
    lead: float = 0.0
    gasoline: float = 0.0
    munitions: float = 0.0
    steel: float = 0.0
    aluminum: float = 0.0
    avg_commerce: float = 0.0    # average commerce % across all cities

    @property
    def food(self) -> float:
        return self.food_production - self.food_consumption


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
    spies
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
    activity_center
    military_research_center
    alliance_id
    alliance_position
    alliance_seniority
    beige_turns
    color
    alliance {
        name
    }
"""

_CITY_FIELDS = """
    id
    infrastructure
    land
    powered
    coal_power
    oil_power
    nuclear_power
    wind_power
    coal_mine
    oil_well
    uranium_mine
    iron_mine
    bauxite_mine
    lead_mine
    farm
    supermarket
    bank
    shopping_mall
    stadium
    subway
    gasrefinery
    aluminum_refinery
    steel_mill
    munitions_factory
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
    "activity_center": "AC",
    "military_research_center": "MRC",
}

_ALLIANCE_MEMBER_FIELDS = """
    id
    num_cities
    alliance_position
    vacation_mode_turns
    beige_turns
"""

# v1 REST API encodes alliance position as an integer; map to string names.
_ALLIANCE_POSITION_MAP: dict[int, str] = {
    0: "NOALLIANCE",
    1: "MEMBER",
    2: "OFFICER",
    3: "HEIR",
    4: "LEADER",
    5: "APPLICANT",
}


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
            spies=-1 if n.get("spies") is None else int(n["spies"]),
            projects_built=projects_built,
            alliance_id=int(n.get("alliance_id") or 0),
            alliance_name=alliance.get("name", "") or "",
            alliance_position=n.get("alliance_position", "") or "",
            alliance_seniority=int(n.get("alliance_seniority") or 0),
            beige_turns=int(n.get("beige_turns") or 0),
            color=n.get("color", "") or "",
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

    @staticmethod
    def _parse_city(c: dict[str, Any]) -> City:
        return City(
            city_id=int(c.get("id") or 0),
            infrastructure=float(c.get("infrastructure") or 0.0),
            land=float(c.get("land") or 0.0),
            powered=bool(c.get("powered", True)),
            coal_power=int(c.get("coal_power") or 0),
            oil_power=int(c.get("oil_power") or 0),
            nuclear_power=int(c.get("nuclear_power") or 0),
            wind_power=int(c.get("wind_power") or 0),
            coal_mine=int(c.get("coal_mine") or 0),
            oil_well=int(c.get("oil_well") or 0),
            uranium_mine=int(c.get("uranium_mine") or 0),
            iron_mine=int(c.get("iron_mine") or 0),
            bauxite_mine=int(c.get("bauxite_mine") or 0),
            lead_mine=int(c.get("lead_mine") or 0),
            farm=int(c.get("farm") or 0),
            supermarket=int(c.get("supermarket") or 0),
            bank=int(c.get("bank") or 0),
            shopping_mall=int(c.get("shopping_mall") or 0),
            stadium=int(c.get("stadium") or 0),
            subway=int(c.get("subway") or 0),
            gasrefinery=int(c.get("gasrefinery") or 0),
            aluminum_refinery=int(c.get("aluminum_refinery") or 0),
            steel_mill=int(c.get("steel_mill") or 0),
            munitions_factory=int(c.get("munitions_factory") or 0),
        )

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_nation_from_nations_list(n: dict[str, Any]) -> Nation:
        """Parse a nation dict from the bulk ``nations/?key=…`` REST endpoint.

        Field names in this endpoint differ from both the single-nation endpoint
        and the GraphQL schema (e.g. ``nation`` vs ``nation_name``, ``leader``
        vs ``leader_name``, integer ``allianceposition`` vs string).
        """
        minutes = int(n.get("minutessinceactive") or 0)
        pos_int = int(n.get("allianceposition") or 0)
        pos_str = _ALLIANCE_POSITION_MAP.get(pos_int, "NOALLIANCE")
        alliance_name = n.get("alliance") or ""
        if alliance_name == "None":
            alliance_name = ""
        return Nation(
            nation_id=int(n.get("nationid") or 0),
            nation_name=n.get("nation", ""),
            leader_name=n.get("leader", ""),
            discord_tag="",
            num_cities=int(n.get("cities") or 0),
            score=float(n.get("score") or 0.0),
            last_active=f"{minutes} minutes ago" if minutes else "",
            minutes_since_active=minutes,
            alliance_id=int(n.get("allianceid") or 0),
            alliance_name=alliance_name,
            alliance_position=pos_str,
            rank=int(n.get("rank") or 0),
            continent=n.get("continent", "") or "",
            war_policy=n.get("war_policy", "") or "",
            color=n.get("color", "") or "",
            offensivewars=int(n.get("offensivewars") or 0),
            defensivewars=int(n.get("defensivewars") or 0),
        )

    async def _fetch_nations_rest(self) -> list[dict[str, Any]]:
        """Fetch all nations from the bulk ``nations/?key=…`` REST endpoint.

        Returns the list of raw nation dicts from the ``nations`` key,
        or an empty list if the response is malformed.
        """
        base = self._rest_url if self._rest_url is not None else PNW_REST_URL
        url = f"{base}nations/?key={self._api_key}"
        session = self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data.get("nations") or []

    async def get_nation(self, nation_id: int) -> Optional[Nation]:
        """Fetch a nation by its numeric ID. Returns None if not found."""
        if self._rest_url is not None:
            nations = await self._fetch_nations_rest()
            for n in nations:
                if int(n.get("nationid") or 0) == nation_id:
                    return self._parse_nation_from_nations_list(n)
            return None
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
        """Search for a nation by name (case-insensitive). Returns the first match or None.

        In REST mode, the bulk nations list is fetched and searched locally by
        nation name first, then by leader name as a fallback.
        """
        if self._rest_url is not None:
            nations = await self._fetch_nations_rest()
            name_lower = name.strip().lower()
            for n in nations:
                if n.get("nation", "").strip().lower() == name_lower:
                    return self._parse_nation_from_nations_list(n)
            # Fallback: search by leader name
            for n in nations:
                if n.get("leader", "").strip().lower() == name_lower:
                    return self._parse_nation_from_nations_list(n)
            return None
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

    async def get_nation_with_cities(
        self, nation_id: int
    ) -> Optional[tuple[Nation, list[City]]]:
        """Fetch a nation and all its city data for revenue calculations.

        Returns ``(nation, cities)`` or ``None`` if the nation is not found.
        Only supported in GraphQL mode; returns ``None`` in REST mode.
        """
        if self._rest_url is not None:
            return None
        query = f"""
        query GetNationWithCities($id: [Int]) {{
            nations(id: $id, first: 1) {{
                data {{
                    {_NATION_FIELDS}
                    continent
                    population
                    domestic_policy
                    war_policy
                    wars(active: true) {{
                        id
                        att_id
                    }}
                    cities {{
                        {_CITY_FIELDS}
                    }}
                }}
            }}
        }}
        """
        data = await self._query(query, {"id": [nation_id]})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        if not nations:
            return None
        n = nations[0]
        nation = self._parse_nation(n)
        nation.continent = n.get("continent", "") or ""
        nation.population = int(n.get("population") or 0)
        nation.domestic_policy = n.get("domestic_policy", "") or ""
        nation.war_policy = n.get("war_policy", "") or ""
        active_wars = n.get("wars") or []
        nation.offensive_wars = sum(
            1 for w in active_wars if int(w.get("att_id") or 0) == nation_id
        )
        nation.defensive_wars = len(active_wars) - nation.offensive_wars
        cities = [self._parse_city(c) for c in (n.get("cities") or [])]
        return nation, cities

    async def get_game_info(self) -> GameInfo:
        """Fetch live game metadata: city average, game date, radiation, and color bonuses.

        Falls back to ``GameInfo()`` defaults if the query fails.
        """
        query = """
        query GetGameInfo {
            game_info {
                city_average
                game_date
                radiation {
                    africa
                    antarctica
                    asia
                    australia
                    europe
                    global
                    north_america
                    south_america
                }
            }
            colors {
                color
                turn_bonus
            }
        }
        """
        try:
            data = await self._query(query, {})
            gi = data.get("data", {}).get("game_info", {}) or {}
            rad = gi.get("radiation", {}) or {}

            # city_average is a direct numeric field on game_info
            city_avg = float(gi.get("city_average") or 43.6)

            # game_date may be "YYYY-MM-DD" or an object with a month field
            game_date = gi.get("game_date", "")
            month = 6
            if isinstance(game_date, str) and game_date:
                try:
                    month = int(game_date.split("-")[1])
                except (IndexError, ValueError):
                    pass
            elif isinstance(game_date, dict):
                month = int(game_date.get("month", 6))

            # Color bloc turn bonuses (skip "gray" which has no bloc bonus)
            color_bonuses: dict[str, int] = {}
            for c in (data.get("data", {}).get("colors", []) or []):
                name = (c.get("color") or "").lower()
                bonus = int(c.get("turn_bonus") or 0)
                if name and name != "gray" and bonus > 0:
                    color_bonuses[name] = bonus

            return GameInfo(
                city_average=city_avg,
                game_month=month,
                global_radiation=float(rad.get("global", 0.0) or 0.0),
                continent_radiation={
                    "AF": float(rad.get("africa", 0.0) or 0.0),
                    "AN": float(rad.get("antarctica", 0.0) or 0.0),
                    "AS": float(rad.get("asia", 0.0) or 0.0),
                    "AU": float(rad.get("australia", 0.0) or 0.0),
                    "EU": float(rad.get("europe", 0.0) or 0.0),
                    "NA": float(rad.get("north_america", 0.0) or 0.0),
                    "SA": float(rad.get("south_america", 0.0) or 0.0),
                },
                color_bonuses=color_bonuses,
            )
        except Exception:
            log.exception("Failed to fetch game_info")
            return GameInfo()

    async def get_trade_prices(self) -> "TradePrice":
        """Fetch the lowest active buy-offer price for war-relevant resources.

        Uses the live trade market: querying open buy orders and taking the
        lowest bid per resource gives the most conservative (floor) valuation
        for looted resources and enemy resource consumption.  Falls back to
        tradeprices averages for any resource with no active buy orders, and
        falls back entirely to tradeprices averages if the trades query fails.
        """
        _WAR_RESOURCES = {"gasoline", "munitions", "aluminum", "steel"}

        # --- Primary: lowest active buy-order price ---
        buy_query = """
        query GetLowestBuyPrices {
            trades(buy_or_sell: "buy", first: 500) {
                data {
                    offer_resource
                    price
                    accepted
                }
            }
        }
        """
        mins: dict[str, float] = {}
        try:
            data = await self._query(buy_query, {})
            for t in data.get("data", {}).get("trades", {}).get("data", []):
                if t.get("accepted"):
                    continue  # skip completed trades; want open offers only
                resource = (t.get("offer_resource") or "").lower()
                if resource not in _WAR_RESOURCES:
                    continue
                ppu = float(t.get("price") or 0)
                if ppu > 0 and (resource not in mins or ppu < mins[resource]):
                    mins[resource] = ppu
        except Exception:
            log.exception("PnW API error fetching lowest buy prices")

        # --- Fallback: tradeprices averages for any missing resource ---
        if len(mins) < len(_WAR_RESOURCES):
            avg_query = """
            query GetTradePrices {
                tradeprices(first: 1) {
                    data {
                        gasoline
                        munitions
                        aluminum
                        steel
                    }
                }
            }
            """
            try:
                data = await self._query(avg_query, {})
                prices = data.get("data", {}).get("tradeprices", {}).get("data", [])
                if prices:
                    p = prices[0]
                    for res in _WAR_RESOURCES:
                        if res not in mins:
                            fallback = float(p.get(res) or 0)
                            if fallback > 0:
                                mins[res] = fallback
            except Exception:
                pass

        return TradePrice(
            gasoline=mins.get("gasoline", 0.0),
            munitions=mins.get("munitions", 0.0),
            aluminum=mins.get("aluminum", 0.0),
            steel=mins.get("steel", 0.0),
        )

    async def get_alliance_damage(
        self,
        alliance_id: int,
        after: datetime,
    ) -> dict[int, dict[str, Any]]:
        """Return per-nation damage and loot totals for wars started after *after*.

        Both offensive wars (where *alliance_id* is the attacker) and defensive
        wars (where *alliance_id* is the defender) are counted.  A nation that
        fought in both roles has all contributions accumulated into a single entry.

        Uses the Locutus per-attack attribution model:

        Phase 1 – wars query: collects qualifying war IDs and records which
        nations are alliance members (attacker or defender).  No war-level
        damage totals are used — those are inaccurate because war-level
        ``def_gas_used`` includes gas from both our attacks *and* enemy
        counterattacks, inflating the resource-damage stat.

        Phase 2 – warattacks query: iterates ALL attack types so that every
        exchange contributes to the correct member's stats.
        ``att_id`` in each WarAttack is the nation that *initiated* that
        specific exchange (either the war's original attacker or the defender
        doing a counterattack).

        Attribution rules per attack:
          • def_gas_used / def_mun_used (all attacks) → enemy gas/mun cost
            (def_alum_used / def_steel_used are war-level only, not per-attack)
          • infra_destroyed_value     (winning attacks)   → infra_value
          • money_stolen + money_looted (winning attacks) → money_looted
            (money_stolen is 0 for VICTORY; money_looted covers VICTORY loot)
          • gasoline/munitions/aluminum/steel_looted (winning attacks) → resource loot
          • resource loot on VICTORY                  → vict_* copies too

        Returns a dict mapping nation_id -> {
            "nation_name": str,
            "num_cities": int,       # nation's current city count
            "infra_value": float,    # monetary value of infrastructure damage dealt
            "money_looted": float,   # money looted across all winning attacks
            "gas_looted": float,     # gasoline looted on member victories
            "mun_looted": float,     # munitions looted on member victories
            "alum_looted": float,    # aluminum looted on member victories
            "steel_looted": float,   # steel looted on member victories
            "vict_gas_looted": float,   # gasoline looted specifically on beige
            "vict_mun_looted": float,   # munitions looted specifically on beige
            "vict_alum_looted": float,  # aluminum looted specifically on beige
            "vict_steel_looted": float, # steel looted specifically on beige
            "def_gas_used": float,   # gasoline the enemy spent defending our attacks
            "def_mun_used": float,   # munitions the enemy spent defending our attacks
            "def_alum_used": float,  # always 0 (not available per-attack in API)
            "def_steel_used": float, # always 0 (not available per-attack in API)
        }.
        """
        results: dict[int, dict[str, Any]] = {}
        war_ids: list[int] = []

        def _make_entry(nation_name: str, num_cities: int) -> dict[str, Any]:
            return {
                "nation_name": nation_name,
                "num_cities": num_cities,
                "infra_value": 0.0,
                "money_looted": 0.0,
                "gas_looted": 0.0,
                "mun_looted": 0.0,
                "alum_looted": 0.0,
                "steel_looted": 0.0,
                # Resources looted specifically on a VICTORY (beige) attack.
                # These are also added to gas/mun/alum/steel_looted above so
                # they appear in the loot column; the vict_* copies are used
                # separately to include them in the resource-damage column.
                "vict_gas_looted": 0.0,
                "vict_mun_looted": 0.0,
                "vict_alum_looted": 0.0,
                "vict_steel_looted": 0.0,
                "def_gas_used": 0.0,
                "def_mun_used": 0.0,
                "def_alum_used": 0.0,
                "def_steel_used": 0.0,
            }

        # ------------------------------------------------------------------
        # Phase 1: collect qualifying wars and nation membership.
        #
        # War-level damage totals are intentionally NOT used here; all damage
        # data is derived from per-attack records in Phase 2 (Locutus model).
        # ------------------------------------------------------------------
        page = 1
        while True:
            query = """
            query GetAllianceWars($alliance_id: [Int], $page: Int) {
                wars(alliance_id: $alliance_id, page: $page, first: 100) {
                    data {
                        id
                        att_id
                        def_id
                        att_alliance_id
                        def_alliance_id
                        date
                        attacker {
                            nation_name
                            num_cities
                        }
                        defender {
                            nation_name
                            num_cities
                        }
                    }
                    paginatorInfo {
                        hasMorePages
                    }
                }
            }
            """
            data = await self._query(query, {"alliance_id": [alliance_id], "page": page})
            payload = data.get("data", {}).get("wars", {})
            wars = payload.get("data", [])
            has_more = payload.get("paginatorInfo", {}).get("hasMorePages", False)

            all_before_cutoff = True
            for war in wars:
                war_date_str = war.get("date", "") or ""
                war_date: datetime | None = None
                if war_date_str:
                    try:
                        war_date = datetime.fromisoformat(war_date_str)
                        if war_date.tzinfo is None:
                            war_date = war_date.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass

                if war_date is not None and war_date >= after:
                    all_before_cutoff = False

                if war_date is None or war_date < after:
                    continue

                war_id = int(war.get("id") or 0)
                att_alliance = int(war.get("att_alliance_id") or 0)
                def_alliance = int(war.get("def_alliance_id") or 0)

                # ---- Offensive contribution (our member is the attacker) ----
                if att_alliance == alliance_id:
                    att_id = int(war.get("att_id") or 0)
                    if att_id:
                        attacker_data = war.get("attacker") or {}
                        nation_name = attacker_data.get("nation_name") or str(att_id)
                        num_cities = int(attacker_data.get("num_cities") or 0)

                        entry = results.setdefault(att_id, _make_entry(nation_name, num_cities))
                        if num_cities > entry["num_cities"]:
                            entry["num_cities"] = num_cities

                        if war_id and war_id not in war_ids:
                            war_ids.append(war_id)

                # ---- Defensive contribution (our member is the defender) ----
                # Skip intra-alliance wars to avoid double-counting.
                if def_alliance == alliance_id and att_alliance != alliance_id:
                    def_id = int(war.get("def_id") or 0)
                    if def_id:
                        defender_data = war.get("defender") or {}
                        nation_name = defender_data.get("nation_name") or str(def_id)
                        num_cities = int(defender_data.get("num_cities") or 0)

                        entry = results.setdefault(def_id, _make_entry(nation_name, num_cities))
                        if num_cities > entry["num_cities"]:
                            entry["num_cities"] = num_cities

                        if war_id and war_id not in war_ids:
                            war_ids.append(war_id)

            # Stop once there are no more pages or every war on this page predates
            # the cutoff (the API returns wars in descending date order).
            if not has_more or all_before_cutoff:
                break
            page += 1

        if not war_ids:
            return results

        # ------------------------------------------------------------------
        # Phase 2: per-attack attribution (Locutus model).
        #
        # For every attack initiated by an alliance member (att_id in results):
        #   • def_*_used                 → enemy resource cost (all attacks)
        #   • att_infra_destroyed_value  → infra_value         (winning attacks)
        #   • money_stolen               → money_looted        (winning attacks)
        #   • loot_info                  → resource loot       (winning GROUND/VICTORY)
        #
        # Using per-attack fields avoids double-counting the enemy's counterattack
        # resource usage that would otherwise be included in war-level totals.
        #
        # att_id in each WarAttack is the nation *initiating* that specific
        # attack (either the war's original attacker or the defender doing a
        # counterattack), so a single loop covers both roles.
        # ------------------------------------------------------------------
        _VICTORY = "VICTORY"
        _LOOT_TYPES = _ATTACK_TYPES_WITH_LOOT  # {"GROUND", "VICTORY"}
        _BATCH = _WARATTACKS_BATCH_SIZE
        for batch_start in range(0, len(war_ids), _BATCH):
            batch = war_ids[batch_start : batch_start + _BATCH]
            atk_page = 1
            while True:
                atk_query = """
                query GetWarAttacks($war_id: [Int], $page: Int) {
                    warattacks(war_id: $war_id, page: $page, first: 100) {
                        data {
                            att_id
                            type
                            victor
                            money_stolen
                            infra_destroyed_value
                            loot_info
                            def_gas_used
                            def_mun_used
                        }
                        paginatorInfo {
                            hasMorePages
                        }
                    }
                }
                """
                try:
                    atk_data = await self._query(
                        atk_query, {"war_id": batch, "page": atk_page}
                    )
                except Exception:
                    log.exception("PnW API error fetching warattacks for resource loot")
                    break
                atk_payload = atk_data.get("data", {}).get("warattacks", {})
                attacks = atk_payload.get("data", [])
                atk_has_more = (
                    atk_payload.get("paginatorInfo", {}).get("hasMorePages", False)
                )

                for attack in attacks:
                    att_id = int(attack.get("att_id") or 0)
                    if att_id not in results:
                        continue

                    attack_type = str(attack.get("type") or "")
                    # VICTORY only occurs when the war attacker's resistance
                    # reaches 0, meaning the attacker always wins.  The API
                    # sometimes returns victor=0 for this synthetic event, so
                    # don't rely on the victor field for VICTORY type.
                    victor = int(attack.get("victor") or 0)
                    won = attack_type == _VICTORY or bool(victor and victor == att_id)

                    # Enemy resource consumption defending against this attack
                    # (counted regardless of outcome — defender uses resources either way).
                    # Note: def_alum_used and def_steel_used are only available at the
                    # war level, not per-attack, so they are not tracked here.
                    results[att_id]["def_gas_used"] += float(attack.get("def_gas_used") or 0)
                    results[att_id]["def_mun_used"] += float(attack.get("def_mun_used") or 0)

                    if not won:
                        continue

                    # Infra destroyed and money looted on a winning attack.
                    results[att_id]["infra_value"] += float(
                        attack.get("infra_destroyed_value") or 0
                    )

                    # Parse loot from loot_info string (covers GROUND and VICTORY).
                    loot_str = attack.get("loot_info") or ""
                    if attack_type in _LOOT_TYPES and loot_str:
                        loot_money, gas, mun, alum, steel = _parse_resource_loot(loot_str)
                    else:
                        loot_money, gas, mun, alum, steel = 0.0, 0.0, 0.0, 0.0, 0.0

                    # money_stolen covers per-city ground loot; loot_info covers
                    # VICTORY beige loot (which may not populate money_stolen).
                    money = float(attack.get("money_stolen") or 0) or loot_money
                    if money > 0:
                        results[att_id]["money_looted"] += money

                    if gas or mun or alum or steel:
                        results[att_id]["gas_looted"] += gas
                        results[att_id]["mun_looted"] += mun
                        results[att_id]["alum_looted"] += alum
                        results[att_id]["steel_looted"] += steel

                        # VICTORY loot also counts as resource damage dealt:
                        # the enemy's stockpile is permanently reduced by the
                        # looted amount, so track it separately for the
                        # res-damage column as well as the loot column.
                        if attack_type == _VICTORY:
                            results[att_id]["vict_gas_looted"] += gas
                            results[att_id]["vict_mun_looted"] += mun
                            results[att_id]["vict_alum_looted"] += alum
                            results[att_id]["vict_steel_looted"] += steel

                if not atk_has_more:
                    break
                atk_page += 1

        return results

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

    async def get_active_def_war_counts_by_alliance(
        self, alliance_ids: list[int]
    ) -> dict[int, int]:
        """Return a mapping of nation_id -> active defensive war count.

        Queries by alliance ID instead of nation IDs, so it can run in
        parallel with :meth:`get_alliance_members` without needing the
        member list first.  Nations not present have zero active defensive wars.
        """
        valid_ids = [aid for aid in alliance_ids if aid]
        if not valid_ids:
            return {}
        query = """
        query GetActiveDefWars($alliance_id: [Int]) {
            wars(alliance_id: $alliance_id, active: true, first: 500) {
                data {
                    def_id
                    def_alliance_id
                }
            }
        }
        """
        data = await self._query(query, {"alliance_id": valid_ids})
        wars = data.get("data", {}).get("wars", {}).get("data", [])
        alliance_id_set = set(valid_ids)
        counts: dict[int, int] = {}
        for war in wars:
            if int(war.get("def_alliance_id") or 0) not in alliance_id_set:
                continue
            def_id = int(war.get("def_id") or 0)
            if def_id:
                counts[def_id] = counts.get(def_id, 0) + 1
        return counts

    async def get_nations_in_alliance_by_score_range(
        self,
        alliance_ids: list[int],
        min_score: float,
        max_score: float,
    ) -> list[Nation]:
        """Fetch non-vacation-mode members of the given alliances whose score
        falls within [min_score, max_score].

        The PnW GraphQL API does not support server-side score range filtering,
        so all members are fetched and filtered locally.
        """
        members = await self.get_alliance_members(alliance_ids)
        return [n for n in members if min_score <= n.score <= max_score]

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

        In REST mode the full alliances list is fetched from ``alliances/?key=…``
        and filtered locally.  In GraphQL mode the query is delegated to the API.
        """
        if self._rest_url is not None:
            alliances = await self._fetch_alliances_rest()
            name_lower = name.strip().lower()
            for a in alliances:
                if a.get("name", "").strip().lower() == name_lower:
                    return self._parse_alliance_rest(a)
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
        base = self._rest_url if self._rest_url is not None else PNW_REST_URL
        url = f"{base}nation/id={nation_id}/&key={self._api_key}"
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

    async def _fetch_alliances_rest(self) -> list[dict[str, Any]]:
        """Fetch all alliances from the PnW v1 REST API.

        Returns the list of raw alliance dicts from the ``alliances`` key,
        or an empty list if the response is malformed.
        """
        base = self._rest_url if self._rest_url is not None else PNW_REST_URL
        url = f"{base}alliances/?key={self._api_key}"
        session = self._get_session()
        async with session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data.get("alliances") or []

    @staticmethod
    def _parse_alliance_rest(a: dict[str, Any]) -> AllianceInfo:
        """Parse a single alliance dict from the PnW v1 REST ``/alliances/`` endpoint."""
        return AllianceInfo(
            alliance_id=int(a.get("id") or 0),
            name=a.get("name", ""),
            acronym=a.get("acronym", "") or "",
            score=float(a.get("score") or 0.0),
            # avgscore is the v1 REST field name; total_cities not available
            average_score=float(a.get("avgscore") or 0.0),
            color=a.get("color", "") or "",
            flag=a.get("flagurl", "") or "",
            # discord / ircchan not reliably present in v1 REST
            discord_link="",
            num_members=int(a.get("members") or 0),
            # applicants not included in the v1 REST alliances list
            num_applicants=0,
            total_cities=0,
            avg_cities=0.0,
            rank=int(a.get("rank") or 0),
        )

    async def _get_alliance_rest(self, alliance_id: int) -> Optional[AllianceInfo]:
        """Fetch alliance info by numeric ID using the PnW v1 REST API.

        Fetches the full alliances list from ``alliances/?key=…`` and
        returns the first entry whose ``id`` matches *alliance_id*, or
        ``None`` if not found.
        """
        alliances = await self._fetch_alliances_rest()
        for a in alliances:
            if int(a.get("id") or 0) == alliance_id:
                return self._parse_alliance_rest(a)
        return None

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


# ---------------------------------------------------------------------------
# Standalone utility functions
# ---------------------------------------------------------------------------


def calculate_infra_cost(buy_from: float, buy_to: float) -> float:
    """Return the base cost (before project discounts) to buy infrastructure
    from *buy_from* to *buy_to* in a single city.

    Formula (integral of the marginal cost curve):
        marginal cost at level i = $300 + $150 * i / 1000
        total cost = 300*(to-from) + (150/2000)*(to²-from²)
                   = 300*(to-from) + 0.075*(to²-from²)

    Returns 0 if *buy_to* <= *buy_from*.
    """
    if buy_to <= buy_from:
        return 0.0
    diff = buy_to - buy_from
    return 300.0 * diff + 0.075 * (buy_to * buy_to - buy_from * buy_from)


# PnW score ↔ war-range constants
WAR_RANGE_MIN_RATIO = 0.75  # can attack nations at ≥ 75 % of own score
WAR_RANGE_MAX_RATIO = 2.5   # can attack nations at ≤ 250 % of own score


# ---------------------------------------------------------------------------
# City cost (Locutus dynamic formula)
# ---------------------------------------------------------------------------

# Default city_average used when game_info is unavailable.
# Locutus uses 40.8216 as its stored fallback; current live value ~43.6.
_CITY_COST_DEFAULT_AVERAGE = 43.6


def calculate_city_cost(
    current_count: int,
    city_average: float = _CITY_COST_DEFAULT_AVERAGE,
    manifest_destiny: bool = False,
    government_support_agency: bool = False,
    bureau_of_domestic_affairs: bool = False,
) -> float:
    """Return the cost of buying the next city when the nation currently has
    *current_count* cities, using the Locutus dynamic formula.

    Source: PW.java – ``PW.Cities.nextCityCost``

    ``city_average`` is fetched live from ``game_info.city_average``
    (default 43.6, Locutus's stored live value).

    Manifest Destiny domestic-policy discounts (if applicable):
        - Base: ×0.95 (5 % off)
        - With Government Support Agency: ×0.925 (additional 2.5 %)
        - With Bureau of Domestic Affairs: additional 1.25 %
    """
    n = current_count + 1          # city count after purchase
    q = city_average * 0.25        # top-20-average-quarter pivot
    dynamic = 100_000.0 * (n - q) ** 3 + 150_000.0 * (n - q) + 75_000.0
    floor = 100_000.0 * n ** 2
    cost = max(floor, dynamic)

    if manifest_destiny:
        factor = 0.05
        if government_support_agency:
            factor += 0.025
        if bureau_of_domestic_affairs:
            factor += 0.0125
        cost *= 1.0 - factor

    return max(1.0, cost)


# ---------------------------------------------------------------------------
# Revenue calculation (per-city formulas from community-verified sources)
# ---------------------------------------------------------------------------

# Turns per real-world day in PnW
_TURNS_PER_DAY = 12

# Commerce income constant: $ per 100 infra per turn at 100 % commerce
_COMMERCE_INCOME = 726.17

# Continent codes for seasonal food production modifier
_NORTHERN_CONTINENTS = frozenset({"NA", "EU", "AS"})
_SOUTHERN_CONTINENTS = frozenset({"SA", "AF", "AU"})


def _normalize_continent(raw: str) -> str:
    """Normalize a continent string to a two-letter upper-case code."""
    mapping = {
        "north america": "NA",
        "europe": "EU",
        "asia": "AS",
        "africa": "AF",
        "south america": "SA",
        "australia": "AU",
        "antarctica": "AN",
    }
    return mapping.get(raw.strip().lower(), raw.strip().upper()[:2])


def _city_commerce_rate(city: City, has_itc: bool) -> float:
    """Return the commerce % contributed by a single city's improvements."""
    rate = (
        city.supermarket * 3.5
        + city.bank * 5.0
        + city.shopping_mall * 5.0
        + city.stadium * 5.0
        + city.subway * 8.0
    )
    if has_itc:
        rate += 2.0
    return min(100.0, rate)


def _raw_prod(count: int) -> float:
    """Daily production for a raw-resource improvement (per mine/well).

    Bonus: +1/18 ≈ 5.56 % per additional improvement of the same type above 1.
    """
    if count <= 0:
        return 0.0
    bonus = max(round((count - 1) * (1.0 / 18.0), 4), 0.0)
    return count * 3.0 * (1.0 + bonus)


def _uranium_prod(count: int, has_uep: bool) -> float:
    """Daily uranium production; UEP doubles output and uses a larger bonus."""
    if count <= 0:
        return 0.0
    bonus = max(round((count - 1) * 0.125, 4), 0.0)
    return count * 3.0 * (1.0 + int(has_uep)) * (1.0 + bonus)


def _manu_prod(count: int, per_unit: float, project_mult: float) -> float:
    """Daily manufactured-resource production.

    ``per_unit``      – base units per improvement per day.
    ``project_mult``  – (1 + project_bonus), e.g. 1.36 for IW/BW, 3.0 for EGR.
    Bonus per extra improvement of the same type: +12.5 %.
    """
    if count <= 0:
        return 0.0
    bonus = max(round((count - 1) * 0.125, 4), 0.0)
    return count * per_unit * (1.0 + bonus) * project_mult


def _coal_oil_power_usage(infra: float, plant_count: int) -> float:
    """Coal or oil consumed per day by coal/oil power plants.

    Each plant covers 500 infra and uses ceil(covered_infra / 100) * 1.2 per day.
    """
    if plant_count <= 0:
        return 0.0
    usage = 0.0
    remaining = infra
    for _ in range(plant_count):
        covered = min(remaining, 500.0)
        if covered <= 0:
            break
        usage += math.ceil(covered / 100.0) * 1.2
        remaining -= 500.0
    return usage


def _nuclear_power_usage(infra: float, plant_count: int) -> float:
    """Uranium consumed per day by nuclear power plants.

    Each plant covers up to 2000 infra and uses ceil(covered_infra / 1000) * 3
    uranium per day, capped at 6 per plant (reached at full 2000 infra).
    """
    if plant_count <= 0:
        return 0.0
    usage = 0.0
    remaining = infra
    for _ in range(plant_count):
        covered = min(remaining, 2000.0)
        if covered <= 0:
            break
        usage += min(math.ceil(covered / 1000.0) * 3.0, 6.0)
        remaining -= 2000.0
    return usage


def _food_prod_per_city(
    farm: int,
    land: float,
    has_mi: bool,
    game_month: int,
    continent: str,
    cont_radiation: float,
    global_radiation: float,
) -> float:
    """Daily food production for a single city.

    Formulas sourced from community-verified pnw_utils.py (Jacob Knox / JacobKnox).
    """
    if farm <= 0:
        return 0.0
    land_div = 400.0 if has_mi else 500.0
    prod = farm * 12.0 * (land / land_div)
    # Per-farm quality bonus: +5/190 ≈ 2.63 % per additional farm above 1
    farm_bonus = max(round((farm - 1) * (5.0 / 190.0), 4), 0.0)
    prod *= 1.0 + farm_bonus

    # Seasonal modifier (based on hemisphere)
    season = 1.0
    if continent in _NORTHERN_CONTINENTS:
        if 5 < game_month < 9:    # Jun–Aug = northern summer
            season = 1.2
        elif game_month > 11 or game_month < 3:   # Dec–Feb = northern winter
            season = 0.8
    elif continent in _SOUTHERN_CONTINENTS:
        if 5 < game_month < 9:    # Jun–Aug = southern winter
            season = 0.8
        elif game_month > 11 or game_month < 3:   # Dec–Feb = southern summer
            season = 1.2
    else:  # Antarctica — cold year-round; season modifier only applies in summer/winter
        if 5 < game_month < 9 or game_month > 11 or game_month < 3:
            season = 0.5
        # spring/fall (Mar–May, Sep–Nov): season stays 1.0

    # Radiation factor (reduces food production)
    rad_factor = max(1.0 - (cont_radiation + global_radiation) / 1000.0, 0.0)
    return prod * season * rad_factor


# Improvement upkeep costs (money per day, sourced from PnW wiki).
# Note: hospital and police_station are not in the City model so are excluded.
_UPKEEP: dict[str, float] = {
    "coal_power":         1_200.0,
    "oil_power":          1_800.0,
    "nuclear_power":     10_500.0,
    "wind_power":           500.0,
    "coal_mine":            400.0,
    "oil_well":             600.0,
    "uranium_mine":       5_000.0,
    "iron_mine":          1_600.0,
    "bauxite_mine":       1_600.0,
    "lead_mine":          1_500.0,
    "farm":                 300.0,
    "gasrefinery":        4_000.0,
    "aluminum_refinery":  2_500.0,
    "steel_mill":         9_000.0,
    "munitions_factory":  8_750.0,
    "supermarket":          600.0,
    "bank":               1_800.0,
    "shopping_mall":      5_400.0,
    "stadium":           12_150.0,
    "subway":             3_250.0,
}


def _improvement_upkeep(city: City) -> float:
    """Return the total daily money upkeep for all tracked improvements in *city*."""
    total = 0.0
    for attr, daily_cost in _UPKEEP.items():
        count = getattr(city, attr, 0)
        if count:
            total += count * daily_cost
    return total


def compute_nation_revenue(
    nation: Nation,
    cities: list[City],
    game_info: Optional[GameInfo] = None,
) -> NationRevenue:
    """Compute estimated gross daily revenue for *nation* from *cities*.

    Assumptions:
    - Peacetime (soldiers consume less food; no wartime bonuses/penalties).
    - All cities treated as powered (``city.powered`` still respected for mfg).
    - Revenue is gross (before alliance tax).
    """
    if game_info is None:
        game_info = GameInfo()

    pb = set(nation.projects_built)
    has_itc = "ITC" in pb
    has_mi  = "MI"  in pb
    has_ala = "ALA" in pb   # Arable Land Agency → +20 % food production
    has_uep = "UEP" in pb
    has_iw  = "IW"  in pb   # Iron Works → steel bonus
    has_bw  = "BW"  in pb   # Bauxite Works → aluminum bonus
    has_egr = "EGR" in pb   # Emergency Gasoline Reserve → ×2 gasoline
    has_as  = "AS"  in pb   # Arms Stockpile → ×1.2 munitions

    continent = _normalize_continent(nation.continent or "NA")
    cont_rad = game_info.radiation_for(continent)

    # Project multipliers for manufactured resources (Locutus boostFactor values)
    gasoline_mult = 2.0 if has_egr else 1.0   # EGR doubles gasoline output
    munitions_mult = 1.2 if has_as  else 1.0  # AS adds 20 % to munitions
    steel_mult     = 1.36 if has_iw else 1.0  # IW adds 36 % to steel
    aluminum_mult  = 1.36 if has_bw else 1.0  # BW adds 36 % to aluminum

    rev = NationRevenue()
    total_commerce = 0.0

    for city in cities:
        commerce = _city_commerce_rate(city, has_itc)
        total_commerce += commerce

        # Money (gross, before tax)
        rev.money += (
            (city.infrastructure / 100.0)
            * (commerce / 100.0)
            * _COMMERCE_INCOME
            * _TURNS_PER_DAY
        )

        # Food production
        rev.food_production += _food_prod_per_city(
            city.farm, city.land, has_mi,
            game_info.game_month, continent,
            cont_rad, game_info.global_radiation,
        )

        # Raw resources
        rev.coal    += _raw_prod(city.coal_mine)
        rev.oil     += _raw_prod(city.oil_well)
        rev.iron    += _raw_prod(city.iron_mine)
        rev.bauxite += _raw_prod(city.bauxite_mine)
        rev.lead    += _raw_prod(city.lead_mine)
        rev.uranium += _uranium_prod(city.uranium_mine, has_uep)

        # Manufactured resources (only if city is powered)
        if city.powered:
            rev.gasoline  += _manu_prod(city.gasrefinery,       6.0,  gasoline_mult)
            rev.munitions += _manu_prod(city.munitions_factory, 18.0, munitions_mult)
            rev.steel     += _manu_prod(city.steel_mill,         9.0,  steel_mult)
            rev.aluminum  += _manu_prod(city.aluminum_refinery,  9.0,  aluminum_mult)

            # Raw inputs consumed by manufacturing
            # Gasoline: 3 oil per refinery (scales with production)
            rev.oil     -= _manu_prod(city.gasrefinery,       3.0, gasoline_mult)
            # Munitions: 6 lead per factory (AS boosts output only, not lead usage)
            rev.lead    -= _manu_prod(city.munitions_factory,  6.0, 1.0)
            # Steel: 3 iron + 3 coal per mill
            rev.iron    -= _manu_prod(city.steel_mill,         3.0, steel_mult)
            rev.coal    -= _manu_prod(city.steel_mill,         3.0, steel_mult)
            # Aluminum: 3 bauxite per refinery
            rev.bauxite -= _manu_prod(city.aluminum_refinery,  3.0, aluminum_mult)

        # Power plant resource consumption (subtracts from raw resources)
        if city.powered:
            rev.coal    -= _coal_oil_power_usage(city.infrastructure, city.coal_power)
            rev.oil     -= _coal_oil_power_usage(city.infrastructure, city.oil_power)
            rev.uranium -= _nuclear_power_usage(city.infrastructure, city.nuclear_power)

        # Improvement upkeep (money cost per day)
        rev.money -= _improvement_upkeep(city)

    # ALA: Arable Land Agency adds 20 % to total food production.
    if has_ala:
        rev.food_production *= 1.2

    # Food consumption (peacetime).
    # Rate: 1 food per 1 000 population + 1 food per 750 soldiers (per turn).
    rev.food_consumption = nation.population / 1000.0 + nation.soldiers / 750.0

    # Color bloc turn bonus (gray nations get no bonus)
    color_key = (nation.color or "").lower()
    color_turn_bonus = game_info.color_bonuses.get(color_key, 0)
    rev.money += color_turn_bonus * _TURNS_PER_DAY

    if cities:
        rev.avg_commerce = total_commerce / len(cities)

    return rev

