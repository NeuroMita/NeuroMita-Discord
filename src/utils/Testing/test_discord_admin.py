import os
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, os.path.abspath("src"))


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = None

    def execute(self, query, params):
        self.query = (query, params)

    def fetchall(self):
        return self.rows


class MemoryListTests(unittest.TestCase):
    def test_list_memories_is_bounded_and_returns_only_manager_fields(self):
        from managers.character_scoped_service import CharacterScopedService
        from managers.memory_manager import MemoryManager

        cursor = FakeCursor([(8, "a saved fact", "fact", "High", "2026-09-26", 0)])
        connection = Mock()
        connection.cursor.return_value = cursor
        manager = MemoryManager.__new__(MemoryManager)
        CharacterScopedService.__init__(
            manager,
            default_character_id="Crazy",
            default_storage_name="Crazy",
        )
        manager.db = Mock(get_connection=Mock(return_value=connection))
        manager._mem_cols = Mock(return_value={
            "eternal_id", "content", "type", "priority", "date_created",
            "is_forgotten", "is_deleted", "character_id",
        })

        result = manager.list_memories(limit=500)

        self.assertEqual(result, [{
            "eternal_id": 8,
            "content": "a saved fact",
            "type": "fact",
            "priority": "High",
            "date_created": "2026-09-26",
            "is_forgotten": 0,
        }])
        self.assertLessEqual(cursor.query[1][-1], 50)
        self.assertIn("LIMIT ?", cursor.query[0])


class DiscordAdminFacadeTests(unittest.TestCase):
    def test_character_reset_all_requires_confirmation_and_reports_destructive_event(self):
        from discord_bot.admin_facade import DiscordAdminFacade
        from core.events import Events

        runtime = Mock()
        runtime.event_bus.emit.return_value = True
        facade = DiscordAdminFacade(runtime)

        self.assertFalse(facade.character_reset_all(confirm=False))
        runtime.event_bus.emit.assert_not_called()
        self.assertTrue(facade.character_reset_all(confirm=True))
        runtime.event_bus.emit.assert_called_once_with(Events.Character.CLEAR_HISTORY, {}, sync=True)

    def test_history_recent_is_bounded_to_small_response_window(self):
        from discord_bot.admin_facade import DiscordAdminFacade

        character = Mock()
        character.history_manager.load_history.return_value = {
            "messages": [{"role": "user", "content": f"message {n}"} for n in range(100)]
        }
        runtime = Mock()
        runtime.character_controller.get_current_ref.return_value = character
        facade = DiscordAdminFacade(runtime)

        items = facade.history_recent(500)

        self.assertEqual(len(items), 20)
        self.assertEqual(items[-1]["content"], "message 99")

    def test_destructive_memory_delete_requires_explicit_confirmation(self):
        from discord_bot.admin_facade import DiscordAdminFacade

        character = Mock()
        runtime = Mock()
        runtime.character_controller.get_current_ref.return_value = character
        facade = DiscordAdminFacade(runtime)

        result = facade.memory_delete(5, confirm=False)

        self.assertFalse(result["deleted"])
        character.memory_system.delete_memory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
