"""
flame_bot – main entry point.

Commands
--------
/register <nation_id>
    Link your Discord account to a Politics and War nation.
    The bot verifies that your Discord username appears in the nation's
    in-game Discord field before accepting the registration.

/unregister
    Remove your own registration.

/whois <query>
    Unified nation look-up command.
    • Numeric query  → fetch that nation from the PnW API by ID.
    • @mention       → look up the mentioned member's registered nation.
    • Text           → search PnW by nation name, then fall back to
                       looking up a Discord username in the local database.
    Response is a rich embed including alliance position, seniority, and
    military capacity percentages.

/alliance info <query>
    Look up a Politics and War alliance by ID, name, or @mention.
    Returns an embed with score, member count, avg cities, and more.

/alliance members <query>
    List members of a Politics and War alliance (10 per page, ◀/▶ to page).

/test whois <query>
    Same as /whois but queries the PnW test API.

/test alliance info <query>
    Same as /alliance info but queries the PnW test API.

/config slots set <alliance_ids>
    (Admin or Milcom only) Set the alliance IDs monitored by /slots.

/config slots show
    Show the currently configured /slots alliance IDs.

/config slots clear
    (Admin or Milcom only) Clear the /slots alliance configuration.

/slots
    Show an embed listing all non-vacation-mode members of the configured
    alliances with their score, city count, and open defensive slots.
    Includes ◀ / ▶ buttons to page through up to 15 nations at a time and
    sort buttons to toggle between open-slots and score ordering.

/roles setup [econ] [milcom] [ia] [gov]
    (Admin only) Map existing server roles to government departments:
    Economics, Military Command, Internal Affairs, and Basic Gov.
    All parameters are optional; omitting one leaves that department unchanged.

/roles show
    Show the currently configured government department roles.

/gov
    Show an embed listing all server members who hold a configured government
    role, organised by department (Economics, Military Command, Internal
    Affairs, Basic Gov).

/setup grant_channel <channel>
    (Admin, Econ, or IA only) Set the channel where /request grant posts are sent.

/admin alliance set <alliance_id>
    (Admin only) Set the guild's primary Politics and War alliance ID.
    Used by /color to determine which alliance to check.

/admin alliance show
    Show the primary alliance ID configured for this guild.

/color
    Check whether all active (non-vacation, non-beige) members of the
    configured primary alliance are on the correct alliance color.
    Lists any members found on the wrong color together with their
    current color and the expected color.

/damage
    Show damage dealt by each member of the configured primary alliance
    over the past 7 days, sorted by total damage (infra destroyed value
    + money looted) descending.

/send <receiver> [sender] [bank_note] [money] [food] [coal] [oil] [uranium] [iron]
      [bauxite] [lead] [gasoline] [munitions] [steel] [aluminum]
    Compose a Locutus /transfer resources command for a resource transfer.
    receiver is a Discord ping or nation ID; bank_note defaults to #grant.
    Posts an embed with all details and the pre-formatted command:
    /transfer resources receiver:<id> transfer:{ money:1000,...} bank_note:#grant

/request grant <note> [money] [food] [coal] [oil] [uranium] [iron]
               [bauxite] [lead] [gasoline] [munitions] [steel] [aluminum]
    Request a grant from the Economics team.
    Posts an embed in the configured grant channel and pings the econ gov role
    if configured, otherwise falls back to pinging the econ role.
    note is used as the reason displayed in the embed and as the bank_note
    in the Locutus command (# prepended automatically if missing).
    Requires both a grant channel and an econ role to be configured via
    /setup grant_channel and /roles setup respectively.

/admin api_key set <api_key>
    (Admin only) Override the PnW API key used by the bot at runtime.
    The new key is persisted in the database and reloaded on restart.

/suggestion <content>
    Send a suggestion to the dev.

"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import Optional

import discord
from aiohttp import web
from discord import app_commands

import config
from api import RoleConfig, create_app
from database import Database
from pnw_api import (
    MAX_AIRCRAFT_PER_CITY,
    MAX_DEFENSIVE_SLOTS,
    MAX_SHIPS_PER_CITY,
    MAX_SOLDIERS_PER_CITY,
    MAX_TANKS_PER_CITY,
    PNW_TEST_REST_URL,
    WAR_RANGE_MAX_RATIO,
    WAR_RANGE_MIN_RATIO,
    AllianceInfo,
    City,
    GameInfo,
    Nation,
    NationRevenue,
    PnWClient,
    TradePrice,
    calculate_city_cost,
    calculate_infra_cost,
    compute_nation_revenue,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("flame_bot")

BOT_NAME = "flame_bot"

# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

ROLE_IDS: dict[str, int | None] = {
    "verified": config.VERIFIED_ROLE_ID,
    "bar3_client": config.BAR3_CLIENT_ROLE_ID,
    "bar3_server": config.BAR3_SERVER_ROLE_ID,
}

_MENTION_RE = re.compile(r"^<@!?(\d+)>$")


def _get_role(guild: discord.Guild, role_id: int | None) -> discord.Role | None:
    if role_id is None:
        return None
    return guild.get_role(role_id)


async def _check_gov_access(interaction: discord.Interaction, *role_keys: str) -> bool:
    """Return True if the caller is a guild admin or holds at least one of the given gov roles.

    *role_keys* should be keys from the gov-roles config (e.g. ``"milcom"``, ``"econ"``, ``"ia"``).
    Admins always pass regardless of configured roles.
    """
    guild_id = interaction.guild_id or 0
    member = interaction.guild and interaction.guild.get_member(interaction.user.id)
    if member and member.guild_permissions.administrator:
        return True
    if not member:
        return False
    gov_roles = bot.db.get_gov_roles(guild_id)
    return any(
        (role_id := gov_roles.get(key)) and any(r.id == role_id for r in member.roles)
        for key in role_keys
    )


async def _check_member_access(interaction: discord.Interaction) -> bool:
    """Return True if the caller is allowed to use member-gated commands.

    Passes if:
    - Caller is a guild admin.
    - The "member" role has not been configured yet (keeps backward compatibility).
    - Caller holds the configured "member" role.
    - Caller holds any configured gov role (gov members are implicitly members).
    """
    guild_id = interaction.guild_id or 0
    member = interaction.guild and interaction.guild.get_member(interaction.user.id)
    if member and member.guild_permissions.administrator:
        return True
    if not member:
        return False
    gov_roles = bot.db.get_gov_roles(guild_id)
    member_role_id = gov_roles.get("member")
    # If the member role is not configured, don't restrict access.
    if not member_role_id:
        return True
    member_role_ids = {r.id for r in member.roles}
    if member_role_id in member_role_ids:
        return True
    # Also pass if the user holds any gov role.
    _GOV_KEYS = ("leader", "2ic", "econ", "econ_gov", "milcom", "milcom_gov", "ia", "ia_asst", "gov")
    return any(
        (role_id := gov_roles.get(k)) and role_id in member_role_ids
        for k in _GOV_KEYS
    )


def _format_discord_identifier(row: object) -> str:
    """Return the stored Discord username, falling back to the numeric ID."""
    return row["discord_username"] or row["discord_id"]


_PNW_BASE_URL = "https://politicsandwar.com"
_PNW_TEST_BASE_URL = "https://test.politicsandwar.com"


def _nation_url(nation_id: int, base_url: str = _PNW_BASE_URL) -> str:
    return f"{base_url}/nation/id={nation_id}/"


def _alliance_url(alliance_id: int, base_url: str = _PNW_BASE_URL) -> str:
    return f"{base_url}/alliance/id={alliance_id}"


def _nation_embed(
    nation: Nation,
    registered_discord: str | None = None,
    note: str | None = None,
    base_url: str = _PNW_BASE_URL,
) -> discord.Embed:
    """Build a rich Discord embed for a PnW nation."""
    embed = discord.Embed(
        title=nation.nation_name,
        url=_nation_url(nation.nation_id, base_url),
        color=discord.Color.blue(),
    )

    embed.add_field(name="ID", value=str(nation.nation_id), inline=True)
    embed.add_field(name="Leader", value=nation.leader_name or "—", inline=True)

    # Alliance — hyperlinked name with position + seniority on the second line
    if nation.alliance_id:
        alliance_label = nation.alliance_name or str(nation.alliance_id)
        alliance_val = f"[{alliance_label}]({_alliance_url(nation.alliance_id, base_url)})"
    else:
        alliance_val = "None"

    pos = nation.alliance_position
    if pos and pos not in ("NOALLIANCE", ""):
        pos_line = pos.title()
        if nation.alliance_seniority > 0:
            pos_line += f" • {nation.alliance_seniority}d"
        alliance_val += f"\n{pos_line}"

    embed.add_field(name="Alliance", value=alliance_val, inline=True)

    embed.add_field(name="Score", value=f"{nation.score:,.2f}", inline=True)
    embed.add_field(name="Cities", value=str(nation.num_cities), inline=True)

    if nation.rank:
        embed.add_field(name="Rank", value=f"#{nation.rank:,}", inline=True)

    if nation.continent:
        embed.add_field(name="Continent", value=nation.continent, inline=True)

    if nation.war_policy:
        embed.add_field(name="War Policy", value=nation.war_policy, inline=True)

    if nation.color:
        embed.add_field(name="Color", value=nation.color.title(), inline=True)

    if nation.offensive_wars or nation.defensive_wars:
        embed.add_field(
            name="Wars",
            value=f"⚔️ {nation.offensive_wars} off / 🛡️ {nation.defensive_wars} def",
            inline=True,
        )

    # Projects: count + short abbreviations of built projects
    if nation.projects_built:
        projects_value = f"{nation.num_projects} — {', '.join(nation.projects_built)}"
    else:
        projects_value = "0"
    embed.add_field(name="Projects", value=projects_value, inline=False)

    # Average infrastructure estimate
    # PnW score = (cities-1)*100 + 10  [city score]
    #           + projects * 20         [project score, 20 pts each]
    #           + total_infra / 40      [infra score, 1 pt per 40 infra]
    #           + military_score        [soldiers*0.0004 + tanks*0.025 + aircraft*0.3
    #                                    + ships*1 + missiles*5 + nukes*15]
    # Solving for avg_infra:
    #   avg_infra = (score - (cities-1)*100 - 10 - projects*20 - military_score) * 40 / cities
    if nation.num_cities > 0:
        military_score = (
            nation.soldiers * 0.0004
            + nation.tanks * 0.025
            + nation.aircraft * 0.3
            + nation.ships * 1.0
            + nation.missiles * 5.0
            + nation.nukes * 15.0
        )
        infra_score = nation.score - (nation.num_cities - 1) * 100 - 10 - nation.num_projects * 20 - military_score
        avg_infra = infra_score * 40 / nation.num_cities
        embed.add_field(name="Avg Infra", value=f"{avg_infra:,.2f}", inline=True)

    # Use a live Discord relative timestamp when available (GraphQL path);
    # fall back to the plain string for REST-sourced nations.
    if nation.last_active_unix:
        embed.add_field(name="Last Active", value=f"<t:{nation.last_active_unix}:R>", inline=True)
    elif nation.last_active:
        embed.add_field(name="Last Active", value=nation.last_active, inline=True)

    # Military capacity percentages
    if nation.num_cities > 0:
        max_sol = MAX_SOLDIERS_PER_CITY * nation.num_cities
        max_tan = MAX_TANKS_PER_CITY * nation.num_cities
        max_air = MAX_AIRCRAFT_PER_CITY * nation.num_cities
        max_shi = MAX_SHIPS_PER_CITY * nation.num_cities

        def pct(val: int, cap: int) -> str:
            if cap == 0:
                return f"{val:,} (—)"
            return f"{val:,} ({val / cap * 100:.1f}%)"

        military_text = (
            f"🪖 Soldiers: {pct(nation.soldiers, max_sol)}\n"
            f"⚔️ Tanks:    {pct(nation.tanks, max_tan)}\n"
            f"✈️ Aircraft: {pct(nation.aircraft, max_air)}\n"
            f"🚢 Ships:    {pct(nation.ships, max_shi)}\n"
            f"🚀 Missiles: {nation.missiles:,}\n"
            f"☢️ Nukes:    {nation.nukes:,}"
        )
        embed.add_field(name="Military", value=military_text, inline=False)

    if registered_discord:
        embed.add_field(name="Discord", value=registered_discord, inline=True)
    elif nation.discord_tag:
        embed.add_field(name="PnW Discord", value=f"`{nation.discord_tag}`", inline=True)

    if note:
        embed.set_footer(text=note)

    return embed


def _alliance_embed(info: AllianceInfo, base_url: str = _PNW_BASE_URL) -> discord.Embed:
    """Build a rich Discord embed for a PnW alliance."""
    title = f"{info.name} ({info.acronym})" if info.acronym else info.name
    embed = discord.Embed(
        title=title,
        url=_alliance_url(info.alliance_id, base_url),
        color=discord.Color.gold(),
    )

    if info.flag:
        embed.set_thumbnail(url=info.flag)

    embed.add_field(name="ID", value=str(info.alliance_id), inline=True)
    embed.add_field(name="Score", value=f"{info.score:,.2f}", inline=True)
    embed.add_field(name="Avg Score", value=f"{info.average_score:,.2f}", inline=True)
    embed.add_field(name="Color", value=info.color.title() if info.color else "—", inline=True)
    embed.add_field(name="Members", value=str(info.num_members), inline=True)
    embed.add_field(name="Applicants", value=str(info.num_applicants), inline=True)
    if info.rank:
        embed.add_field(name="Rank", value=f"#{info.rank}", inline=True)
    if info.total_cities:
        embed.add_field(name="Total Cities", value=str(info.total_cities), inline=True)
    embed.add_field(name="Avg Cities", value=f"{info.avg_cities:.1f}", inline=True)

    if info.discord_link:
        embed.add_field(name="Discord", value=f"[Join Server]({info.discord_link})", inline=True)

    return embed


def _error_embed(description: str) -> discord.Embed:
    """Return a red embed for error messages."""
    return discord.Embed(description=description, color=discord.Color.red())


def _info_embed(description: str) -> discord.Embed:
    """Return a blurple embed for informational messages."""
    return discord.Embed(description=description, color=discord.Color.blurple())


def _success_embed(description: str) -> discord.Embed:
    """Return a green embed for success messages."""
    return discord.Embed(description=description, color=discord.Color.green())


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------


class FlameBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.MONGODB_URI)
        self.pnw = PnWClient(config.PNW_API_KEY)
        self.pnw_test = PnWClient(config.PNW_TEST_API_KEY, rest_url=PNW_TEST_REST_URL)
        self._api_runner: web.AppRunner | None = None
        self._invite_refresh_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        await self.tree.sync()
        log.info("Slash commands synced globally.")

    async def _create_guild_invite(self, guild: discord.Guild) -> str | None:
        """Create and return a non-expiring, unlimited-use invite URL for this guild."""
        me = guild.me
        if me is None:
            return None
        candidate_channels: list[discord.TextChannel] = []
        if isinstance(guild.system_channel, discord.TextChannel):
            candidate_channels.append(guild.system_channel)
        candidate_channels.extend(guild.text_channels)

        seen_channel_ids: set[int] = set()
        for channel in candidate_channels:
            if channel.id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel.id)
            if not channel.permissions_for(me).create_instant_invite:
                continue
            try:
                invite = await channel.create_invite(
                    max_age=0,
                    max_uses=0,
                    unique=True,
                    reason="Persist guild invite link for flame_bot metadata.",
                )
                return invite.url
            except (discord.Forbidden, discord.HTTPException):
                continue
        return None

    async def _persist_guild(self, guild: discord.Guild) -> None:
        """Persist this guild's ID, name, and invite link in MongoDB."""
        invite_link = await self._create_guild_invite(guild)
        self.db.upsert_guild(guild.id, guild.name, invite_link)

    async def _invite_exists(self, invite_link: str) -> bool:
        """Return True when the invite URL still resolves, False when deleted/invalid."""
        try:
            await self.fetch_invite(invite_link)
            return True
        except discord.NotFound:
            return False
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _refresh_deleted_guild_invites_once(self) -> None:
        """Recreate and persist invites for guilds whose stored invite is missing."""
        for doc in self.db.get_all_guilds():
            guild_id_raw = doc.get("guild_id")
            if not guild_id_raw:
                continue
            guild = self.get_guild(int(guild_id_raw))
            if guild is None:
                continue
            invite_link = doc.get("invite_link")
            if invite_link and await self._invite_exists(invite_link):
                continue
            await self._persist_guild(guild)
            log.info("Refreshed deleted/missing invite for guild %s (%d).", guild.name, guild.id)

    async def _daily_invite_refresh_loop(self) -> None:
        """Check once per day whether stored invites still exist, and recreate if missing."""
        await self.wait_until_ready()
        while not self.is_closed():
            await self._refresh_deleted_guild_invites_once()
            await asyncio.sleep(24 * 60 * 60)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%d)", self.user, self.user.id)
        for guild in self.guilds:
            await self._persist_guild(guild)
        log.info("Persisted metadata for %d guilds to MongoDB.", len(self.guilds))
        if self._invite_refresh_task is None or self._invite_refresh_task.done():
            self._invite_refresh_task = asyncio.create_task(self._daily_invite_refresh_loop())
            log.info("Started daily guild invite refresh loop.")
        overridden_key = self.db.get_pnw_api_key()
        if overridden_key:
            self.pnw._api_key = overridden_key
            log.info("Loaded overridden PnW API key from database.")
        if config.API_KEY:
            await self._start_api()

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._persist_guild(guild)
        log.info("Joined guild %s (%d); name persisted.", guild.name, guild.id)

    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if before.name != after.name:
            await self._persist_guild(after)
            log.info("Guild renamed %s -> %s (%d); name persisted.", before.name, after.name, after.id)

    async def _start_api(self) -> None:
        app = create_app(
            guild_getter=lambda: self.get_guild(config.GUILD_ID) if config.GUILD_ID else None,
            api_key=config.API_KEY,  # type: ignore[arg-type]
            role_config=RoleConfig(
                verified_role_id=config.VERIFIED_ROLE_ID,
                bar3_client_role_id=config.BAR3_CLIENT_ROLE_ID,
                bar3_server_role_id=config.BAR3_SERVER_ROLE_ID,
            ),
        )
        self._api_runner = web.AppRunner(app)
        await self._api_runner.setup()
        site = web.TCPSite(self._api_runner, "0.0.0.0", config.API_PORT)
        await site.start()
        log.info("bar3 API listening on port %d.", config.API_PORT)

    async def close(self) -> None:
        await self.pnw.close()
        await self.pnw_test.close()
        if self._invite_refresh_task is not None:
            self._invite_refresh_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._invite_refresh_task
        if self._api_runner is not None:
            await self._api_runner.cleanup()
        await super().close()


bot = FlameBot()

# ---------------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="register",
    description="Link your Discord account to your Politics and War nation.",
)
@app_commands.describe(nation_id="Your numeric Politics and War nation ID.")
async def register(interaction: discord.Interaction, nation_id: int) -> None:
    await interaction.response.defer()

    if nation_id <= 0:
        await interaction.followup.send(
            embed=_error_embed("❌ Please provide a valid positive nation ID.")
        )
        return

    # ------------------------------------------------------------------
    # Check whether this nation is already registered to someone else
    # ------------------------------------------------------------------
    existing_by_nation = bot.db.get_by_nation_id(nation_id)
    if existing_by_nation and int(existing_by_nation["discord_id"]) != interaction.user.id:
        await interaction.followup.send(
            embed=_error_embed("❌ That nation is already registered to a different Discord account."),
        )
        return

    # ------------------------------------------------------------------
    # Fetch nation from PnW
    # ------------------------------------------------------------------
    try:
        nation = await bot.pnw.get_nation(nation_id)
    except Exception as exc:
        log.exception("PnW API error while fetching nation %d", nation_id)
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if nation is None:
        await interaction.followup.send(
            embed=_error_embed(f"❌ Nation with ID **{nation_id}** was not found.")
        )
        return

    # ------------------------------------------------------------------
    # Verify Discord username against the nation's discord field
    # ------------------------------------------------------------------
    username = interaction.user.name
    if not PnWClient.discord_matches(nation.discord_tag, username):
        await interaction.followup.send(
            embed=_error_embed(
                f"❌ Verification failed.\n\n"
                f"Nation **{nation.nation_name}** (leader: {nation.leader_name}) "
                f"has `{nation.discord_tag or '(empty)'}` as its Discord handle, "
                f"but your Discord username is `{username}`.\n\n"
                f"Please set your Discord handle on your nation's edit page to "
                f"`{username}` and try again."
            ),
        )
        return

    # ------------------------------------------------------------------
    # Save registration
    # ------------------------------------------------------------------
    bot.db.register(interaction.user.id, nation_id, discord_username=interaction.user.name)
    log.info(
        "Registered discord=%d (%s) → nation=%d (%s)",
        interaction.user.id,
        username,
        nation_id,
        nation.nation_name,
    )

    # ------------------------------------------------------------------
    # Assign roles
    # ------------------------------------------------------------------
    guild = interaction.guild
    role_mentions: list[str] = []

    if guild is not None:
        member = guild.get_member(interaction.user.id)
        if member is not None:
            verified_role = _get_role(guild, config.VERIFIED_ROLE_ID)
            if verified_role and verified_role not in member.roles:
                try:
                    await member.add_roles(verified_role, reason=f"{BOT_NAME}: /register")
                    role_mentions.append(verified_role.mention)
                except discord.Forbidden:
                    log.warning("Missing permission to assign role %s", verified_role)

    roles_text = (
        f"\n\nYou have been given: {', '.join(role_mentions)}"
        if role_mentions
        else ""
    )

    await interaction.followup.send(
        embed=_success_embed(
            f"✅ Successfully registered!\n"
            f"Nation: **{nation.nation_name}** (ID: `{nation_id}`, leader: {nation.leader_name})"
            f"{roles_text}"
        ),
    )


# ---------------------------------------------------------------------------
# /whois  (unified replacement for the former /who and /whois commands)
# ---------------------------------------------------------------------------


async def _handle_whois(
    interaction: discord.Interaction, pnw: PnWClient, query: str,
    base_url: str = _PNW_BASE_URL,
) -> None:
    """Shared logic for /whois and /test whois."""
    query = query.strip()

    # ------------------------------------------------------------------
    # 1. @mention → look up the mentioned member's registered nation
    # ------------------------------------------------------------------
    mention_match = _MENTION_RE.match(query)
    if mention_match:
        target_id = int(mention_match.group(1))
        row = bot.db.get_by_discord_id(target_id)
        if row is None:
            # Not registered locally — try to find them on PnW by Discord username.
            member = interaction.guild and interaction.guild.get_member(target_id)
            if member is None and interaction.guild is not None:
                try:
                    member = await interaction.guild.fetch_member(target_id)
                except discord.HTTPException:
                    member = None
            nation: Optional[Nation] = None
            if member is not None:
                try:
                    nation = await pnw.get_nation_by_discord_tag(member.name)
                    # Verify the returned nation's tag actually matches this user
                    # (the API filter may return partial matches).
                    if nation is not None and not pnw.discord_matches(
                        nation.discord_tag, member.name
                    ):
                        nation = None
                    # Also try the legacy "username#discriminator" format — many
                    # PnW players stored their old Discord tag before Discord
                    # migrated to the new username system.
                    if nation is None and member.discriminator and member.discriminator != "0":
                        legacy_tag = f"{member.name}#{member.discriminator}"
                        nation = await pnw.get_nation_by_discord_tag(legacy_tag)
                        if nation is not None and not pnw.discord_matches(
                            nation.discord_tag, legacy_tag
                        ):
                            nation = None
                except Exception:
                    nation = None
            if nation is not None:
                embed = _nation_embed(
                    nation,
                    registered_discord=f"<@{target_id}>",
                    note="ℹ️ Found via PnW discord field (not locally registered).",
                    base_url=base_url,
                )
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send(
                    embed=_info_embed(f"ℹ️ <@{target_id}> has not registered yet and no matching PnW nation was found.")
                )
            return

        nation_id = row["nation_id"]
        try:
            nation = await pnw.get_nation(nation_id)
        except Exception:
            nation = None

        if nation:
            embed = _nation_embed(nation, registered_discord=f"<@{target_id}>", base_url=base_url)
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                embed=_info_embed(
                    f"<@{target_id}> is registered with nation ID `{nation_id}` "
                    "(nation details unavailable)."
                )
            )
        return

    # ------------------------------------------------------------------
    # 2. Numeric query → fetch nation from the PnW API by ID
    # ------------------------------------------------------------------
    if query.lstrip("-").isdigit():
        nation_id = int(query)
        if nation_id <= 0:
            await interaction.followup.send(
                embed=_error_embed("❌ Please provide a valid positive nation ID.")
            )
            return

        try:
            nation = await pnw.get_nation(nation_id)
        except Exception as exc:
            log.exception("PnW API error while fetching nation %d", nation_id)
            await interaction.followup.send(
                embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
            )
            return

        if nation is None:
            await interaction.followup.send(
                embed=_info_embed(f"ℹ️ No nation with ID `{nation_id}` was found.")
            )
            return

        # Surface any local registration for this nation
        row = bot.db.get_by_nation_id(nation_id)
        discord_user = f"`{_format_discord_identifier(row)}`" if row else None

        embed = _nation_embed(nation, registered_discord=discord_user, base_url=base_url)
        await interaction.followup.send(embed=embed)
        return

    # ------------------------------------------------------------------
    # 3. Text → try nation name search (PnW API), then Discord username (DB)
    # ------------------------------------------------------------------
    try:
        nation = await pnw.get_nation_by_name(query)
    except Exception as exc:
        log.exception("PnW API error while searching nation name '%s'", query)
        nation = None

    if nation:
        row = bot.db.get_by_nation_id(nation.nation_id)
        discord_user = f"`{_format_discord_identifier(row)}`" if row else None
        embed = _nation_embed(nation, registered_discord=discord_user, base_url=base_url)
        await interaction.followup.send(embed=embed)
        return

    # Fall back to Discord username lookup in the local database
    row = bot.db.get_by_discord_username(query)
    if row is None:
        await interaction.followup.send(
            embed=_info_embed(f"ℹ️ No nation or Discord user found for `{query}`.")
        )
        return

    nation_id = row["nation_id"]
    try:
        nation = await pnw.get_nation(nation_id)
    except Exception:
        nation = None

    stored_name = _format_discord_identifier(row)
    if nation:
        embed = _nation_embed(nation, registered_discord=f"`{stored_name}`", base_url=base_url)
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(
            embed=_info_embed(
                f"**{stored_name}** is registered with nation ID `{nation_id}` "
                "(nation details unavailable)."
            )
        )


@bot.tree.command(
    name="whois",
    description="Look up a PnW nation by ID, nation name, or @mention / Discord username.",
)
@app_commands.describe(
    query=(
        "A nation ID, an @mention, a nation name, or a Discord username."
    )
)
async def whois(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_whois(interaction, bot.pnw, query)


# ---------------------------------------------------------------------------
# /unregister
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="unregister",
    description="Remove your Politics and War nation registration from this bot.",
)
async def unregister(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    deleted = bot.db.delete(interaction.user.id)
    if deleted:
        await interaction.followup.send(
            embed=_success_embed("✅ Your registration has been removed."), ephemeral=True
        )
    else:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ You are not currently registered."), ephemeral=True
        )


# ---------------------------------------------------------------------------
# /alliance  (command group)
# ---------------------------------------------------------------------------

_MEMBERS_PAGE_SIZE = 10


async def _handle_alliance_find(
    interaction: discord.Interaction, pnw: PnWClient, query: str,
    base_url: str = _PNW_BASE_URL,
) -> None:
    """Shared logic for /alliance info and /test alliance info."""
    query = query.strip()
    mention_match = _MENTION_RE.match(query)

    async def _get_mentioned_nation_via_api(discord_id: int) -> Nation | None:
        """Resolve a mentioned member to a nation using the PnW API only."""
        member = interaction.guild and interaction.guild.get_member(discord_id)
        if member is None and interaction.guild is not None:
            try:
                member = await interaction.guild.fetch_member(discord_id)
            except discord.HTTPException:
                member = None
        if member is None:
            return None

        candidate_tags = [
            member.name,
            member.display_name,
            member.global_name or "",
        ]
        if member.discriminator and member.discriminator != "0":
            candidate_tags.append(f"{member.name}#{member.discriminator}")

        for tag in candidate_tags:
            candidate = tag.strip()
            if not candidate:
                continue
            nation = await pnw.get_nation_by_discord_tag(candidate)
            if nation is not None and pnw.discord_matches(nation.discord_tag, candidate):
                return nation
        return None

    try:
        if mention_match:
            discord_id = int(mention_match.group(1))
            nation = await _get_mentioned_nation_via_api(discord_id)
            if nation is None:
                await interaction.followup.send(
                    embed=_info_embed(
                        f"ℹ️ Could not resolve <@{discord_id}> via the PnW Discord field."
                    )
                )
                return
            if nation.alliance_id <= 0:
                await interaction.followup.send(
                    embed=_info_embed(
                        f"ℹ️ [{nation.nation_name}]({_nation_url(nation.nation_id, base_url)}) is not currently in an alliance."
                    )
                )
                return
            info = await pnw.get_alliance_by_id(nation.alliance_id)
        elif query.isdigit():
            info = await pnw.get_alliance_by_id(int(query))
        else:
            info = await pnw.get_alliance_by_name(query)
    except Exception as exc:
        log.exception("PnW API error while fetching alliance '%s'", query)
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if info is None:
        await interaction.followup.send(
            embed=_info_embed(f"ℹ️ No alliance found for `{query}`.")
        )
        return

    await interaction.followup.send(embed=_alliance_embed(info, base_url=base_url))


async def _handle_alliance_members(
    interaction: discord.Interaction, pnw: PnWClient, query: str,
    base_url: str = _PNW_BASE_URL,
) -> None:
    """Shared logic for /alliance members and /test alliance members."""
    query = query.strip()
    try:
        if query.isdigit():
            info = await pnw.get_alliance_by_id(int(query))
        else:
            info = await pnw.get_alliance_by_name(query)
    except Exception as exc:
        log.exception("PnW API error while fetching alliance '%s'", query)
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if info is None:
        await interaction.followup.send(
            embed=_info_embed(f"ℹ️ No alliance found for `{query}`.")
        )
        return

    try:
        members = await pnw.get_alliance_members([info.alliance_id])
    except Exception as exc:
        log.exception("PnW API error while fetching members for alliance %d", info.alliance_id)
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not fetch alliance members: {exc}")
        )
        return

    members.sort(key=lambda n: n.score, reverse=True)
    embed = _build_alliance_members_page(members, info, 0, base_url)
    view = AllianceMembersView(members, info, base_url=base_url)
    await interaction.followup.send(embed=embed, view=view)


def _build_alliance_members_page(
    members: list[Nation],
    alliance: AllianceInfo,
    page: int = 0,
    base_url: str = _PNW_BASE_URL,
) -> discord.Embed:
    """Return a single paginated embed listing alliance members (up to 10 nations)."""
    total = len(members)
    total_pages = max(1, (total + _MEMBERS_PAGE_SIZE - 1) // _MEMBERS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    page_members = members[page * _MEMBERS_PAGE_SIZE : (page + 1) * _MEMBERS_PAGE_SIZE]

    _POS_ICON: dict[str, str] = {
        "LEADER": "👑",
        "HEIR": "⚔️",
        "OFFICER": "🌟",
        "MEMBER": "👤",
        "APPLICANT": "📝",
    }

    lines: list[str] = []
    start = page * _MEMBERS_PAGE_SIZE
    for i, nation in enumerate(page_members, start=start + 1):
        icon = _POS_ICON.get(nation.alliance_position, "👤")
        line = (
            f"`{i:>3}.` {icon} [{nation.nation_name}]({_nation_url(nation.nation_id, base_url)})"
            f" — 🏙️ {nation.num_cities} | ⭐ {nation.score:,.0f}"
        )
        lines.append(line)

    title = (
        f"{alliance.name} ({alliance.acronym}) — Members"
        if alliance.acronym
        else f"{alliance.name} — Members"
    )
    embed = discord.Embed(
        title=title,
        url=_alliance_url(alliance.alliance_id, base_url),
        description="\n".join(lines) if lines else "*(no members)*",
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Page {page + 1}/{total_pages} • {total} members total")
    return embed


class AllianceMembersView(discord.ui.View):
    """◀/▶ pagination buttons for the /alliance members response."""

    def __init__(
        self,
        members: list[Nation],
        alliance: AllianceInfo,
        page: int = 0,
        base_url: str = _PNW_BASE_URL,
    ) -> None:
        super().__init__(timeout=600)
        self.members = members
        self.alliance = alliance
        self.page = page
        self.base_url = base_url
        self._refresh_buttons()

    def _total_pages(self) -> int:
        return max(1, (len(self.members) + _MEMBERS_PAGE_SIZE - 1) // _MEMBERS_PAGE_SIZE)

    def _refresh_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self._total_pages() - 1

    async def _update(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        embed = _build_alliance_members_page(self.members, self.alliance, self.page, self.base_url)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = min(self._total_pages() - 1, self.page + 1)
        await self._update(interaction)


alliance_group = app_commands.Group(
    name="alliance",
    description="Politics and War alliance commands.",
)
bot.tree.add_command(alliance_group)


@alliance_group.command(
    name="info",
    description="Look up a Politics and War alliance by ID, name, or @mention.",
)
@app_commands.describe(query="Alliance ID, alliance name, or a @mention.")
async def alliance_find(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_alliance_find(interaction, bot.pnw, query)


@alliance_group.command(
    name="members",
    description="List members of a Politics and War alliance (10 per page).",
)
@app_commands.describe(query="Alliance ID (numeric) or alliance name.")
async def alliance_members(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_alliance_members(interaction, bot.pnw, query)


# ---------------------------------------------------------------------------
# /config  (command group)
# ---------------------------------------------------------------------------

config_group = app_commands.Group(
    name="config",
    description="Bot configuration commands (admin only).",
)
bot.tree.add_command(config_group)

# /config slots  — nested subgroup
config_slots_group = app_commands.Group(
    name="slots",
    description="Configure the alliances monitored by /slots.",
)
config_group.add_command(config_slots_group)


def _parse_alliance_ids(raw: str) -> list[int] | None:
    """Parse a comma-separated string of positive integers. Returns None on error."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    result: list[int] = []
    for part in parts:
        if not part.isdigit() or int(part) <= 0:
            return None
        result.append(int(part))
    return result or None


@config_slots_group.command(
    name="set",
    description="Set the alliance IDs monitored by /slots (admin or milcom only).",
)
@app_commands.describe(
    alliance_ids="Comma-separated Politics and War alliance IDs to monitor."
)
async def config_slots_set(interaction: discord.Interaction, alliance_ids: str) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            "❌ You need the **Member** role to use this command.",
            ephemeral=True,
        )
        return

    if not await _check_gov_access(interaction, "milcom", "milcom_gov"):
        await interaction.followup.send(
            "❌ You need the **Administrator** or **Military Command** role to use this command.",
            ephemeral=True,
        )
        return

    parsed = _parse_alliance_ids(alliance_ids)
    if parsed is None:
        await interaction.followup.send(
            "❌ Invalid input. Please provide positive integers separated by commas.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    bot.db.set_slots_alliances(guild_id, parsed)
    log.info("Guild %d: /slots alliances set to %s by %s", guild_id, parsed, interaction.user)
    await interaction.followup.send(
        f"✅ /slots will now monitor alliance(s): `{', '.join(str(a) for a in parsed)}`",
        ephemeral=True,
    )


@config_slots_group.command(
    name="show",
    description="Show the currently configured /slots alliance IDs.",
)
async def config_slots_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            "❌ You need the **Member** role to use this command.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    ids = bot.db.get_slots_alliances(guild_id)
    if not ids:
        await interaction.followup.send(
            "ℹ️ No alliances configured yet. Use `/config slots set` to configure.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"ℹ️ Currently monitoring: `{', '.join(str(a) for a in ids)}`",
            ephemeral=True,
        )


@config_slots_group.command(
    name="clear",
    description="Clear the /slots alliance configuration (admin or milcom only).",
)
async def config_slots_clear(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            "❌ You need the **Member** role to use this command.",
            ephemeral=True,
        )
        return

    if not await _check_gov_access(interaction, "milcom", "milcom_gov"):
        await interaction.followup.send(
            "❌ You need the **Administrator** or **Military Command** role to use this command.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    bot.db.set_slots_alliances(guild_id, [])
    log.info("Guild %d: /slots alliances cleared by %s", guild_id, interaction.user)
    await interaction.followup.send(
        "✅ /slots alliance configuration cleared.", ephemeral=True
    )


# ---------------------------------------------------------------------------
# /slots  — helpers + View
# ---------------------------------------------------------------------------

_PAGE_SIZE = 15


def _sort_members(
    members: list[Nation],
    war_counts: dict[int, int],
    sort_key: str,
) -> list[Nation]:
    if sort_key == "slots":
        return sorted(
            members,
            key=lambda n: (
                MAX_DEFENSIVE_SLOTS - war_counts.get(n.nation_id, 0),
                n.score,
            ),
            reverse=True,
        )
    return sorted(members, key=lambda n: n.score, reverse=True)


def _build_slots_page(
    members: list[Nation],
    war_counts: dict[int, int],
    page: int = 0,
    sort_key: str = "slots",
) -> discord.Embed:
    """Return a single paginated embed for the /slots display (up to 15 nations)."""
    sorted_members = _sort_members(members, war_counts, sort_key)
    total = len(sorted_members)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))

    page_members = sorted_members[page * _PAGE_SIZE : (page + 1) * _PAGE_SIZE]
    total_open = sum(
        MAX_DEFENSIVE_SLOTS - war_counts.get(n.nation_id, 0) for n in members
    )

    lines: list[str] = []
    for nation in page_members:
        open_slots = MAX_DEFENSIVE_SLOTS - war_counts.get(nation.nation_id, 0)
        if nation.alliance_name:
            aa = nation.alliance_name
        elif nation.alliance_id:
            aa = f"AA:{nation.alliance_id}"
        else:
            aa = "None"
        line = (
            f"[{nation.nation_name}]({_nation_url(nation.nation_id)}) ({aa})"
            f" — 🏙️ {nation.num_cities} | ⭐ {nation.score:,.0f}"
            f" | 🛡️ {open_slots}/{MAX_DEFENSIVE_SLOTS}"
        )
        if nation.beige_turns > 0:
            line += f" | 🟡 {nation.beige_turns} beige turns"
        lines.append(line)

    sort_label = "Open Slots" if sort_key == "slots" else "Score"
    embed = discord.Embed(
        title=f"Defensive Slots — Sorted by {sort_label}",
        description="\n".join(lines) if lines else "*(no members)*",
        color=discord.Color.green(),
    )
    embed.set_footer(
        text=f"Page {page + 1}/{total_pages} · {total} members total · {total_open} open slots total"
    )
    return embed


class SlotsView(discord.ui.View):
    """Pagination and sort-toggle buttons attached to the /slots response."""

    def __init__(
        self,
        members: list[Nation],
        war_counts: dict[int, int],
        page: int = 0,
        sort_key: str = "slots",
    ) -> None:
        super().__init__(timeout=600)
        self.members = members
        self.war_counts = war_counts
        self.page = page
        self.sort_key = sort_key
        self._refresh_buttons()

    def _total_pages(self) -> int:
        return max(1, (len(self.members) + _PAGE_SIZE - 1) // _PAGE_SIZE)

    def _refresh_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self._total_pages() - 1

    async def _update(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        embed = _build_slots_page(self.members, self.war_counts, self.page, self.sort_key)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="Sort: Open Slots", style=discord.ButtonStyle.primary, emoji="🛡️", row=0)
    async def sort_slots(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.sort_key = "slots"
        self.page = 0
        await self._update(interaction)

    @discord.ui.button(label="Sort: Score", style=discord.ButtonStyle.secondary, emoji="⭐", row=0)
    async def sort_score(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.sort_key = "score"
        self.page = 0
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = min(self._total_pages() - 1, self.page + 1)
        await self._update(interaction)


# ---------------------------------------------------------------------------
# /slots
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="slots",
    description="Show open defensive slots for members of the configured alliances.",
)
async def slots(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    alliance_ids = bot.db.get_slots_alliances(guild_id)

    if not alliance_ids:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No alliances configured. An admin can use `/config slots set` to set them up.")
        )
        return

    # Fetch alliance members
    try:
        members = await bot.pnw.get_alliance_members(alliance_ids)
    except Exception as exc:
        log.exception("PnW API error while fetching alliance members")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if not members:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No active members found for the configured alliance(s).")
        )
        return

    # Fetch active defensive war counts for all members
    nation_ids = [n.nation_id for n in members]
    try:
        war_counts = await bot.pnw.get_active_war_counts(nation_ids)
    except Exception:
        log.exception("PnW API error while fetching war counts")
        war_counts = {}

    # Default: sorted by open slots descending (most vulnerable first)
    embed = _build_slots_page(members, war_counts, page=0, sort_key="slots")
    view = SlotsView(members, war_counts, page=0, sort_key="slots")
    await interaction.followup.send(embed=embed, view=view)


# ---------------------------------------------------------------------------
# /roles  (command group)
# ---------------------------------------------------------------------------

_GOV_DEPT_LABELS: dict[str, str] = {
    "leader": "Leader",
    "2ic": "Second in Command",
    "econ": "Economics",
    "econ_gov": "Economics Gov",
    "milcom": "Military Command",
    "milcom_gov": "Military Command Gov",
    "ia": "Internal Affairs",
    "ia_asst": "Internal Affairs Assistant",
    "gov": "Basic Gov",
    "member": "Member",
}

roles_group = app_commands.Group(
    name="roles",
    description="Government role configuration.",
)
bot.tree.add_command(roles_group)


@roles_group.command(
    name="setup",
    description="Map existing server roles to government departments (admin only).",
)
@app_commands.describe(
    leader="Role that counts as Leader.",
    two_ic="Role that counts as Second in Command.",
    econ="Role that counts as Economics.",
    econ_gov="Role that counts as Economics Gov.",
    milcom="Role that counts as Military Command.",
    milcom_gov="Role that counts as Military Command Gov.",
    ia="Role that counts as Internal Affairs.",
    ia_asst="Role that counts as Internal Affairs Assistant.",
    gov="Role that counts as Basic Gov.",
    member="Role required to use most bot commands (not shown in /gov).",
)
@app_commands.checks.has_permissions(administrator=True)
async def roles_setup(
    interaction: discord.Interaction,
    leader: discord.Role | None = None,
    two_ic: discord.Role | None = None,
    econ: discord.Role | None = None,
    econ_gov: discord.Role | None = None,
    milcom: discord.Role | None = None,
    milcom_gov: discord.Role | None = None,
    ia: discord.Role | None = None,
    ia_asst: discord.Role | None = None,
    gov: discord.Role | None = None,
    member: discord.Role | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild_id or 0
    current = bot.db.get_gov_roles(guild_id)

    updates = {
        "leader": leader.id if leader else current["leader"],
        "2ic": two_ic.id if two_ic else current["2ic"],
        "econ": econ.id if econ else current["econ"],
        "econ_gov": econ_gov.id if econ_gov else current["econ_gov"],
        "milcom": milcom.id if milcom else current["milcom"],
        "milcom_gov": milcom_gov.id if milcom_gov else current["milcom_gov"],
        "ia": ia.id if ia else current["ia"],
        "ia_asst": ia_asst.id if ia_asst else current["ia_asst"],
        "gov": gov.id if gov else current["gov"],
        "member": member.id if member else current["member"],
    }
    bot.db.set_gov_roles(guild_id, updates)
    log.info("Guild %d: gov roles updated to %s by %s", guild_id, updates, interaction.user)

    guild = interaction.guild
    lines: list[str] = []
    for key, label in _GOV_DEPT_LABELS.items():
        role_id = updates[key]
        if role_id and guild:
            role = guild.get_role(role_id)
            lines.append(f"**{label}:** {role.mention if role else f'<@&{role_id}>'}")
        else:
            lines.append(f"**{label}:** *(not set)*")

    await interaction.followup.send(
        "✅ Government role configuration updated:\n" + "\n".join(lines),
        ephemeral=True,
    )


@roles_setup.error
async def roles_setup_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


@roles_group.command(
    name="show",
    description="Show the currently configured government department roles.",
)
async def roles_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            "❌ You need the **Member** role to use this command.",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    config_roles = bot.db.get_gov_roles(guild_id)
    guild = interaction.guild

    lines: list[str] = []
    for key, label in _GOV_DEPT_LABELS.items():
        role_id = config_roles[key]
        if role_id and guild:
            role = guild.get_role(role_id)
            lines.append(f"**{label}:** {role.mention if role else f'<@&{role_id}>'}")
        else:
            lines.append(f"**{label}:** *(not set)*")

    await interaction.followup.send(
        "ℹ️ Current government role configuration:\n" + "\n".join(lines),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /gov
# ---------------------------------------------------------------------------

# Emoji prefix for each department shown in the /gov embed.
_GOV_DEPT_EMOJI: dict[str, str] = {
    "leader": "👑",
    "2ic": "🥈",
    "econ": "💰",
    "econ_gov": "📊",
    "milcom": "⚔️",
    "milcom_gov": "🛡️",
    "ia": "🤝",
    "ia_asst": "📋",
    "gov": "🏛️",
}

# Departments hidden from the /gov embed (still configurable via /roles setup).
_GOV_HIDDEN_FROM_EMBED: frozenset[str] = frozenset({"gov", "member"})


@bot.tree.command(
    name="gov",
    description="Show server members who hold a configured government role.",
)
async def gov(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send(embed=_error_embed("❌ This command can only be used inside a server."))
        return

    guild_id = interaction.guild_id or 0
    config_roles = bot.db.get_gov_roles(guild_id)

    if not any(config_roles.values()):
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No government roles configured yet. An admin can use `/roles setup` to set them up.")
        )
        return

    embed = discord.Embed(
        title="Government",
        color=discord.Color.blurple(),
    )

    guild_roles = {r.id: r for r in guild.roles}

    total = 0
    for key, label in _GOV_DEPT_LABELS.items():
        if key in _GOV_HIDDEN_FROM_EMBED:
            continue
        role_id = config_roles[key]
        if not role_id:
            continue
        role = guild_roles.get(role_id)
        if role is None:
            embed.add_field(
                name=f"{_GOV_DEPT_EMOJI[key]} {label}",
                value="*(role not found)*",
                inline=False,
            )
            continue

        members_with_role = [m for m in role.members if not m.bot]
        total += len(members_with_role)
        if members_with_role:
            value = " ".join(m.mention for m in sorted(members_with_role, key=lambda m: m.display_name.lower()))
        else:
            value = "*(no members)*"

        embed.add_field(
            name=f"{_GOV_DEPT_EMOJI[key]} {label} ({len(members_with_role)})",
            value=value,
            inline=False,
        )

    embed.set_footer(text=f"{total} government member(s) total")
    await interaction.followup.send(embed=embed)


@gov.error
async def gov_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    log.exception("Unhandled error in /gov", exc_info=error)
    embed = _error_embed(f"❌ An unexpected error occurred: {error}")
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# /send
# ---------------------------------------------------------------------------

# Resource keys accepted by Locutus /transfer resources (JSON field names).
_LOCUTUS_RES_KEYS = (
    "money", "food", "coal", "oil", "uranium", "iron",
    "bauxite", "lead", "gasoline", "munitions", "steel", "aluminum",
)

_SUGGESTION_DM_USERNAMES = ("glaernisch", "glaernischtheonly")


async def _send_suggestion_dms(
    bot_client: discord.Client, message: str
) -> tuple[list[str], list[str]]:
    """Attempt to DM configured usernames; return (sent_to, missing)."""
    wanted = {u.lower() for u in _SUGGESTION_DM_USERNAMES}
    found: dict[str, discord.User | discord.Member] = {}

    for guild in bot_client.guilds:
        for member in guild.members:
            for handle in {member.name.lower(), member.display_name.lower(), (member.global_name or "").lower()}:
                if handle in wanted and handle not in found:
                    found[handle] = member

    sent_to: list[str] = []
    for username in _SUGGESTION_DM_USERNAMES:
        user_obj = found.get(username.lower())
        if user_obj is None:
            continue
        try:
            await user_obj.send(message)
            sent_to.append(username)
        except Exception:
            log.exception("Failed to DM %s for /suggestion", username)

    missing = [u for u in _SUGGESTION_DM_USERNAMES if u not in sent_to]
    return sent_to, missing


@bot.tree.command(
    name="send",
    description="Compose a Locutus /transfer resources command to send resources to a nation.",
)
@app_commands.describe(
    receiver="Receiving nation – Discord ping or nation ID.",
    sender="Sender nation name or ID (optional, for record-keeping).",
    bank_note="Bank note attached to the transfer (defaults to #grant).",
    money="Amount of money.",
    food="Amount of food.",
    coal="Amount of coal.",
    oil="Amount of oil.",
    uranium="Amount of uranium.",
    iron="Amount of iron.",
    bauxite="Amount of bauxite.",
    lead="Amount of lead.",
    gasoline="Amount of gasoline.",
    munitions="Amount of munitions.",
    steel="Amount of steel.",
    aluminum="Amount of aluminum.",
)
async def send_resources(
    interaction: discord.Interaction,
    receiver: str,
    sender: str | None = None,
    bank_note: str = "#grant",
    money: float | None = None,
    food: float | None = None,
    coal: float | None = None,
    oil: float | None = None,
    uranium: float | None = None,
    iron: float | None = None,
    bauxite: float | None = None,
    lead: float | None = None,
    gasoline: float | None = None,
    munitions: float | None = None,
    steel: float | None = None,
    aluminum: float | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    # Check that the invoking member holds the configured econ role (admins bypass).
    guild_id = interaction.guild_id or 0
    member = interaction.guild and interaction.guild.get_member(interaction.user.id)
    is_admin = member and member.guild_permissions.administrator
    if not is_admin:
        if not await _check_gov_access(interaction, "econ", "econ_gov"):
            await interaction.followup.send(
                embed=_error_embed("❌ You need the **Economics** role to use this command."),
                ephemeral=True,
            )
            return

    raw: list[tuple[str, float | None]] = [
        ("money", money), ("food", food), ("coal", coal), ("oil", oil),
        ("uranium", uranium), ("iron", iron), ("bauxite", bauxite),
        ("lead", lead), ("gasoline", gasoline), ("munitions", munitions),
        ("steel", steel), ("aluminum", aluminum),
    ]
    resources: dict[str, float] = {
        name: val for name, val in raw if val is not None and val > 0
    }

    if not resources:
        await interaction.followup.send(
            embed=_error_embed("❌ Please provide at least one resource amount greater than zero."),
            ephemeral=True,
        )
        return

    def _fmt_amount(v: float) -> str:
        return str(int(v)) if v == int(v) else str(v)

    # Build the JSON transfer payload: {"money":1000,"food":500,...}
    # Use integer values where possible to keep the string clean.
    transfer_json = "{" + ",".join(
        f'{k}:{_fmt_amount(v)}' for k, v in resources.items()
    ) + "}"

    locutus_cmd = (
        f"/transfer resources receiver:{receiver} "
        f"transfer:{transfer_json} bank_note:{bank_note}"
    )

    embed = discord.Embed(
        title="💸 Resource Transfer Request",
        color=discord.Color.green(),
    )
    if sender:
        embed.add_field(name="From", value=sender, inline=True)
    embed.add_field(name="To", value=receiver, inline=True)
    embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Bank note", value=bank_note, inline=True)

    res_lines = [
        f"**{name.title()}:** {_fmt_amount(val)}" for name, val in resources.items()
    ]
    embed.add_field(name="Resources", value="\n".join(res_lines), inline=False)
    embed.add_field(name="Locutus Command", value=f"```{locutus_cmd}```", inline=False)

    await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="suggestion",
    description="Send a suggestion to the dev.",
)
@app_commands.describe(content="Your suggestion text.")
async def suggestion(interaction: discord.Interaction, content: str) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    body = content.strip()
    if not body:
        await interaction.followup.send(
            embed=_error_embed("❌ Suggestion content cannot be empty."),
            ephemeral=True,
        )
        return

    if len(body) > 1800:
        await interaction.followup.send(
            embed=_error_embed("❌ Suggestion is too long. Please keep it under 1800 characters."),
            ephemeral=True,
        )
        return

    dm_message = (
        "📬 **New /suggestion submission**\n"
        f"From: {interaction.user} (ID: {interaction.user.id})\n"
        f"Guild: {interaction.guild.name if interaction.guild else 'DM/Unknown'}\n"
        f"Content:\n{body}"
    )

    dm_sent_to, dm_missing = await _send_suggestion_dms(bot, dm_message)

    status_lines = [
        (
            f"✅ DMs sent to: {', '.join(f'`{u}`' for u in dm_sent_to)}."
            if dm_sent_to
            else "⚠️ No suggestion DMs were delivered."
        ),
    ]
    if dm_missing:
        status_lines.append(
            "ℹ️ Could not DM: " + ", ".join(f"`{u}`" for u in dm_missing) + "."
        )

    await interaction.followup.send(
        embed=_success_embed("\n".join(status_lines)),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /test  (command group — uses the PnW test API)
# ---------------------------------------------------------------------------

test_group = app_commands.Group(
    name="test",
    description="Test-API equivalents of lookup commands (uses test.politicsandwar.com).",
)
bot.tree.add_command(test_group)


@test_group.command(
    name="whois",
    description="Look up a PnW nation via the TEST API by ID, name, or @mention.",
)
@app_commands.describe(
    query="A nation ID, an @mention, a nation name, or a Discord username."
)
async def test_whois(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_whois(interaction, bot.pnw_test, query, base_url=_PNW_TEST_BASE_URL)


test_alliance_group = app_commands.Group(
    name="alliance",
    description="Test-API equivalents of alliance commands.",
)
test_group.add_command(test_alliance_group)


@test_alliance_group.command(
    name="info",
    description="Look up a PnW alliance via the TEST API by ID, name, or @mention.",
)
@app_commands.describe(query="Alliance ID, alliance name, or a @mention.")
async def test_alliance_find(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_alliance_find(interaction, bot.pnw_test, query, base_url=_PNW_TEST_BASE_URL)


# ---------------------------------------------------------------------------
# /setup  (command group)
# ---------------------------------------------------------------------------

setup_group = app_commands.Group(
    name="setup",
    description="Bot setup commands (admin only).",
)
bot.tree.add_command(setup_group)


@setup_group.command(
    name="grant_channel",
    description="Set the channel where /request grant posts are sent (admin, econ, or IA only).",
)
@app_commands.describe(channel="The text channel that will receive grant requests.")
async def setup_grant_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    if not await _check_gov_access(interaction, "econ", "econ_gov", "ia", "ia_asst"):
        await interaction.followup.send(
            embed=_error_embed(
                "❌ You need the **Administrator**, **Economics**, or **Internal Affairs** role to use this command."
            ),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    bot.db.set_grant_channel(guild_id, channel.id)
    log.info("Guild %d: grant channel set to #%s (%d) by %s", guild_id, channel.name, channel.id, interaction.user)
    await interaction.followup.send(
        embed=_success_embed(f"✅ Grant requests will now be posted in {channel.mention}."),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /request  (command group)
# ---------------------------------------------------------------------------

request_group = app_commands.Group(
    name="request",
    description="Submit requests to the government.",
)
bot.tree.add_command(request_group)


@request_group.command(
    name="grant",
    description="Request a grant from the Economics team.",
)
@app_commands.describe(
    note="Reason / bank note for this grant (# will be prepended automatically).",
    money="Amount of money.",
    food="Amount of food.",
    coal="Amount of coal.",
    oil="Amount of oil.",
    uranium="Amount of uranium.",
    iron="Amount of iron.",
    bauxite="Amount of bauxite.",
    lead="Amount of lead.",
    gasoline="Amount of gasoline.",
    munitions="Amount of munitions.",
    steel="Amount of steel.",
    aluminum="Amount of aluminum.",
)
async def request_grant(
    interaction: discord.Interaction,
    note: str,
    money: float | None = None,
    food: float | None = None,
    coal: float | None = None,
    oil: float | None = None,
    uranium: float | None = None,
    iron: float | None = None,
    bauxite: float | None = None,
    lead: float | None = None,
    gasoline: float | None = None,
    munitions: float | None = None,
    steel: float | None = None,
    aluminum: float | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0

    # Check that both the grant channel and the econ role are configured.
    grant_channel_id = bot.db.get_grant_channel(guild_id)
    gov_roles = bot.db.get_gov_roles(guild_id)
    econ_gov_role_id = gov_roles.get("econ_gov")
    econ_role_id = gov_roles.get("econ")

    missing: list[str] = []
    if not grant_channel_id:
        missing.append("grant channel (`/setup grant_channel`)")
    if not econ_role_id:
        missing.append("Economics role (`/roles setup`)")

    if missing:
        await interaction.followup.send(
            embed=_error_embed(
                "❌ Cannot submit grant request — the following have not been configured:\n"
                + "\n".join(f"• {m}" for m in missing)
            ),
            ephemeral=True,
        )
        return

    # Resolve the grant channel and econ role objects.
    guild = interaction.guild
    grant_channel = guild and guild.get_channel(grant_channel_id)
    if grant_channel is None or not isinstance(grant_channel, discord.TextChannel):
        await interaction.followup.send(
            embed=_error_embed("❌ The configured grant channel no longer exists. An admin must re-run `/setup grant_channel`."),
            ephemeral=True,
        )
        return

    raw: list[tuple[str, float | None]] = [
        ("money", money), ("food", food), ("coal", coal), ("oil", oil),
        ("uranium", uranium), ("iron", iron), ("bauxite", bauxite),
        ("lead", lead), ("gasoline", gasoline), ("munitions", munitions),
        ("steel", steel), ("aluminum", aluminum),
    ]
    resources: dict[str, float] = {
        name: val for name, val in raw if val is not None and val > 0
    }

    if not resources:
        await interaction.followup.send(
            embed=_error_embed("❌ Please provide at least one resource amount greater than zero."),
            ephemeral=True,
        )
        return

    def _fmt_amount(v: float) -> str:
        return str(int(v)) if v == int(v) else str(v)

    # Build the bank note: prepend # if not already present.
    bank_note = note if note.startswith("#") else f"#{note}"

    # Determine receiver for the Locutus command: use registered nation ID if available.
    reg = bot.db.get_by_discord_id(interaction.user.id)
    receiver = str(reg["nation_id"]) if reg else interaction.user.mention

    transfer_json = "{" + ",".join(
        f"{k}:{_fmt_amount(v)}" for k, v in resources.items()
    ) + "}"
    locutus_cmd = (
        f"/transfer resources receiver:{receiver} "
        f"transfer:{transfer_json} bank_note:{bank_note}"
    )

    embed = discord.Embed(
        title="📋 Grant Request",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Receiver", value=receiver, inline=True)
    embed.add_field(name="Note", value=note, inline=False)

    res_lines = [
        f"**{name.title()}:** {_fmt_amount(val)}" for name, val in resources.items()
    ]
    embed.add_field(name="Resources", value="\n".join(res_lines), inline=False)
    embed.add_field(name="Locutus Command", value=f"```{locutus_cmd}```", inline=False)

    ping_role_id = econ_gov_role_id if econ_gov_role_id else econ_role_id
    econ_mention = f"<@&{ping_role_id}>"
    await grant_channel.send(content=econ_mention, embed=embed)
    log.info(
        "Guild %d: grant request posted by %s to #%s",
        guild_id, interaction.user, grant_channel.name,
    )

    await interaction.followup.send(
        embed=_success_embed(f"✅ Your grant request has been posted in {grant_channel.mention}."),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /admin  (command group)
# ---------------------------------------------------------------------------

admin_group = app_commands.Group(
    name="admin",
    description="Bot administration commands (admin only).",
)
bot.tree.add_command(admin_group)

admin_alliance_group = app_commands.Group(
    name="alliance",
    description="Configure the guild's primary alliance.",
)
admin_group.add_command(admin_alliance_group)


@admin_alliance_group.command(
    name="set",
    description="Set the primary alliance ID for this guild (admin only).",
)
@app_commands.describe(alliance_id="The Politics and War alliance ID to associate with this guild.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_alliance_set(interaction: discord.Interaction, alliance_id: int) -> None:
    await interaction.response.defer(ephemeral=True)
    if alliance_id <= 0:
        await interaction.followup.send(
            "❌ Please provide a positive integer for the alliance ID.",
            ephemeral=True,
        )
        return
    guild_id = interaction.guild_id or 0
    bot.db.set_alliance_id(guild_id, alliance_id)
    log.info("Guild %d: primary alliance set to %d by %s", guild_id, alliance_id, interaction.user)
    await interaction.followup.send(
        f"✅ Primary alliance set to **{alliance_id}**.",
        ephemeral=True,
    )


@admin_alliance_set.error
async def admin_alliance_set_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


@admin_alliance_group.command(
    name="show",
    description="Show the primary alliance ID configured for this guild.",
)
async def admin_alliance_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id or 0
    alliance_id = bot.db.get_alliance_id(guild_id)
    if alliance_id is None:
        await interaction.followup.send(
            "ℹ️ No primary alliance configured. An admin can use `/admin alliance set` to set one.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"ℹ️ Primary alliance ID: **{alliance_id}**",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /admin api_key  (subgroup)
# ---------------------------------------------------------------------------

admin_api_key_group = app_commands.Group(
    name="api_key",
    description="Manage the PnW API key used by this bot.",
)
admin_group.add_command(admin_api_key_group)


@admin_api_key_group.command(
    name="set",
    description="Override the PnW API key used by this bot (admin only).",
)
@app_commands.describe(api_key="The new Politics and War API key.")
@app_commands.checks.has_permissions(administrator=True)
async def admin_api_key_set(interaction: discord.Interaction, api_key: str) -> None:
    await interaction.response.defer(ephemeral=True)
    bot.db.set_pnw_api_key(api_key)
    bot.pnw._api_key = api_key
    log.info("PnW API key overridden by %s", interaction.user)
    await interaction.followup.send(
        "✅ PnW API key updated successfully.",
        ephemeral=True,
    )


@admin_api_key_set.error
async def admin_api_key_set_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


# ---------------------------------------------------------------------------
# /admin clear_guild_commands
# ---------------------------------------------------------------------------


@admin_group.command(
    name="clear_guild_commands",
    description="Clear all guild-scoped slash commands to remove duplicates (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def admin_clear_guild_commands(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command must be used inside a server.", ephemeral=True)
        return
    bot.tree.clear_commands(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info("Guild %d: guild-scoped slash commands cleared by %s", guild.id, interaction.user)
    await interaction.followup.send(
        "✅ Guild-specific slash commands cleared. Duplicates should be gone within a few seconds.",
        ephemeral=True,
    )


@admin_clear_guild_commands.error
async def admin_clear_guild_commands_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


# ---------------------------------------------------------------------------
# /admin sync
# ---------------------------------------------------------------------------


@admin_group.command(
    name="sync",
    description="Copy global commands to this server for instant propagation (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def admin_sync(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command must be used inside a server.", ephemeral=True)
        return
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    log.info("Guild %d: commands synced to guild by %s", guild.id, interaction.user)
    await interaction.followup.send(
        "✅ Commands synced to this server. New commands should appear within seconds.",
        ephemeral=True,
    )


@admin_sync.error
async def admin_sync_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


# ---------------------------------------------------------------------------
# /color check
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="color",
    description="Check whether alliance members are on the correct color.",
)
async def color_check(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    alliance_id = bot.db.get_alliance_id(guild_id)
    if alliance_id is None:
        await interaction.followup.send(
            embed=_info_embed(
                "ℹ️ No primary alliance configured. An admin can use `/admin alliance set` to set one."
            )
        )
        return

    # Fetch the alliance to get its expected color
    try:
        alliance_info = await bot.pnw.get_alliance_by_id(alliance_id)
    except Exception as exc:
        log.exception("PnW API error while fetching alliance info for /color check")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if alliance_info is None:
        await interaction.followup.send(
            embed=_error_embed(f"❌ Alliance **{alliance_id}** not found on Politics and War.")
        )
        return

    expected_color = alliance_info.color.strip().lower()

    # Fetch members
    try:
        members = await bot.pnw.get_alliance_members([alliance_id])
    except Exception as exc:
        log.exception("PnW API error while fetching alliance members for /color check")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if not members:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No active members found for the configured alliance.")
        )
        return

    # Find members not on the correct color (skip beige — it's involuntary)
    wrong_color: list[tuple[Nation, str]] = []
    for nation in members:
        nation_color = nation.color.strip().lower()
        if nation_color == "beige":
            continue
        if nation_color != expected_color:
            wrong_color.append((nation, nation_color))

    if not wrong_color:
        embed = discord.Embed(
            title="✅ Color Check",
            description=(
                f"All active members of **{alliance_info.name}** are on the correct color "
                f"(**{expected_color.title()}**)."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"{len(members)} members checked")
        await interaction.followup.send(embed=embed)
        return

    lines = [
        f"[{nation.nation_name}]({_nation_url(nation.nation_id)}) — "
        f"🎨 **{nation_color.title()}** (expected **{expected_color.title()}**)"
        for nation, nation_color in wrong_color
    ]
    embed = discord.Embed(
        title=f"⚠️ Color Check — {alliance_info.name}",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    embed.set_footer(
        text=(
            f"{len(wrong_color)} member(s) on wrong color · "
            f"{len(members)} total checked · expected: {expected_color.title()}"
        )
    )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /damage  (command group)
# ---------------------------------------------------------------------------

_DAMAGE_LOOKBACK_DAYS = 7
_LEADERBOARD_PAGE_SIZE = 10

# Sort-mode keys and their display labels (button text).
_LEADERBOARD_SORT_LABELS: dict[str, str] = {
    "loot":      "💰 Loot",
    "dmg_city":  "💥 /City",
    "infra":     "🏗️ Infra",
    "res_dmg":   "💥 Res Dmg",
}


def _fmt_k(val: float) -> str:
    """Compact dollar formatter: $1.2M / $500K / $42."""
    if abs(val) >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    if abs(val) >= 10_000:
        return f"${val / 1_000:.0f}K"
    return f"${val:,.0f}"


def _lb_stat(val: float, emoji: str, cities: int, active: bool) -> str:
    """Format one leaderboard stat cell, bolding it when it is the active sort."""
    pc = f" ({_fmt_k(val / cities)}/c)" if cities > 0 else ""
    text = f"{emoji} {_fmt_k(val)}{pc}"
    return f"**{text}**" if active else text


def _build_leaderboard_page(
    sorted_nations: list[tuple[int, dict]],
    prices: "TradePrice",
    page: int,
    sort_mode: str,
) -> discord.Embed:
    start = page * _LEADERBOARD_PAGE_SIZE
    page_slice = sorted_nations[start : start + _LEADERBOARD_PAGE_SIZE]
    total_pages = max(
        1, (len(sorted_nations) + _LEADERBOARD_PAGE_SIZE - 1) // _LEADERBOARD_PAGE_SIZE
    )

    lines: list[str] = []
    for i, (nation_id, s) in enumerate(page_slice):
        rank = start + i + 1
        cities = s["num_cities"]
        infra = s["infra_value"]
        res_dmg = prices.resource_value(
            gasoline=s["def_gas_used"] + s["gas_looted"],
            munitions=s["def_mun_used"] + s["mun_looted"],
            aluminum=s["def_alum_used"] + s["alum_looted"],
            steel=s["def_steel_used"] + s["steel_looted"],
        ) + prices.unit_kill_value(
            soldiers=s["def_soldiers_killed"],
            tanks=s["def_tanks_killed"],
            aircraft=s["def_aircraft_killed"],
            ships=s["def_ships_sunk"],
        )
        loot = s["money_looted"] + prices.resource_value(
            gasoline=s["gas_looted"],
            munitions=s["mun_looted"],
            aluminum=s["alum_looted"],
            steel=s["steel_looted"],
        )
        city_str = f" · {cities}🏙️" if cities > 0 else ""
        stats = "  ".join([
            _lb_stat(infra,   "🏗️", cities, sort_mode in ("infra", "dmg_city")),
            _lb_stat(res_dmg, "💥", cities, sort_mode in ("res_dmg", "dmg_city")),
            _lb_stat(loot,    "💰", cities, sort_mode == "loot"),
        ])
        lines.append(
            f"**{rank}.** [{s['nation_name']}]({_nation_url(nation_id)}){city_str}\n{stats}"
        )

    sort_label = _LEADERBOARD_SORT_LABELS.get(sort_mode, sort_mode)
    footer_parts: list[str] = [f"Sorted: {sort_label}"]
    if total_pages > 1:
        footer_parts.append(f"Page {page + 1}/{total_pages}")
    footer_parts.append(f"{len(sorted_nations)} members")
    footer_parts.append("🏗️ infra  💥 res dmg (enemy res used + unit kills + loot @ mkt)  💰 loot (money + looted res @ mkt)  (/c = per city)")

    embed = discord.Embed(
        title=f"⚔️ War Leaderboard — Past {_DAMAGE_LOOKBACK_DAYS} Days",
        description="\n\n".join(lines) if lines else "*No data.*",
        color=discord.Color.gold(),
    )
    embed.set_footer(text="  ·  ".join(footer_parts))
    return embed


damage_group = app_commands.Group(
    name="damage",
    description="Damage statistics commands.",
)
bot.tree.add_command(damage_group)


class LeaderboardView(discord.ui.View):
    """Sort and pagination buttons for /damage leaderboard."""

    def __init__(
        self,
        all_nations: list[tuple[int, dict]],
        prices: "TradePrice",
        sort_mode: str = "loot",
        page: int = 0,
    ) -> None:
        super().__init__(timeout=600)
        self._all = all_nations
        self._prices = prices
        self.sort_mode = sort_mode
        self.page = page
        self._sorted: list[tuple[int, dict]] = []
        self._resort()
        self._refresh_buttons()

    # ---- sorting helpers ----

    def _loot(self, s: dict) -> float:
        return s["money_looted"] + self._prices.resource_value(
            gasoline=s["gas_looted"],
            munitions=s["mun_looted"],
            aluminum=s["alum_looted"],
            steel=s["steel_looted"],
        )

    def _res_dmg(self, s: dict) -> float:
        return self._prices.resource_value(
            gasoline=s["def_gas_used"] + s["gas_looted"],
            munitions=s["def_mun_used"] + s["mun_looted"],
            aluminum=s["def_alum_used"] + s["alum_looted"],
            steel=s["def_steel_used"] + s["steel_looted"],
        ) + self._prices.unit_kill_value(
            soldiers=s["def_soldiers_killed"],
            tanks=s["def_tanks_killed"],
            aircraft=s["def_aircraft_killed"],
            ships=s["def_ships_sunk"],
        )

    def _sort_key(self, item: tuple[int, dict]) -> float:
        s = item[1]
        cities = max(s["num_cities"], 1)
        if self.sort_mode == "loot":
            return self._loot(s)
        if self.sort_mode == "dmg_city":
            return (s["infra_value"] + self._res_dmg(s)) / cities
        if self.sort_mode == "infra":
            return s["infra_value"]
        if self.sort_mode == "res_dmg":
            return self._res_dmg(s)
        return 0.0

    def _resort(self) -> None:
        self._sorted = sorted(self._all, key=self._sort_key, reverse=True)

    @property
    def _total_pages(self) -> int:
        return max(
            1, (len(self._sorted) + _LEADERBOARD_PAGE_SIZE - 1) // _LEADERBOARD_PAGE_SIZE
        )

    # ---- button management ----

    def _refresh_buttons(self) -> None:
        self.clear_items()
        # Row 0: sort buttons (one per sort mode, active = primary style)
        for mode, label in _LEADERBOARD_SORT_LABELS.items():
            btn = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if mode == self.sort_mode
                    else discord.ButtonStyle.secondary
                ),
                custom_id=f"lb_sort_{mode}",
                row=0,
            )
            btn.callback = self._make_sort_cb(mode)
            self.add_item(btn)
        # Row 1: prev / next (only when there is more than one page)
        if self._total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀",
                style=discord.ButtonStyle.secondary,
                custom_id="lb_prev",
                disabled=self.page <= 0,
                row=1,
            )
            prev_btn.callback = self._prev_cb
            self.add_item(prev_btn)

            next_btn = discord.ui.Button(
                label="▶",
                style=discord.ButtonStyle.secondary,
                custom_id="lb_next",
                disabled=self.page >= self._total_pages - 1,
                row=1,
            )
            next_btn.callback = self._next_cb
            self.add_item(next_btn)

    def _make_sort_cb(self, mode: str):
        async def _cb(interaction: discord.Interaction) -> None:
            self.sort_mode = mode
            self.page = 0
            self._resort()
            self._refresh_buttons()
            await interaction.response.edit_message(
                embed=_build_leaderboard_page(
                    self._sorted, self._prices, self.page, self.sort_mode
                ),
                view=self,
            )
        return _cb

    async def _prev_cb(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=_build_leaderboard_page(
                self._sorted, self._prices, self.page, self.sort_mode
            ),
            view=self,
        )

    async def _next_cb(self, interaction: discord.Interaction) -> None:
        self.page = min(self._total_pages - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=_build_leaderboard_page(
                self._sorted, self._prices, self.page, self.sort_mode
            ),
            view=self,
        )


@damage_group.command(
    name="leaderboard",
    description="Show loot and damage dealt by each member of the configured alliance in the past week.",
)
async def damage_command(
    interaction: discord.Interaction,
) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    alliance_id = bot.db.get_alliance_id(guild_id)
    if alliance_id is None:
        await interaction.followup.send(
            embed=_info_embed(
                "ℹ️ No primary alliance configured. An admin can use `/admin alliance set` to set one."
            )
        )
        return

    from datetime import datetime, timedelta, timezone

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=_DAMAGE_LOOKBACK_DAYS)

    damage_task = asyncio.ensure_future(bot.pnw.get_alliance_damage(alliance_id, cutoff))
    prices_task = asyncio.ensure_future(bot.pnw.get_trade_prices())
    members_task = asyncio.ensure_future(bot.pnw.get_alliance_members([alliance_id]))

    try:
        damage_map = await damage_task
    except Exception as exc:
        log.exception("PnW API error while fetching alliance damage")
        prices_task.cancel()
        members_task.cancel()
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    try:
        prices = await prices_task
    except Exception:
        log.exception("PnW API error while fetching trade prices")
        prices = TradePrice()

    try:
        all_members = await members_task
    except Exception:
        log.exception("PnW API error while fetching alliance members")
        all_members = []

    # Include all current alliance members even if they have no wars.
    # Members with no activity appear with zero stats and sort to the bottom.
    for member in all_members:
        if member.nation_id not in damage_map:
            damage_map[member.nation_id] = {
                "nation_name": member.nation_name,
                "num_cities": member.num_cities,
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
                "def_soldiers_killed": 0.0,
                "def_tanks_killed": 0.0,
                "def_aircraft_killed": 0.0,
                "def_ships_sunk": 0.0,
            }
        else:
            # Update city count to current value from the member query.
            if member.num_cities > damage_map[member.nation_id]["num_cities"]:
                damage_map[member.nation_id]["num_cities"] = member.num_cities

    if not damage_map:
        await interaction.followup.send(
            embed=_info_embed(
                "ℹ️ No members found for the configured alliance."
            )
        )
        return

    view = LeaderboardView(list(damage_map.items()), prices)
    embed = _build_leaderboard_page(view._sorted, prices, 0, view.sort_mode)
    await interaction.followup.send(embed=embed, view=view)



# ---------------------------------------------------------------------------
# /spy target find
# ---------------------------------------------------------------------------

_SPY_TARGETS_PAGE_SIZE = 15


def _build_spy_targets_page(
    members: list[Nation],
    title: str,
    multi_alliance: bool,
    page: int,
) -> discord.Embed:
    total = len(members)
    total_pages = max(1, (total + _SPY_TARGETS_PAGE_SIZE - 1) // _SPY_TARGETS_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _SPY_TARGETS_PAGE_SIZE
    page_members = members[start : start + _SPY_TARGETS_PAGE_SIZE]

    lines: list[str] = []
    for i, nation in enumerate(page_members, start=start + 1):
        beige = " 🔵" if nation.beige_turns > 0 else ""
        alliance_tag = (
            f" [{nation.alliance_name}]" if multi_alliance and nation.alliance_name else ""
        )
        spy_str = f" | 🕵️ {nation.spies}" if nation.spies >= 0 else " | 🕵️ ?"
        line = (
            f"`{i:>3}.` [{nation.nation_name}]({_nation_url(nation.nation_id)})"
            f"{beige}{alliance_tag}"
            f" — 🏙️ {nation.num_cities} | ⭐ {nation.score:,.0f}{spy_str}"
        )
        lines.append(line)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else "*(no targets found)*",
        color=discord.Color.dark_grey(),
    )
    footer = f"Page {page + 1}/{total_pages} · {total} nations · sorted by cities desc · 🔵 = beiged"
    embed.set_footer(text=footer)
    return embed


class SpyTargetView(discord.ui.View):
    """◀/▶ pagination buttons for /spy target find."""

    def __init__(
        self,
        members: list[Nation],
        title: str,
        multi_alliance: bool,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=600)
        self._members = members
        self._title = title
        self._multi_alliance = multi_alliance
        self.page = page
        self._refresh_buttons()

    def _total_pages(self) -> int:
        return max(
            1, (len(self._members) + _SPY_TARGETS_PAGE_SIZE - 1) // _SPY_TARGETS_PAGE_SIZE
        )

    def _refresh_buttons(self) -> None:
        self.prev_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self._total_pages() - 1

    async def _update(self, interaction: discord.Interaction) -> None:
        self._refresh_buttons()
        embed = _build_spy_targets_page(
            self._members, self._title, self._multi_alliance, self.page
        )
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        await self._update(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = min(self._total_pages() - 1, self.page + 1)
        await self._update(interaction)


spy_group = app_commands.Group(
    name="spy",
    description="Spy-related commands.",
)
bot.tree.add_command(spy_group)

spy_target_group = app_commands.Group(
    name="target",
    description="Find nations to target with spy operations.",
)
spy_group.add_command(spy_target_group)


@spy_target_group.command(
    name="find",
    description="Find nations in the given alliances sorted by spy capacity (cities).",
)
@app_commands.describe(
    alliances="Comma-separated alliance names or IDs to search (e.g. Rose, Camelot)."
)
async def spy_target_find(interaction: discord.Interaction, alliances: str) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    names = [n.strip() for n in alliances.split(",") if n.strip()]
    if not names:
        await interaction.followup.send(
            embed=_error_embed("Please provide at least one alliance name or ID."),
            ephemeral=True,
        )
        return

    alliance_ids: list[int] = []
    alliance_names: list[str] = []
    not_found: list[str] = []

    for name in names:
        if name.isdigit():
            info = await bot.pnw.get_alliance_by_id(int(name))
        else:
            info = await bot.pnw.get_alliance_by_name(name)
        if info is None:
            not_found.append(name)
        elif info.alliance_id not in alliance_ids:
            alliance_ids.append(info.alliance_id)
            alliance_names.append(info.name)

    if not_found:
        plural = "s" if len(not_found) > 1 else ""
        missing = ", ".join(f"**{n}**" for n in not_found)
        await interaction.followup.send(
            embed=_error_embed(f"Alliance{plural} not found: {missing}"),
            ephemeral=True,
        )
        return

    members = await bot.pnw.get_alliance_members(alliance_ids)
    members = [
        m for m in members
        if m.alliance_position not in ("APPLICANT", "NOALLIANCE", "")
    ]
    members.sort(key=lambda m: m.num_cities, reverse=True)

    if not members:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No active members found in the given alliances."),
            ephemeral=True,
        )
        return

    multi_alliance = len(alliance_ids) > 1
    title = f"🕵️ Spy Targets — {', '.join(alliance_names)}"
    embed = _build_spy_targets_page(members, title, multi_alliance, 0)
    view = SpyTargetView(members, title, multi_alliance)
    await interaction.followup.send(embed=embed, view=view)


# ---------------------------------------------------------------------------
# /missile targets find
# ---------------------------------------------------------------------------

_MISSILE_TOP_N = 20
_PNW_MAX_DEF_WARS = 3


def _estimate_avg_infra(nation: pnw_api.Nation) -> float:
    """Estimate average infrastructure per city from the nation's score.

    Uses the same formula as /whois:
        score = (cities-1)*100 + 10 + projects*20 + total_infra/40 + military_score
        => avg_infra = (score - (cities-1)*100 - 10 - projects*20 - military_score) * 40 / cities
    """
    if nation.num_cities <= 0:
        return 0.0
    military_score = (
        nation.soldiers * 0.0004
        + nation.tanks * 0.025
        + nation.aircraft * 0.3
        + nation.ships * 1.0
        + nation.missiles * 5.0
        + nation.nukes * 15.0
    )
    infra_score = (
        nation.score
        - (nation.num_cities - 1) * 100
        - 10
        - nation.num_projects * 20
        - military_score
    )
    return max(0.0, infra_score * 40 / nation.num_cities)


def _build_missile_targets_embed(
    nations: list[tuple[pnw_api.Nation, int, float]],
    alliance_names: list[str],
) -> discord.Embed:
    """Build the missile-targets embed.

    *nations* is a list of (Nation, active_defensive_wars, avg_infra) tuples,
    already sorted and limited to the top N.
    """
    title = "🚀 Missile Targets — " + ", ".join(alliance_names)
    lines: list[str] = []
    for i, (nation, def_wars, avg_infra) in enumerate(nations, start=1):
        beige = " 🔵" if nation.beige_turns > 0 else ""
        infra_str = f" | 🏗️ {avg_infra:,.0f} avg infra" if avg_infra > 0 else ""
        line = (
            f"`{i:>3}.` [{nation.nation_name}]({_nation_url(nation.nation_id)})"
            f"{beige}"
            f" — 🏙️ {nation.num_cities}"
            f"{infra_str}"
            f" | 🛡️ {def_wars}/{_PNW_MAX_DEF_WARS} def"
        )
        lines.append(line)

    embed = discord.Embed(
        title=title,
        description="\n".join(lines) if lines else "*(no targets found)*",
        color=discord.Color.red(),
    )
    embed.set_footer(
        text=f"Top {len(nations)} · sorted by avg infra desc · open def slots only · 🔵 = beiged"
    )
    return embed


missile_group = app_commands.Group(
    name="missile",
    description="Missile-related commands.",
)
bot.tree.add_command(missile_group)

missile_targets_group = app_commands.Group(
    name="targets",
    description="Find nations to target with missile strikes.",
)
missile_group.add_command(missile_targets_group)


@missile_targets_group.command(
    name="find",
    description="Top 20 nations in the /slots alliances with the most cities that have open defensive slots.",
)
async def missile_target_find(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    guild_id = interaction.guild_id or 0
    alliance_ids = bot.db.get_slots_alliances(guild_id)

    if not alliance_ids:
        await interaction.followup.send(
            embed=_info_embed(
                "ℹ️ No alliances configured. An admin can use `/config slots set` to set them up."
            )
        )
        return

    try:
        members, war_counts = await asyncio.gather(
            bot.pnw.get_alliance_members(alliance_ids),
            bot.pnw.get_active_def_war_counts_by_alliance(alliance_ids),
        )
    except Exception as exc:
        log.exception("PnW API error while fetching data for missile targets")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if not members:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ No active members found for the configured alliance(s).")
        )
        return

    # Keep only nations that have at least one open defensive slot
    open_slot_nations = [
        (n, war_counts.get(n.nation_id, 0))
        for n in members
        if war_counts.get(n.nation_id, 0) < _PNW_MAX_DEF_WARS
    ]

    if not open_slot_nations:
        await interaction.followup.send(
            embed=_info_embed("ℹ️ All nations in the configured alliances currently have full defensive slots.")
        )
        return

    # Fetch avg infra per city for the open-slot nations using the score formula
    # (same as /whois — no extra API call needed)
    avg_infra_map: dict[int, float] = {
        n.nation_id: _estimate_avg_infra(n) for n, _ in open_slot_nations
    }

    # Sort by avg infra descending, fall back to city count for ties
    open_slot_nations.sort(
        key=lambda t: (avg_infra_map.get(t[0].nation_id, 0.0), t[0].num_cities),
        reverse=True,
    )
    top_nations_raw = open_slot_nations[:_MISSILE_TOP_N]
    top_nations = [
        (n, def_wars, avg_infra_map.get(n.nation_id, 0.0))
        for n, def_wars in top_nations_raw
    ]

    # Collect distinct alliance names for the embed title
    seen: set[int] = set()
    alliance_names: list[str] = []
    for n, _, _ in top_nations:
        if n.alliance_id not in seen:
            seen.add(n.alliance_id)
            if n.alliance_name:
                alliance_names.append(n.alliance_name)
    if not alliance_names:
        alliance_names = [str(aid) for aid in alliance_ids]

    embed = _build_missile_targets_embed(top_nations, alliance_names)
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /infra cost
# ---------------------------------------------------------------------------

# Project discount rates for infrastructure purchase cost.
_INFRA_DISCOUNT_UP = 0.05   # Urban Planning
_INFRA_DISCOUNT_ACP = 0.10  # Advanced Urban Planning (stacks with UP)


@bot.tree.command(
    name="infra",
    description="Calculate the cost to buy infrastructure.",
)
@app_commands.describe(
    current_infra="Current infrastructure level per city.",
    target_infra="Target infrastructure level per city.",
    cities="Number of cities to calculate total cost across (default: 1).",
    urban_planning="Does the nation have Urban Planning? (−5% cost)",
    advanced_urban_planning="Does the nation have Advanced Urban Planning? (−10% cost, stacks with UP)",
)
async def infra_cost_command(
    interaction: discord.Interaction,
    current_infra: float,
    target_infra: float,
    cities: int = 1,
    urban_planning: bool = False,
    advanced_urban_planning: bool = False,
) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    if cities < 1:
        await interaction.followup.send(
            embed=_error_embed("❌ Number of cities must be at least 1.")
        )
        return

    if target_infra <= current_infra:
        await interaction.followup.send(
            embed=_error_embed("❌ Target infrastructure must be greater than current infrastructure.")
        )
        return

    if current_infra < 0 or target_infra > 100_000:
        await interaction.followup.send(
            embed=_error_embed("❌ Infrastructure values must be between 0 and 100,000.")
        )
        return

    base_cost_per_city = calculate_infra_cost(current_infra, target_infra)

    # Apply project discounts (additive).
    discount = 0.0
    discount_parts: list[str] = []
    if urban_planning:
        discount += _INFRA_DISCOUNT_UP
        discount_parts.append(f"Urban Planning (−{_INFRA_DISCOUNT_UP * 100:.0f}%)")
    if advanced_urban_planning:
        discount += _INFRA_DISCOUNT_ACP
        discount_parts.append(f"Advanced Urban Planning (−{_INFRA_DISCOUNT_ACP * 100:.0f}%)")

    discounted_cost_per_city = base_cost_per_city * (1.0 - discount)
    total_cost = discounted_cost_per_city * cities

    embed = discord.Embed(
        title="🏗️ Infrastructure Cost Calculator",
        color=discord.Color.blue(),
    )
    embed.add_field(name="From", value=f"{current_infra:,.2f}", inline=True)
    embed.add_field(name="To", value=f"{target_infra:,.2f}", inline=True)
    embed.add_field(name="Amount", value=f"+{target_infra - current_infra:,.2f}", inline=True)
    embed.add_field(name="Cost per City", value=f"${discounted_cost_per_city:,.0f}", inline=True)
    embed.add_field(name="Cities", value=str(cities), inline=True)
    embed.add_field(name="Total Cost", value=f"**${total_cost:,.0f}**", inline=True)

    if discount_parts:
        embed.add_field(
            name=f"Discounts (−{discount * 100:.0f}% total)",
            value="\n".join(discount_parts),
            inline=False,
        )
    else:
        embed.add_field(name="Discounts", value="None", inline=False)

    if discount > 0:
        total_savings = (base_cost_per_city - discounted_cost_per_city) * cities
        embed.set_footer(
            text=f"Base cost per city: ${base_cost_per_city:,.0f}  ·  Savings: ${total_savings:,.0f}"
        )
    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /war range targets
# ---------------------------------------------------------------------------

_WAR_RANGE_PAGE_SIZE = 15


def _build_war_range_embed(
    nation: pnw_api.Nation,
    targets: list[tuple[pnw_api.Nation, int]],
    page: int,
    multi_alliance: bool,
) -> discord.Embed:
    """Build the war-range targets embed."""
    min_score = nation.score * WAR_RANGE_MIN_RATIO
    max_score = nation.score * WAR_RANGE_MAX_RATIO

    total = len(targets)
    total_pages = max(1, (total + _WAR_RANGE_PAGE_SIZE - 1) // _WAR_RANGE_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _WAR_RANGE_PAGE_SIZE
    page_slice = targets[start : start + _WAR_RANGE_PAGE_SIZE]

    lines: list[str] = []
    for i, (t, def_wars) in enumerate(page_slice, start=start + 1):
        beige = " 🔵" if t.beige_turns > 0 else ""
        alliance_tag = (
            f" [{t.alliance_name}]" if multi_alliance and t.alliance_name else ""
        )
        open_slots = MAX_DEFENSIVE_SLOTS - def_wars
        line = (
            f"`{i:>3}.` [{t.nation_name}]({_nation_url(t.nation_id)})"
            f"{beige}{alliance_tag}"
            f" — 🏙️ {t.num_cities} · 📊 {t.score:,.0f}"
            f" | 🛡️ {open_slots}/{MAX_DEFENSIVE_SLOTS} slots"
        )
        lines.append(line)

    embed = discord.Embed(
        title=f"⚔️ War Range Targets for {nation.nation_name}",
        description="\n".join(lines) if lines else "*(no targets found)*",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Your Score", value=f"{nation.score:,.2f}", inline=True)
    embed.add_field(name="Min Target Score", value=f"{min_score:,.2f}", inline=True)
    embed.add_field(name="Max Target Score", value=f"{max_score:,.2f}", inline=True)

    footer_parts: list[str] = [f"{total} target(s) in range"]
    if total_pages > 1:
        footer_parts.append(f"Page {page + 1}/{total_pages}")
    footer_parts.append("open def slots only · 🔵 = beiged")
    embed.set_footer(text="  ·  ".join(footer_parts))
    return embed


class WarRangeView(discord.ui.View):
    """Pagination buttons for /war range targets."""

    def __init__(
        self,
        nation: pnw_api.Nation,
        targets: list[tuple[pnw_api.Nation, int]],
        multi_alliance: bool,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=300)
        self._nation = nation
        self._targets = targets
        self._multi_alliance = multi_alliance
        self.page = page
        self._refresh_buttons()

    @property
    def _total_pages(self) -> int:
        return max(
            1,
            (len(self._targets) + _WAR_RANGE_PAGE_SIZE - 1) // _WAR_RANGE_PAGE_SIZE,
        )

    def _refresh_buttons(self) -> None:
        self.clear_items()
        if self._total_pages <= 1:
            return
        prev_btn = discord.ui.Button(
            label="◀",
            style=discord.ButtonStyle.secondary,
            custom_id="wr_prev",
            disabled=self.page <= 0,
            row=0,
        )
        prev_btn.callback = self._prev_cb
        self.add_item(prev_btn)

        next_btn = discord.ui.Button(
            label="▶",
            style=discord.ButtonStyle.secondary,
            custom_id="wr_next",
            disabled=self.page >= self._total_pages - 1,
            row=0,
        )
        next_btn.callback = self._next_cb
        self.add_item(next_btn)

    async def _prev_cb(self, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=_build_war_range_embed(
                self._nation, self._targets, self.page, self._multi_alliance
            ),
            view=self,
        )

    async def _next_cb(self, interaction: discord.Interaction) -> None:
        self.page = min(self._total_pages - 1, self.page + 1)
        self._refresh_buttons()
        await interaction.response.edit_message(
            embed=_build_war_range_embed(
                self._nation, self._targets, self.page, self._multi_alliance
            ),
            view=self,
        )


war_group = app_commands.Group(
    name="war",
    description="War-related commands.",
)
bot.tree.add_command(war_group)

war_range_group = app_commands.Group(
    name="range",
    description="War range commands.",
)
war_group.add_command(war_range_group)


@war_range_group.command(
    name="targets",
    description="Show /slots nations in your war range that have open defensive slots.",
)
@app_commands.describe(
    user="The Discord user to look up (defaults to yourself).",
)
async def war_range_targets(
    interaction: discord.Interaction,
    user: Optional[discord.Member] = None,
) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    target_user = user or interaction.user
    guild_id = interaction.guild_id or 0

    # Resolve the nation for the target user.
    row = bot.db.get_by_discord_id(target_user.id)
    nation: Optional[pnw_api.Nation] = None

    if row is not None:
        try:
            nation = await bot.pnw.get_nation(row["nation_id"])
        except Exception:
            log.exception("PnW API error fetching nation for war range targets")

    if nation is None:
        # Fall back to PnW discord tag lookup.
        try:
            nation = await bot.pnw.get_nation_by_discord_tag(target_user.name)
            if nation is not None and not PnWClient.discord_matches(
                nation.discord_tag, target_user.name
            ):
                nation = None
        except Exception:
            pass

    if nation is None:
        mention = target_user.mention if user else "You"
        await interaction.followup.send(
            embed=_info_embed(
                f"ℹ️ {mention} {'is' if user else 'are'} not registered. "
                "Use `/register <nation_id>` to link your Discord account."
            )
        )
        return

    alliance_ids = bot.db.get_slots_alliances(guild_id)
    if not alliance_ids:
        await interaction.followup.send(
            embed=_info_embed(
                "ℹ️ No alliances configured. An admin can use `/config slots set` to set them up."
            )
        )
        return

    min_score = nation.score * WAR_RANGE_MIN_RATIO
    max_score = nation.score * WAR_RANGE_MAX_RATIO

    try:
        members, war_counts = await asyncio.gather(
            bot.pnw.get_alliance_members(alliance_ids),
            bot.pnw.get_active_def_war_counts_by_alliance(alliance_ids),
        )
    except Exception as exc:
        log.exception("PnW API error while fetching data for war range targets")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    # Filter: in war range AND has at least one open defensive slot.
    targets = [
        (n, war_counts.get(n.nation_id, 0))
        for n in members
        if min_score <= n.score <= max_score
        and war_counts.get(n.nation_id, 0) < MAX_DEFENSIVE_SLOTS
    ]

    if not targets:
        await interaction.followup.send(
            embed=_info_embed(
                f"ℹ️ No nations from the configured alliance(s) are in your war range "
                f"({min_score:,.0f}–{max_score:,.0f} score) with open defensive slots."
            )
        )
        return

    # Sort by city count descending (most cities first — most valuable targets).
    targets.sort(key=lambda t: t[0].num_cities, reverse=True)

    multi_alliance = len({n.alliance_id for n, _ in targets}) > 1
    embed = _build_war_range_embed(nation, targets, page=0, multi_alliance=multi_alliance)
    view = WarRangeView(nation, targets, multi_alliance, page=0)
    await interaction.followup.send(embed=embed, view=view)


# ---------------------------------------------------------------------------
# /city cost
# ---------------------------------------------------------------------------

city_group = app_commands.Group(name="city", description="City-related commands.")
bot.tree.add_command(city_group)


@city_group.command(
    name="cost",
    description="Calculate city purchase cost using the live dynamic formula.",
)
@app_commands.describe(
    current="Current number of cities.",
    target="Target number of cities (defaults to current + 1).",
    manifest_destiny="Is the nation's domestic policy Manifest Destiny? (−5% cost)",
    government_support_agency="Does the nation have Government Support Agency? (additional −2.5%)",
)
async def city_cost_command(
    interaction: discord.Interaction,
    current: int,
    target: Optional[int] = None,
    manifest_destiny: bool = False,
    government_support_agency: bool = False,
) -> None:
    await interaction.response.defer()

    if not await _check_member_access(interaction):
        await interaction.followup.send(
            embed=_error_embed("❌ You need the **Member** role to use this command."),
            ephemeral=True,
        )
        return

    if current < 0:
        await interaction.followup.send(
            embed=_error_embed("❌ Current city count must be 0 or greater.")
        )
        return

    if target is None:
        target = current + 1

    if target <= current:
        await interaction.followup.send(
            embed=_error_embed("❌ Target city count must be greater than current.")
        )
        return

    if target - current > 50:
        await interaction.followup.send(
            embed=_error_embed("❌ Range too large — maximum 50 cities at a time.")
        )
        return

    # Fetch live city_average from game_info
    try:
        game_info = await bot.pnw.get_game_info()
    except Exception:
        game_info = GameInfo()

    city_avg = game_info.city_average

    total_cost = 0.0
    rows: list[str] = []
    for c in range(current, target):
        cost = calculate_city_cost(
            c,
            city_average=city_avg,
            manifest_destiny=manifest_destiny,
            government_support_agency=government_support_agency,
        )
        total_cost += cost
        rows.append(f"City **{c + 1}→{c + 2}**: ${cost:,.0f}")

    embed = discord.Embed(
        title="🏙️ City Cost Calculator",
        color=discord.Color.green(),
    )

    # If only one city, show inline detail; otherwise summarise
    if len(rows) == 1:
        embed.description = rows[0]
    else:
        # Show individual rows for small ranges, just total for large ones
        if len(rows) <= 20:
            embed.description = "\n".join(rows)
        else:
            embed.description = f"*Buying {len(rows)} cities ({current + 1}→{target})*"
        embed.add_field(name="Total Cost", value=f"**${total_cost:,.0f}**", inline=False)

    discount_notes: list[str] = []
    if manifest_destiny:
        pct = 7.5 if government_support_agency else 5.0
        discount_notes.append(
            f"Manifest Destiny{' + GSA' if government_support_agency else ''} (−{pct:.1f}%)"
        )
    embed.add_field(
        name="Discounts",
        value="\n".join(discount_notes) if discount_notes else "None",
        inline=True,
    )
    embed.set_footer(text=f"City average used: {city_avg:.2f}  ·  Formula: Locutus dynamic")

    await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# /revenue
# ---------------------------------------------------------------------------

async def _handle_revenue(
    interaction: discord.Interaction,
    pnw: PnWClient,
    query: str | None,
) -> None:
    """Resolve a nation from *query* (same semantics as /whois) and display revenue.

    If query is omitted, defaults to the invoking user's registered nation.
    """
    query = (query or "").strip()

    # --- resolve nation_id from the query (mirrors _handle_whois logic) ---
    nation_id: Optional[int] = None

    if not query:
        row = bot.db.get_by_discord_id(interaction.user.id)
        if row is None:
            await interaction.followup.send(
                embed=_info_embed(
                    "ℹ️ You are not registered. Use `/register <nation_id>` "
                    "or provide a query to `/revenue`."
                )
            )
            return
        nation_id = int(row["nation_id"])
    elif (mention_match := _MENTION_RE.match(query)):
        target_id = int(mention_match.group(1))
        row = bot.db.get_by_discord_id(target_id)
        if row is not None:
            nation_id = int(row["nation_id"])
        else:
            # Try PnW discord tag lookup
            member = interaction.guild and interaction.guild.get_member(target_id)
            if member is None and interaction.guild is not None:
                try:
                    member = await interaction.guild.fetch_member(target_id)
                except discord.HTTPException:
                    member = None
            if member is not None:
                try:
                    n = await pnw.get_nation_by_discord_tag(member.name)
                    if n is not None and PnWClient.discord_matches(n.discord_tag, member.name):
                        nation_id = n.nation_id
                except Exception:
                    pass
            if nation_id is None:
                await interaction.followup.send(
                    embed=_info_embed(f"ℹ️ <@{target_id}> has no registered nation.")
                )
                return
    elif query.lstrip("-").isdigit():
        nation_id = int(query)
    else:
        # Nation name or Discord username
        try:
            n = await pnw.get_nation_by_name(query)
            if n is not None:
                nation_id = n.nation_id
        except Exception:
            pass
        if nation_id is None:
            row = bot.db.get_by_discord_username(query)
            if row is not None:
                nation_id = int(row["nation_id"])
        if nation_id is None:
            await interaction.followup.send(
                embed=_info_embed(f"ℹ️ No nation found for `{query}`.")
            )
            return

    # --- fetch nation + cities ---
    try:
        result = await pnw.get_nation_with_cities(nation_id)
    except Exception as exc:
        log.exception("PnW API error fetching nation/cities for /revenue")
        await interaction.followup.send(
            embed=_error_embed(f"❌ Could not reach the Politics and War API: {exc}")
        )
        return

    if result is None:
        await interaction.followup.send(
            embed=_info_embed(f"ℹ️ No nation with ID `{nation_id}` was found.")
        )
        return

    nation, cities = result

    # --- fetch game_info for accurate food (radiation, season) ---
    try:
        game_info = await pnw.get_game_info()
    except Exception:
        game_info = GameInfo()

    rev = compute_nation_revenue(nation, cities, game_info)

    # --- build embed ---
    embed = discord.Embed(
        title=f"💰 Revenue — {nation.nation_name}",
        url=_nation_url(nation.nation_id),
        color=discord.Color.gold(),
    )
    embed.add_field(name="🏙️ Cities", value=str(nation.num_cities), inline=True)
    embed.add_field(name="🛒 Avg Commerce", value=f"{rev.avg_commerce:.1f}%", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    embed.add_field(name="💵 Money/day", value=f"${rev.money:,.0f}", inline=True)
    embed.add_field(name="🌾 Food/day (net)", value=f"{rev.food:+,.2f}", inline=True)
    embed.add_field(name="\u200b", value="\u200b", inline=True)

    # Raw resources
    raws = [
        ("⛏️ Coal",    rev.coal),
        ("🛢️ Oil",     rev.oil),
        ("☢️ Uranium", rev.uranium),
        ("🔩 Iron",    rev.iron),
        ("🪨 Bauxite", rev.bauxite),
        ("🔦 Lead",    rev.lead),
    ]
    raw_lines = "\n".join(
        f"{icon} **{name}**: {val:+,.2f}/day"
        for icon, name, val in (
            ("⛏️", "Coal",    rev.coal),
            ("🛢️", "Oil",     rev.oil),
            ("☢️", "Uranium", rev.uranium),
            ("🔩", "Iron",    rev.iron),
            ("🪨", "Bauxite", rev.bauxite),
            ("🔦", "Lead",    rev.lead),
        )
    )
    embed.add_field(name="Raw Resources", value=raw_lines or "*none*", inline=True)

    mfg_lines = "\n".join(
        f"{icon} **{name}**: {val:+,.2f}/day"
        for icon, name, val in (
            ("⛽", "Gasoline",  rev.gasoline),
            ("💣", "Munitions", rev.munitions),
            ("🔧", "Steel",     rev.steel),
            ("🪟", "Aluminum",  rev.aluminum),
        )
    )
    embed.add_field(name="Manufactured", value=mfg_lines or "*none*", inline=True)

    embed.set_footer(
        text=(
            f"Food: {rev.food_production:,.2f} prod − {rev.food_consumption:,.2f} use  ·  "
            f"Season month: {game_info.game_month}  ·  "
            f"Money net of improvement upkeep, before military upkeep & tax"
        )
    )

    await interaction.followup.send(embed=embed)


@bot.tree.command(
    name="revenue",
    description="Show estimated gross daily revenue for a PnW nation (or your own if omitted).",
)
@app_commands.describe(
    query="Optional: a nation ID, @mention, nation name, or Discord username."
)
async def revenue_command(interaction: discord.Interaction, query: str | None = None) -> None:
    await interaction.response.defer()
    await _handle_revenue(interaction, bot.pnw, query)


_HELP_COMMANDS = [
    ("/register <nation_id>", "Link your Discord account to a PnW nation."),
    ("/unregister", "Remove your PnW nation registration."),
    ("/whois <query>", "Look up a nation by ID, name, or @mention."),
    ("/revenue [query]", "Show estimated gross daily revenue for a nation; defaults to your registered nation."),
    ("/city cost <current> [target] [options]", "Calculate city purchase cost(s) using the live dynamic formula."),
    ("/alliance info <query>", "Look up an alliance by ID, name, or @mention."),
    ("/alliance members <query>", "List members of an alliance (10 per page)."),
    ("/test whois <query>", "Look up a nation via the PnW test API."),
    ("/test alliance info <query>", "Look up an alliance via the PnW test API by ID, name, or @mention."),
    ("/gov", "Show members who hold a configured government role."),
    ("/slots", "Show open defensive war slots for monitored alliances."),
    ("/roles setup", "Map server roles to government departments. *(admin)*"),
    ("/roles show", "Show the currently configured government roles."),
    ("/config slots set <ids>", "Set alliance IDs monitored by /slots. *(admin or milcom)*"),
    ("/config slots show", "Show configured /slots alliance IDs."),
    ("/config slots clear", "Clear the /slots alliance configuration. *(admin or milcom)*"),
    ("/setup grant_channel <channel>", "Set the channel for grant requests. *(admin, econ, or IA)*"),
    ("/admin alliance set <id>", "Set the guild's primary alliance ID. *(admin)*"),
    ("/admin alliance show", "Show the guild's configured primary alliance ID."),
    ("/admin api_key set <key>", "Override the PnW API key used by this bot. *(admin)*"),
    ("/admin clear_guild_commands", "Clear guild-scoped commands to remove duplicates. *(admin)*"),
    ("/admin sync", "Copy global commands to this server for instant propagation. *(admin)*"),
    ("/color", "Check whether alliance members are on the correct color."),
    ("/damage leaderboard", "Show all alliance members' loot & damage (money + resources at market price, infra destroyed) for the past 7 days. Includes members with no wars at zero."),
    ("/spy target find <alliances>", "Find nations in given alliances (comma-separated names or IDs) sorted by spy capacity (cities)."),
    ("/missile targets find", "Top 20 nations in the /slots alliances with the most cities that have open defensive slots."),
    ("/infra <current> <target> [cities] [projects]", "Calculate infrastructure purchase cost with optional project discounts."),
    ("/war range targets [user]", "Show /slots nations in your (or another user's) war range with open defensive slots."),
    ("/send <receiver> [options]", "Compose a Locutus resource-transfer command."),
    ("/request grant <note> [resources]", "Request a grant; pings econ gov (or econ if not set)."),
    ("/suggestion <content>", "Submit a suggestion via Discord DMs to leadership."),
    ("/help", "Show this help message."),
]


@bot.tree.command(name="help", description="List all available bot commands.")
async def help_command(interaction: discord.Interaction) -> None:
    lines = [f"`{name}` — {description}" for name, description in _HELP_COMMANDS]
    embed = discord.Embed(
        title="Available Commands",
        description="\n".join(lines),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)
