import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    POLL_INTERVAL: int = int(os.getenv("POLL_INTERVAL", "90"))
    MAX_ITEMS_PER_POLL: int = int(os.getenv("MAX_ITEMS_PER_POLL", "20"))
    SEEN_ITEMS_RETENTION_DAYS: int = int(os.getenv("SEEN_ITEMS_RETENTION_DAYS", "7"))
    DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/vinted.db")
