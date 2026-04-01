import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    return value


def _optional_int(key: str) -> int | None:
    value = os.getenv(key)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as e:
        raise EnvironmentError(
            f"Environment variable '{key}' must be an integer."
        ) from e


DISCORD_TOKEN: str = _require("DISCORD_TOKEN")
PNW_API_KEY: str = _require("PNW_API_KEY")
GUILD_ID: int = int(_require("GUILD_ID"))

VERIFIED_ROLE_ID: int | None = _optional_int("VERIFIED_ROLE_ID")
BAR3_CLIENT_ROLE_ID: int | None = _optional_int("BAR3_CLIENT_ROLE_ID")
BAR3_SERVER_ROLE_ID: int | None = _optional_int("BAR3_SERVER_ROLE_ID")

DB_PATH: str = os.getenv("DB_PATH", "registrations.db")
