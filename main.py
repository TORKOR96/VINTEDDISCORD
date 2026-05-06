"""
Point d'entrée principal du bot Vinted Discord.

Usage :
    python main.py
    # ou via Docker :
    docker compose up -d
"""

import asyncio
import logging
import sys

from src.bot import VintedBot
from src.config import Config


def _setup_logging():
    import os
    os.makedirs("data", exist_ok=True)

    fmt = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler("data/bot.log", encoding="utf-8"))
    except OSError:
        pass

    logging.basicConfig(level=logging.INFO, format=fmt, datefmt=datefmt, handlers=handlers)

    # Réduire le bruit des bibliothèques tierces
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def main():
    _setup_logging()
    config = Config()

    if not config.DISCORD_TOKEN:
        logging.critical(
            "DISCORD_TOKEN manquant ! Copiez .env.example en .env et remplissez le token."
        )
        sys.exit(1)

    bot = VintedBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
