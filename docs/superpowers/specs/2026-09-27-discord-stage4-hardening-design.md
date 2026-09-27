# Discord Stage 4 Pre-deploy Hardening Design

## Goal

Harden the existing Discord room presence runtime for a first VPS deployment while keeping its current behavior and low-memory footprint. This work closes scheduling, anti-spam, context duplication, and malformed attention-output gaps; it does not add new user-facing capabilities.

## Current State

The `discord-lite` branch is at `427e1d0a`. Stage 4 already provides a persistent room timeline, ambient context, social and initiative decisions, summaries, and room observation. The current implementation uses one executor for character generation and utility calls, immediately performs full character observation after summaries, stores no unanswered initiative backoff, accepts unknown attention actions/reasons through fallbacks, and includes `/chat ask` text in both room context and the direct input.

## Design

### Worker ownership and admission

`DiscordBot` owns two single-worker executors: a generation executor for all full character pipeline work (direct, social, initiative, and observation), and a utility executor for attention classification and room summaries. This keeps the shared character runtime serialized while preventing utility requests from blocking direct character turns. Both executors are shut down idempotently with the bot.

The presence controller admits at most one utility request at a time. A running attention decision takes precedence over a summary; summaries are skipped or rescheduled while utility work is occupied. No unbounded executor queue is allowed. Bot and presence status expose generation, utility, attention, and observation busy state.

### Deferred room observation

After a successful summary, the controller schedules observation after a configurable idle delay (default 60 seconds). It captures the room revision and cancels pending work when a human message, direct generation, room edit/delete, or shutdown makes the observation stale. It checks revision and busy states before building context and again before starting the generation call. Observation remains on the generation executor and retains its no-history/no-fake-human-turn policy.

### Persistent initiative protections

Extend `PresenceSettings` with a six-hour autonomous initiative cap (default 2), unanswered backoff intervals (2, 4, and 6 hours), and a minimum silence-ping gap (2 hours), all bounded and validated. Existing settings files receive defaults through current settings loading.

Add additive SQLite migration for unanswered streak and last initiative/silence-ping timestamps in `room_state`. Timeline methods count only sent `mita_start_topic` and `mita_silence_ping` messages, read the current streak, record a successful initiative send atomically, and reset the streak after any human activity while preserving timestamps. The controller gates initiatives on the six-hour budget and streak-based backoff; silence-ping also checks its separate minimum gap. Streak and timestamps are recorded only after Discord accepts the send. Failed generation or send consumes no autonomous budget. Social join messages remain outside the unanswered streak while retaining the shared hourly voluntary cap.

### Strict attention decisions

Use explicit action/reason sets for social and initiative decisions. The parser requires a JSON object, exact boolean `speak`, exact integer `desire` in 0..100, valid action and reason for that decision type, consistent speak/action pairs, and valid reply targets. Initiative cannot target replies; social targets must be null or refer to a message in the supplied context. Unknown values, coercible types, and inconsistent fields fail closed to a silent decision with a bounded diagnostic code. Unrelated extra JSON fields remain ignored.

### Direct context and diagnostics

`/chat ask` builds ambient context excluding the current interaction ID, matching mention and reply handling. The supplied text remains the direct `user_input`, and direct history continues to use the normal request ID and history policy.

Presence diagnostics add only bounded enum reasons and busy state; they never include message text, room context, summaries, memory text, provider errors, or secrets. `/presence status` reports hourly and six-hour usage, unanswered streak, last initiative timestamps, cooldown state, and worker occupancy.

## Data and Compatibility

The SQLite change is additive and migrates existing `room_state` tables in place without deleting or rewriting room messages, summaries, or character history. Existing presence settings continue to load, with new defaults supplied for absent fields. Existing message kinds and direct history semantics remain intact.

## Verification Criteria

- A blocked utility worker does not block direct character generation, and they use distinct threads.
- Pending observations are canceled by room revision changes/direct generation and never overlap full character generation.
- Existing SQLite databases migrate without data loss; initiative budgets, streak reset, timestamps, and send-failure behavior are covered.
- `/chat ask` passes its text once and excludes it from ambient context.
- Invalid attention outputs fail closed across type, action/reason, consistency, bounds, and reply-target cases; valid outputs remain accepted.
- Observation keeps history writes disabled; ordinary direct turns retain user and assistant history events.
- Utility admission remains bounded, and diagnostic/status output contains only approved operational fields.
- The complete Discord test suite, syntax compilation, and diff whitespace checks pass on the Windows checkout. Linux readiness remains a separate deployment verification and is not claimed by this change.

## Scope

No new Discord commands beyond enriching `/presence status`, no provider or prompt feature work, no VPS deployment, and no commit or push are included. Implementation will use regression tests for changed behavior and preserve unrelated working-tree files.
