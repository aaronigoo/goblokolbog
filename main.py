"""Run: python main.py [--env-file .env] [--check-config]."""
import argparse
import asyncio
import logging
import os
from pathlib import Path
import signal
import sys
import time

from config import load_settings


def load_env_file(path: str) -> None:
    """Simple KEY=value file; existing environment variables take precedence.

    Blank lines and full-line comments are supported, as are surrounding quotes.
    No shell expansion, execution, escaping, or inline comments are performed.
    """
    allowed = {"DISCORD_TOKEN", "DISCORD_GUILD_ID", "DISCORD_VOICE_CHANNEL_ID",
               "DISCORD_ALLOWED_ROLE_ID", "LOG_LEVEL"}
    for line_number, raw in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in allowed:
            raise ValueError(f"Invalid configuration entry on line {line_number}.")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


class RedactingFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, secret: str):
        super().__init__("%(asctime)sZ %(levelname)s %(name)s %(message)s", "%Y-%m-%dT%H:%M:%S")
        self.secret = secret

    def format(self, record):
        return super().format(record).replace(self.secret, "[REDACTED]")


def configure_logging(settings):
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(RedactingFormatter(settings.token))
    logging.basicConfig(level=settings.log_level, handlers=[handler], force=True)
    # Raw SDK DEBUG payloads can contain voice-session secrets. Keep them off
    # even if application debugging is enabled.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)


async def serve(settings):
    from bot import VoiceKeeperBot

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows local runs use asyncio.run's KeyboardInterrupt path.

    async with VoiceKeeperBot(settings) as bot:
        controller_task = asyncio.create_task(bot.controller.run(), name="voice-controller")
        discord_task = asyncio.create_task(bot.start(settings.token, reconnect=True), name="discord-client")
        stop_task = asyncio.create_task(stop.wait(), name="shutdown-signal")
        try:
            done, _ = await asyncio.wait(
                {controller_task, discord_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            for task in (controller_task, discord_task):
                if task in done:
                    task.result()  # Propagate failure instead of leaving a dead worker.
                    raise RuntimeError(f"{task.get_name()} stopped unexpectedly")
        finally:
            bot.controller.request_shutdown()
            try:
                await asyncio.wait_for(asyncio.shield(controller_task), timeout=15)
            except (TimeoutError, asyncio.CancelledError):
                controller_task.cancel()
            finally:
                # Close the gateway even when controller cleanup raised.
                await bot.close()
                for task in (controller_task, discord_task, stop_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(controller_task, discord_task, stop_task, return_exceptions=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="A silent Discord voice-channel keeper.")
    parser.add_argument("--env-file", help="Optional local configuration file (never commit it).")
    parser.add_argument("--check-config", action="store_true", help="Validate settings without connecting to Discord.")
    args = parser.parse_args()
    try:
        if args.env_file:
            load_env_file(args.env_file)
        settings = load_settings()
    except (ValueError, OSError) as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 2
    if args.check_config:
        print("Configuration format is valid. IDs, token validity, and permissions still require a live connection.")
        return 0
    configure_logging(settings)
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return 0
    except ImportError:
        logging.error("Missing runtime dependency. Install requirements.txt before starting.")
        return 1
    except Exception:
        logging.exception("Fatal application error; process is exiting.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
