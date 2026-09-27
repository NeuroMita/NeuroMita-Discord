from __future__ import annotations

import os
from dataclasses import dataclass, field


DEFAULT_GUILD_ID = 1341427480942350356
DEFAULT_CHANNEL_ID = 1353745092065624144
DEFAULT_ADMIN_IDS = frozenset({
    292002437932384256,
    418100424583675904,
    446312390271696927,
})
REQUIRED_ADMIN_IDS = DEFAULT_ADMIN_IDS


@dataclass(frozen=True)
class DiscordBotConfig:
    token: str = field(repr=False)
    guild_id: int = DEFAULT_GUILD_ID
    channel_id: int = DEFAULT_CHANNEL_ID
    admin_ids: frozenset[int] = DEFAULT_ADMIN_IDS

    def is_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self.admin_ids

    @classmethod
    def from_env(cls) -> "DiscordBotConfig":
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("DISCORD_BOT_TOKEN is not configured")

        raw_guild_id = os.environ.get("DISCORD_GUILD_ID", str(DEFAULT_GUILD_ID)).strip()
        try:
            guild_id = int(raw_guild_id)
        except ValueError as exc:
            raise ValueError("DISCORD_GUILD_ID must be a positive integer") from exc
        if guild_id <= 0:
            raise ValueError("DISCORD_GUILD_ID must be a positive integer")

        raw_channel_id = os.environ.get("DISCORD_CHANNEL_ID", str(DEFAULT_CHANNEL_ID)).strip()
        try:
            channel_id = int(raw_channel_id)
        except ValueError as exc:
            raise ValueError("DISCORD_CHANNEL_ID must be a positive integer") from exc
        if channel_id <= 0:
            raise ValueError("DISCORD_CHANNEL_ID must be a positive integer")

        raw_admin_ids = os.environ.get("DISCORD_ADMIN_IDS", "").strip()
        try:
            configured_admin_ids = frozenset(
                int(item.strip()) for item in raw_admin_ids.split(",") if item.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "DISCORD_ADMIN_IDS must be a comma-separated list of positive integers"
            ) from exc
        if any(value <= 0 for value in configured_admin_ids):
            raise ValueError(
                "DISCORD_ADMIN_IDS must be a comma-separated list of positive integers"
            )
        admin_ids = REQUIRED_ADMIN_IDS | configured_admin_ids

        return cls(
            token=token,
            guild_id=guild_id,
            channel_id=channel_id,
            admin_ids=admin_ids,
        )
