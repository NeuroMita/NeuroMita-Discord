import os
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.abspath("src"))


class DiscordEntrypointTests(unittest.TestCase):
    def test_main_starts_bot_with_character_runtime(self):
        from discord_bot import __main__ as entrypoint

        config = Mock()
        runtime = Mock()
        bot = Mock()
        with (
            patch.object(entrypoint.DiscordBotConfig, "from_env", return_value=config),
            patch.object(entrypoint, "DiscordCharacterRuntime", return_value=runtime),
            patch.object(entrypoint, "create_bot", return_value=bot) as create_bot,
        ):
            result = entrypoint.main()

        self.assertEqual(result, 0)
        create_bot.assert_called_once_with(config, runtime=runtime)
        bot.run_configured.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
