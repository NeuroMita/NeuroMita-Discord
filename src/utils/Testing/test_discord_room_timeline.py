import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))


class DiscordRoomTimelineTests(unittest.TestCase):
    def setUp(self):
        from discord_bot.room_timeline import DiscordRoomTimeline

        self.temp = tempfile.TemporaryDirectory()
        self.timeline = DiscordRoomTimeline(Path(self.temp.name) / "room.sqlite3")

    def tearDown(self):
        self.timeline.close()
        self.temp.cleanup()

    def message(self, mid, content, *, kind="human", author_id="7", created_at=None):
        from discord_bot.room_timeline import RoomMessage

        return RoomMessage(
            discord_message_id=str(mid), guild_id="1", channel_id="2",
            author_id=author_id, author_name=f"user-{author_id}", author_kind=kind,
            content=content, created_at=created_at or f"2026-09-27T00:00:{int(mid):02d}+00:00",
        )

    def test_append_is_idempotent_and_recent_is_newest_limited_in_chronological_order(self):
        self.timeline.append_message(self.message(1, "one"))
        self.timeline.append_message(self.message(1, "duplicate"))
        self.timeline.append_message(self.message(2, "two"))
        self.timeline.append_message(self.message(3, "three"))

        recent = self.timeline.recent(guild_id="1", channel_id="2", limit=2)

        self.assertEqual([message.content for message in recent], ["two", "three"])
        self.assertEqual(self.timeline.count_messages(guild_id="1", channel_id="2"), 3)

    def test_edit_delete_and_summary_anchor_persist_after_reopen(self):
        self.timeline.append_message(self.message(1, "one"))
        self.timeline.append_message(self.message(2, "two"))
        self.assertTrue(self.timeline.update_message("1", content="edited"))
        self.assertTrue(self.timeline.mark_deleted("2"))
        self.timeline.commit_summary(
            guild_id="1", channel_id="2", summary="old room facts", through_row_id=2,
        )
        path = self.timeline.path
        self.timeline.close()

        from discord_bot.room_timeline import DiscordRoomTimeline
        self.timeline = DiscordRoomTimeline(path)
        self.assertEqual(self.timeline.get_summary(guild_id="1", channel_id="2"), "old room facts")
        self.assertEqual(self.timeline.summary_through_row_id(guild_id="1", channel_id="2"), 2)
        self.assertEqual([m.content for m in self.timeline.recent(guild_id="1", channel_id="2")], ["edited"])

    def test_messages_for_summary_keeps_recent_tail_and_bounds_batch(self):
        for mid in range(1, 8):
            self.timeline.append_message(self.message(mid, str(mid)))

        batch = self.timeline.messages_for_summary(
            guild_id="1", channel_id="2", keep_recent=2, max_items=3,
        )

        self.assertEqual([message.content for message in batch], ["1", "2", "3"])

    def test_edit_or_delete_inside_compacted_range_invalidates_stale_room_summary(self):
        for mid in range(1, 5):
            self.timeline.append_message(self.message(mid, str(mid)))
        self.timeline.commit_summary(
            guild_id="1", channel_id="2", summary="contains old message 1", through_row_id=2,
        )

        self.assertTrue(self.timeline.mark_deleted("1"))

        self.assertEqual(self.timeline.get_summary(guild_id="1", channel_id="2"), "")
        self.assertEqual(self.timeline.summary_through_row_id(guild_id="1", channel_id="2"), 0)
        self.assertEqual([m.content for m in self.timeline.recent(guild_id="1", channel_id="2")], ["2", "3", "4"])


if __name__ == "__main__":
    unittest.main()
