"""
flame_bot – main entry point.

Commands
--------
/register <nation_id>
    Link your Discord account to a Politics and War nation.
    The bot verifies that your Discord username appears in the nation's
    in-game Discord field before accepting the registration.

/whois <query>
    Unified nation look-up command.
    • Numeric query  → fetch that nation from the PnW API by ID.
    • @mention       → look up the mentioned member's registered nation.
    • Text           → search PnW by nation name, then fall back to
                       looking up a Discord username in the local database.
    Response is a rich embed including military capacity percentages.

/config slots <alliance_ids>
    (Admin only) Set the alliance IDs monitored by /slots.

/slots
    Show an embed listing all non-vacation-mode members of the configured
    alliances with their score, city count, and open defensive slots.

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


def _nation_embed(
    nation: Nation,
    registered_discord: str | None = None,
) -> discord.Embed:
    """Build a rich Discord embed for a PnW nation."""
    embed = discord.Embed(
        title=nation.nation_name,
        url=_nation_url(nation.nation_id),
        color=discord.Color.blue(),
    )

    embed.add_field(name="Leader", value=nation.leader_name or "—", inline=True)

    alliance_val = nation.alliance_name or (str(nation.alliance_id) if nation.alliance_id else "None")
    embed.add_field(name="Alliance", value=alliance_val, inline=True)

    embed.add_field(name="Score", value=f"{nation.score:,.2f}", inline=True)
    embed.add_field(name="Cities", value=str(nation.num_cities), inline=True)

    if nation.last_active:
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
            f"🛡️ Tanks:    {pct(nation.tanks, max_tan)}\n"
            f"✈️ Aircraft: {pct(nation.aircraft, max_air)}\n"
            f"🚢 Ships:    {pct(nation.ships, max_shi)}"
        )
        embed.add_field(name="Military", value=military_text, inline=False)

    if registered_discord:
        embed.add_field(name="Discord", value=registered_discord, inline=True)
    elif nation.discord_tag:
        embed.add_field(name="PnW Discord", value=f"`{nation.discord_tag}`", inline=True)

    return embed


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
            "❌ Please provide a valid positive nation ID."
        )
        return

    # ------------------------------------------------------------------
    # Check whether this nation is already registered to someone else
    # ------------------------------------------------------------------
    existing_by_nation = bot.db.get_by_nation_id(nation_id)
    if existing_by_nation and int(existing_by_nation["discord_id"]) != interaction.user.id:
        await interaction.followup.send(
            "❌ That nation is already registered to a different Discord account.",
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
            f"❌ Could not reach the Politics and War API: {exc}"
        )
        return

    if nation is None:
        await interaction.followup.send(
            f"❌ Nation with ID **{nation_id}** was not found."
        )
        return

    # ------------------------------------------------------------------
    # Verify Discord username against the nation's discord field
    # ------------------------------------------------------------------
    username = interaction.user.name
    if not PnWClient.discord_matches(nation.discord_tag, username):
        await interaction.followup.send(
            f"❌ Verification failed.\n\n"
            f"Nation **{nation.nation_name}** (leader: {nation.leader_name}) "
            f"has `{nation.discord_tag or '(empty)'}` as its Discord handle, "
            f"but your Discord username is `{username}`.\n\n"
            f"Please set your Discord handle on your nation's edit page to "
            f"`{username}` and try again.",
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
        f"✅ Successfully registered!\n"
        f"Nation: **{nation.nation_name}** (ID: `{nation_id}`, leader: {nation.leader_name})"
        f"{roles_text}",
    )


# ---------------------------------------------------------------------------
# /whois  (unified replacement for the former /who and /whois commands)
# ---------------------------------------------------------------------------


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

    query = query.strip()

    # ------------------------------------------------------------------
    # 1. @mention → look up the mentioned member's registered nation
    # ------------------------------------------------------------------
    mention_match = _MENTION_RE.match(query)
    if mention_match:
        target_id = int(mention_match.group(1))
        row = bot.db.get_by_discord_id(target_id)
        if row is None:
            await interaction.followup.send(
                f"ℹ️ <@{target_id}> has not registered yet."
            )
            return

        nation_id = row["nation_id"]
        try:
            nation = await bot.pnw.get_nation(nation_id)
        except Exception:
            nation = None

        if nation:
            embed = _nation_embed(nation, registered_discord=f"<@{target_id}>")
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                f"<@{target_id}> is registered with nation ID `{nation_id}` "
                "(nation details unavailable)."
            )
        return

    # ------------------------------------------------------------------
    # 2. Numeric query → fetch nation from the PnW API by ID
    # ------------------------------------------------------------------
    if query.lstrip("-").isdigit():
        nation_id = int(query)
        if nation_id <= 0:
            await interaction.followup.send(
                "❌ Please provide a valid positive nation ID."
            )
            return

        try:
            nation = await bot.pnw.get_nation(nation_id)
        except Exception as exc:
            log.exception("PnW API error while fetching nation %d", nation_id)
            await interaction.followup.send(
                f"❌ Could not reach the Politics and War API: {exc}"
            )
            return

        if nation is None:
            await interaction.followup.send(
                f"ℹ️ No nation with ID `{nation_id}` was found."
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
        nation = await bot.pnw.get_nation_by_name(query)
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
            f"ℹ️ No nation or Discord user found for `{query}`."
        )
        return

    nation_id = row["nation_id"]
    try:
        nation = await bot.pnw.get_nation(nation_id)
    except Exception:
        nation = None

    stored_name = _format_discord_identifier(row)
    if nation:
        embed = _nation_embed(nation, registered_discord=f"`{stored_name}`")
        await interaction.followup.send(embed=embed)
    else:
        await interaction.followup.send(
            f"**{stored_name}** is registered with nation ID `{nation_id}` "
            "(nation details unavailable)."
        )


# ---------------------------------------------------------------------------
# /config  (command group)
# ---------------------------------------------------------------------------

config_group = app_commands.Group(
    name="config",
    description="Bot configuration commands (admin only).",
)
bot.tree.add_command(config_group)


@config_group.command(
    name="slots",
    description="Set the alliance IDs monitored by /slots (admin only).",
)
@app_commands.describe(
    alliance_ids="Comma-separated Politics and War alliance IDs to monitor."
)
@app_commands.checks.has_permissions(administrator=True)
async def config_slots(interaction: discord.Interaction, alliance_ids: str) -> None:
    await interaction.response.defer(ephemeral=True)

    raw_ids = [part.strip() for part in alliance_ids.split(",") if part.strip()]
    parsed: list[int] = []
    for part in raw_ids:
        if not part.isdigit() or int(part) <= 0:
            await interaction.followup.send(
                f"❌ `{part}` is not a valid alliance ID. "
                "Please provide positive integers separated by commas.",
                ephemeral=True,
            )
            return
        parsed.append(int(part))

    if not parsed:
        await interaction.followup.send(
            "❌ No valid alliance IDs provided.", ephemeral=True
        )
        return

    guild_id = interaction.guild_id or 0
    bot.db.set_slots_alliances(guild_id, parsed)
    log.info(
        "Guild %d: /slots alliances updated to %s by %s",
        guild_id,
        parsed,
        interaction.user,
    )
    await interaction.followup.send(
        f"✅ /slots will now monitor alliance(s): `{', '.join(str(a) for a in parsed)}`",
        ephemeral=True,
    )


@config_slots.error
async def config_slots_error(
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
            "ℹ️ No alliances configured. An admin can use `/config slots` to set them up."
        )
        return

    # Fetch alliance members
    try:
        members = await bot.pnw.get_alliance_members(alliance_ids)
    except Exception as exc:
        log.exception("PnW API error while fetching alliance members")
        await interaction.followup.send(
            f"❌ Could not reach the Politics and War API: {exc}"
        )
        return

    if not members:
        await interaction.followup.send(
            "ℹ️ No active members found for the configured alliance(s)."
        )
        return

    # Fetch active defensive war counts for all members
    nation_ids = [n.nation_id for n in members]
    try:
        war_counts = await bot.pnw.get_active_war_counts(nation_ids)
    except Exception:
        log.exception("PnW API error while fetching war counts")
        war_counts = {}

    # Sort by score descending
    members.sort(key=lambda n: n.score, reverse=True)

    # Build embed(s) – Discord description cap is 4096 chars
    # Group by alliance for readability
    alliances_map: dict[int, list[Nation]] = {}
    for nation in members:
        alliances_map.setdefault(nation.alliance_id, []).append(nation)

    embeds: list[discord.Embed] = []
    # Stay comfortably below Discord's 4096-char description hard limit
    EMBED_DESC_SOFT_LIMIT = 4000

    def _flush_embed(title: str, lines: list[str]) -> None:
        if not lines:
            return
        embeds.append(
            discord.Embed(
                title=title,
                description="\n".join(lines),
                color=discord.Color.green(),
            )
        )

    for alliance_id, nations in alliances_map.items():
        alliance_name = nations[0].alliance_name or f"Alliance {alliance_id}"
        section_lines: list[str] = []
        for nation in nations:
            active_def = war_counts.get(nation.nation_id, 0)
            open_slots = MAX_DEFENSIVE_SLOTS - active_def
            line = (
                f"[{nation.nation_name}]({_nation_url(nation.nation_id)}) "
                f"— 🏙️ {nation.num_cities} "
                f"| ⭐ {nation.score:,.0f} "
                f"| 🛡️ {open_slots}/{MAX_DEFENSIVE_SLOTS} slots"
            )
            section_lines.append(line)

        # Chunk into embeds that fit within the description limit
        chunk: list[str] = []
        chunk_len = 0
        part = 1
        for line in section_lines:
            # +1 accounts for the newline separator between lines
            needed = len(line) + (1 if chunk else 0)
            if chunk_len + needed > EMBED_DESC_SOFT_LIMIT and chunk:
                title = alliance_name if part == 1 else f"{alliance_name} (cont.)"
                _flush_embed(title, chunk)
                chunk = [line]
                chunk_len = len(line)
                part += 1
            else:
                chunk.append(line)
                chunk_len += needed
        title = alliance_name if part == 1 else f"{alliance_name} (cont.)"
        _flush_embed(title, chunk)

    # Discord allows up to 10 embeds per message; send additional messages if needed
    BATCH = 10
    for i in range(0, len(embeds), BATCH):
        await interaction.followup.send(embeds=embeds[i : i + BATCH])


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)

