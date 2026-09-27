# Discord Stage 4 Pre-deploy Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Harden the existing Discord presence runtime for a bounded first VPS deployment.

**Architecture:** Keep shared character-pipeline work serialized in one generation executor and utility decisions/summaries in one separately admitted utility executor. Keep room state in the existing SQLite timeline, add only additive migrations, and cancel stale room observations by revision.

**Tech Stack:** Python 3.12, asyncio, `ThreadPoolExecutor`, SQLite, unittest.

**Spec:** `docs/superpowers/specs/2026-09-27-discord-stage4-hardening-design.md`

## Global Constraints

- Do not add dependencies, voice, providers, or Discord capabilities beyond expanded `/presence status`.
- Keep exactly one generation worker and one utility worker; do not concurrently call the shared character runtime.
- Never log room text, prompt/summary/memory content, provider error strings, or secrets.
- Preserve direct history (`req_id`, `origin_message_id=None`) and observation's no-history policy.
- Do not commit, push, deploy, or restart the live bot.

## Review Focus

- Existing SQLite databases must preserve summaries/messages and initialize new state without data loss.
- Discord send failures and empty generations must not consume autonomous initiative budget.
- Human messages and direct turns must reset unanswered streak/cancel stale observation without erasing cooldown timestamps.
- A malformed attention response must never trigger speech, even if fields look individually valid.
- Shutdown/cancellation must not close SQLite or the runtime while a worker still uses them.

---

### Task 1: Isolate utility work and bound admission

**Files:**
- Modify: `src/discord_bot/bot.py`
- Modify: `src/discord_bot/presence_controller.py`
- Modify: `src/discord_bot/presence_commands.py`
- Test: `src/utils/Testing/test_discord_bot.py`
- Test: `src/utils/Testing/test_discord_presence.py`

**Interfaces:** `DiscordBot._run_presence_worker(function)` runs utility work and raises `UtilityBusyError` rather than queueing a second job; `DiscordBot.generation_busy` and `utility_busy` expose booleans. Presence receives `is_utility_busy` and returns a bounded skip/reschedule when occupied.

- [x] Add tests proving a blocked utility call does not block direct generation and uses a separate thread.
- [x] Add tests proving canceled awaits remain busy until the backing generation or utility worker finishes.
- [x] Add tests proving overlapping presence utility submissions are not queued without bound; attention takes precedence and summaries remain eligible later.
- [x] Run focused tests and observe expected failure.
- [x] Add the second one-worker executor, idempotent shutdown, utility admission/state, and expose occupancy in presence status.
- [x] Run bot and presence tests.

### Task 2: Persist autonomous anti-spam state

**Files:**
- Modify: `src/discord_bot/presence_settings.py`
- Modify: `src/discord_bot/room_timeline.py`
- Modify: `src/discord_bot/presence_controller.py`
- Modify: `src/discord_bot/diagnostics.py`
- Modify: `src/discord_bot/presence_commands.py`
- Test: `src/utils/Testing/test_discord_presence_settings.py`
- Test: `src/utils/Testing/test_discord_room_timeline.py`
- Test: `src/utils/Testing/test_discord_presence.py`
- Test: `src/utils/Testing/test_discord_diagnostics.py`

**Interfaces:** `DiscordRoomTimeline.count_initiative_messages_since(*, guild_id, channel_id, since) -> int`; `unanswered_initiative_streak(*, guild_id, channel_id) -> int`; `record_initiative_sent(*, guild_id, channel_id, action, created_at) -> None`; `reset_unanswered_initiative(*, guild_id, channel_id) -> None`.

- [x] Test setting defaults and clamps, including ordered 2h/4h/6h backoff values.
- [x] Test migration from the old `room_state` schema, preserving messages/summary; test count filters, atomic state updates, and streak reset preserving timestamps.
- [x] Test initiative six-hour cap, unanswered backoff, silence-ping gap, reset on human activity, and no budget consumption after send failure.
- [x] Run those tests and observe expected failures.
- [x] Implement defaults (`initiative_max_per_6h=2`, backoff `7200/14400/21600`, silence gap `7200`), SQLite migration, timeline APIs, gates, and post-send recording.
- [x] Add safe diagnostic enums and status fields for current counts, streak, and timestamps.
- [x] Run settings, timeline, presence, diagnostics, and command tests.

### Task 3: Defer room observation and fix slash ambient context

**Files:**
- Modify: `src/discord_bot/presence_controller.py`
- Modify: `src/discord_bot/bot.py`
- Test: `src/utils/Testing/test_discord_presence.py`
- Test: `src/utils/Testing/test_discord_presence_integration.py`
- Test: `src/utils/Testing/test_discord_character_runtime.py`

**Interfaces:** Presence adds `_schedule_observation()`, `_observation_worker(revision)`, and `notify_room_changed()` cancellation behavior. Observation delay defaults to 60 seconds. `/chat ask` builds context using `exclude_message_ids={str(interaction.id)}`.

- [x] Test delayed observation scheduling, cancellation on room/direct revision changes and stop, and non-overlap while generation/attention is busy.
- [x] Test observations are skipped while character or attention work is busy, and stale summaries do not overwrite current room state.
- [x] Test successful observation keeps no-history request policy; retain existing direct history regression.
- [x] Test slash direct input is passed once and excluded from ambient context.
- [x] Run tests and observe expected failures.
- [x] Implement deferred observation on the generation path with revision/busy checks; cancel pending work on all specified room/direct/stop events; capture summary revision so stale summaries do not trigger observation.
- [x] Exclude the active slash interaction from ambient context.
- [x] Run presence, integration, and character runtime tests.

### Task 4: Make attention parsing fail closed

**Files:**
- Modify: `src/discord_bot/attention.py`
- Modify: `src/discord_bot/diagnostics.py`
- Test: `src/utils/Testing/test_discord_attention.py`
- Test: `src/utils/Testing/test_discord_diagnostics.py`

**Interfaces:** `_parse_decision(raw, *, valid_reply_ids, allowed_actions, allowed_reasons, allow_reply_target) -> AttentionDecision`; `decide_social` uses social sets and allows validated reply targets; `decide_initiative` uses initiative sets and rejects non-null targets.

- [x] Replace fallback-oriented tests with failing tests for unknown reason/action, wrong mode action, malformed types, inconsistent speak/action, invalid desire bounds, and invalid reply target.
- [x] Test valid social/initiative decisions and ignored harmless extra fields.
- [x] Run attention tests and observe expected failures.
- [x] Implement separate action/reason sets and strict validation returning silent diagnostic decisions; remove fallback action behavior and obsolete diagnostic values.
- [x] Run attention and diagnostics tests.

### Task 5: Full Discord verification and review

**Files:** all files above.

- [x] Run all `test_discord_*.py` tests under `src/utils/Testing`.
- [x] Run `compileall` for `src/discord_bot` and `git diff --check`.
- [x] Review the complete diff for architecture, privacy, cancellation, and migration defects; fix critical/important findings with failing regression tests first.
- [x] Report test output, working-tree status, and explicitly state that Linux/live Discord/VPS readiness was not established here.
