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


if __name__ == "__main__":
    unittest.main()
