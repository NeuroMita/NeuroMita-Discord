import os
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import PropertyMock, patch

sys.path.insert(0, os.path.abspath("src"))


class AsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class DiscordPresenceIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mention_uses_character_runtime_and_records_both_room_speakers(self):
        from discord_bot.bot import DiscordBot

        with tempfile.TemporaryDirectory() as tmp:
            class Runtime:
                data_dir = Path(tmp)
                active_character_id = "Crazy"
                settings = SimpleNamespace(get=lambda *_args: None)

                def __init__(self):
                    self.calls = []

                def generate_utility(self, _request):
                    raise AssertionError("direct turn must use the character pipeline")

                def generate(self, text, **kwargs):
                    self.calls.append((text, kwargs))
                    return SimpleNamespace(text="Hi there", error="", error_message="")

                def close(self):
                    pass

            runtime = Runtime()
            bot = DiscordBot(token="test", runtime=runtime)
            bot_user = SimpleNamespace(id=999, name="Mita", display_name="Mita")
            human = SimpleNamespace(
                id=292002437932384256, name="Dima", display_name="Dima", global_name="Dima",
                bot=False,
            )
            sent_by_bot = SimpleNamespace(
                id=200, name="Mita", display_name="Mita", global_name="Mita", bot=True,
            )
            guild = SimpleNamespace(id=1341427480942350356)
            channel = SimpleNamespace(id=1353745092065624144, typing=lambda: AsyncContext())
            message = SimpleNamespace(
                id=100, guild=guild, channel=channel, author=human, content="<@999> hello",
                mentions=[bot_user], reference=None, webhook_id=None,
                created_at=datetime.now(timezone.utc),
            )
            reply = SimpleNamespace(
                id=200, guild=guild, channel=channel, author=sent_by_bot,
                content="Hi there", reference=None, created_at=datetime.now(timezone.utc),
            )
            async def send_reply(*_args, **_kwargs):
                return reply
            message.reply = send_reply
            bot._room_catchup_done.set()
            with patch.object(type(bot), "user", new_callable=PropertyMock, return_value=bot_user):
                try:
                    await bot.on_message(message)
                    records = bot.room_timeline.recent(
                        guild_id=str(guild.id), channel_id=str(channel.id), limit=10,
                    )
                    self.assertEqual([(row.author_kind, row.content) for row in records], [
                        ("human", "<@999> hello"), ("mita", "Hi there"),
                    ])
                    self.assertEqual(runtime.calls[0][0], "hello")
                    self.assertEqual(runtime.calls[0][1]["request_id"], "100")
                    self.assertIsNone(runtime.calls[0][1].get("origin_message_id"))
                    log_text = bot.diagnostics.path.read_text(encoding="utf-8")
                    log_events = [json.loads(line)["event"] for line in log_text.splitlines()]
                    self.assertIn("message_received", log_events)
                    self.assertIn("direct_reply_sent", log_events)
                    self.assertNotIn("hello", log_text)
                    self.assertNotIn("Hi there", log_text)
                finally:
                    await bot.close()


if __name__ == "__main__":
    unittest.main()
