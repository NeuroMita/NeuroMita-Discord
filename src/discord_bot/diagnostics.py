from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import Any


_EVENT_FIELDS = {
    "bot_created": {"guild_id", "channel_id"},
    "bot_stopped": set(),
    "gateway_failed": {"exception_type"},
    "commands_synced": {"count"},
    "command_sync_failed": {"exception_type"},
    "gateway_ready": {"bot_id"},
    "channel_ready": {"channel_id"},
    "presence_ready": set(),
    "presence_start_failed": {"exception_type"},
    "message_received": {"guild_id", "channel_id", "trigger"},
    "message_ignored": {"guild_id", "channel_id", "reason"},
    "direct_generation_started": set(),
    "direct_generation_finished": {"response_chars", "chunk_count"},
    "generation_failed": {"exception_type"},
    "direct_reply_sent": {"chunk_count"},
    "discord_send_failed": {"exception_type", "status"},
    "presence_gate": {"kind", "allowed", "reason"},
    "presence_decision": {"action", "desire", "speak", "reason", "diagnostic_code", "status"},
    "presence_generation_finished": {"response_chars"},
    "presence_message_sent": {"action"},
    "attention_request_failed": {"diagnostic_code", "status"},
    "presence_send_skipped": {"reason"},
    "presence_send_failed": {"exception_type", "status"},
    "presence_evaluation_failed": {"kind", "exception_type"},
}
_ENUMS = {
    "trigger": {"mention", "reply", "ambient", "slash"},
    "reason": {
        "disabled", "mode_direct", "mode_not_alive", "paused", "generation_busy",
        "attention_busy", "evaluation_cooldown", "voluntary_cooldown", "hourly_budget",
        "no_room_activity", "invalid_activity_time", "room_not_quiet", "not_direct",
        "empty_content", "bot_or_webhook", "outside_allowed_location", "attention_silent",
        "desire_below_threshold", "generation_failed", "empty_response", "room_changed",
        "decision_error", "direct_relevance", "open_question", "topic_fit",
        "social_moment", "invited", "conversation_active", "conversation_closed",
        "nothing_to_add", "too_intrusive", "good_topic_to_start", "silence_checkin",
    },
    "kind": {"social", "initiative"},
    "action": {"silent", "join", "start_topic", "silence_ping"},
    "diagnostic_code": {
        "provider_error", "empty_result", "request_exception", "invalid_json",
        "invalid_schema", "invalid_desire", "invalid_action", "invalid_reason",
        "invalid_reply_target", "invalid_speak_action", "unrecognized_reason",
        "unrecognized_action",
    },
}
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class DiscordRuntimeDiagnostics:
    """Per-run allowlisted diagnostics; never accepts message or prompt content."""

    def __init__(self, log_dir: Path):
        self.run_id = uuid.uuid4().hex[:12]
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        self.path = log_dir / f"discord-runtime-{os.getpid()}-{self.run_id}.jsonl"
        self._logger = logging.getLogger(f"discord_bot.diagnostics.{os.getpid()}.{self.run_id}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = logging.FileHandler(self.path, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)

    def record(self, event: str, **fields: Any) -> None:
        allowed = _EVENT_FIELDS.get(event)
        if allowed is None:
            return
        safe: dict[str, Any] = {}
        for key in sorted(allowed):
            value = fields.get(key)
            if value is None:
                continue
            if key in _ENUMS:
                if isinstance(value, str) and value in _ENUMS[key]:
                    safe[key] = value
            elif key in {"guild_id", "channel_id", "bot_id"}:
                if isinstance(value, (str, int)) and str(value).isdigit() and len(str(value)) <= 24:
                    safe[key] = str(value)
            elif key == "exception_type":
                if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value):
                    safe[key] = value
            elif key == "status":
                if type(value) is int and 100 <= value <= 599:
                    safe[key] = value
            elif key in {"allowed", "speak"}:
                if isinstance(value, bool):
                    safe[key] = value
            elif key in {"count", "desire", "response_chars", "chunk_count"}:
                if type(value) is int and 0 <= value <= 10_000_000:
                    safe[key] = value

        payload = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "pid": os.getpid(),
            "run_id": self.run_id,
            "event": event,
            **safe,
        }
        self._logger.info(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()
