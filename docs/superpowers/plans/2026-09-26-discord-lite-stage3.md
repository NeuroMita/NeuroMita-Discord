# Discord Lite Stage 3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route the Discord bot through NeuroMita's character, history, and memory pipeline, with a small admin repair surface.

**Architecture:** Add a `DiscordCharacterRuntime` composition root that registers only the core services required by `GenerationService`; do not instantiate `MainController`. Keep Discord event handling and command groups in the adapter, and keep all history/memory operations delegated to existing character resources and controllers.

**Tech Stack:** Python 3.11+, discord.py 2.x, existing NeuroMita services/controllers, SQLite managers, unittest, WSL Ubuntu clean venv.

**Spec:** `docs/discord-lite-stage3-design.md`

## Global Constraints

- Only answer in guild `1341427480942350356`, channel `1353745092065624144`; do not respond in DMs, threads, or other channels.
- Share history and memory per character; include stable Discord user ID in sender identity.
- Use `GenerationService.generate_chat(ChatGenerationRequest)` and the existing `History.MESSAGE_COMPLETED` lifecycle.
- Use the built-in `Keyword+FTS only` preset; keep vector embeddings, reranking, and graph search off. Unrelated tools, voice, streaming, and UI echo stay off.
- Keep no `MainController`, Qt, Unity, TTS, ASR, or local-ML imports; install only `requirements-discord.txt`.
- Never create Discord-specific history, summary, or memory tables.
- Admin ID `292002437932384256`; destructive/admin actions are ephemeral, confirmed, and never expose API keys.
- No Discord network in automated tests. Do not remove or clean user files while preparing WSL.

## Review Focus

- Missing prompt assets: health/status must identify the missing prompt set and prevent a misleading “ready” state.
- Concurrent Discord events: busy requests must be rejected immediately, not wait in an unbounded queue.
- Shared character state: serialize through the existing per-character generation lock and preserve sender IDs.
- Core initialization/shutdown failures: release each registration/resource once, including partial startup failure.
- Untrusted generated text: disable mentions and keep provider/stack traces out of user-facing responses.

---

### Task 1: Generation admission and gateway hardening

**Files:** `src/discord_bot/bot.py`, `src/discord_bot/response_formatter.py`, `src/utils/Testing/test_discord_bot.py`

**Interfaces:** `DiscordBot._generate(text, sender) -> ChatGenerationResult | None`; expose a non-blocking one-request admission gate and a safe busy result. Keep one generation executor.

- [x] Write tests for busy rejection, executor thread use, exception conversion/log traceback, and one-time runtime/executor shutdown.
- [x] Implement non-blocking admission; make the interaction/message handlers send a safe busy reply immediately.
- [x] Log generation exceptions with `logger.exception`; return generic text to Discord.
- [x] Verify all Discord bot tests pass.

### Task 2: Guild, channel, and admin policy

**Files:** `src/discord_bot/config.py`, `src/discord_bot/bot.py`, `src/utils/Testing/test_discord_bot.py`

**Interfaces:** Config exposes allowed guild/channel IDs and an immutable admin-ID set; a shared predicate validates both message and interaction locations and the actor's admin role.

- [x] Test allowed and denied guild/channel combinations, DM/thread denial, and owner/admin matching.
- [x] Implement the fixed initial target IDs and parse `DISCORD_ADMIN_IDS` overrides without logging secrets.
- [x] Register admin commands behind the common check; use ephemeral denials.
- [x] Verify config and gate tests pass.

### Task 3: Minimal character-pipeline composition root

**Files:** Create `src/discord_bot/character_runtime.py`; modify `src/discord_bot/llm_runtime.py` only to share safe bootstrap pieces if the test proves extraction necessary; test `src/utils/Testing/test_discord_character_runtime.py`.

**Interfaces:** `DiscordCharacterRuntime(data_dir: Path, prompts_dir: Path | None)`; `generate(user_input: str, *, sender: str, origin_message_id: str) -> ChatGenerationResult | None`; `status() -> dict[str, object]`; `close() -> None`.

- [x] Test service construction, request policy, keyword/FTS-only RAG, disabled vector work/tools, and shutdown.
- [x] Run in a subprocess and verify construction does not load PyQt6, torch, transformers, cv2, pygame, pyaudio, or audio backends.
- [x] Compose disconnected runtime capabilities, character registry, `HistoryController`, `PromptController`, `CharacterController`, and `ModelController` without `MainController`.
- [x] Send `ChatGenerationRequest(event_type="chat")` with history enabled, voice/stream/UI disabled, stable sender identity, and original Discord message ID.
- [x] Verify generation delegates to `GenerationService` and shutdown restores services/environment.

### Task 4: Route Discord chat through the character runtime

**Files:** `src/discord_bot/bot.py`, `src/discord_bot/__main__.py`, `src/discord_bot/character_runtime.py`, tests.

**Interfaces:** message and `/chat ask` handlers pass plain user text plus stable Discord identity to `DiscordCharacterRuntime`; result formatting never inspects provider details.

- [x] Replace stateless message-list generation with the character runtime; keep Stage 1 CLI smoke as a separate entrypoint.
- [x] Test runtime errors, empty response, busy response, and mention suppression.
- [x] Verify all adapter and CLI smoke tests pass.

### Task 5: Admin facade and essential operations

**Files:** Create `src/discord_bot/admin_facade.py`; keep the small Discord slash-command adapter in `src/discord_bot/bot.py`; test `src/utils/Testing/test_discord_admin.py`.

**Interfaces:** `DiscordAdminFacade` receives runtime/config and exposes typed, bounded methods; command modules only translate Discord arguments/results and do not access SQL directly.

- [x] Test admin-only checks, bounded history/memory output, and confirmation requirements.
- [x] Implement `/bot status|resources|uptime|health`, `/character list|status|set`, `/history status|recent|summary|reset`, `/memory status|list|show|add|delete|maintenance`, `/ai status`, `/compression status|on|off`, and `/debug last-error|health`.
- [x] Use existing character/history/memory managers and settings APIs; no bot-owned persistence. Manual maintenance uses the existing manager and requires confirmation.
- [x] Require confirmation for reset/delete/maintenance; keep outputs ephemeral and never return API keys.
- [x] Verify admin tests pass and forbidden destructive calls cannot be reached without the admin check.

### Task 6: Deployment docs and Linux verification

**Files:** `docs/discord-lite.md`, `requirements-discord.txt` only if the clean environment demonstrates a missing dependency.

- [x] Document the allowed guild/channel, `DISCORD_ADMIN_IDS`, prompt asset path, shared per-character memory, blocked DMs/threads, and admin commands.
- [x] Install Ubuntu 24.04 as WSL1 after verifying disk headroom; keep the existing `Ubuntu-ExternalTTS` distro untouched and do not delete files.
- [x] Create a fresh venv in the Ubuntu Linux filesystem, install `requirements-discord.txt`, and run Discord adapter plus mock-provider tests and lightweight-import smoke.
- [x] Linux result: Python 3.12.3, requirements installed, 35 tests passed under native WSL1 processes.
- [x] Run `git diff --check`, focused Windows tests, and the Discord Lite smoke suite.
- [x] Commit and push after completing the Linux verification gate.
