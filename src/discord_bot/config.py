from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiscordBotConfig:
    token: str = field(repr=False)
    guild_id: int | None = None

    @classmethod
    def from_env(cls) -> "DiscordBotConfig":
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is not configured")

        raw_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
        try:
            guild_id = int(raw_guild_id) if raw_guild_id else None
        except ValueError as exc:
            raise ValueError("DISCORD_GUILD_ID must be a positive integer") from exc
        if guild_id is not None and guild_id <= 0:
            raise ValueError("DISCORD_GUILD_ID must be a positive integer")
        return cls(token=token, guild_id=guild_id)
