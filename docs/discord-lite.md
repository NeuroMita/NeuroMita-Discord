# NeuroMita Discord Lite

Discord Lite is a standalone, low-resource runtime that reuses NeuroMita's existing API preset resolver, model settings, retry/key rotation, fallback chain, and provider manager. It does not start the desktop controller, Unity, voice, local models, or Discord gateway.

## Stage 1: CLI LLM smoke test

Install only the Discord runtime dependencies into a fresh virtual environment (do not install `requirements.txt`):

```bash
python3 -m venv .venv-discord
. .venv-discord/bin/activate
python -m pip install -r requirements-discord.txt
```

Configure the NeuroMita API preset files and `settings.json` under `DiscordData/Settings/`. Do not copy desktop settings wholesale; add only the preset(s) and credentials needed on this server. A key is optional for keyless `common` endpoints such as a private Ollama or LM Studio server; use a key when the selected provider requires one. Keep the data directory private (`chmod 700 DiscordData`) and the preset file private (`chmod 600 DiscordData/Settings/api_presets.json`). Never commit these files.

Run one request without connecting to Discord:

```bash
PYTHONPATH=src python -m discord_bot.test_llm "Привет"
```

An alternate data directory can be selected with `--data-dir`; an optional preset id can be passed with `--preset-id`. The CLI prints the model response and exits nonzero if all configured attempts fail.

Only move on to the Discord gateway after this CLI request succeeds on the target VPS.

## Stage 2: Discord gateway

Set the bot token in the process environment; do not put it in settings or commit it:

```bash
export DISCORD_BOT_TOKEN='...'
# Optional: make slash commands available immediately in one development server.
export DISCORD_GUILD_ID='123456789012345678'
# Optional: restrict to one channel; defaults target the NeuroMita test channel.
export DISCORD_CHANNEL_ID='1353745092065624144'
# Optional: comma-separated admin user IDs; defaults to the project owner.
export DISCORD_ADMIN_IDS='292002437932384256,418100424583675904,446312390271696927'
PYTHONPATH=src python -m discord_bot
```

Slash commands are synced to `DISCORD_GUILD_ID`. In the Discord Developer Portal, enable **Message Content Intent** for the bot. The runtime requests no members or presence intents. It responds only in the configured guild and channel; DMs and threads are ignored. Mentions, replies, and `/chat ask` share the active NeuroMita character's history across Discord users. RAG uses the `Keyword+FTS only` preset (SQLite FTS5 and keyword search; vector search and reranking remain off). LLM work is limited to one generation at a time and runs outside the Discord event loop. Long answers are split into Discord-sized messages.

Each process writes a separate privacy-safe diagnostic file under `<NEUROMITA_DISCORD_DATA_DIR>/Logs/` (default `DiscordData/Logs/`) named `discord-runtime-<pid>-<run-id>.jsonl`. Entries include only lifecycle, trigger type, gate/attention decisions, response lengths, and exception class/status; message text, prompts, tokens, and API keys are excluded. The filename and each record identify the exact process run. These files are retained between runs.

Only IDs in `DISCORD_ADMIN_IDS` can use the ephemeral `/bot`, `/character`, `/history`, `/memory`, `/ai`, `/compression`, and `/debug` administration commands, and only in the configured channel. History resets and memory deletion require an explicit confirmation argument. Prompt assets should be installed in the normal NeuroMita `Prompts` tree or selected with `NEUROMITA_PROMPTS_DIR`.

## Stage 4: Ambient room presence

The configured channel is observed as a shared room. Human messages, Mita's public replies, edits, and deletions are stored in `DiscordData/room.sqlite3`; other bots, webhooks, DMs, threads, and other channels are ignored. A bounded recent transcript plus a separately compressed room summary is added as hidden context to character generations. This room timeline is separate from NeuroMita character history and memory. When old room messages are summarized, a silent request also passes the summarized room context through the normal character memory pipeline; history writes, voice, streaming, and UI echo are disabled for that observation. Ambient context can also inform memory operations during a real reply. Neither path writes the room transcript as fake user dialogue.

Defaults are intentionally conservative: `alive` mode, initiative 55/100, 6–12 second social debounce, four-minute voluntary cooldown, and at most three voluntary public messages per hour. Direct mentions and replies still take priority. The autonomous timer wakes with jitter, checks local gates first, and only considers an attention-model request after the room has been quiet for 15 minutes. The attention model uses a single bounded utility request; invalid output or provider failure means silence. The Python-side revision, busy, cooldown, and hourly-budget gates run again before generation and posting. Room history is caught up after reconnect, without replying to old messages.

Owner-only commands in the configured channel:

- `/presence status`, `/presence on`, `/presence off`
- `/presence mode direct|social|alive`, `/presence initiative`, `/presence cooldown`, `/presence max-hour`
- `/presence pause`, `/presence resume`, `/presence speak-now`
- `/attention test` evaluates the room without posting a response
- `/room status`, `/room recent`, `/room summary`, `/room summarize`, `/room reset confirm:true`
- `/character reset-all confirm:true` clears character history, memories, state, and graph data

`/room reset` clears only the Discord room timeline and summary. Presence settings and NeuroMita character history/memory remain separate. The bot also needs **Read Message History** in its allowed channel for restart catch-up. On Linux, keep the data directory private (`chmod 700 DiscordData`); the room database is created with owner-only file permissions.

Discord conversation history and memory share the active character's standard NeuroMita storage under `DiscordData/Histories` and its character memory database. Do not share this runtime data directory with a desktop NeuroMita process while both are running.

Keep the token in the service manager's protected environment when deploying. The bot does not start automatically from the CLI smoke test; the gateway starts only when `python -m discord_bot` is run with a token configured.

## Memory budget

The VPS's roughly 500 MB currently available is shared with the OS and VPN. Treat it as the whole remaining headroom, not the bot allocation. Start with one generation worker and short conversations; measure the process RSS and available host memory on the VPS before raising concurrency or adding features. Swap is only an OOM buffer, not usable RAM.
