from __future__ import annotations

import json
import os
import asyncio
import tempfile
import unittest
from pathlib import Path


class DiscordRuntimeDiagnosticsTests(unittest.TestCase):
    def test_writes_process_and_run_identity_without_unapproved_fields(self):
        from discord_bot.diagnostics import DiscordRuntimeDiagnostics

        with tempfile.TemporaryDirectory() as temp_dir:
            diagnostics = DiscordRuntimeDiagnostics(Path(temp_dir))
            diagnostics.record(
                "message_received",
                trigger="mention",
                channel_id=1353745092065624144,
                content="private conversation text",
                token="secret-token",
            )
            diagnostics.record(
                "room_observation_skipped", reason="observation_busy",
                room_context="private room text", summary="private summary",
            )
            self.assertIn(str(os.getpid()), diagnostics.path.name)
            diagnostics.close()

            lines = diagnostics.path.read_text(encoding="utf-8").splitlines()
            line = lines[0]
            payload = json.loads(line)
            self.assertEqual(payload["pid"], os.getpid())
            self.assertTrue(payload["run_id"])
            self.assertIn(payload["run_id"], diagnostics.path.name)
            self.assertEqual(payload["event"], "message_received")
            self.assertEqual(payload["trigger"], "mention")
            self.assertNotIn("private conversation text", line)
            self.assertNotIn("secret-token", line)
            self.assertNotIn("content", payload)
            self.assertNotIn("token", payload)
            observation = json.loads(lines[-1])
            self.assertEqual(observation["reason"], "observation_busy")
            self.assertNotIn("private room text", lines[-1])
            self.assertNotIn("private summary", lines[-1])

    def test_gateway_failure_remains_logged_after_client_shutdown(self):
        from discord_bot.bot import DiscordBot

        with tempfile.TemporaryDirectory() as temp_dir:
            class Runtime:
                data_dir = Path(temp_dir)

                def close(self):
                    pass

            bot = DiscordBot(token="test", runtime=Runtime())
            log_path = bot.diagnostics.path

            async def close_then_fail():
                await bot.close()
                raise RuntimeError("test only")

            def run(*_args, **_kwargs):
                asyncio.run(close_then_fail())

            bot.run = run
            with self.assertRaisesRegex(RuntimeError, "test only"):
                bot.run_configured()

            events = [json.loads(line)["event"] for line in log_path.read_text(encoding="utf-8").splitlines()]
            self.assertIn("gateway_failed", events)
            self.assertEqual(events.count("bot_stopped"), 1)


if __name__ == "__main__":
    unittest.main()
