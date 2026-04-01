# flame_bot

A Discord bot for the **bar3** client/server system that integrates with [Politics and War](https://politicsandwar.com).

## Features

| Command | Description |
|---|---|
| `/register <nation_id>` | Link your Discord account to your PnW nation (with verification) |
| `/whois <member>` | Look up the registered nation for a Discord member |
| `/check_roles <member>` *(admin)* | Re-evaluate and sync bar3 roles for a member |

### Verification flow (`/register`)

1. User runs `/register <their_nation_id>`.
2. The bot fetches the nation from the PnW API.
3. It checks that the nation's **in-game Discord field** matches the user's Discord username.
4. If it matches, the registration is stored and the **Verified** role is granted.
5. If it doesn't match, the user is told exactly what to fix on their nation page.

---

## Setup

### Prerequisites

- Python 3.12+
- A [Discord application/bot](https://discord.com/developers/applications) with the **Server Members** intent enabled
- A [Politics and War API key](https://politicsandwar.com/account)

### Install dependencies

```bash
pip install -r requirements.txt
```

### Configure environment variables

```bash
cp .env.example .env
# edit .env and fill in all values
```

| Variable | Required | Description |
|---|---|---|
| `DISCORD_TOKEN` | ✅ | Bot token from the Discord developer portal |
| `PNW_API_KEY` | ✅ | Your PnW API key |
| `GUILD_ID` | ✅ | The numeric ID of your Discord server |
| `VERIFIED_ROLE_ID` | ☑️ | Role assigned after a successful `/register` |
| `BAR3_CLIENT_ROLE_ID` | ☑️ | bar3 client role |
| `BAR3_SERVER_ROLE_ID` | ☑️ | bar3 server role |
| `DB_PATH` | ☑️ | SQLite file path (default: `registrations.db`) |

> ☑️ = optional but recommended

### Run the bot

```bash
python bot.py
```

---

## Running tests

```bash
pip install pytest pytest-asyncio
pytest tests/ -v
```

---

## Project layout

```
bot.py          – main entry point; all slash commands
config.py       – reads .env variables
database.py     – SQLite storage for registrations
pnw_api.py      – async Politics and War GraphQL client
tests/
  test_core.py  – unit tests for database and API helpers
.env.example    – environment variable template
requirements.txt
```
