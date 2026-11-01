"""Console entrypoint for a single long-polling Telegram process."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import os
import signal
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from .bot import ValeriaBot
from .config import ConfigError, Settings
from .runner import run_bot
from .store import Store
from .telegram_client import TelegramClient

if TYPE_CHECKING:
    from collections.abc import Iterator


class StartupError(Exception):
    """A fixed, safe startup diagnostic suitable for display."""


@contextmanager
def _exclusive_lock(database_path: Path) -> Iterator[None]:
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = database_path.with_name(database_path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise StartupError("Другой процесс уже использует базу Telegram-бота") from None
        yield
    finally:
        os.close(descriptor)


async def _serve(settings: Settings) -> None:
    store = Store(settings.database_path)
    telegram = TelegramClient(settings)
    bot = ValeriaBot(settings, store, telegram)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)
    try:
        await telegram.get_me()
        webhook = await telegram.get_webhook_info()
        if webhook.get("url"):
            raise StartupError(
                "У Telegram-бота настроен webhook; отключите его перед запуском long polling"
            )
        await run_bot(bot, store, telegram, settings, stop_event=stop_event)
    finally:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(signum)
        try:
            await bot.aclose()
        finally:
            await telegram.aclose()
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram-клиент Valeria")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "config.env",
        help="Путь к конфигурации (переменные окружения имеют приоритет)",
    )
    args = parser.parse_args()
    # Telegram embeds the bot token into every URL. Never enable HTTP request logs.
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    load_dotenv(args.env_file, override=False)
    try:
        settings = Settings.from_env()
        with _exclusive_lock(Path(settings.database_path)):
            asyncio.run(_serve(settings))
    except ConfigError as error:
        print(f"Ошибка конфигурации: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass
    except StartupError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception as error:
        print(f"Ошибка запуска Telegram-бота ({type(error).__name__})", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
