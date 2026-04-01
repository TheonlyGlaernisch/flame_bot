"""SQLite-backed storage for nation-to-Discord registrations."""
import sqlite3
from datetime import datetime, timezone
from typing import Optional


class Database:
    def __init__(self, db_path: str = "registrations.db") -> None:
        self.db_path = db_path
        self._init()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS registrations (
                    discord_id       TEXT PRIMARY KEY,
                    nation_id        INTEGER NOT NULL UNIQUE,
                    registered_at    TEXT NOT NULL
                )
                """
            )
            # Migration: add discord_username column if it doesn't exist yet.
            existing_cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(registrations)")
            }
            if "discord_username" not in existing_cols:
                conn.execute(
                    "ALTER TABLE registrations ADD COLUMN discord_username TEXT NOT NULL DEFAULT ''"
                )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def register(self, discord_id: int, nation_id: int, discord_username: str = "") -> None:
        """Insert or replace a registration entry."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO registrations (discord_id, nation_id, registered_at, discord_username)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(discord_id) DO UPDATE SET
                    nation_id        = excluded.nation_id,
                    registered_at    = excluded.registered_at,
                    discord_username = excluded.discord_username
                """,
                (str(discord_id), nation_id, now, discord_username),
            )

    def get_by_discord_id(self, discord_id: int) -> Optional[sqlite3.Row]:
        """Return the registration row for a Discord user, or None."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM registrations WHERE discord_id = ?",
                (str(discord_id),),
            ).fetchone()

    def get_by_nation_id(self, nation_id: int) -> Optional[sqlite3.Row]:
        """Return the registration row for a nation ID, or None."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM registrations WHERE nation_id = ?",
                (nation_id,),
            ).fetchone()

    def get_by_discord_username(self, username: str) -> Optional[sqlite3.Row]:
        """Return the registration row for a Discord username (case-insensitive), or None."""
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM registrations WHERE LOWER(discord_username) = LOWER(?)",
                (username.strip(),),
            ).fetchone()

    def delete(self, discord_id: int) -> bool:
        """Remove a registration. Returns True if a row was deleted."""
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM registrations WHERE discord_id = ?",
                (str(discord_id),),
            )
            return cursor.rowcount > 0
