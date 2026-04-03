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

/alliance <query>
    Look up a Politics and War alliance by ID or name.
    Returns an embed with score, member count, avg cities, and more.

/test whois <query>
    Same as /whois but queries the PnW test API.

/test alliance <query>
    Same as /alliance but queries the PnW test API.

/config slots set <alliance_ids>
    (Admin only) Set the alliance IDs monitored by /slots.

/config slots show
    Show the currently configured /slots alliance IDs.

/config slots clear
    (Admin only) Clear the /slots alliance configuration.

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
    (Admin only) Set the channel where /request grant posts are sent.

/send <receiver> [sender] [bank_note] [money] [food] [coal] [oil] [uranium] [iron]
      [bauxite] [lead] [gasoline] [munitions] [steel] [aluminum]
    Compose a Locutus /transfer resources command for a resource transfer.
    receiver is a Discord ping or nation ID; bank_note defaults to #grant.
    Posts an embed with all details and the pre-formatted command:
    /transfer resources receiver:<id> transfer:{ money:1000,...} bank_note:#grant

/request grant <reason> [money] [food] [coal] [oil] [uranium] [iron]
               [bauxite] [lead] [gasoline] [munitions] [steel] [aluminum]
    Request a grant from the Economics team.
    Posts an embed in the configured grant channel and pings the econ role.
    Requires both a grant channel and an econ role to be configured via
    /setup grant_channel and /roles setup respectively.

"""
from __future__ import annotations

import logging
import re

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
    AllianceInfo,
    Nation,
    PnWClient,
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


def _format_discord_identifier(row: object) -> str:
    """Return the stored Discord username, falling back to the numeric ID."""
    return row["discord_username"] or row["discord_id"]


def _nation_url(nation_id: int) -> str:
    return f"https://politicsandwar.com/nation/id={nation_id}/"


def _alliance_url(alliance_id: int) -> str:
    return f"https://politicsandwar.com/alliance/id={alliance_id}"


def _nation_embed(
    nation: Nation,
    registered_discord: str | None = None,
    note: str | None = None,
) -> discord.Embed:
    """Build a rich Discord embed for a PnW nation."""
    embed = discord.Embed(
        title=nation.nation_name,
        url=_nation_url(nation.nation_id),
        color=discord.Color.blue(),
    )

    embed.add_field(name="Leader", value=nation.leader_name or "—", inline=True)

    # Alliance — hyperlinked name with position + seniority on the second line
    if nation.alliance_id:
        alliance_label = nation.alliance_name or str(nation.alliance_id)
        alliance_val = f"[{alliance_label}]({_alliance_url(nation.alliance_id)})"
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

    if nation.offensivewars or nation.defensivewars:
        embed.add_field(
            name="Wars",
            value=f"⚔️ {nation.offensivewars} off / 🛡️ {nation.defensivewars} def",
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


def _alliance_embed(info: AllianceInfo) -> discord.Embed:
    """Build a rich Discord embed for a PnW alliance."""
    title = f"{info.name} ({info.acronym})" if info.acronym else info.name
    embed = discord.Embed(
        title=title,
        url=_alliance_url(info.alliance_id),
        color=discord.Color.gold(),
    )

    if info.flag:
        embed.set_thumbnail(url=info.flag)

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

    async def setup_hook(self) -> None:
        guild = discord.Object(id=config.GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        log.info("Slash commands synced to guild %d.", config.GUILD_ID)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%d)", self.user, self.user.id)
        if config.API_KEY:
            await self._start_api()

    async def _start_api(self) -> None:
        app = create_app(
            guild_getter=lambda: self.get_guild(config.GUILD_ID),
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
    interaction: discord.Interaction, pnw: PnWClient, query: str
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
            embed = _nation_embed(nation, registered_discord=f"<@{target_id}>")
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

        embed = _nation_embed(nation, registered_discord=discord_user)
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
        embed = _nation_embed(nation, registered_discord=discord_user)
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
        embed = _nation_embed(nation, registered_discord=f"`{stored_name}`")
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
# /alliance
# ---------------------------------------------------------------------------


async def _handle_alliance_find(
    interaction: discord.Interaction, pnw: PnWClient, query: str
) -> None:
    """Shared logic for /alliance and /test alliance."""
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

    await interaction.followup.send(embed=_alliance_embed(info))


@bot.tree.command(
    name="alliance",
    description="Look up a Politics and War alliance by ID or name.",
)
@app_commands.describe(query="Alliance ID (numeric) or alliance name.")
async def alliance_find(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_alliance_find(interaction, bot.pnw, query)


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
    description="Set the alliance IDs monitored by /slots (admin only).",
)
@app_commands.describe(
    alliance_ids="Comma-separated Politics and War alliance IDs to monitor."
)
@app_commands.checks.has_permissions(administrator=True)
async def config_slots_set(interaction: discord.Interaction, alliance_ids: str) -> None:
    await interaction.response.defer(ephemeral=True)

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


@config_slots_set.error
async def config_slots_set_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the **Administrator** permission to use this command.",
            ephemeral=True,
        )
    else:
        raise error


@config_slots_group.command(
    name="show",
    description="Show the currently configured /slots alliance IDs.",
)
async def config_slots_show(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
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
    description="Clear the /slots alliance configuration (admin only).",
)
@app_commands.checks.has_permissions(administrator=True)
async def config_slots_clear(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id or 0
    bot.db.set_slots_alliances(guild_id, [])
    log.info("Guild %d: /slots alliances cleared by %s", guild_id, interaction.user)
    await interaction.followup.send(
        "✅ /slots alliance configuration cleared.", ephemeral=True
    )


@config_slots_clear.error
async def config_slots_clear_error(
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
            line += f" | 🟡 {nation.beige_turns}t beige"
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
    "econ": "Economics",
    "milcom": "Military Command",
    "ia": "Internal Affairs",
    "gov": "Basic Gov",
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
    econ="Role that counts as Economics.",
    milcom="Role that counts as Military Command.",
    ia="Role that counts as Internal Affairs.",
    gov="Role that counts as Basic Gov.",
)
@app_commands.checks.has_permissions(administrator=True)
async def roles_setup(
    interaction: discord.Interaction,
    leader: discord.Role | None = None,
    econ: discord.Role | None = None,
    milcom: discord.Role | None = None,
    ia: discord.Role | None = None,
    gov: discord.Role | None = None,
) -> None:
    await interaction.response.defer(ephemeral=True)

    guild_id = interaction.guild_id or 0
    current = bot.db.get_gov_roles(guild_id)

    updates = {
        "leader": leader.id if leader else current["leader"],
        "econ": econ.id if econ else current["econ"],
        "milcom": milcom.id if milcom else current["milcom"],
        "ia": ia.id if ia else current["ia"],
        "gov": gov.id if gov else current["gov"],
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
    "econ": "💰",
    "milcom": "⚔️",
    "ia": "🤝",
    "gov": "🏛️",
}

# Departments hidden from the /gov embed (still configurable via /roles setup).
_GOV_HIDDEN_FROM_EMBED: frozenset[str] = frozenset({"gov"})


@bot.tree.command(
    name="gov",
    description="Show server members who hold a configured government role.",
)
async def gov(interaction: discord.Interaction) -> None:
    await interaction.response.defer()

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

    # Check that the invoking member holds the configured econ role (admins bypass).
    guild_id = interaction.guild_id or 0
    member = interaction.guild and interaction.guild.get_member(interaction.user.id)
    is_admin = member and member.guild_permissions.administrator
    if not is_admin:
        econ_role_id = bot.db.get_gov_roles(guild_id).get("econ")
        if not econ_role_id or not member or not any(r.id == econ_role_id for r in member.roles):
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
    await _handle_whois(interaction, bot.pnw_test, query)


@test_group.command(
    name="alliance",
    description="Look up a PnW alliance via the TEST API by ID or name.",
)
@app_commands.describe(query="Alliance ID (numeric) or alliance name.")
async def test_alliance_find(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    await _handle_alliance_find(interaction, bot.pnw_test, query)


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
    description="Set the channel where /request grant posts are sent (admin only).",
)
@app_commands.describe(channel="The text channel that will receive grant requests.")
@app_commands.checks.has_permissions(administrator=True)
async def setup_grant_channel(
    interaction: discord.Interaction, channel: discord.TextChannel
) -> None:
    await interaction.response.defer(ephemeral=True)
    guild_id = interaction.guild_id or 0
    bot.db.set_grant_channel(guild_id, channel.id)
    log.info("Guild %d: grant channel set to #%s (%d) by %s", guild_id, channel.name, channel.id, interaction.user)
    await interaction.followup.send(
        embed=_success_embed(f"✅ Grant requests will now be posted in {channel.mention}."),
        ephemeral=True,
    )


@setup_grant_channel.error
async def setup_grant_channel_error(
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
    reason="Why you need this grant.",
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
    reason: str,
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

    guild_id = interaction.guild_id or 0

    # Check that both the grant channel and the econ role are configured.
    grant_channel_id = bot.db.get_grant_channel(guild_id)
    econ_role_id = bot.db.get_gov_roles(guild_id).get("econ")

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

    # Determine receiver for the Locutus command: use registered nation ID if available.
    reg = bot.db.get_by_discord_id(interaction.user.id)
    receiver = str(reg["nation_id"]) if reg else interaction.user.mention

    transfer_json = "{" + ",".join(
        f"{k}:{_fmt_amount(v)}" for k, v in resources.items()
    ) + "}"
    locutus_cmd = (
        f"/transfer resources receiver:{receiver} "
        f"transfer:{transfer_json} bank_note:#grant"
    )

    embed = discord.Embed(
        title="📋 Grant Request",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Requested by", value=interaction.user.mention, inline=True)
    embed.add_field(name="Receiver", value=receiver, inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)

    res_lines = [
        f"**{name.title()}:** {_fmt_amount(val)}" for name, val in resources.items()
    ]
    embed.add_field(name="Resources", value="\n".join(res_lines), inline=False)
    embed.add_field(name="Locutus Command", value=f"```{locutus_cmd}```", inline=False)

    econ_mention = f"<@&{econ_role_id}>"
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
# /help
# ---------------------------------------------------------------------------

_HELP_COMMANDS = [
    ("/register <nation_id>", "Link your Discord account to a PnW nation."),
    ("/unregister", "Remove your PnW nation registration."),
    ("/whois <query>", "Look up a nation by ID, name, or @mention."),
    ("/alliance <query>", "Look up an alliance by ID or name."),
    ("/test whois <query>", "Look up a nation via the PnW test API."),
    ("/test alliance <query>", "Look up an alliance via the PnW test API."),
    ("/gov", "Show members who hold a configured government role."),
    ("/slots", "Show open defensive war slots for monitored alliances."),
    ("/roles setup", "Map server roles to government departments. *(admin)*"),
    ("/roles show", "Show the currently configured government roles."),
    ("/config slots set <ids>", "Set alliance IDs monitored by /slots. *(admin)*"),
    ("/config slots show", "Show configured /slots alliance IDs."),
    ("/config slots clear", "Clear the /slots alliance configuration. *(admin)*"),
    ("/setup grant_channel <channel>", "Set the channel for grant requests. *(admin)*"),
    ("/send <receiver> [options]", "Compose a Locutus resource-transfer command."),
    ("/request grant <reason> [resources]", "Request a grant from the Economics team."),
    ("/help", "Show this help message."),
]


@bot.tree.command(name="help", description="List all available bot commands.")
async def help_command(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Available Commands",
        color=discord.Color.blurple(),
    )
    for name, description in _HELP_COMMANDS:
        embed.add_field(name=name, value=description, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)

