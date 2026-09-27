from __future__ import annotations

import sqlite3
import threading
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RoomMessage:
    discord_message_id: str
    guild_id: str
    channel_id: str
    author_id: str
    author_name: str
    author_kind: str
    content: str
    created_at: str
    reply_to_message_id: str | None = None
    reply_to_author_id: str | None = None
    reply_to_author_name: str | None = None
    message_kind: str = "human"
    is_deleted: bool = False
    row_id: int | None = None


class DiscordRoomTimeline:
    """Persistent, transport-level record of the one configured Discord room."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS room_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_message_id TEXT NOT NULL UNIQUE,
                guild_id TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                author_id TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_kind TEXT NOT NULL,
                content TEXT NOT NULL,
                reply_to_message_id TEXT,
                reply_to_author_id TEXT,
                reply_to_author_name TEXT,
                created_at TEXT NOT NULL,
                message_kind TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_room_messages_room
                ON room_messages(guild_id, channel_id, id);
            CREATE TABLE IF NOT EXISTS room_state (
                room_key TEXT PRIMARY KEY,
                summary TEXT NOT NULL DEFAULT '',
                summary_through_row_id INTEGER NOT NULL DEFAULT 0,
                last_seen_discord_message_id TEXT,
                pause_until TEXT,
                last_attention_at TEXT,
                last_attention_action TEXT,
                last_attention_reason TEXT,
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._db.commit()
        if os.name != "nt":
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _room_key(guild_id: str, channel_id: str) -> str:
        return f"{guild_id}:{channel_id}"

    @staticmethod
    def _from_row(row: sqlite3.Row) -> RoomMessage:
        values = dict(row)
        values["row_id"] = values.pop("id")
        values["is_deleted"] = bool(values["is_deleted"])
        return RoomMessage(**values)

    def append_message(self, message: RoomMessage) -> bool:
        with self._lock:
            cursor = self._db.execute(
                """INSERT OR IGNORE INTO room_messages
                (discord_message_id,guild_id,channel_id,author_id,author_name,author_kind,
                 content,reply_to_message_id,reply_to_author_id,reply_to_author_name,
                 created_at,message_kind,is_deleted)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(message.discord_message_id), str(message.guild_id), str(message.channel_id),
                    str(message.author_id), str(message.author_name)[:120], str(message.author_kind),
                    str(message.content)[:4000], message.reply_to_message_id,
                    message.reply_to_author_id, message.reply_to_author_name,
                    str(message.created_at), str(message.message_kind), int(message.is_deleted),
                ),
            )
            self._db.commit()
            return cursor.rowcount == 1

    def update_message(self, discord_message_id: str, *, content: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT id,guild_id,channel_id FROM room_messages WHERE discord_message_id=? AND is_deleted=0",
                (str(discord_message_id),),
            ).fetchone()
            cursor = self._db.execute(
                "UPDATE room_messages SET content=? WHERE discord_message_id=? AND is_deleted=0",
                (str(content)[:4000], str(discord_message_id)),
            )
            if row is not None:
                self._invalidate_summary_if_covered(row)
            self._db.commit()
            return cursor.rowcount == 1

    def mark_deleted(self, discord_message_id: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT id,guild_id,channel_id FROM room_messages WHERE discord_message_id=? AND is_deleted=0",
                (str(discord_message_id),),
            ).fetchone()
            cursor = self._db.execute(
                "UPDATE room_messages SET is_deleted=1 WHERE discord_message_id=? AND is_deleted=0",
                (str(discord_message_id),),
            )
            if row is not None:
                self._invalidate_summary_if_covered(row)
            self._db.commit()
            return cursor.rowcount == 1

    def _invalidate_summary_if_covered(self, message_row: sqlite3.Row) -> None:
        state = self._state(message_row["guild_id"], message_row["channel_id"])
        if state and int(message_row["id"]) <= int(state["summary_through_row_id"] or 0):
            self._db.execute(
                "UPDATE room_state SET summary='', summary_through_row_id=0 WHERE room_key=?",
                (self._room_key(message_row["guild_id"], message_row["channel_id"]),),
            )

    def recent(
        self, *, guild_id: str, channel_id: str, limit: int = 40,
        after_row_id: int | None = None, exclude_ids: set[str] | None = None,
    ) -> list[RoomMessage]:
        clauses = ["guild_id=?", "channel_id=?", "is_deleted=0"]
        params: list[Any] = [str(guild_id), str(channel_id)]
        if after_row_id is not None:
            clauses.append("id > ?")
            params.append(int(after_row_id))
        excluded = sorted(str(value) for value in (exclude_ids or set()))
        if excluded:
            clauses.append(f"discord_message_id NOT IN ({','.join('?' for _ in excluded)})")
            params.extend(excluded)
        params.append(max(1, min(200, int(limit))))
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM room_messages WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._from_row(row) for row in reversed(rows)]

    def count_messages(self, *, guild_id: str, channel_id: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) AS n FROM room_messages WHERE guild_id=? AND channel_id=? AND is_deleted=0",
                (str(guild_id), str(channel_id)),
            ).fetchone()
        return int(row["n"])

    def count_unsummarized(self, *, guild_id: str, channel_id: str) -> int:
        with self._lock:
            anchor = self.summary_through_row_id(guild_id=guild_id, channel_id=channel_id)
            row = self._db.execute(
                """SELECT COUNT(*) AS n FROM room_messages WHERE guild_id=? AND channel_id=?
                   AND is_deleted=0 AND id>?""",
                (str(guild_id), str(channel_id), anchor),
            ).fetchone()
        return int(row["n"])

    def _state(self, guild_id: str, channel_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM room_state WHERE room_key=?",
            (self._room_key(str(guild_id), str(channel_id)),),
        ).fetchone()

    def get_summary(self, *, guild_id: str, channel_id: str) -> str:
        with self._lock:
            state = self._state(guild_id, channel_id)
            return str(state["summary"] or "") if state else ""

    def summary_through_row_id(self, *, guild_id: str, channel_id: str) -> int:
        with self._lock:
            state = self._state(guild_id, channel_id)
            return int(state["summary_through_row_id"] or 0) if state else 0

    def messages_for_summary(
        self, *, guild_id: str, channel_id: str, keep_recent: int = 30, max_items: int = 50,
    ) -> list[RoomMessage]:
        with self._lock:
            anchor = self.summary_through_row_id(guild_id=guild_id, channel_id=channel_id)
            rows = self._db.execute(
                """SELECT * FROM room_messages WHERE guild_id=? AND channel_id=? AND is_deleted=0
                   AND id>? AND id < COALESCE((
                     SELECT id FROM room_messages WHERE guild_id=? AND channel_id=? AND is_deleted=0
                     ORDER BY id DESC LIMIT 1 OFFSET ?
                   ), 9223372036854775807)
                   ORDER BY id ASC LIMIT ?""",
                (str(guild_id), str(channel_id), anchor, str(guild_id), str(channel_id),
                 max(0, int(keep_recent) - 1), max(1, min(100, int(max_items)))),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def commit_summary(self, *, guild_id: str, channel_id: str, summary: str, through_row_id: int) -> None:
        import datetime

        with self._lock:
            self._db.execute(
                """INSERT INTO room_state(room_key,summary,summary_through_row_id,updated_at)
                   VALUES(?,?,?,?) ON CONFLICT(room_key) DO UPDATE SET
                   summary=excluded.summary, summary_through_row_id=excluded.summary_through_row_id,
                   updated_at=excluded.updated_at""",
                (self._room_key(str(guild_id), str(channel_id)), str(summary)[:12000], int(through_row_id),
                 datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            self._db.commit()

    def last_seen_message_id(self, *, guild_id: str, channel_id: str) -> str | None:
        with self._lock:
            state = self._state(guild_id, channel_id)
            return str(state["last_seen_discord_message_id"] or "") or None if state else None

    def set_last_seen_message_id(self, *, guild_id: str, channel_id: str, message_id: str) -> None:
        import datetime

        with self._lock:
            current = self._state(guild_id, channel_id)
            existing_id = str(current["last_seen_discord_message_id"] or "") if current else ""
            incoming_id = str(message_id)
            if existing_id and incoming_id.isdigit() and existing_id.isdigit() and int(incoming_id) <= int(existing_id):
                return
            self._db.execute(
                """INSERT INTO room_state(room_key,last_seen_discord_message_id,updated_at)
                   VALUES(?,?,?) ON CONFLICT(room_key) DO UPDATE SET
                   last_seen_discord_message_id=excluded.last_seen_discord_message_id,
                   updated_at=excluded.updated_at""",
                (self._room_key(str(guild_id), str(channel_id)), incoming_id,
                 datetime.datetime.now(datetime.timezone.utc).isoformat()),
            )
            self._db.commit()

    def last_human_message(self, *, guild_id: str, channel_id: str) -> RoomMessage | None:
        return self._last_by_kind(guild_id, channel_id, "human")

    def get_message(self, discord_message_id: str) -> RoomMessage | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM room_messages WHERE discord_message_id=?",
                (str(discord_message_id),),
            ).fetchone()
        return self._from_row(row) if row else None

    def last_mita_message(self, *, guild_id: str, channel_id: str) -> RoomMessage | None:
        return self._last_by_kind(guild_id, channel_id, "mita")

    def _last_by_kind(self, guild_id: str, channel_id: str, kind: str) -> RoomMessage | None:
        with self._lock:
            row = self._db.execute(
                """SELECT * FROM room_messages WHERE guild_id=? AND channel_id=?
                   AND author_kind=? AND is_deleted=0 ORDER BY id DESC LIMIT 1""",
                (str(guild_id), str(channel_id), kind),
            ).fetchone()
        return self._from_row(row) if row else None

    def count_mita_messages_since(self, *, guild_id: str, channel_id: str, since: str) -> int:
        with self._lock:
            row = self._db.execute(
                """SELECT COUNT(*) AS n FROM room_messages WHERE guild_id=? AND channel_id=?
                   AND author_kind='mita' AND message_kind!='mita_direct' AND created_at>=?""",
                (str(guild_id), str(channel_id), str(since)),
            ).fetchone()
        return int(row["n"])

    def human_messages_after(self, *, guild_id: str, channel_id: str, row_id: int) -> int:
        with self._lock:
            row = self._db.execute(
                """SELECT COUNT(*) AS n FROM room_messages WHERE guild_id=? AND channel_id=?
                   AND author_kind='human' AND is_deleted=0 AND id>?""",
                (str(guild_id), str(channel_id), int(row_id)),
            ).fetchone()
        return int(row["n"])

    def state(self, *, guild_id: str, channel_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._state(guild_id, channel_id)
            return dict(row) if row else {}

    def update_presence_state(self, *, guild_id: str, channel_id: str, **values: Any) -> None:
        import datetime

        allowed = {"pause_until", "last_attention_at", "last_attention_action", "last_attention_reason"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        updates["updated_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO room_state(room_key) VALUES(?)",
                (self._room_key(str(guild_id), str(channel_id)),),
            )
            assignment = ",".join(f"{key}=?" for key in updates)
            self._db.execute(
                f"UPDATE room_state SET {assignment} WHERE room_key=?",
                (*updates.values(), self._room_key(str(guild_id), str(channel_id))),
            )
            self._db.commit()

    def clear_room(self, *, guild_id: str, channel_id: str, preserve_last_seen: str | None = None) -> int:
        with self._lock:
            cursor = self._db.execute(
                "DELETE FROM room_messages WHERE guild_id=? AND channel_id=?",
                (str(guild_id), str(channel_id)),
            )
            room_key = self._room_key(str(guild_id), str(channel_id))
            self._db.execute(
                """INSERT OR IGNORE INTO room_state(room_key) VALUES(?)""", (room_key,),
            )
            self._db.execute(
                "UPDATE room_state SET summary='', summary_through_row_id=0 WHERE room_key=?",
                (room_key,),
            )
            self._db.commit()
            if preserve_last_seen:
                self.set_last_seen_message_id(
                    guild_id=guild_id, channel_id=channel_id, message_id=preserve_last_seen,
                )
            return cursor.rowcount

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
