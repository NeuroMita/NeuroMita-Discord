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
export DISCORD_ADMIN_IDS='292002437932384256'
PYTHONPATH=src python -m discord_bot
```

Slash commands are synced to `DISCORD_GUILD_ID`. In the Discord Developer Portal, enable **Message Content Intent** for the bot. The runtime requests no members or presence intents. It responds only in the configured guild and channel; DMs and threads are ignored. Mentions, replies, and `/chat ask` share the active NeuroMita character's history across Discord users. RAG uses the `Keyword+FTS only` preset (SQLite FTS5 and keyword search; vector search and reranking remain off). LLM work is limited to one generation at a time and runs outside the Discord event loop. Long answers are split into Discord-sized messages.

Only IDs in `DISCORD_ADMIN_IDS` can use the ephemeral `/bot`, `/character`, `/history`, `/memory`, `/ai`, `/compression`, and `/debug` administration commands, and only in the configured channel. History resets and memory deletion require an explicit confirmation argument. Prompt assets should be installed in the normal NeuroMita `Prompts` tree or selected with `NEUROMITA_PROMPTS_DIR`.

Discord conversation history and memory share the active character's standard NeuroMita storage under `DiscordData/Histories` and its character memory database. Do not share this runtime data directory with a desktop NeuroMita process while both are running.

Keep the token in the service manager's protected environment when deploying. The bot does not start automatically from the CLI smoke test; the gateway starts only when `python -m discord_bot` is run with a token configured.

## Memory budget

The VPS's roughly 500 MB currently available is shared with the OS and VPN. Treat it as the whole remaining headroom, not the bot allocation. Start with one generation worker and short conversations; measure the process RSS and available host memory on the VPS before raising concurrency or adding features. Swap is only an OOM buffer, not usable RAM.
