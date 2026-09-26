import os
import subprocess
import sys
import unittest
from unittest.mock import patch

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
            author_is_bot=False, webhook_id=None, is_dm=True,
            is_mentioned=False, is_reply_to_bot=False,
        ))
        self.assertTrue(should_respond_to_message(
            author_is_bot=False, webhook_id=None, is_dm=False,
            is_mentioned=False, is_reply_to_bot=True,
        ))
        self.assertFalse(should_respond_to_message(
            author_is_bot=True, webhook_id=None, is_dm=True,
            is_mentioned=True, is_reply_to_bot=True,
        ))
        self.assertFalse(should_respond_to_message(
            author_is_bot=False, webhook_id=99, is_dm=True,
            is_mentioned=True, is_reply_to_bot=True,
        ))

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


if __name__ == "__main__":
    unittest.main()
