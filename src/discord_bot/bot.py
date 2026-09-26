from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from discord_bot.config import DiscordBotConfig
from discord_bot.response_formatter import split_response
from discord_bot.llm_runtime import DiscordLLMRuntime


DISCORD_SYSTEM_PROMPT = """You are communicating through Discord.
Respond as a conversational assistant.
There is no Unity game client connected.
Voice, camera, screen capture and game-only actions are unavailable.
Do not output Unity-only commands."""
logger = logging.getLogger(__name__)


def extract_mention_prompt(content: str, bot_id: int) -> str | None:
    cleaned = re.sub(rf"<@!?{bot_id}>", " ", content)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def should_respond_to_message(
    *,
    author_is_bot: bool,
    webhook_id: int | None,
    is_dm: bool,
    is_mentioned: bool,
    is_reply_to_bot: bool,
) -> bool:
    if author_is_bot or webhook_id is not None:
        return False
    return is_dm or is_mentioned or is_reply_to_bot


class DiscordBot(commands.Bot):
    def __init__(
        self,
        *,
        token: str,
        runtime: DiscordLLMRuntime,
        guild_id: int | None = None,
    ) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.token = token
        self.runtime = runtime
        self.guild_id = guild_id
        self._generation_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="discord-generation"
        )
        self._generation_semaphore = asyncio.Semaphore(1)

        chat = app_commands.Group(name="chat", description="Chat with NeuroMita")

        @chat.command(name="ask", description="Send a message to the assistant")
        @app_commands.describe(text="Your message")
        async def ask(interaction: discord.Interaction, text: str) -> None:
            await interaction.response.defer(thinking=True)
            response = await self._generate(text)
            chunks = split_response(self._response_text(response))
            await interaction.edit_original_response(
                content=chunks[0], allowed_mentions=discord.AllowedMentions.none()
            )
            for chunk in chunks[1:]:
                await interaction.followup.send(
                    chunk, allowed_mentions=discord.AllowedMentions.none()
                )

        self.tree.add_command(chat)

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
        ):
            return

        prompt = (
            extract_mention_prompt(message.content, self.user.id)
            if is_mentioned
            else " ".join(message.content.split()) or None
        )
        if prompt is None:
            return

        async with message.channel.typing():
            response = await self._generate(prompt)
        for chunk in split_response(self._response_text(response)):
            await message.reply(
                chunk,
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _generate(self, text: str) -> Any:
        messages = [
            {"role": "system", "content": DISCORD_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        async with self._generation_semaphore:
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(
                    self._generation_executor,
                    lambda: self.runtime.generate(messages),
                )
            except Exception:
                logger.warning("Discord LLM generation raised an exception")
                return None

    @staticmethod
    def _response_text(response: Any) -> str:
        text = getattr(response, "text", None)
        if text:
            return str(text)
        error = getattr(response, "error_message", None)
        if error:
            logger.warning("Discord LLM request failed")
            return "I couldn't generate a response. Please try again later."
        return "I couldn't generate a response. Please try again later."

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            self._generation_executor.shutdown(wait=False, cancel_futures=True)
            self.runtime.close()

    def run_configured(self) -> None:
        self.run(self.token, log_handler=None)


def create_bot(
    config: DiscordBotConfig,
    *,
    runtime: DiscordLLMRuntime | None = None,
) -> DiscordBot:
    return DiscordBot(
        token=config.token,
        guild_id=config.guild_id,
        runtime=runtime or DiscordLLMRuntime(),
    )
