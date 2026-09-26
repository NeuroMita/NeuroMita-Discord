from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from discord_bot.llm_runtime import DiscordLLMRuntime


class DiscordCharacterRuntime:
    """Composition root for NeuroMita's existing character chat pipeline."""

    _ENV_PATH_KEYS = ("NEUROMITA_HISTORIES_DIR", "NEUROMITA_PROMPTS_DIR")

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        prompts_dir: Path | None = None,
    ) -> None:
        self.data_dir = (
            data_dir
            or Path(os.environ.get("NEUROMITA_DISCORD_DATA_DIR", "DiscordData"))
        ).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        default_prompts = Path(__file__).resolve().parents[2] / "Prompts"
        self.prompts_dir = (
            prompts_dir
            or Path(os.environ.get("NEUROMITA_PROMPTS_DIR", str(default_prompts)))
        ).expanduser().resolve()
        self._previous_env = {
            key: os.environ.get(key)
            for key in self._ENV_PATH_KEYS
        }
        self._base_runtime: DiscordLLMRuntime | None = None
        self._controllers: dict[str, Any] = {}
        self._extra_contracts: tuple[type, ...] = ()
        self._previous_extra_services: dict[type, Any] = {}
        self._started = False
        self._closed = False
        self._generation_service = None
        self._character_id = ""
        self.last_error = ""

        os.environ["NEUROMITA_HISTORIES_DIR"] = str(self.data_dir / "Histories")
        os.environ["NEUROMITA_PROMPTS_DIR"] = str(self.prompts_dir)
        try:
            self._base_runtime = DiscordLLMRuntime(data_dir=self.data_dir)
            self._register_runtime_services()
            self._create_controllers()
            self._generation_service = self._get_generation_service()
            self._character_id = self._active_character_id()
            self._started = True
        except Exception:
            self.close()
            raise

    @property
    def settings(self):
        if self._base_runtime is None:
            raise RuntimeError("Discord character runtime is not initialized")
        return self._base_runtime.settings

    @property
    def event_bus(self):
        if self._base_runtime is None:
            raise RuntimeError("Discord character runtime is not initialized")
        return self._base_runtime.event_bus

    def _register_runtime_services(self) -> None:
        from core.services import services
        from services.character_environment_context import DefaultCharacterEnvironmentContextService
        from services.contracts import (
            AppVarsService,
            CharacterEnvironmentContextService,
            CharacterRegistry,
            GameLinkService,
            GenerationService,
            HistoryService,
            ModelStateService,
            PromptBuilderService,
            RuntimeCapabilitiesService,
        )
        from services.game_link_service import DisconnectedGameLinkService
        from services.runtime_capabilities import DefaultRuntimeCapabilitiesService
        from services.settings_service import DefaultAppVarsService

        self._extra_contracts = (
            GameLinkService,
            RuntimeCapabilitiesService,
            AppVarsService,
            CharacterEnvironmentContextService,
            CharacterRegistry,
            HistoryService,
            PromptBuilderService,
            GenerationService,
            ModelStateService,
        )
        self._previous_extra_services = {
            contract: services().get_optional(contract)
            for contract in self._extra_contracts
        }
        game_link = DisconnectedGameLinkService()
        services().register(GameLinkService, game_link, replace=True)
        services().register(
            RuntimeCapabilitiesService,
            DefaultRuntimeCapabilitiesService(self.settings, game_link),
            replace=True,
        )
        services().register(
            AppVarsService,
            DefaultAppVarsService(self.settings, game_link),
            replace=True,
        )
        services().register(
            CharacterEnvironmentContextService,
            DefaultCharacterEnvironmentContextService(self.settings),
            replace=True,
        )

        # Keep chat API-only, while using the lightweight lexical RAG preset.
        self.settings.set("TOOLS_ON", False)
        self.settings.set("TOOLS_MODE", "off")
        self.settings.set("RAG_ENABLED", True)
        self.settings.set("RAG_PIPELINE_PRESET", "Keyword+FTS only")
        self.settings.set("RAG_VECTOR_SEARCH_ENABLED", False)
        self.settings.set("RAG_CROSS_ENCODER_ENABLED", False)
        self.settings.set("RAG_KEYWORD_SEARCH", True)
        self.settings.set("RAG_USE_FTS", True)
        self.settings.set("RAG_COMBINE_MODE", "union")
        self.settings.set("RAG_USE_RRF", False)
        self.settings.set("RAG_SEARCH_GRAPH", False)
        self.settings.set("GRAPH_EXTRACTION_ENABLED", False)
        self.settings.set("GRAPH_EXTRACTION_INLINE", False)
        self.settings.set("USE_VOICEOVER", False)

    def _create_controllers(self) -> None:
        from controllers.character_controller import CharacterController
        from controllers.history_controller import HistoryController
        from controllers.model_controller import ModelController
        from controllers.prompt_controller import PromptController

        self._controllers["history"] = HistoryController()
        self._controllers["prompt"] = PromptController()
        self._controllers["character"] = CharacterController(self.settings)
        self._controllers["model"] = ModelController(self.settings)

    @staticmethod
    def _get_generation_service():
        from core.services import services
        from services.contracts import GenerationService

        return services().get(GenerationService)

    def _active_character_id(self) -> str:
        character = self._controllers["character"].get_current_ref()
        return str(getattr(character, "char_id", "") or "")

    def generate(
        self,
        user_input: str,
        *,
        sender: str,
        origin_message_id: str,
    ):
        if not self._started or self._closed:
            raise RuntimeError("Discord character runtime is not ready")
        from core.request_policy import RequestPolicy
        from services.contracts import ChatGenerationRequest

        self._character_id = self._active_character_id()
        request = ChatGenerationRequest(
            character_id=self._character_id,
            user_input=str(user_input or ""),
            event_type="chat",
            sender=str(sender or "Discord user"),
            origin_message_id=str(origin_message_id or ""),
            policy=RequestPolicy(
                use_history_in_prompt=True,
                write_to_history=True,
                allow_voiceover=False,
                allow_streaming=False,
                echo_to_ui=False,
                system_input_role="system",
            ),
        )
        return self._generation_service.generate_chat(request)

    def record_error(self, message: str) -> None:
        self.last_error = str(message or "")[:500]

    def status(self) -> dict[str, object]:
        character = None
        if self._controllers.get("character") is not None:
            character = self._controllers["character"].get_current_ref()
        raw_prompt_path = str(getattr(character, "base_data_path", "") or "")
        prompt_path = Path(raw_prompt_path) if raw_prompt_path else None
        prompt_available = bool(
            prompt_path is not None
            and prompt_path.is_dir()
            and any(prompt_path.iterdir())
        )
        character_id = str(getattr(character, "char_id", "") or "")
        message = (
            "Character prompt assets are available."
            if prompt_available
            else f"Character prompt assets are missing at {prompt_path or self.prompts_dir}."
        )
        return {
            "state": "ready" if self._started and prompt_available else "degraded",
            "runtime_ready": self._started and not self._closed,
            "character_id": character_id,
            "prompt_set": str(getattr(character, "prompt_set_name", "") or ""),
            "prompt_path": str(prompt_path) if prompt_path else "",
            "prompt_assets_available": prompt_available,
            "message": message,
            "rag_enabled": bool(self.settings.get("RAG_ENABLED", False)),
            "rag_preset": str(self.settings.get("RAG_PIPELINE_PRESET", "") or ""),
            "last_error": str(getattr(self, "last_error", "") or ""),
            "history_dir": str(self.data_dir / "Histories"),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        event_bus = getattr(self._base_runtime, "event_bus", None)
        try:
            model = self._controllers.get("model")
            if model is not None:
                model.shutdown()
            history = self._controllers.get("history")
            if history is not None:
                history.close()
            character = self._controllers.get("character")
            if character is not None:
                if event_bus is not None:
                    event_bus.unsubscribe_owner(character)
                resources = character.character_manager.resources
                resources.history_manager.shutdown_executor()
                resources.memory_manager.shutdown_executor()
            if event_bus is not None:
                for controller in self._controllers.values():
                    event_bus.unsubscribe_owner(controller)
            if self._extra_contracts:
                from core.services import services

                for contract in reversed(self._extra_contracts):
                    services().unregister(contract)
                for contract, previous in self._previous_extra_services.items():
                    if previous is not None:
                        services().register(contract, previous, replace=True)
        finally:
            if self._base_runtime is not None:
                self._base_runtime.close()
            for key, value in self._previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
