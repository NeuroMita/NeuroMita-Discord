from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PresenceSettings:
    enabled: bool = True
    mode: str = "alive"
    initiative: int = 55
    cooldown_seconds: int = 240
    max_per_hour: int = 3
    debounce_min_seconds: float = 6.0
    debounce_max_seconds: float = 12.0
    evaluation_min_interval_seconds: int = 120
    quiet_start_seconds: int = 900
    summary_threshold: int = 80
    summary_keep_recent: int = 30
    summary_max_batch: int = 50
    initiative_max_per_6h: int = 2
    unanswered_backoff_1_seconds: int = 7200
    unanswered_backoff_2_seconds: int = 14400
    unanswered_backoff_3_seconds: int = 21600
    silence_ping_min_gap_seconds: int = 7200
    observation_idle_seconds: int = 60


class PresenceSettingsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._settings = self._load()

    @staticmethod
    def _validated(values: dict[str, Any]) -> PresenceSettings:
        defaults = asdict(PresenceSettings())
        clean = {key: values.get(key, default) for key, default in defaults.items()}
        clean["mode"] = str(clean["mode"]).lower()
        if clean["mode"] not in {"direct", "social", "alive"}:
            clean["mode"] = "alive"
        clean["enabled"] = clean["enabled"] if isinstance(clean["enabled"], bool) else defaults["enabled"]
        for key, lower, upper in (
            ("initiative", 0, 100), ("cooldown_seconds", 30, 86400),
            ("max_per_hour", 0, 20), ("evaluation_min_interval_seconds", 60, 3600),
            ("quiet_start_seconds", 180, 86400), ("summary_threshold", 20, 1000),
            ("summary_keep_recent", 5, 100), ("summary_max_batch", 5, 100),
            ("initiative_max_per_6h", 0, 20),
            ("unanswered_backoff_1_seconds", 300, 86400),
            ("unanswered_backoff_2_seconds", 300, 172800),
            ("unanswered_backoff_3_seconds", 300, 259200),
            ("silence_ping_min_gap_seconds", 300, 259200),
            ("observation_idle_seconds", 10, 3600),
        ):
            clean[key] = max(lower, min(upper, int(clean[key])))
        clean["unanswered_backoff_2_seconds"] = max(
            clean["unanswered_backoff_1_seconds"], clean["unanswered_backoff_2_seconds"],
        )
        clean["unanswered_backoff_3_seconds"] = max(
            clean["unanswered_backoff_2_seconds"], clean["unanswered_backoff_3_seconds"],
        )
        clean["debounce_min_seconds"] = max(1.0, min(60.0, float(clean["debounce_min_seconds"])))
        clean["debounce_max_seconds"] = max(
            clean["debounce_min_seconds"], min(120.0, float(clean["debounce_max_seconds"]))
        )
        return PresenceSettings(**clean)

    def _load(self) -> PresenceSettings:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._validated(raw if isinstance(raw, dict) else {})
        except (OSError, ValueError, TypeError):
            return PresenceSettings()

    def get(self) -> PresenceSettings:
        with self._lock:
            return self._settings

    def update(self, **changes: Any) -> PresenceSettings:
        with self._lock:
            values = asdict(self._settings)
            values.update(changes)
            updated = self._validated(values)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix="presence-", suffix=".tmp", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(asdict(updated), stream, ensure_ascii=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, self.path)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            self._settings = updated
            return updated
