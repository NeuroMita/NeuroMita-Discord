import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))


def _raise_send_error():
    raise RuntimeError("send failed")


async def _unexpected_call(*_args, **_kwargs):
    raise AssertionError("initiative generation must be suppressed by its cooldown")


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

    async def test_utility_busy_is_a_local_gate(self):
        self.controller.is_utility_busy = lambda: True

        allowed, reason = self.controller._local_gate(kind="social")

        self.assertFalse(allowed)
        self.assertEqual(reason, "utility_busy")
        self.assertTrue(self.controller.status()["utility_busy"])

    async def test_social_evaluation_retries_once_after_utility_work(self):
        utility_busy = True
        self.controller.is_utility_busy = lambda: utility_busy
        async def immediate_sleep(_seconds):
            await asyncio.sleep(0)
        self.controller.sleep = immediate_sleep

        await self.controller._evaluate_social(self.controller._room_revision)
        self.assertEqual(self.attention.social_calls, 0)
        self.assertIsNotNone(self.controller._social_task)
        utility_busy = False
        await asyncio.sleep(0.05)

        self.assertEqual(self.attention.social_calls, 1)

    async def test_human_activity_resets_unanswered_initiative_streak(self):
        from discord_bot.room_timeline import RoomMessage

        sent_at = datetime.now(timezone.utc).isoformat()
        self.timeline.record_initiative_sent(
            guild_id="1", channel_id="2", action="silence_ping", created_at=sent_at,
        )
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="human-reset", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="I'm here",
            created_at=datetime.now(timezone.utc).isoformat(),
        ), direct=True)

        state = self.timeline.state(guild_id="1", channel_id="2")
        self.assertEqual(state["unanswered_initiative_streak"], 0)
        self.assertEqual(state["last_silence_ping_at"], sent_at)

    async def test_initiative_gate_enforces_six_hour_cap_then_unanswered_backoff(self):
        from discord_bot.room_timeline import RoomMessage

        now = datetime.now(timezone.utc)
        self.timeline.append_message(RoomMessage(
            discord_message_id="old-human", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="old conversation",
            created_at=(now - timedelta(hours=1)).isoformat(),
        ))
        self.settings.update(initiative_max_per_6h=0, max_per_hour=20, quiet_start_seconds=180)

        allowed, reason = self.controller._local_gate(kind="initiative")
        self.assertFalse(allowed)
        self.assertEqual(reason, "initiative_budget")

        self.settings.update(initiative_max_per_6h=2)
        self.timeline.record_initiative_sent(
            guild_id="1", channel_id="2", action="start_topic",
            created_at=(now - timedelta(minutes=1)).isoformat(),
        )
        allowed, reason = self.controller._local_gate(kind="initiative")
        self.assertFalse(allowed)
        self.assertEqual(reason, "unanswered_backoff")

    async def test_initiative_counts_and_streak_are_recorded_only_after_discord_send(self):
        from discord_bot.attention import AttentionDecision
        from discord_bot.room_timeline import RoomMessage

        self.settings.update(max_per_hour=20, quiet_start_seconds=180)
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="quiet-human", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="old conversation",
            created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        ), direct=True)
        self.controller.send_public_message = lambda *_args: _raise_send_error()
        decision = AttentionDecision(True, "start_topic", 100, None, "good_topic_to_start")
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            await self.controller._run_voluntary_generation(
                decision, kind="initiative", revision=self.controller._room_revision,
            )
        self.assertEqual(self.timeline.unanswered_initiative_streak(guild_id="1", channel_id="2"), 0)

    async def test_recent_silence_ping_suppresses_another_initiative(self):
        from discord_bot.attention import AttentionDecision
        from discord_bot.room_timeline import RoomMessage

        self.settings.update(max_per_hour=20, quiet_start_seconds=180)
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="quiet-human-2", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="old conversation",
            created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        ), direct=True)
        self.timeline.record_initiative_sent(
            guild_id="1", channel_id="2", action="silence_ping",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.timeline.reset_unanswered_initiative(guild_id="1", channel_id="2")
        self.controller.generate_initiative = _unexpected_call

        sent = await self.controller._run_voluntary_generation(
            AttentionDecision(True, "silence_ping", 100, None, "silence_checkin"),
            kind="initiative", revision=self.controller._room_revision,
        )

        self.assertFalse(sent)

    async def test_successful_initiative_send_records_budget_and_status(self):
        from discord_bot.attention import AttentionDecision
        from discord_bot.room_timeline import RoomMessage

        self.settings.update(max_per_hour=20, quiet_start_seconds=180)
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="quiet-human-3", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="old conversation",
            created_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        ), direct=True)

        async def accepted_send(_content, _reply_to, message_kind):
            self.timeline.append_message(RoomMessage(
                discord_message_id="mita-accepted", guild_id="1", channel_id="2", author_id="999",
                author_name="Mita", author_kind="mita", content="hello",
                created_at=datetime.now(timezone.utc).isoformat(), message_kind=message_kind,
            ))
            return object()

        self.controller.send_public_message = accepted_send
        sent = await self.controller._run_voluntary_generation(
            AttentionDecision(True, "start_topic", 100, None, "good_topic_to_start"),
            kind="initiative", revision=self.controller._room_revision,
        )

        status = self.controller.status()
        self.assertTrue(sent)
        self.assertEqual(status["initiative_6h_count"], 1)
        self.assertEqual(status["initiative_max_per_6h"], 2)
        self.assertEqual(status["unanswered_streak"], 1)
        self.assertIsNotNone(status["last_initiative_at"])

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
        self.settings.update(summary_threshold=20, observation_idle_seconds=60)
        observation_started = asyncio.Event()
        release_observation = asyncio.Event()
        async def wait_for_idle(_seconds):
            observation_started.set()
            await release_observation.wait()
        self.controller.sleep = wait_for_idle
        self.attention.summary_text = "Dima is working on a Discord bot."
        for index in range(100, 136):
            await self.controller.on_human_message(RoomMessage(
                discord_message_id=str(index), guild_id="1", channel_id="2", author_id="9",
                author_name="A", author_kind="human", content=f"room message {index}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ), direct=True)
        await asyncio.wait_for(observation_started.wait(), timeout=1)

        self.assertEqual(self.attention.summary_calls, 1)
        self.assertEqual(observed, [])
        self.assertTrue(self.controller.status()["observation_busy"])
        release_observation.set()
        await asyncio.sleep(0.05)

        self.assertEqual(len(observed), 1)
        self.assertIn("Dima is working on a Discord bot", observed[0][0])
        self.assertTrue(observed[0][1].startswith("discord-observe-"))

    async def test_human_activity_cancels_pending_room_observation(self):
        from discord_bot.room_timeline import RoomMessage

        observed = []
        async def observe(context, request_id):
            observed.append((context, request_id))
            return type("Result", (), {"error": ""})()
        started = asyncio.Event()
        release = asyncio.Event()
        async def wait_for_idle(_seconds):
            started.set()
            await release.wait()
        self.controller.observe_room = observe
        self.controller.sleep = wait_for_idle
        self.settings.update(summary_threshold=20)
        self.attention.summary_text = "facts"
        for index in range(200, 236):
            await self.controller.on_human_message(RoomMessage(
                discord_message_id=str(index), guild_id="1", channel_id="2", author_id="9",
                author_name="A", author_kind="human", content=f"room message {index}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ), direct=True)
        await asyncio.wait_for(started.wait(), timeout=1)
        self.settings.update(summary_threshold=1000)
        await self.controller.on_human_message(RoomMessage(
            discord_message_id="new-activity", guild_id="1", channel_id="2", author_id="9",
            author_name="A", author_kind="human", content="new activity",
            created_at=datetime.now(timezone.utc).isoformat(),
        ), direct=True)
        release.set()
        await asyncio.sleep(0.05)

        self.assertEqual(observed, [])

    async def test_observation_skips_when_character_or_attention_pipeline_is_busy(self):
        observed = []
        async def observe(context, request_id):
            observed.append((context, request_id))
            return type("Result", (), {"error": ""})()
        async def immediate_sleep(_seconds):
            await asyncio.sleep(0)
        self.controller.observe_room = observe
        self.controller.sleep = immediate_sleep

        self.controller.is_generation_busy = lambda: True
        self.controller._schedule_observation()
        await asyncio.sleep(0.02)
        self.assertEqual(observed, [])

        self.controller.is_generation_busy = lambda: False
        self.controller._attention_busy = True
        self.controller._schedule_observation()
        await asyncio.sleep(0.02)
        self.assertEqual(observed, [])

    async def test_stale_summary_does_not_replace_summary_after_room_change(self):
        from discord_bot.room_timeline import RoomMessage

        self.timeline.commit_summary(
            guild_id="1", channel_id="2", summary="current summary", through_row_id=0,
        )
        self.settings.update(summary_threshold=20)
        for index in range(300, 336):
            await self.controller.on_human_message(RoomMessage(
                discord_message_id=str(index), guild_id="1", channel_id="2", author_id="9",
                author_name="A", author_kind="human", content=f"room message {index}",
                created_at=datetime.now(timezone.utc).isoformat(),
            ), direct=True)
        started = asyncio.Event()
        release = asyncio.Event()
        async def delayed_worker(function):
            started.set()
            await release.wait()
            return function()
        self.controller.run_worker = delayed_worker
        self.attention.summary_text = "summary from old snapshot"
        task = asyncio.create_task(self.controller.summarize_room())
        await started.wait()
        self.controller._room_revision += 1
        release.set()
        await task

        self.assertEqual(self.timeline.get_summary(guild_id="1", channel_id="2"), "current summary")


if __name__ == "__main__":
    unittest.main()
