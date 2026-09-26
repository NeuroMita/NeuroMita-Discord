import os
import asyncio
import threading
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath("src"))


class DiscordBotConfigTests(unittest.TestCase):
    def test_requires_token_without_echoing_it(self):
        from discord_bot.config import DiscordBotConfig

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "DISCORD_BOT_TOKEN is not configured"):
                DiscordBotConfig.from_env()

    def test_reads_optional_guild_id(self):
        from discord_bot.config import DiscordBotConfig

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret", "DISCORD_GUILD_ID": "123"}, clear=True):
            config = DiscordBotConfig.from_env()

        self.assertEqual(config.token, "secret")
        self.assertEqual(config.guild_id, 123)
        self.assertNotIn("secret", repr(config))

    def test_uses_configured_discord_scope_and_admin(self):
        from discord_bot.config import DiscordBotConfig

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret"}, clear=True):
            config = DiscordBotConfig.from_env()

        self.assertEqual(config.guild_id, 1341427480942350356)
        self.assertEqual(config.channel_id, 1353745092065624144)
        self.assertEqual(config.admin_ids, frozenset({292002437932384256}))

    def test_rejects_invalid_guild_id(self):
        from discord_bot.config import DiscordBotConfig

        with patch.dict(os.environ, {"DISCORD_BOT_TOKEN": "secret", "DISCORD_GUILD_ID": "nope"}, clear=True):
            with self.assertRaisesRegex(ValueError, "DISCORD_GUILD_ID"):
                DiscordBotConfig.from_env()

    def test_module_exits_cleanly_when_token_is_missing(self):
        env = dict(os.environ)
        env.pop("DISCORD_BOT_TOKEN", None)
        env["PYTHONPATH"] = os.path.abspath("src")
        result = subprocess.run(
            [sys.executable, "-m", "discord_bot"],
            cwd=os.getcwd(),
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "DISCORD_BOT_TOKEN is not configured")
        self.assertNotIn("secret", result.stdout + result.stderr)


class DiscordMessageHelpersTests(unittest.TestCase):
    def test_mention_trigger_strips_bot_mention_and_empty_text_is_ignored(self):
        from discord_bot.bot import extract_mention_prompt

        self.assertEqual(extract_mention_prompt("<@123> привет <@!123>", 123), "привет")
        self.assertIsNone(extract_mention_prompt("<@123>", 123))

    def test_message_triggers_and_ignores_bot_and_webhook_messages(self):
        from discord_bot.bot import should_respond_to_message

        self.assertTrue(should_respond_to_message(
            author_is_bot=False, webhook_id=None, is_dm=False,
            is_mentioned=False, is_reply_to_bot=True,
            guild_id=1341427480942350356, channel_id=1353745092065624144,
        ))
        self.assertFalse(should_respond_to_message(
            author_is_bot=False, webhook_id=None, is_dm=True,
            is_mentioned=False, is_reply_to_bot=False,
            guild_id=None, channel_id=None,
        ))
        self.assertFalse(should_respond_to_message(
            author_is_bot=True, webhook_id=None, is_dm=True,
            is_mentioned=True, is_reply_to_bot=True,
            guild_id=1341427480942350356, channel_id=1353745092065624144,
        ))
        self.assertFalse(should_respond_to_message(
            author_is_bot=False, webhook_id=99, is_dm=True,
            is_mentioned=True, is_reply_to_bot=True,
            guild_id=1341427480942350356, channel_id=1353745092065624144,
        ))

    def test_location_guard_rejects_other_guild_channel_and_threads(self):
        from discord_bot.bot import is_allowed_location

        self.assertTrue(is_allowed_location(1341427480942350356, 1353745092065624144))
        self.assertFalse(is_allowed_location(1, 1353745092065624144))
        self.assertFalse(is_allowed_location(1341427480942350356, 1))
        self.assertFalse(is_allowed_location(1341427480942350356, 1353745092065624144, is_thread=True))

    def test_only_configured_admin_id_is_admin(self):
        from discord_bot.config import DiscordBotConfig

        self.assertTrue(DiscordBotConfig(token="x").is_admin(292002437932384256))
        self.assertFalse(DiscordBotConfig(token="x").is_admin(1))

    def test_sender_identity_uses_discord_user_id_not_display_name_alone(self):
        from discord_bot.bot import discord_sender

        user = type(
            "User",
            (),
            {"id": 292002437932384256, "global_name": "Mita", "display_name": "Mita", "name": "mita"},
        )()
        self.assertEqual(
            discord_sender(user),
            "Discord:Mita [id:292002437932384256]",
        )

    def test_response_chunks_fit_discord_message_limit(self):
        from discord_bot.response_formatter import split_response

        chunks = split_response("абв " * 700)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 2000 for chunk in chunks))
        self.assertEqual("".join(chunks), "абв " * 700)

    def test_long_unbroken_response_is_split_without_loss(self):
        from discord_bot.response_formatter import split_response

        source = "x" * 4500
        chunks = split_response(source)
        self.assertEqual([len(chunk) for chunk in chunks], [2000, 2000, 500])
        self.assertEqual("".join(chunks), source)


class DiscordBotRuntimeTests(unittest.TestCase):
    def test_registers_chat_command_and_only_message_content_intent(self):
        from discord_bot.bot import DiscordBot

        class Runtime:
            def close(self):
                pass

        bot = DiscordBot(token="secret", runtime=Runtime())
        try:
            chat = bot.tree.get_command("chat")
            self.assertIsNotNone(chat)
            self.assertIsNotNone(chat.get_command("ask"))
            self.assertTrue(bot.intents.message_content)
            self.assertFalse(bot.intents.members)
            self.assertFalse(bot.intents.presences)
        finally:
            import asyncio

            asyncio.run(bot.close())


class DiscordBotGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from discord_bot.bot import DiscordBot

        class Runtime:
            def __init__(self):
                self.calls = 0
                self.started = threading.Event()
                self.release = threading.Event()
                self.thread_ids = []
                self.close_calls = 0
                self.raise_error = False
                self.generate_args = []

            def generate(self, text, *, sender, origin_message_id):
                self.calls += 1
                self.generate_args.append((text, sender, origin_message_id))
                self.thread_ids.append(threading.get_ident())
                self.started.set()
                if self.raise_error:
                    raise RuntimeError("provider details must stay server-side")
                self.release.wait(timeout=2)
                return type("Response", (), {"text": "ok", "error_message": ""})()

            def close(self):
                self.close_calls += 1

        self.runtime = Runtime()
        self.bot = DiscordBot(token="secret", runtime=self.runtime)

    async def asyncTearDown(self):
        self.runtime.release.set()
        await self.bot.close()

    async def test_generation_runs_off_event_loop_thread(self):
        loop_thread = threading.get_ident()
        task = asyncio.create_task(
            self.bot._generate("hello", sender="Discord:Tester [id:1]", origin_message_id="2")
        )
        await asyncio.to_thread(self.runtime.started.wait, 1)
        self.runtime.release.set()
        await task

        self.assertEqual(len(self.runtime.thread_ids), 1)
        self.assertNotEqual(self.runtime.thread_ids[0], loop_thread)
        self.assertEqual(self.runtime.generate_args[0], ("hello", "Discord:Tester [id:1]", "2"))

    async def test_busy_generation_is_rejected_without_queueing(self):
        from discord_bot.bot import GenerationBusyError

        first = asyncio.create_task(
            self.bot._generate("first", sender="Discord:Tester [id:1]", origin_message_id="1")
        )
        await asyncio.to_thread(self.runtime.started.wait, 1)
        start = time.monotonic()
        with self.assertRaises(GenerationBusyError):
            await self.bot._generate("second", sender="Discord:Tester [id:1]", origin_message_id="2")
        elapsed = time.monotonic() - start
        self.runtime.release.set()
        await first

        self.assertLess(elapsed, 0.1)
        self.assertEqual(self.runtime.calls, 1)

    async def test_generation_exception_is_logged_with_traceback_and_hidden_from_user(self):
        self.runtime.raise_error = True
        with self.assertLogs("discord_bot.bot", level="ERROR") as captured:
            result = await self.bot._generate(
                "hello", sender="Discord:Tester [id:1]", origin_message_id="1"
            )

        self.assertTrue(captured.records[0].exc_info)
        self.assertEqual(
            self.bot._response_text(result),
            "I couldn't generate a response. Please try again later.",
        )
        self.assertNotIn("provider details", self.bot._response_text(result))

    async def test_shutdown_closes_executor_and_runtime_only_once(self):
        shutdown = self.bot._generation_executor.shutdown
        with patch.object(self.bot._generation_executor, "shutdown", wraps=shutdown) as mocked:
            await self.bot.close()
            await self.bot.close()

        self.assertEqual(mocked.call_count, 1)
        self.assertEqual(self.runtime.close_calls, 1)

    async def test_admin_commands_are_registered_and_restricted_to_configured_owner(self):
        from unittest.mock import AsyncMock

        for name in ("bot", "character", "history", "memory", "ai"):
            self.assertIsNotNone(self.bot.tree.get_command(name))
        interaction = Mock()
        interaction.guild_id = self.bot.config.guild_id
        interaction.channel_id = self.bot.config.channel_id
        interaction.user.id = 292002437932384256
        interaction.response.send_message = AsyncMock()
        self.assertTrue(await self.bot._require_admin(interaction))

        interaction.user.id = 123
        self.assertFalse(await self.bot._require_admin(interaction))
        interaction.response.send_message.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
