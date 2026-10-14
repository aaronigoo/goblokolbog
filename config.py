"""Environment configuration. No credentials are written to disk or logs."""
from dataclasses import dataclass, field
import os
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    guild_id: int
    channel_id: int
    role_id: int
    log_level: str = "INFO"


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    token = env.get("DISCORD_TOKEN", "").strip()
    if not token or token == "your_bot_token" or token.startswith("Bot "):
        raise ValueError("Set DISCORD_TOKEN to the raw bot token, without a Bot prefix.")

    def snowflake(name: str) -> int:
        value = env.get(name, "").strip()
        if not value.isascii() or not value.isdecimal() or not 0 < int(value) < 2**64:
            raise ValueError(f"{name} must be a positive Discord ID copied in Developer Mode.")
        return int(value)

    level = env.get("LOG_LEVEL", "INFO").upper()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR.")
    settings = Settings(token, snowflake("DISCORD_GUILD_ID"),
                        snowflake("DISCORD_VOICE_CHANNEL_ID"),
                        snowflake("DISCORD_ALLOWED_ROLE_ID"), level)
    return settings
