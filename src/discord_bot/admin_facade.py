from __future__ import annotations

from typing import Any


class DiscordAdminFacade:
    """Bounded administrative view over the existing character managers."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    @property
    def character_controller(self):
        controllers = getattr(self.runtime, "_controllers", {})
        return getattr(self.runtime, "character_controller", None) or controllers["character"]

    @property
    def history_controller(self):
        controllers = getattr(self.runtime, "_controllers", {})
        return getattr(self.runtime, "history_controller", None) or controllers["history"]

    def _character(self):
        return self.character_controller.get_current_ref()

    def bot_status(self) -> dict[str, Any]:
        status = dict(self.runtime.status())
        status["character_id"] = str(getattr(self._character(), "char_id", "") or "")
        return status

    @staticmethod
    def resource_usage() -> dict[str, float]:
        import psutil

        return {"rss_mb": psutil.Process().memory_info().rss / (1024 * 1024)}

    def characters(self) -> list[dict[str, str]]:
        manager = self.character_controller.character_manager
        return [
            {"id": char_id, "name": manager.get_display_name(char_id)}
            for char_id in manager.get_all_characters()
        ]

    def character_status(self) -> dict[str, str]:
        character = self._character()
        return {
            "id": str(getattr(character, "char_id", "") or ""),
            "name": str(getattr(character, "display_name", "") or ""),
            "prompt_set": str(getattr(character, "prompt_set_name", "") or ""),
        }

    def character_set(self, character_id: str) -> bool:
        from core.events import Events

        target = self.character_controller.get_ref(character_id)
        if target is None:
            return False
        self.runtime.event_bus.emit(
            Events.Character.SET_CURRENT, {"character_id": character_id}, sync=True
        )
        return str(getattr(self._character(), "char_id", "")) == character_id

    def history_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        character = self._character()
        data = character.history_manager.load_history() or {}
        messages = data.get("messages", []) if isinstance(data, dict) else data
        return list(messages or [])[-max(1, min(20, int(limit))):]

    def history_summary(self) -> str:
        return self.history_controller.get_summary(self._character())

    def history_reset(self, *, confirm: bool = False) -> bool:
        if not confirm:
            return False
        from core.events import Events

        return bool(self.runtime.event_bus.emit(Events.Character.CLEAR_HISTORY, {}, sync=True))

    def memory_list(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._character().memory_system.list_memories(limit=limit)

    def memory_show(self, memory_id: int) -> str | None:
        return self._character().memory_system.get_memory_content(int(memory_id))

    def memory_add(self, content: str, priority: str = "Normal") -> int | None:
        if not str(content or "").strip():
            return None
        return self._character().memory_system.add_memory(
            str(content)[:2000], priority=str(priority)[:20], memory_type="fact"
        )

    def memory_delete(self, memory_id: int, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            return {"deleted": False, "reason": "confirmation_required"}
        deleted = bool(self._character().memory_system.delete_memory(int(memory_id)))
        return {"deleted": deleted}

    def memory_maintenance(self, *, confirm: bool = False) -> dict[str, Any]:
        if not confirm:
            return {"merged": 0, "clusters": 0, "confirmation_required": True}
        return self._character().memory_system.run_maintenance()

    def compression_status(self) -> dict[str, Any]:
        settings = self.runtime.settings
        return {
            "enabled": bool(settings.get("ENABLE_HISTORY_COMPRESSION_ON_LIMIT", True))
            or bool(settings.get("ENABLE_HISTORY_COMPRESSION_PERIODIC", False)),
            "provider": "utility preset chain",
        }

    def compression_set(self, enabled: bool) -> None:
        self.runtime.settings.set("ENABLE_HISTORY_COMPRESSION_ON_LIMIT", bool(enabled))
        if not enabled:
            self.runtime.settings.set("ENABLE_HISTORY_COMPRESSION_PERIODIC", False)

    def ai_status(self) -> dict[str, Any]:
        cid = str(getattr(self._character(), "char_id", "") or "")
        resolver = self.runtime._base_runtime.preset_resolver
        chain = resolver.resolve_chain()
        safe_chain = [{
            "name": str(getattr(preset, "preset_name", "")),
            "provider": str(getattr(preset, "provider_display_name", "") or getattr(preset, "provider_name", "")),
            "model": str(getattr(preset, "api_model", "")),
        } for preset in chain]
        return {
            "character_id": cid,
            "chain": safe_chain,
        }
