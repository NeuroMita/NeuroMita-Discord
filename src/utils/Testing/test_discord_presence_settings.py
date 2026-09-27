import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))


class DiscordPresenceSettingsTests(unittest.TestCase):
    def test_settings_are_bounded_persistent_and_default_to_alive(self):
        from discord_bot.presence_settings import PresenceSettingsStore

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Settings" / "presence.json"
            store = PresenceSettingsStore(path)
            self.assertEqual(store.get().mode, "alive")
            updated = store.update(mode="invalid", initiative=1000, max_per_hour=-1)
            reopened = PresenceSettingsStore(path).get()

        self.assertEqual(updated.mode, "alive")
        self.assertEqual(updated.initiative, 100)
        self.assertEqual(updated.max_per_hour, 0)
        self.assertEqual(reopened, updated)

    def test_autonomous_limits_default_and_validate_in_backoff_order(self):
        from discord_bot.presence_settings import PresenceSettingsStore

        with tempfile.TemporaryDirectory() as tmp:
            store = PresenceSettingsStore(Path(tmp) / "presence.json")
            defaults = store.get()
            bounded = store.update(
                initiative_max_per_6h=500,
                unanswered_backoff_1_seconds=1000,
                unanswered_backoff_2_seconds=500,
                unanswered_backoff_3_seconds=600,
                silence_ping_min_gap_seconds=1,
            )

        self.assertEqual(defaults.initiative_max_per_6h, 2)
        self.assertEqual((defaults.unanswered_backoff_1_seconds,
                          defaults.unanswered_backoff_2_seconds,
                          defaults.unanswered_backoff_3_seconds), (7200, 14400, 21600))
        self.assertEqual(defaults.silence_ping_min_gap_seconds, 7200)
        self.assertEqual(defaults.observation_idle_seconds, 60)
        self.assertEqual(bounded.initiative_max_per_6h, 20)
        self.assertEqual((bounded.unanswered_backoff_1_seconds,
                          bounded.unanswered_backoff_2_seconds,
                          bounded.unanswered_backoff_3_seconds), (1000, 1000, 1000))
        self.assertEqual(bounded.silence_ping_min_gap_seconds, 300)


if __name__ == "__main__":
    unittest.main()
