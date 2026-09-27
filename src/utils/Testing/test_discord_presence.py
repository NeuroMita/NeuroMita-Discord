import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))


class DiscordPresenceControllerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from discord_bot.attention import AttentionDecision
        from discord_bot.presence_controller import DiscordPresenceController
        from discord_bot.presence_settings import PresenceSettingsStore
        from discord_bot.room_context import DiscordRoomContextBuilder
        from discord_bot.room_timeline import DiscordRoomTimeline

        self.temp = tempfile.TemporaryDirectory()
        self.timeline = DiscordRoomTimeline(Path(self.temp.name) / "room.db")
        self.settings = PresenceSettingsStore(Path(self.temp.name) / "presence.json")
        self.settings.update(debounce_min_seconds=1, debounce_max_seconds=1,
                             evaluation_min_interval_seconds=60, cooldown_seconds=30,
                             quiet_start_seconds=180)

        class Attention:
            def __init__(inner):
                inner.social_calls = 0
                inner.initiative_calls = 0
                inner.summary_calls = 0
                inner.summary_text = ""

            def decide_social(inner, **_kwargs):
                inner.social_calls += 1
                return AttentionDecision(True, "join", 100, None, "topic_fit")

            def decide_initiative(inner, **_kwargs):
                inner.initiative_calls += 1
                return AttentionDecision(True, "start_topic", 100, None, "good_topic_to_start")

            def summarize_room(inner, **_kwargs):
                inner.summary_calls += 1
                return inner.summary_text

        self.attention = Attention()
        self.sent = []
        async def run_worker(function):
            return function()
        async def generate_social(context, request_id):
            return type("Response", (), {"text": "Joining in", "error": ""})()
        async def generate_initiative(context, request_id, mode):
            return type("Response", (), {"text": "Hello room", "error": ""})()
        async def send(content, reply_to, message_kind):
            self.sent.append((content, reply_to, message_kind))

        self.controller = DiscordPresenceController(
            timeline=self.timeline,
            context_builder=DiscordRoomContextBuilder(self.timeline, guild_id="1", channel_id="2"),
            attention=self.attention, settings=self.settings, run_worker=run_worker,
            generate_social=generate_social, generate_initiative=generate_initiative,
            send_public_message=send, is_generation_busy=lambda: False,
            guild_id="1", channel_id="2",
        )

    async def asyncTearDown(self):
        await self.controller.stop()
        self.timeline.close()
        self.temp.cleanup()

    async def test_social_message_is_debounced_then_classified_and_sent(self):
        from discord_bot.room_timeline import RoomMessage

        await self.controller.on_human_message(RoomMessage(
            discord_message_id="1", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="Anyone tried this model?",
            created_at=datetime.now(timezone.utc).isoformat(),
        ), direct=False)
        await asyncio.sleep(1.1)

        self.assertEqual(self.attention.social_calls, 1)
        self.assertEqual(self.sent, [("Joining in", None, "mita_join")])

    async def test_hourly_budget_zero_skips_attention_model(self):
        from discord_bot.room_timeline import RoomMessage

        self.settings.update(max_per_hour=0)
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="2", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="hello",
            created_at=datetime.now(timezone.utc).isoformat(),
        ), direct=False)
        await asyncio.sleep(1.1)

        self.assertEqual(self.attention.social_calls, 0)
        self.assertEqual(self.sent, [])

    async def test_direct_message_cancels_pending_social_evaluation(self):
        from discord_bot.room_timeline import RoomMessage

        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed_sleep(_seconds):
            started.set()
            await release.wait()

        self.controller.sleep = delayed_sleep
        first = RoomMessage(
            discord_message_id="3", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="conversation",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        direct = RoomMessage(
            discord_message_id="4", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="@Mita hi",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        await self.controller.on_human_message(first, direct=False)
        await started.wait()
        await self.controller.on_human_message(direct, direct=True)
        release.set()
        await asyncio.sleep(0)

        self.assertEqual(self.attention.social_calls, 0)

    async def test_summary_provider_failure_does_not_create_a_retry_loop(self):
        from discord_bot.room_timeline import RoomMessage

        self.settings.update(summary_threshold=20)
        for index in range(9, 45):
            await self.controller.on_human_message(RoomMessage(
                discord_message_id=str(index), guild_id="1", channel_id="2", author_id="9",
                author_name="A", author_kind="human", content=f"observed fact {index}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ), direct=True)
        await asyncio.sleep(0.05)

        self.assertEqual(self.attention.summary_calls, 1)
        self.assertEqual(self.timeline.get_summary(guild_id="1", channel_id="2"), "")

    async def test_successful_room_summary_runs_silent_character_observation(self):
        from discord_bot.room_timeline import RoomMessage

        observed = []
        async def observe(context, request_id):
            observed.append((context, request_id))
            return type("Result", (), {"error": ""})()
        self.controller.observe_room = observe
        self.settings.update(summary_threshold=20)
        self.attention.summary_text = "Dima is working on a Discord bot."
        for index in range(100, 136):
            await self.controller.on_human_message(RoomMessage(
                discord_message_id=str(index), guild_id="1", channel_id="2", author_id="9",
                author_name="A", author_kind="human", content=f"room message {index}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ), direct=True)
        await asyncio.sleep(0.05)

        self.assertEqual(self.attention.summary_calls, 1)
        self.assertEqual(len(observed), 1)
        self.assertIn("Dima is working on a Discord bot", observed[0][0])
        self.assertTrue(observed[0][1].startswith("discord-observe-"))


if __name__ == "__main__":
    unittest.main()
