from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.config import DiscordBotConfig
from discord_bot.config import DEFAULT_CHANNEL_ID, DEFAULT_GUILD_ID
from discord_bot.diagnostics import DiscordRuntimeDiagnostics
from discord_bot.response_formatter import split_response
from discord_bot.character_runtime import DiscordCharacterRuntime
from discord_bot.admin_facade import DiscordAdminFacade
from discord_bot.attention import DiscordAttentionService
from discord_bot.presence_controller import DiscordPresenceController
from discord_bot.presence_settings import PresenceSettingsStore
from discord_bot.room_context import DiscordRoomContextBuilder
from discord_bot.room_timeline import DiscordRoomTimeline, RoomMessage
from discord_bot.presence_commands import register_presence_commands


logger = logging.getLogger(__name__)
BUSY_REPLY = "A request is already running. Please try again shortly."


class GenerationBusyError(RuntimeError):
    """Raised when the single Discord generation slot is occupied."""


class UtilityBusyError(RuntimeError):
    """Raised instead of queueing unbounded Discord utility work."""


def extract_mention_prompt(content: str, bot_id: int) -> str | None:
    cleaned = re.sub(rf"<@!?{bot_id}>", " ", content)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def discord_sender(user: discord.abc.User) -> str:
    display = str(
        getattr(user, "global_name", None)
        or getattr(user, "display_name", None)
        or getattr(user, "name", "Discord user")
    ).strip()
    return f"Discord:{display[:80]} [id:{int(user.id)}]"


def should_respond_to_message(
    *,
    author_is_bot: bool,
    webhook_id: int | None,
    is_dm: bool,
    is_mentioned: bool,
    is_reply_to_bot: bool,
    guild_id: int | None = None,
    channel_id: int | None = None,
    is_thread: bool = False,
    config: DiscordBotConfig | None = None,
) -> bool:
    if author_is_bot or webhook_id is not None:
        return False
    if is_dm or not is_allowed_location(guild_id, channel_id, is_thread=is_thread, config=config):
        return False
    return is_mentioned or is_reply_to_bot


def is_allowed_location(
    guild_id: int | None,
    channel_id: int | None,
    *,
    is_thread: bool = False,
    config: DiscordBotConfig | None = None,
) -> bool:
    config = config or DiscordBotConfig(token="")
    return (
        not is_thread
        and guild_id == config.guild_id
        and channel_id == config.channel_id
    )


class DiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        token: str,
        runtime: DiscordCharacterRuntime,
        guild_id: int = DEFAULT_GUILD_ID,
        channel_id: int = DEFAULT_CHANNEL_ID,
        admin_ids: frozenset[int] | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.token = token
        self.runtime = runtime
        data_dir = getattr(runtime, "data_dir", None)
        self.diagnostics = (
            DiscordRuntimeDiagnostics(Path(data_dir) / "Logs")
            if data_dir is not None else None
        )
        self.admin = DiscordAdminFacade(runtime)
        self.started_at = time.monotonic()
        self.guild_id = guild_id
        self.config = DiscordBotConfig(
            token=token,
            guild_id=guild_id,
            channel_id=channel_id,
            admin_ids=admin_ids if admin_ids is not None else DiscordBotConfig(token="").admin_ids,
        )
        if self.diagnostics is not None:
            self.diagnostics.record(
                "bot_created", guild_id=guild_id, channel_id=channel_id,
            )
        self._generation_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-generation"
        )
        self._utility_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-utility"
        )
        self._generation_busy = threading.Event()
        self._utility_busy = threading.Event()
        self._closed = False
        self._managed_run = False
        self.presence = None
        self.room_timeline = None
        self.presence_settings = None
        self._room_channel = None
        self._room_catchup_done = asyncio.Event()
        if self.presence is None:
            self._room_catchup_done.set()
        if hasattr(runtime, "data_dir") and hasattr(runtime, "generate_utility"):
            data_dir = Path(runtime.data_dir)
            self.room_timeline = DiscordRoomTimeline(data_dir / "room.sqlite3")
            self.presence_settings = PresenceSettingsStore(data_dir / "Settings" / "presence.json")
            context_builder = DiscordRoomContextBuilder(
                self.room_timeline, guild_id=str(guild_id), channel_id=str(channel_id),
            )
            attention = DiscordAttentionService(
                runtime,
                character_id_provider=lambda: runtime.active_character_id,
                preset_id_provider=lambda: runtime.settings.get("DISCORD_ATTENTION_PRESET_ID", None),
            )
            self.presence = DiscordPresenceController(
                timeline=self.room_timeline,
                context_builder=context_builder,
                attention=attention,
                settings=self.presence_settings,
                run_worker=self._run_presence_worker,
                generate_social=self._generate_social,
                generate_initiative=self._generate_initiative,
                observe_room=self._observe_room,
                send_public_message=self._send_presence_message,
                is_generation_busy=lambda: self.generation_busy,
                is_utility_busy=lambda: self.utility_busy,
                diagnostics=self.diagnostics,
                guild_id=str(guild_id),
                channel_id=str(channel_id),
            )

        chat = app_commands.Group(name="chat", description="Chat with NeuroMita")

        @chat.command(name="ask", description="Send a message to the assistant")
        @app_commands.describe(text="Your message")
        async def ask(interaction: discord.Interaction, text: str) -> None:
            if not is_allowed_location(
                interaction.guild_id,
                interaction.channel_id,
                config=self.config,
            ):
                await interaction.response.send_message(
                    "This bot is not enabled in this channel.", ephemeral=True
                )
                return
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "message_received", guild_id=interaction.guild_id,
                    channel_id=interaction.channel_id, trigger="slash",
                )
            await interaction.response.defer(thinking=True)
            if self.presence is not None:
                await self.presence.on_human_message(RoomMessage(
                    discord_message_id=str(interaction.id),
                    guild_id=str(interaction.guild_id),
                    channel_id=str(interaction.channel_id),
                    author_id=str(interaction.user.id),
                    author_name=str(getattr(interaction.user, "display_name", interaction.user.name)),
                    author_kind="human",
                    content=str(text)[:4000],
                    created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    message_kind="human",
                ), direct=True)
                self.presence.notify_direct_generation_started()
            if self.diagnostics is not None:
                self.diagnostics.record("direct_generation_started")
            try:
                response = await self._generate(
                    text,
                    sender=discord_sender(interaction.user),
                    request_id=str(interaction.id),
                    ambient_context=(
                        self.presence.context_builder.build(exclude_message_ids={str(interaction.id)})
                        if self.presence else ""
                    ),
                )
            except GenerationBusyError:
                await interaction.edit_original_response(content=BUSY_REPLY)
                return
            finally:
                if self.presence is not None:
                    self.presence.notify_direct_generation_finished()
            chunks = split_response(self._response_text(response))
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "direct_generation_finished", response_chars=sum(map(len, chunks)), chunk_count=len(chunks),
                )
            first = await interaction.edit_original_response(
                content=chunks[0], allowed_mentions=discord.AllowedMentions.none()
            )
            if self.presence is not None and first is not None:
                await self.presence.on_mita_message(self._room_message(
                    first, author_kind="mita", message_kind="mita_direct",
                ))
            for chunk in chunks[1:]:
                sent = await interaction.followup.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none(), wait=True
                )
                if self.presence is not None and sent is not None:
                    await self.presence.on_mita_message(self._room_message(
                        sent, author_kind="mita", message_kind="mita_direct",
                    ))
            if self.diagnostics is not None:
                self.diagnostics.record("direct_reply_sent", chunk_count=len(chunks))

        self.tree.add_command(chat)

        bot_group = app_commands.Group(name="bot", description="Bot administration")
        @bot_group.command(name="status", description="Show bot runtime status")
        async def bot_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            status = self.admin.bot_status()
            await interaction.response.send_message(
                f"State: {status.get('state')} | Character: {status.get('character_id')}\n"
                f"RAG: {status.get('rag_preset')} | Prompt assets: {status.get('prompt_assets_available')}",
                ephemeral=True,
            )

        @bot_group.command(name="uptime", description="Show bot uptime")
        async def bot_uptime(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            seconds = max(0, int(time.monotonic() - self.started_at))
            await interaction.response.send_message(
                f"Uptime: {seconds // 3600}h {(seconds % 3600) // 60}m {seconds % 60}s",
                ephemeral=True,
            )

        @bot_group.command(name="resources", description="Show process memory use")
        async def bot_resources(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            usage = self.admin.resource_usage()
            await interaction.response.send_message(
                f"Process RSS: {usage['rss_mb']:.1f} MiB", ephemeral=True
            )

        @bot_group.command(name="health", description="Show runtime health")
        async def bot_health(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            status = self.admin.bot_status()
            await interaction.response.send_message(
                f"Runtime ready: {status.get('runtime_ready')} | state: {status.get('state')} | "
                f"prompt assets: {status.get('prompt_assets_available')}", ephemeral=True
            )

        character_group = app_commands.Group(name="character", description="Character administration")
        @character_group.command(name="list", description="List available characters")
        async def character_list(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            lines = [f"{item['id']}: {item['name']}" for item in self.admin.characters()]
            await interaction.response.send_message("\n".join(lines)[:1900] or "No characters.", ephemeral=True)

        @character_group.command(name="status", description="Show active character")
        async def character_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            character = self.admin.character_status()
            await interaction.response.send_message(
                f"{character['id']} | {character['name']} | prompt set: {character['prompt_set']}",
                ephemeral=True,
            )

        @character_group.command(name="set", description="Switch the active character")
        @app_commands.describe(character_id="Character ID")
        async def character_set(interaction: discord.Interaction, character_id: str) -> None:
            if not await self._require_admin(interaction):
                return
            ok = self.admin.character_set(character_id.strip())
            await interaction.response.send_message(
                "Character switched." if ok else "Unknown character ID.", ephemeral=True
            )

        @character_group.command(name="reset-all", description="Clear character history, memories, state, and graph")
        @app_commands.describe(confirm="Confirm clearing history, memories, state, and graph data")
        async def character_reset_all(interaction: discord.Interaction, confirm: bool) -> None:
            if not await self._require_admin(interaction):
                return
            done = self.admin.character_reset_all(confirm=confirm)
            await interaction.response.send_message(
                "Character history, memories, state, and graph cleared."
                if done else "Set confirm=true to clear history, memories, state, and graph data.",
                ephemeral=True,
            )

        history_group = app_commands.Group(name="history", description="Character history administration")
        @history_group.command(name="status", description="Show history and summary status")
        async def history_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            items = self.admin.history_recent(20)
            summary = self.admin.history_summary()
            await interaction.response.send_message(
                f"Messages in current window: {len(items)} | Summary chars: {len(summary)}",
                ephemeral=True,
            )

        @history_group.command(name="summary", description="Show the current character summary")
        async def history_summary(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            summary = self.admin.history_summary()
            await interaction.response.send_message(
                (summary[:1900] or "No summary."), ephemeral=True
            )

        @history_group.command(name="recent", description="Show recent history messages")
        @app_commands.describe(limit="Number of messages, maximum 20")
        async def history_recent(interaction: discord.Interaction, limit: int = 10) -> None:
            if not await self._require_admin(interaction):
                return
            items = self.admin.history_recent(limit)
            lines = [f"{item.get('role', '?')}: {str(item.get('content', ''))[:300]}" for item in items]
            await interaction.response.send_message("\n".join(lines)[-1900:] or "History is empty.", ephemeral=True)

        memory_group = app_commands.Group(name="memory", description="Character memory administration")
        @memory_group.command(name="list", description="List saved memories")
        @app_commands.describe(limit="Number of memories, maximum 20")
        async def memory_list(interaction: discord.Interaction, limit: int = 10) -> None:
            if not await self._require_admin(interaction):
                return
            items = self.admin.memory_list(limit)
            lines = [f"#{item.get('eternal_id')}: {str(item.get('content', ''))[:250]}" for item in items]
            await interaction.response.send_message("\n".join(lines)[-1900:] or "No memories.", ephemeral=True)

        @memory_group.command(name="show", description="Show one saved memory")
        @app_commands.describe(memory_id="Memory ID")
        async def memory_show(interaction: discord.Interaction, memory_id: int) -> None:
            if not await self._require_admin(interaction):
                return
            content = self.admin.memory_show(memory_id)
            await interaction.response.send_message(
                (str(content)[:1900] if content is not None else "Memory not found."), ephemeral=True
            )

        @memory_group.command(name="status", description="Show memory and lexical search status")
        async def memory_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            character = self.admin.character_status()
            status = self.admin.bot_status()
            await interaction.response.send_message(
                f"Character: {character['id']} | memories: {len(self.admin.memory_list(50))} shown max | "
                f"RAG: {status.get('rag_preset')}", ephemeral=True,
            )

        @memory_group.command(name="maintenance", description="Run existing character memory maintenance")
        @app_commands.describe(confirm="Confirm duplicate-memory merging")
        async def memory_maintenance(interaction: discord.Interaction, confirm: bool) -> None:
            if not await self._require_admin(interaction):
                return
            if not confirm:
                await interaction.response.send_message(
                    "Set confirm=true to merge duplicate memories.", ephemeral=True
                )
                return
            result = self.admin.memory_maintenance(confirm=True)
            await interaction.response.send_message(str(result)[:1900], ephemeral=True)

        @memory_group.command(name="add", description="Add a character memory")
        @app_commands.describe(content="Fact to remember")
        async def memory_add(interaction: discord.Interaction, content: str) -> None:
            if not await self._require_admin(interaction):
                return
            memory_id = self.admin.memory_add(content)
            await interaction.response.send_message(
                f"Memory added (#{memory_id})." if memory_id is not None else "Memory was not added.",
                ephemeral=True,
            )

        @memory_group.command(name="delete", description="Soft-delete a character memory")
        @app_commands.describe(memory_id="Memory ID", confirm="Confirm deletion")
        async def memory_delete(interaction: discord.Interaction, memory_id: int, confirm: bool) -> None:
            if not await self._require_admin(interaction):
                return
            result = self.admin.memory_delete(memory_id, confirm=confirm)
            await interaction.response.send_message(
                "Memory deleted." if result["deleted"] else "Set confirm=true and verify the memory ID.",
                ephemeral=True,
            )

        ai_group = app_commands.Group(name="ai", description="AI provider status")
        @ai_group.command(name="status", description="Show configured primary and fallback presets")
        async def ai_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            chain = self.admin.ai_status()["chain"]
            lines = [f"{p['name']} | {p['provider']} | {p['model']}" for p in chain]
            await interaction.response.send_message("\n".join(lines)[-1900:] or "No API presets configured.", ephemeral=True)

        self.tree.add_command(bot_group)
        self.tree.add_command(character_group)
        self.tree.add_command(history_group)
        self.tree.add_command(memory_group)
        self.tree.add_command(ai_group)

        compression_group = app_commands.Group(name="compression", description="History compression settings")
        @compression_group.command(name="status", description="Show history compression state")
        async def compression_status(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            state = self.admin.compression_status()
            await interaction.response.send_message(
                f"Enabled: {state['enabled']} | Provider: {state['provider']}", ephemeral=True
            )

        @compression_group.command(name="on", description="Enable automatic history compression")
        async def compression_on(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            self.admin.compression_set(True)
            await interaction.response.send_message("Compression enabled.", ephemeral=True)

        @compression_group.command(name="off", description="Disable automatic history compression")
        async def compression_off(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            self.admin.compression_set(False)
            await interaction.response.send_message("Compression disabled.", ephemeral=True)

        self.tree.add_command(compression_group)

        debug_group = app_commands.Group(name="debug", description="Runtime diagnostics")
        @debug_group.command(name="health", description="Show runtime health details")
        async def debug_health(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            status = self.admin.bot_status()
            await interaction.edit_original_response(
                content=f"{status.get('message')} | character: {status.get('character_id')}",
            )

        @debug_group.command(name="last-error", description="Show the last safe error summary")
        async def debug_last_error(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            error = self.admin.bot_status().get("last_error") or "No recorded errors."
            await interaction.edit_original_response(content=str(error)[:1500])

        self.tree.add_command(debug_group)
        register_presence_commands(self)

    async def _require_admin(self, interaction: discord.Interaction) -> bool:
        allowed = is_allowed_location(
            interaction.guild_id,
            interaction.channel_id,
            config=self.config,
        ) and self.config.is_admin(interaction.user.id)
        if not allowed:
            await interaction.response.send_message("Administrator access is required here.", ephemeral=True)
        return allowed

    async def setup_hook(self) -> None:
        try:
            if self.guild_id is not None:
                guild = discord.Object(id=self.guild_id)
                self.tree.copy_global_to(guild=guild)
                commands_synced = await self.tree.sync(guild=guild)
            else:
                commands_synced = await self.tree.sync()
            if self.diagnostics is not None:
                self.diagnostics.record("commands_synced", count=len(commands_synced))
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record("command_sync_failed", exception_type=type(exc).__name__)
            raise

    async def on_ready(self) -> None:
        if self.diagnostics is not None:
            self.diagnostics.record("gateway_ready", bot_id=getattr(self.user, "id", None))
        if self.presence is None:
            return
        try:
            self._room_catchup_done.clear()
            channel = self.get_channel(self.config.channel_id)
            if channel is None:
                channel = await self.fetch_channel(self.config.channel_id)
            self._room_channel = channel
            if self.diagnostics is not None:
                self.diagnostics.record("channel_ready", channel_id=getattr(channel, "id", None))
            await self._catch_up_room(channel)
            await self.presence.start()
            if self.diagnostics is not None:
                self.diagnostics.record("presence_ready")
            logger.info("Discord room presence is ready")
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record("presence_start_failed", exception_type=type(exc).__name__)
            logger.exception("Could not initialize Discord room presence")
        finally:
            self._room_catchup_done.set()

    async def _catch_up_room(self, channel) -> None:
        if self.presence is None or self.user is None:
            return
        last_seen = self.room_timeline.last_seen_message_id(
            guild_id=str(self.config.guild_id), channel_id=str(self.config.channel_id),
        )
        cursor = int(last_seen) if last_seen else None
        first_batch = True
        while first_batch or last_seen:
            history = (
                channel.history(limit=100, oldest_first=False)
                if cursor is None
                else channel.history(after=discord.Object(id=cursor), limit=1000, oldest_first=True)
            )
            fetched = [message async for message in history]
            if cursor is None:
                fetched.reverse()
            if not fetched:
                break
            for message in fetched:
                cursor = int(message.id)
                self.room_timeline.set_last_seen_message_id(
                    guild_id=str(self.config.guild_id), channel_id=str(self.config.channel_id),
                    message_id=str(message.id),
                )
                if message.webhook_id is not None:
                    continue
                if message.author.id == self.user.id:
                    kind, message_kind = "mita", "mita_social"
                elif message.author.bot:
                    continue
                else:
                    kind, message_kind = "human", "human"
                if not message.content.strip():
                    continue
                self.room_timeline.append_message(self._room_message(
                    message, author_kind=kind, message_kind=message_kind,
                ))
            if not last_seen or len(fetched) < 1000:
                break
            last_seen = str(cursor)
            first_batch = False

    def _room_message(self, message, *, author_kind: str, message_kind: str) -> RoomMessage:
        reply_id = None
        reply_author_id = None
        reply_author_name = None
        reference = getattr(message, "reference", None)
        if reference is not None and getattr(reference, "message_id", None) is not None:
            reply_id = str(reference.message_id)
            resolved = getattr(reference, "resolved", None)
            if isinstance(resolved, discord.Message):
                reply_author_id = str(resolved.author.id)
                reply_author_name = str(resolved.author.display_name)
            elif self.room_timeline is not None:
                prior = self.room_timeline.get_message(reply_id)
                if prior is not None:
                    reply_author_id = prior.author_id
                    reply_author_name = prior.author_name
        return RoomMessage(
            discord_message_id=str(message.id),
            guild_id=str(getattr(getattr(message, "guild", None), "id", self.config.guild_id)),
            channel_id=str(message.channel.id),
            author_id=str(message.author.id),
            author_name=str(getattr(message.author, "display_name", message.author.name)),
            author_kind=author_kind,
            content=str(message.content or ""),
            reply_to_message_id=reply_id,
            reply_to_author_id=reply_author_id,
            reply_to_author_name=reply_author_name,
            created_at=message.created_at.astimezone(dt.timezone.utc).isoformat(),
            message_kind=message_kind,
        )

    async def on_message(self, message: discord.Message) -> None:
        if self.user is None:
            return

        is_dm = isinstance(message.channel, discord.abc.PrivateChannel)
        is_thread = isinstance(message.channel, discord.Thread)
        guild_id = message.guild.id if message.guild is not None else None
        if not is_allowed_location(
            guild_id,
            message.channel.id,
            is_thread=is_thread,
            config=self.config,
        ):
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "message_ignored", guild_id=guild_id, channel_id=message.channel.id,
                    reason="outside_allowed_location",
                )
            return
        if self.presence is not None:
            await self._room_catchup_done.wait()
        if message.author.id == self.user.id:
            return
        if message.author.bot or message.webhook_id is not None or not message.content.strip():
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "message_ignored", guild_id=guild_id, channel_id=message.channel.id,
                    reason="bot_or_webhook" if message.author.bot or message.webhook_id is not None else "empty_content",
                )
            return
        is_mentioned = self.user in message.mentions
        reference = message.reference
        resolved = reference.resolved if reference else None
        is_reply_to_bot = (
            isinstance(resolved, discord.Message)
            and resolved.author.id == self.user.id
        )
        if not is_reply_to_bot and reference is not None and reference.message_id is not None:
            prior_message = (
                self.room_timeline.get_message(str(reference.message_id))
                if self.room_timeline is not None else None
            )
            is_reply_to_bot = prior_message is not None and prior_message.author_kind == "mita"
        is_direct = is_mentioned or is_reply_to_bot
        if self.diagnostics is not None:
            self.diagnostics.record(
                "message_received", guild_id=guild_id, channel_id=message.channel.id,
                trigger="mention" if is_mentioned else "reply" if is_reply_to_bot else "ambient",
            )
        room_message = self._room_message(message, author_kind="human", message_kind="human")
        if self.presence is not None:
            await self.presence.on_human_message(room_message, direct=is_direct)
        if not should_respond_to_message(
            author_is_bot=message.author.bot,
            webhook_id=message.webhook_id,
            is_dm=is_dm,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            guild_id=guild_id,
            channel_id=message.channel.id,
            is_thread=is_thread,
            config=self.config,
        ):
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "message_ignored", guild_id=guild_id, channel_id=message.channel.id,
                    reason="not_direct",
                )
            return

        prompt = (
            extract_mention_prompt(message.content, self.user.id)
            if is_mentioned
            else " ".join(message.content.split()) or None
        )
        if prompt is None:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "message_ignored", guild_id=guild_id, channel_id=message.channel.id,
                    reason="empty_content",
                )
            return

        if self.presence is not None:
            self.presence.notify_direct_generation_started()
        if self.diagnostics is not None:
            self.diagnostics.record("direct_generation_started")
        try:
            async with message.channel.typing():
                response = await self._generate(
                    prompt,
                    sender=discord_sender(message.author),
                    request_id=str(message.id),
                    ambient_context=(
                        self.presence.context_builder.build(exclude_message_ids={str(message.id)})
                        if self.presence else ""
                    ),
                )
        except GenerationBusyError:
            sent = await message.reply(
                BUSY_REPLY,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if self.presence is not None:
                await self.presence.on_mita_message(self._room_message(
                    sent, author_kind="mita", message_kind="mita_direct",
                ))
            return
        finally:
            if self.presence is not None:
                self.presence.notify_direct_generation_finished()
        response_text = self._response_text(response)
        chunks = split_response(response_text)
        if self.diagnostics is not None:
            self.diagnostics.record(
                "direct_generation_finished", response_chars=len(response_text), chunk_count=len(chunks),
            )
        try:
            for chunk in chunks:
                sent = await message.reply(
                    chunk,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                if self.presence is not None:
                    await self.presence.on_mita_message(self._room_message(
                        sent, author_kind="mita", message_kind="mita_direct",
                    ))
            if self.diagnostics is not None:
                self.diagnostics.record("direct_reply_sent", chunk_count=len(chunks))
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "discord_send_failed", exception_type=type(exc).__name__,
                    status=getattr(exc, "status", None),
                )
            raise

    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if self.presence is None or after.guild is None:
            return
        if not is_allowed_location(
            after.guild.id, after.channel.id, is_thread=isinstance(after.channel, discord.Thread),
            config=self.config,
        ) or after.author.bot or after.webhook_id is not None:
            return
        await self._room_catchup_done.wait()
        self.room_timeline.update_message(str(after.id), content=after.content)
        await self.presence.notify_room_changed()

    async def on_message_delete(self, message: discord.Message) -> None:
        if self.presence is None or message.guild is None:
            return
        if not is_allowed_location(
            message.guild.id, message.channel.id,
            is_thread=isinstance(message.channel, discord.Thread), config=self.config,
        ):
            return
        await self._room_catchup_done.wait()
        self.room_timeline.mark_deleted(str(message.id))
        await self.presence.notify_room_changed()

    async def _run_presence_worker(self, function):
        if self._utility_busy.is_set():
            raise UtilityBusyError("Discord utility worker is occupied")
        loop = asyncio.get_running_loop()
        self._utility_busy.set()

        def run_utility():
            try:
                return function()
            finally:
                self._utility_busy.clear()

        try:
            future = loop.run_in_executor(self._utility_executor, run_utility)
        except BaseException:
            self._utility_busy.clear()
            raise
        return await future

    @property
    def generation_busy(self) -> bool:
        return self._generation_busy.is_set()

    @property
    def utility_busy(self) -> bool:
        return self._utility_busy.is_set()

    async def _generate_social(self, ambient_context: str, request_id: str):
        return await self._generate(
            "", sender="DiscordRoom", request_id=request_id,
            ambient_context=ambient_context, event_type="discord_social",
        )

    async def _generate_initiative(self, ambient_context: str, request_id: str, mode: str):
        return await self._generate(
            "", sender="DiscordRoom", request_id=request_id,
            ambient_context=ambient_context, event_type="discord_initiative",
            system_input=mode,
        )

    async def _observe_room(self, ambient_context: str, request_id: str):
        return await self._generate(
            "", sender="DiscordRoom", request_id=request_id,
            ambient_context=ambient_context, event_type="discord_room_observe",
        )

    async def _send_presence_message(self, content: str, reply_to_id: str | None, message_kind: str):
        if self._room_channel is None:
            return None
        sent = []
        for chunk in split_response(content):
            if reply_to_id:
                try:
                    target = await self._room_channel.fetch_message(int(reply_to_id))
                    current = await target.reply(
                        chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none(),
                    )
                except (discord.NotFound, discord.Forbidden, ValueError):
                    current = await self._room_channel.send(
                        chunk, allowed_mentions=discord.AllowedMentions.none(),
                    )
            else:
                current = await self._room_channel.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none(),
                )
            sent.append(current)
            if self.presence is not None:
                await self.presence.on_mita_message(self._room_message(
                    current, author_kind="mita", message_kind=message_kind,
                ))
        return sent[-1] if sent else None

    async def clear_room_context(self) -> int:
        if self.presence is None:
            return 0
        latest_id = getattr(self._room_channel, "last_message_id", None)
        if self._room_channel is not None:
            try:
                async for latest in self._room_channel.history(limit=1):
                    latest_id = latest.id
                    break
            except discord.HTTPException:
                logger.warning("Could not refresh Discord room cursor while clearing room context")
        removed = self.presence.clear_room()
        if latest_id is not None:
            self.room_timeline.set_last_seen_message_id(
                guild_id=str(self.config.guild_id), channel_id=str(self.config.channel_id),
                message_id=str(latest_id),
            )
        return removed

    async def _generate(
        self,
        text: str,
        *,
        sender: str,
        request_id: str,
        ambient_context: str = "",
        system_input: str = "",
        event_type: str = "chat",
    ) -> Any:
        if self._generation_busy.is_set():
            raise GenerationBusyError
        self._generation_busy.set()
        loop = asyncio.get_running_loop()

        def run_generation():
            try:
                return self._run_character_generation(
                    text, sender=sender, request_id=request_id,
                    ambient_context=ambient_context, system_input=system_input,
                    event_type=event_type,
                )
            finally:
                self._generation_busy.clear()

        try:
            future = loop.run_in_executor(self._generation_executor, run_generation)
        except BaseException:
            self._generation_busy.clear()
            raise
        try:
            return await future
        except Exception as exc:
            logger.exception("Discord LLM generation raised an exception")
            if self.diagnostics is not None:
                self.diagnostics.record("generation_failed", exception_type=type(exc).__name__)
            record_error = getattr(self.runtime, "record_error", None)
            if callable(record_error):
                record_error("Generation raised an exception; details are in server logs.")
            return None

    def _run_character_generation(
        self, text: str, *, sender: str, request_id: str,
        ambient_context: str, system_input: str, event_type: str,
    ):
        if event_type == "chat" and callable(getattr(self.runtime, "generate_direct", None)):
            return self.runtime.generate_direct(
                text=text, sender=sender, discord_message_id=request_id,
                ambient_context=ambient_context,
            )
        if event_type == "discord_social" and callable(getattr(self.runtime, "generate_social", None)):
            return self.runtime.generate_social(ambient_context=ambient_context, request_id=request_id)
        if event_type == "discord_initiative" and callable(getattr(self.runtime, "generate_initiative", None)):
            return self.runtime.generate_initiative(
                ambient_context=ambient_context, request_id=request_id, mode=system_input,
            )
        if event_type == "discord_room_observe" and callable(getattr(self.runtime, "observe_room", None)):
            return self.runtime.observe_room(
                ambient_context=ambient_context, request_id=request_id,
            )
        return self.runtime.generate(
            text, sender=sender, request_id=request_id,
            ambient_context=ambient_context, system_input=system_input,
            event_type=event_type,
        )

    def _response_text(self, response: Any) -> str:
        text = getattr(response, "text", None)
        if text:
            return str(text)
        error = getattr(response, "error_message", None)
        if error:
            logger.warning("Discord LLM request failed")
            record_error = getattr(self.runtime, "record_error", None)
            if callable(record_error):
                record_error("The configured provider request failed; inspect protected server logs.")
            return "I couldn't generate a response. Please try again later."
        record_error = getattr(self.runtime, "record_error", None)
        if callable(record_error):
            record_error("Generation returned an empty response.")
        return "I couldn't generate a response. Please try again later."

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.presence is not None:
                await self.presence.stop()
            await super().close()
        finally:
            self._generation_executor.shutdown(wait=True, cancel_futures=True)
            self._utility_executor.shutdown(wait=True, cancel_futures=True)
            if self.room_timeline is not None:
                self.room_timeline.close()
            try:
                self.runtime.close()
            finally:
                if self.diagnostics is not None and not self._managed_run:
                    self.diagnostics.record("bot_stopped")
                    self.diagnostics.close()

    def run_configured(self) -> None:
        self._managed_run = True
        try:
            self.run(self.token, log_handler=None)
        except Exception as exc:
            if self.diagnostics is not None:
                self.diagnostics.record("gateway_failed", exception_type=type(exc).__name__)
            raise
        finally:
            if self.diagnostics is not None:
                self.diagnostics.record("bot_stopped")
                self.diagnostics.close()


def create_bot(
    config: DiscordBotConfig,
    *,
    runtime: DiscordCharacterRuntime | None = None,
) -> DiscordBot:
    return DiscordBot(
        token=config.token,
        guild_id=config.guild_id,
        channel_id=config.channel_id,
        admin_ids=config.admin_ids,
        runtime=runtime or DiscordCharacterRuntime(),
    )
