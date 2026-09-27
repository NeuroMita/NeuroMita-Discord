from __future__ import annotations

import logging
import sys

from discord_bot.bot import create_bot
from discord_bot.character_runtime import DiscordCharacterRuntime
from discord_bot.config import DiscordBotConfig


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        config = DiscordBotConfig.from_env()
        runtime = DiscordCharacterRuntime()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception:
        logging.exception("Could not initialize Discord character runtime")
        return 1

    bot = create_bot(config, runtime=runtime)
    bot.run_configured()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
