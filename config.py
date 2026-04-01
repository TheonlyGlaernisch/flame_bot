import os
from dotenv import load_dotenv

load_dotenv()


_PLACEHOLDER_PREFIXES = ("your_",)


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise EnvironmentError(f"Required environment variable '{key}' is not set.")
    if any(value.lower().startswith(p) for p in _PLACEHOLDER_PREFIXES):
        raise EnvironmentError(
            f"Environment variable '{key}' still contains a placeholder value. "
            "Replace it with a real value in your .env file."
        )
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

_MONGODB_PASSWORD: str = _require("MONGODB_PASSWORD")
MONGODB_URI: str = (
    f"mongodb+srv://glaernischgaming_db_user:{_MONGODB_PASSWORD}"
    "@glaernisch.0o1fjdx.mongodb.net/?appName=Glaernisch"
)

# HTTP API for bar3 integration
# If API_KEY is not set the API server will not start.
API_KEY: str | None = os.getenv("API_KEY") or None
API_PORT: int = int(os.getenv("API_PORT", "8080"))
