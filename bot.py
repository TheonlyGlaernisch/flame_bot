"""
flame_bot – main entry point.

Commands
--------
/register <nation_id>
    Link your Discord account to a Politics and War nation.
    The bot verifies that your Discord username appears in the nation's
    in-game Discord field before accepting the registration.

/who <query>
    If <query> is numeric, fetch that nation from the PnW API.
    If <query> is a Discord username, look it up in the local database.

/whois <member>
    Show the registered nation for a Discord member.

/check_roles
    Re-evaluate and sync bar3 roles for the calling user.
    Used by bar3 to decide whether the logged-in Discord user has access.
"""
from __future__ import annotations

import logging

import discord
from aiohttp import web
from discord import app_commands

import config
from api import RoleConfig, create_app
from database import Database
from pnw_api import PnWClient

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


def _get_role(guild: discord.Guild, role_id: int | None) -> discord.Role | None:
    if role_id is None:
        return None
    return guild.get_role(role_id)


def _format_discord_identifier(row: object) -> str:
    """Return the stored Discord username, falling back to the numeric ID."""
    return row["discord_username"] or row["discord_id"]


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------


class FlameBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.db = Database(config.DB_PATH)
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
            db=self.db,
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
    await interaction.response.defer(ephemeral=True)

    if nation_id <= 0:
        await interaction.followup.send(
            "❌ Please provide a valid positive nation ID.", ephemeral=True
        )
        return

    # ------------------------------------------------------------------
    # Check whether this nation is already registered to someone else
    # ------------------------------------------------------------------
    existing_by_nation = bot.db.get_by_nation_id(nation_id)
    if existing_by_nation and int(existing_by_nation["discord_id"]) != interaction.user.id:
        await interaction.followup.send(
            "❌ That nation is already registered to a different Discord account.",
            ephemeral=True,
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
            f"❌ Could not reach the Politics and War API: {exc}", ephemeral=True
        )
        return

    if nation is None:
        await interaction.followup.send(
            f"❌ Nation with ID **{nation_id}** was not found.", ephemeral=True
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
            ephemeral=True,
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
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# /who
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="who",
    description="Look up a nation by ID (PnW API) or a Discord username (database).",
)
@app_commands.describe(
    query="A numeric nation ID to fetch from PnW, or a Discord username to look up in the database."
)
async def who(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer(ephemeral=True)

    query = query.strip()

    # ------------------------------------------------------------------
    # Numeric query → fetch nation from the PnW API
    # ------------------------------------------------------------------
    if query.lstrip("-").isdigit():
        nation_id = int(query)
        if nation_id <= 0:
            await interaction.followup.send(
                "❌ Please provide a valid positive nation ID.", ephemeral=True
            )
            return

        try:
            nation = await bot.pnw.get_nation(nation_id)
        except Exception as exc:
            log.exception("PnW API error while fetching nation %d", nation_id)
            await interaction.followup.send(
                f"❌ Could not reach the Politics and War API: {exc}", ephemeral=True
            )
            return

        if nation is None:
            await interaction.followup.send(
                f"ℹ️ No nation with ID `{nation_id}` was found.", ephemeral=True
            )
            return

        # Also surface any local registration for this nation
        row = bot.db.get_by_nation_id(nation_id)
        registered_part = (
            f"\nRegistered Discord user: `{_format_discord_identifier(row)}`"
            if row
            else ""
        )

        await interaction.followup.send(
            f"🌐 **{nation.nation_name}** (ID: `{nation_id}`, leader: {nation.leader_name})"
            f"{registered_part}",
            ephemeral=True,
        )
        return

    # ------------------------------------------------------------------
    # String query → look up Discord username in the database
    # ------------------------------------------------------------------
    row = bot.db.get_by_discord_username(query)
    if row is None:
        await interaction.followup.send(
            f"ℹ️ No registration found for Discord username `{query}`.", ephemeral=True
        )
        return

    nation_id = row["nation_id"]
    try:
        nation = await bot.pnw.get_nation(nation_id)
    except Exception:
        nation = None

    stored_name = _format_discord_identifier(row)
    if nation:
        await interaction.followup.send(
            f"**{stored_name}** is registered as nation "
            f"**{nation.nation_name}** (ID: `{nation_id}`, leader: {nation.leader_name}).",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"**{stored_name}** is registered with nation ID `{nation_id}` "
            f"(nation details unavailable).",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /whois
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="whois",
    description="Look up the registered nation for a Discord member.",
)
@app_commands.describe(member="The Discord member to look up.")
async def whois(interaction: discord.Interaction, member: discord.Member) -> None:
    await interaction.response.defer(ephemeral=True)

    row = bot.db.get_by_discord_id(member.id)
    if row is None:
        await interaction.followup.send(
            f"ℹ️ {member.mention} has not registered yet.", ephemeral=True
        )
        return

    nation_id = row["nation_id"]
    try:
        nation = await bot.pnw.get_nation(nation_id)
    except Exception:
        nation = None

    if nation:
        await interaction.followup.send(
            f"**{member.display_name}** is registered as nation "
            f"**{nation.nation_name}** (ID: `{nation_id}`, leader: {nation.leader_name}).",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            f"**{member.display_name}** is registered with nation ID `{nation_id}` "
            f"(nation details unavailable).",
            ephemeral=True,
        )


# ---------------------------------------------------------------------------
# /check_roles
# ---------------------------------------------------------------------------


@bot.tree.command(
    name="check_roles",
    description="Sync your bar3 roles so bar3 can verify your access.",
)
async def check_roles(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ This command must be used in a server.", ephemeral=True)
        return

    member = guild.get_member(interaction.user.id)
    if member is None:
        await interaction.followup.send("❌ Could not resolve your server membership.", ephemeral=True)
        return

    row = bot.db.get_by_discord_id(interaction.user.id)
    if row is None:
        await interaction.followup.send(
            "ℹ️ You have not registered yet. Use `/register` to link your nation first.",
            ephemeral=True,
        )
        return

    nation_id = row["nation_id"]

    # Fetch nation to confirm it still exists
    try:
        nation = await bot.pnw.get_nation(nation_id)
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Could not reach the PnW API: {exc}", ephemeral=True
        )
        return

    if nation is None:
        await interaction.followup.send(
            f"⚠️ Nation ID `{nation_id}` no longer exists. Roles were not updated.",
            ephemeral=True,
        )
        return

    added: list[str] = []
    already_had: list[str] = []

    # Ensure the Verified role is present
    verified_role = _get_role(guild, config.VERIFIED_ROLE_ID)
    if verified_role:
        if verified_role not in member.roles:
            try:
                await member.add_roles(verified_role, reason=f"{BOT_NAME}: /check_roles")
                added.append(verified_role.name)
            except discord.Forbidden:
                log.warning("Missing permission to assign role %s", verified_role)
        else:
            already_had.append(verified_role.name)

    # bar3 client role
    bar3_client_role = _get_role(guild, config.BAR3_CLIENT_ROLE_ID)
    if bar3_client_role:
        if bar3_client_role not in member.roles:
            try:
                await member.add_roles(bar3_client_role, reason=f"{BOT_NAME}: /check_roles")
                added.append(bar3_client_role.name)
            except discord.Forbidden:
                log.warning("Missing permission to assign role %s", bar3_client_role)
        else:
            already_had.append(bar3_client_role.name)

    # bar3 server role
    bar3_server_role = _get_role(guild, config.BAR3_SERVER_ROLE_ID)
    if bar3_server_role:
        if bar3_server_role not in member.roles:
            try:
                await member.add_roles(bar3_server_role, reason=f"{BOT_NAME}: /check_roles")
                added.append(bar3_server_role.name)
            except discord.Forbidden:
                log.warning("Missing permission to assign role %s", bar3_server_role)
        else:
            already_had.append(bar3_server_role.name)

    parts: list[str] = [
        f"✅ Role check complete for {member.mention} "
        f"(nation: **{nation.nation_name}**, ID: `{nation_id}`)."
    ]
    if added:
        parts.append(f"**Added:** {', '.join(added)}")
    if already_had:
        parts.append(f"**Already had:** {', '.join(already_had)}")
    if not added and not already_had:
        parts.append("No configured roles to assign.")

    await interaction.followup.send("\n".join(parts), ephemeral=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    bot.run(config.DISCORD_TOKEN)
