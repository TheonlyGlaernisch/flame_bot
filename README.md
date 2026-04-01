# flame_bot

A Discord bot for the **bar3** client/server system that integrates with [Politics and War](https://politicsandwar.com).

## Features

| Command | Description |
|---|---|
| `/register <nation_id>` | Link your Discord account to your PnW nation (with verification) |
| `/who <query>` | Numeric ID → fetch nation from PnW API; Discord username → look up in database |
| `/whois <member>` | Look up the registered nation for a Discord member |
| `/check_roles` | Sync your bar3 roles — bar3 uses this to verify your access when you log in with Discord |

### bar3 HTTP API

When `API_KEY` is set, the bot exposes a small HTTP API on `API_PORT` (default `8080`) that
the bar3 website can call after a user logs in via Discord OAuth.

**`GET /api/roles/{discord_id}`**

Query the role status for a Discord user ID.

```
curl -H "X-API-Key: <your_api_key>" http://localhost:8080/api/roles/123456789
```

```json
{
  "discord_id": "123456789",
  "registered": true,
  "roles": {
    "verified":    true,
    "bar3_client": true,
    "bar3_server": false
  }
}
```

| Field | Description |
|---|---|
| `registered` | Whether the user has linked a PnW nation via `/register` |
| `roles.verified` | Whether the user holds the `VERIFIED_ROLE_ID` Discord role |
| `roles.bar3_client` | Whether the user holds the `BAR3_CLIENT_ROLE_ID` Discord role |
| `roles.bar3_server` | Whether the user holds the `BAR3_SERVER_ROLE_ID` Discord role |

Error responses: `401 Unauthorized` (missing/wrong key), `400 Bad Request` (invalid ID).

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
| `API_KEY` | ☑️ | Secret key for the bar3 HTTP API; if unset the API server does not start |
| `API_PORT` | ☑️ | Port for the bar3 HTTP API (default: `8080`) |

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
bot.py          – main entry point; all slash commands; starts the API server
api.py          – aiohttp HTTP API server for bar3 integration
config.py       – reads .env variables
database.py     – SQLite storage for registrations
pnw_api.py      – async Politics and War GraphQL client
tests/
  test_core.py  – unit tests for database and API helpers
  test_api.py   – unit tests for the bar3 HTTP API
.env.example    – environment variable template
requirements.txt
```
