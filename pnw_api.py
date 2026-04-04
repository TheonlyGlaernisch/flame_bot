"""Thin async wrapper around the Politics and War GraphQL and REST APIs."""
from __future__ import annotations

import logging
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
# patterns that tolerate comma-formatted numbers (e.g. "1,234") with up to two
# decimal places.
_LOOT_GAS_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)\s+gasoline", re.IGNORECASE)
_LOOT_MUN_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)\s+munitions", re.IGNORECASE)
_LOOT_ALU_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)\s+aluminum", re.IGNORECASE)
_LOOT_STL_RE = re.compile(r"([\d,]+(?:\.\d{1,2})?)\s+steel", re.IGNORECASE)

# Attack types that yield resource loot when the attacker wins.
# GROUND: per-city loot on a successful ground battle.
# VICTORY: beige loot when the defender's resistance reaches 0.
_ATTACK_TYPES_WITH_LOOT: frozenset[str] = frozenset({"GROUND", "VICTORY"})

# Number of war IDs to include per warattacks API request.
_WARATTACKS_BATCH_SIZE = 50


def _parse_resource_loot(loot_info: str) -> tuple[float, float, float, float]:
    """Return ``(gasoline, munitions, aluminum, steel)`` looted from a *loot_info* string."""

    def _extract(pattern: re.Pattern) -> float:  # type: ignore[type-arg]
        m = pattern.search(loot_info)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        return 0.0

    return (
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
    offensivewars: int = 0
    defensivewars: int = 0

    # Turns remaining on beige (0 = not beiged); populated by GraphQL path
    beige_turns: int = 0


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

    async def get_trade_prices(self) -> TradePrice:
        """Fetch the latest market trade prices for war-relevant resources.

        Returns a :class:`TradePrice` with prices for gasoline, munitions,
        aluminum, and steel.  Falls back to zeros on any API error.
        """
        query = """
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
            data = await self._query(query, {})
            prices = data.get("data", {}).get("tradeprices", {}).get("data", [])
            if prices:
                p = prices[0]
                return TradePrice(
                    gasoline=float(p.get("gasoline") or 0),
                    munitions=float(p.get("munitions") or 0),
                    aluminum=float(p.get("aluminum") or 0),
                    steel=float(p.get("steel") or 0),
                )
        except Exception:
            pass
        return TradePrice()

    async def get_alliance_damage(
        self,
        alliance_id: int,
        after: datetime,
    ) -> dict[int, dict[str, Any]]:
        """Return per-nation damage and loot totals for wars started after *after*.

        Both offensive wars (where *alliance_id* is the attacker) and defensive
        wars (where *alliance_id* is the defender) are counted.  A nation that
        fought in both roles has all contributions accumulated into a single entry.

        Phase 1 – wars query: collects infra destroyed value, money looted, and
        war IDs for all qualifying wars.

        Phase 2 – warattacks query: for each collected war, fetches individual
        attacks of type ``GROUND`` (regular ground battle) or ``VICTORY`` (beige
        loot), and parses the ``loot_info`` text to accumulate resource loot
        totals for each member that was the victor of that specific attack.

        Returns a dict mapping nation_id -> {
            "nation_name": str,
            "num_cities": int,      # nation's current city count
            "infra_value": float,   # monetary value of infrastructure destroyed
                                    #   (att_infra_destroyed_value for offensive wars
                                    #    + def_infra_destroyed_value for defensive wars)
            "money_looted": float,  # money looted (att_money_looted for offensive wars
                                    #   only; defensive wars do not contribute because
                                    #   def_money_looted is what the enemy took FROM the
                                    #   defender, not what the defender looted)
            "gas_looted": float,    # gasoline looted on member victories
            "mun_looted": float,    # munitions looted on member victories
            "alum_looted": float,   # aluminum looted on member victories
            "steel_looted": float,  # steel looted on member victories
            "def_gas_used": float,  # gasoline the enemy was forced to spend
            "def_mun_used": float,  # munitions the enemy was forced to spend
            "def_alum_used": float, # aluminum the enemy was forced to spend
            "def_steel_used": float,# steel the enemy was forced to spend
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
                "def_gas_used": 0.0,
                "def_mun_used": 0.0,
                "def_alum_used": 0.0,
                "def_steel_used": 0.0,
            }

        # ------------------------------------------------------------------
        # Phase 1: collect qualifying wars.
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
                        att_infra_destroyed_value
                        def_infra_destroyed_value
                        att_money_looted
                        def_money_looted
                        att_gas_used
                        att_mun_used
                        att_alum_used
                        att_steel_used
                        def_gas_used
                        def_mun_used
                        def_alum_used
                        def_steel_used
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
                        entry["infra_value"] += float(war.get("att_infra_destroyed_value") or 0)
                        entry["money_looted"] += float(war.get("att_money_looted") or 0)
                        entry["def_gas_used"] += float(war.get("def_gas_used") or 0)
                        entry["def_mun_used"] += float(war.get("def_mun_used") or 0)
                        entry["def_alum_used"] += float(war.get("def_alum_used") or 0)
                        entry["def_steel_used"] += float(war.get("def_steel_used") or 0)

                        if war_id:
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
                        entry["infra_value"] += float(war.get("def_infra_destroyed_value") or 0)
                        # def_money_looted is money the ATTACKER looted FROM the defender
                        # (loot the defender lost, not gained), so we do not credit it here.
                        # Resources the enemy attacker was forced to spend.
                        entry["def_gas_used"] += float(war.get("att_gas_used") or 0)
                        entry["def_mun_used"] += float(war.get("att_mun_used") or 0)
                        entry["def_alum_used"] += float(war.get("att_alum_used") or 0)
                        entry["def_steel_used"] += float(war.get("att_steel_used") or 0)

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
        # Phase 2: collect resource loot from individual attack records.
        # We look for GROUND attacks (per-city loot on a ground battle win)
        # and VICTORY attacks (beige loot when resistance hits 0).
        # Loot only goes to the attacker of each individual attack — only
        # offensive attacks (victor == att_id) yield loot.
        # ------------------------------------------------------------------
        _LOOT_TYPES = _ATTACK_TYPES_WITH_LOOT
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
                            def_id
                            type
                            victor
                            loot_info
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
                    # Only process GROUND and VICTORY attack types.
                    if str(attack.get("type") or "") not in _LOOT_TYPES:
                        continue

                    victor = int(attack.get("victor") or 0)
                    loot_info = attack.get("loot_info") or ""
                    if not loot_info or not victor:
                        continue

                    att_id = int(attack.get("att_id") or 0)

                    # Loot only goes to the attacker of the individual attack.
                    if att_id in results and victor == att_id:
                        gas, mun, alum, steel = _parse_resource_loot(loot_info)
                        results[att_id]["gas_looted"] += gas
                        results[att_id]["mun_looted"] += mun
                        results[att_id]["alum_looted"] += alum
                        results[att_id]["steel_looted"] += steel

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

    async def get_nations_avg_infra(self, nation_ids: list[int]) -> dict[int, float]:
        """Return a mapping of nation_id -> average infrastructure per city.

        Queries the cities sub-object for each nation.  Nations not present in
        the result have no city data and are omitted from the dict.
        """
        if not nation_ids:
            return {}
        query = """
        query GetNationsAvgInfra($nation_id: [Int]) {
            nations(id: $nation_id, first: 500) {
                data {
                    id
                    cities {
                        infrastructure
                    }
                }
            }
        }
        """
        data = await self._query(query, {"nation_id": nation_ids})
        nations = data.get("data", {}).get("nations", {}).get("data", [])
        result: dict[int, float] = {}
        for n in nations:
            cities = n.get("cities") or []
            if cities:
                total = sum(float(c.get("infrastructure") or 0) for c in cities)
                result[int(n["id"])] = total / len(cities)
        return result

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
