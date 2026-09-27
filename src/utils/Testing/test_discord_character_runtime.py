import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

sys.path.insert(0, os.path.abspath("src"))


class DiscordCharacterRuntimeRequestTests(unittest.TestCase):
    def test_room_observation_runs_native_memory_pipeline_without_history_or_fake_turn(self):
        from discord_bot.character_runtime import DiscordCharacterRuntime

        result = object()
        generation = Mock(return_value=result)
        generation.generate_chat.return_value = result
        runtime = DiscordCharacterRuntime.__new__(DiscordCharacterRuntime)
        runtime._generation_service = generation
        runtime._character_id = "Crazy"
        runtime._started = True
        runtime._closed = False
        runtime._controllers = {"character": Mock()}
        runtime._controllers["character"].get_current_ref.return_value = type(
            "Character", (), {"char_id": "Crazy"}
        )()

        actual = runtime.observe_room(
            ambient_context="observed participants discuss an ongoing issue",
            request_id="discord-observe-1",
        )

        self.assertIs(actual, result)
        request = generation.generate_chat.call_args.args[0]
        self.assertEqual(request.event_type, "discord_room_observe")
        self.assertEqual(request.user_input, "")
        self.assertEqual(request.sender, "DiscordRoom")
        self.assertEqual(request.req_id, "discord-observe-1")
        self.assertIsNone(request.origin_message_id)
        self.assertEqual(request.hidden_user_context, "observed participants discuss an ongoing issue")
        self.assertFalse(request.policy.use_history_in_prompt)
        self.assertFalse(request.policy.write_to_history)
        self.assertFalse(request.policy.allow_voiceover)
        self.assertFalse(request.policy.allow_streaming)
        self.assertFalse(request.policy.echo_to_ui)

    def test_runtime_bootstraps_character_pipeline_without_desktop_or_ml_imports(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ)
            env["PYTHONPATH"] = os.path.abspath("src")
            env["NEUROMITA_DISCORD_DATA_DIR"] = directory
            env["NEUROMITA_PROMPTS_DIR"] = str(Path(directory) / "missing-prompts")
            code = """
import sys
from discord_bot.character_runtime import DiscordCharacterRuntime
from services.contracts import GenerationService
from core.services import services
runtime = DiscordCharacterRuntime()
assert services().get(GenerationService) is runtime._generation_service
assert runtime.settings.get('RAG_ENABLED') is True
assert runtime.settings.get('RAG_PIPELINE_PRESET') == 'Keyword+FTS only'
assert runtime.settings.get('RAG_VECTOR_SEARCH_ENABLED') is False
assert runtime.settings.get('RAG_USE_FTS') is True
blocked = {'PyQt6', 'torch', 'transformers', 'cv2', 'pygame', 'pyaudio'}
assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)
runtime.close()
assert not blocked.intersection(sys.modules), blocked.intersection(sys.modules)
print('character runtime lightweight bootstrap OK')
"""
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=os.getcwd(),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=45,
            )

        self.assertEqual(result.returncode, 0, (result.stdout or "") + (result.stderr or ""))
        self.assertIn("character runtime lightweight bootstrap OK", result.stdout)

    def test_generate_uses_native_chat_generation_contract_and_history_policy(self):
        from discord_bot.character_runtime import DiscordCharacterRuntime

        result = object()
        generation = Mock(return_value=result)
        generation.generate_chat.return_value = result
        runtime = DiscordCharacterRuntime.__new__(DiscordCharacterRuntime)
        runtime._generation_service = generation
        runtime._character_id = "Crazy"
        runtime._started = True
        runtime._closed = False
        runtime._controllers = {"character": Mock()}
        runtime._controllers["character"].get_current_ref.return_value = type(
            "Character", (), {"char_id": "Crazy"}
        )()

        actual = runtime.generate(
            "remember this",
            sender="Discord:Test user [id:292002437932384256]",
            request_id="1461234567890123456",
            ambient_context="observed room context",
        )

        self.assertIs(actual, result)
        request = generation.generate_chat.call_args.args[0]
        self.assertEqual(request.character_id, "Crazy")
        self.assertEqual(request.user_input, "remember this")
        self.assertEqual(request.sender, "Discord:Test user [id:292002437932384256]")
        self.assertEqual(request.req_id, "1461234567890123456")
        self.assertIsNone(request.origin_message_id)
        self.assertEqual(request.hidden_user_context, "observed room context")
        self.assertEqual(request.event_type, "chat")
        self.assertEqual(request.generation_params_override, {})
        self.assertTrue(request.policy.use_history_in_prompt)
        self.assertTrue(request.policy.write_to_history)
        self.assertFalse(request.policy.allow_voiceover)
        self.assertFalse(request.policy.allow_streaming)
        self.assertFalse(request.policy.echo_to_ui)

    def test_proactive_character_turn_bounds_completion_and_reasoning(self):
        from discord_bot.character_runtime import DiscordCharacterRuntime

        generation = Mock()
        generation.generate_chat.return_value = object()
        runtime = DiscordCharacterRuntime.__new__(DiscordCharacterRuntime)
        runtime._generation_service = generation
        runtime._started = True
        runtime._closed = False
        runtime._controllers = {"character": Mock()}
        runtime._controllers["character"].get_current_ref.return_value = type(
            "Character", (), {"char_id": "Crazy"}
        )()

        runtime.generate_initiative(
            ambient_context="a recent room conversation",
            request_id="discord-initiative-1",
            mode="start_topic",
        )

        request = generation.generate_chat.call_args.args[0]
        self.assertEqual(request.generation_params_override, {
            "max_tokens": 1200,
            "reasoning": {"enabled": True, "max_tokens": 256},
        })

    def test_memory_embed_schedule_does_not_initialize_rag_when_disabled(self):
        from managers.memory_manager import MemoryManager
        from managers.settings_manager import SettingsManager

        manager = MemoryManager.__new__(MemoryManager)
        with (
            patch.object(SettingsManager, "get", return_value=False),
            patch.object(MemoryManager, "rag", new_callable=PropertyMock) as rag,
        ):
            manager._schedule_embed(42, "remembered fact")

        rag.assert_not_called()

    def test_memory_embed_schedule_does_not_vectorize_in_keyword_fts_mode(self):
        from managers.memory_manager import MemoryManager
        from managers.settings_manager import SettingsManager

        manager = MemoryManager.__new__(MemoryManager)
        with (
            patch.object(
                SettingsManager,
                "get",
                side_effect=lambda key, default=None: {
                    "RAG_ENABLED": True,
                    "RAG_VECTOR_SEARCH_ENABLED": False,
                }.get(key, default),
            ),
            patch.object(MemoryManager, "rag", new_callable=PropertyMock) as rag,
        ):
            manager._schedule_embed(42, "remembered fact")

        rag.assert_not_called()

    def test_status_reports_missing_character_prompt_assets_as_degraded(self):
        from discord_bot.character_runtime import DiscordCharacterRuntime

        with tempfile.TemporaryDirectory() as directory:
            runtime = DiscordCharacterRuntime.__new__(DiscordCharacterRuntime)
            runtime._started = True
            runtime._closed = False
            runtime.data_dir = Path(directory)
            runtime.prompts_dir = Path(directory) / "Prompts"
            runtime._character_id = "Crazy"
            runtime._controllers = {"character": Mock()}
            character = Mock()
            character.prompt_set_name = "Default"
            character.base_data_path = str(runtime.prompts_dir / "Crazy" / "Default")
            runtime._controllers["character"].get_current_ref.return_value = character
            runtime._base_runtime = Mock()
            runtime._base_runtime.settings.get.side_effect = lambda key, default=None: default

            status = runtime.status()

        self.assertEqual(status["state"], "degraded")
        self.assertFalse(status["prompt_assets_available"])

    def test_status_does_not_treat_runtime_generated_files_as_prompt_assets(self):
        from discord_bot.character_runtime import DiscordCharacterRuntime

        with tempfile.TemporaryDirectory() as directory:
            prompt_path = Path(directory) / "Crazy" / "Default"
            prompt_path.mkdir(parents=True)
            (prompt_path / "variables.json").write_text("{}", encoding="utf-8")
            runtime = DiscordCharacterRuntime.__new__(DiscordCharacterRuntime)
            runtime._started = True
            runtime._closed = False
            runtime.data_dir = Path(directory)
            runtime.prompts_dir = Path(directory)
            runtime._controllers = {"character": Mock()}
            character = Mock()
            character.char_id = "Crazy"
            character.prompt_set_name = "Default"
            character.base_data_path = str(prompt_path)
            runtime._controllers["character"].get_current_ref.return_value = character
            runtime._base_runtime = Mock()
            runtime._base_runtime.settings.get.side_effect = lambda key, default=None: default

            status = runtime.status()

        self.assertEqual(status["state"], "degraded")
        self.assertFalse(status["prompt_assets_available"])
        self.assertIn("prompt", status["message"].lower())


if __name__ == "__main__":
    unittest.main()
