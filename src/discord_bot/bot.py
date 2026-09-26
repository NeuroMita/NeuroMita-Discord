from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.config import DiscordBotConfig
from discord_bot.config import DEFAULT_CHANNEL_ID, DEFAULT_GUILD_ID
from discord_bot.response_formatter import split_response
from discord_bot.character_runtime import DiscordCharacterRuntime
from discord_bot.admin_facade import DiscordAdminFacade


logger = logging.getLogger(__name__)
BUSY_REPLY = "A request is already running. Please try again shortly."


class GenerationBusyError(RuntimeError):
    """Raised when the single Discord generation slot is occupied."""


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
) -> bool:
    if author_is_bot or webhook_id is not None:
        return False
    if is_dm or not is_allowed_location(guild_id, channel_id, is_thread=is_thread):
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
        self.admin = DiscordAdminFacade(runtime)
        self.started_at = time.monotonic()
        self.guild_id = guild_id
        self.config = DiscordBotConfig(
            token=token,
            guild_id=guild_id,
            channel_id=channel_id,
            admin_ids=admin_ids if admin_ids is not None else DiscordBotConfig(token="").admin_ids,
        )
        self._generation_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-generation"
        )
        self._generation_busy = False
        self._closed = False

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
            await interaction.response.defer(thinking=True)
            try:
                response = await self._generate(
                    text,
                    sender=discord_sender(interaction.user),
                    origin_message_id=str(interaction.id),
                )
            except GenerationBusyError:
                await interaction.edit_original_response(content=BUSY_REPLY)
                return
            chunks = split_response(self._response_text(response))
            await interaction.edit_original_response(
                content=chunks[0], allowed_mentions=discord.AllowedMentions.none()
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none()
                )

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

        @history_group.command(name="reset", description="Clear current character history")
        @app_commands.describe(confirm="Confirm permanent history reset")
        async def history_reset(interaction: discord.Interaction, confirm: bool) -> None:
            if not await self._require_admin(interaction):
                return
            done = self.admin.history_reset(confirm=confirm)
            await interaction.response.send_message(
                "History cleared." if done else "Set confirm=true to clear history.", ephemeral=True
            )

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
            lines = [f"{p['id']} | {p['provider']} | {p['model']}" for p in chain]
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
            status = self.admin.bot_status()
            await interaction.response.send_message(
                f"{status.get('message')} | character: {status.get('character_id')}",
                ephemeral=True,
            )

        @debug_group.command(name="last-error", description="Show the last safe error summary")
        async def debug_last_error(interaction: discord.Interaction) -> None:
            if not await self._require_admin(interaction):
                return
            error = self.admin.bot_status().get("last_error") or "No recorded errors."
            await interaction.response.send_message(str(error)[:1500], ephemeral=True)

        self.tree.add_command(debug_group)

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
        if self.guild_id is not None:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

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
            return
        is_mentioned = self.user in message.mentions
        reference = message.reference
        resolved = reference.resolved if reference else None
        is_reply_to_bot = (
            isinstance(resolved, discord.Message)
            and resolved.author.id == self.user.id
        )
        if not should_respond_to_message(
            author_is_bot=message.author.bot,
            webhook_id=message.webhook_id,
            is_dm=is_dm,
            is_mentioned=is_mentioned,
            is_reply_to_bot=is_reply_to_bot,
            guild_id=guild_id,
            channel_id=message.channel.id,
            is_thread=is_thread,
        ):
            return

        prompt = (
            extract_mention_prompt(message.content, self.user.id)
            if is_mentioned
            else " ".join(message.content.split()) or None
        )
        if prompt is None:
            return

        try:
            async with message.channel.typing():
                response = await self._generate(
                    prompt,
                    sender=discord_sender(message.author),
                    origin_message_id=str(message.id),
                )
        except GenerationBusyError:
            await message.reply(
                BUSY_REPLY,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        for chunk in split_response(self._response_text(response)):
            await message.reply(
                chunk,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _generate(
        self,
        text: str,
        *,
        sender: str,
        origin_message_id: str,
    ) -> Any:
        if self._generation_busy:
            raise GenerationBusyError
        self._generation_busy = True
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                self._generation_executor,
                lambda: self.runtime.generate(
                    text,
                    sender=sender,
                    origin_message_id=origin_message_id,
                ),
            )
        except Exception:
            logger.exception("Discord LLM generation raised an exception")
            record_error = getattr(self.runtime, "record_error", None)
            if callable(record_error):
                record_error("Generation raised an exception; details are in server logs.")
            return None
        finally:
            self._generation_busy = False

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
            await super().close()
        finally:
            self._generation_executor.shutdown(wait=True, cancel_futures=True)
            self.runtime.close()

    def run_configured(self) -> None:
        self.run(self.token, log_handler=None)


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
