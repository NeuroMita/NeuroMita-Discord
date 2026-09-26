# Discord Lite Stage 3 — design proposal

**Status:** awaiting user review. No production implementation is included yet.

## Goal

Make the Discord adapter generate replies through NeuroMita's existing character pipeline so that character prompts and state, SQLite history, structured memory operations, history summaries, and post-response maintenance behave as they do in the main application. Discord must not create a second history or memory implementation.

## Agreed behavior and scope

- Use the existing `GenerationService.generate_chat(ChatGenerationRequest)` contract and return its `ChatGenerationResult`; Discord stops building generic LLM messages itself.
- Keep one shared history and memory per active character. Every Discord sender is identified by a stable Discord user ID plus a display name; all accepted channel conversations contribute to that character's single history.
- Accept chat only from guild `1341427480942350356`, channel `1353745092065624144`. Ignore DMs, other guilds/channels, and threads unless separately approved. Apply the same location guard to slash interactions.
- Close admin/repair commands behind `DISCORD_ADMIN_IDS`, initially containing the supplied owner ID `292002437932384256`. Make destructive actions ephemeral and require an explicit confirmation argument.
- Enable the built-in `Keyword+FTS only` RAG preset so character history and memories can be retrieved lexically. Keep vector search, embeddings, reranking, and graph search disabled. Turn off unrelated tools, streaming, voice output, and UI echo. Retain the native character structured response and memory processing path.
- Keep the lightweight deployment boundary: no `MainController`, Qt, Unity, voice/ASR, local ML, or full `requirements.txt`.
- Preserve Stage 1 CLI smoke and current Discord gateway behavior; use the same preset/provider stack through the existing core services.

## Proposed composition

Add a `DiscordCharacterRuntime` as the Discord composition root. It builds on the existing settings/preset bootstrap and explicitly registers only the application services the character pipeline needs: disconnected game link, runtime capabilities, app variables, character environment, character registry, history, prompt builder, character, and `ModelController` as `GenerationService`. It must not instantiate `MainController`.

Construct controllers in dependency order and close them in reverse order. Use the application's `CharacterResourceManager`, `HistoryManager`, `MemoryManager`, and `ConversationEventWriter`; do not add Discord-specific persistence. Isolate writable history and state beneath `DiscordData`, and resolve character prompts through `NEUROMITA_PROMPTS_DIR`. Set the no-Unity runtime capability before any character generation so Unity-only structured actions are excluded.

For a Discord turn, submit a `ChatGenerationRequest` with `event_type="chat"`, the selected character ID, the user's text, sender identity, and source Discord message ID. Use normal history read/write policy while setting voice, streaming, and UI echo off. Let `ModelController` process the response and emit `History.MESSAGE_COMPLETED`; that event is what activates the existing asynchronous summary and memory-maintenance lifecycle.

## Discord operations facade

Expose diagnostics and repair operations through a Discord-only facade over existing controllers/managers, never through SQL tables owned by the bot. Proposed initial command groups:

- `/bot status`, `/bot resources`, `/bot uptime`, `/bot health`.
- `/character list`, `/character status`, `/character set`, `/character reload`.
- `/history status`, `/history recent`, `/history summary`, `/history compress`, `/history reset`.
- `/memory status`, `/memory list`, `/memory show`, `/memory add`, `/memory delete`, `/memory maintenance`, `/memory explain`.
- `/context status` and admin-only `/context dump`, with secrets redacted.
- `/ai status`, `/ai preset`, `/ai fallbacks`, `/ai test`.
- `/compression status`, `/compression run`, `/compression provider`, `/compression on`, `/compression off`.
- `/debug last-error`, `/debug health`.

History/context/memory reads and all repair/configuration commands are admin-only because the character's memory is shared. Never show API keys. Reset, delete, provider changes, and context dump require explicit confirmation and produce ephemeral responses. `/chat status` is folded into `/bot status`.

## Stage 2 hardening carried into this stage

- Reject new generation immediately with a safe busy reply while one generation is running; do not queue old messages.
- Log generation exceptions with traceback on the server and return only a generic Discord error.
- Test busy admission, executor thread use, exception handling, shutdown, and actual message chunk delivery.

## Verification and known environment limit

Unit tests must not connect to Discord. Add integration-level tests around the real `GenerationService` request boundary using isolated services/data, and assert that the completion event reaches the existing history controller. Keep the heavy-import guard after constructing the character runtime.

The requested clean Linux venv check remains a release gate. On this Windows host, `wsl.exe --list` currently exits with code 1 and prints usage; no usable WSL distribution was detected. Do not install or change Windows/WSL settings as part of this work. If Linux CI is added as the substitute, report its actual workflow result before calling the stage complete.

## Deployment note

The repository working tree has no `Prompts` directory. Deployment must provide the user's character prompt assets at `NEUROMITA_PROMPTS_DIR`; the Discord runtime must report a clear startup/health error rather than silently pretending the character prompt loaded.
