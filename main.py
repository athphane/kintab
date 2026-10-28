#!/usr/bin/env python3
import asyncio
import logging
from logging.handlers import RotatingFileHandler

from pyrogram import Client, idle
from pyrogram.types import BotCommand

from bot.config import load_settings
from bot.db import Database
from bot.groups import TabService
from bot.handlers import register_handlers
from bot.notify import Notifier
from bot.webapp import start_web


async def run(client, settings):
    db = await Database(settings.data_dir / "kintab.sqlite3").open()
    service = TabService(db)
    notifier = Notifier(client, service, settings)
    register_handlers(client, service, notifier)
    runner = None
    started = False
    try:
        await client.start()
        started = True
        if client.me.username.lower() != settings.bot_username.lower():
            raise ValueError("Configured bot_username does not match the bot token.")
        await client.set_bot_commands(
            [
                BotCommand(command, description)
                for command, description in [
                    ("spent", "Record a shared expense"),
                    ("paid", "Record a wallet payment"),
                    ("tab", "Show and pin the running total"),
                    ("damage", "A picture of who owes whom"),
                    ("leave", "Leave the tab"),
                    ("export", "Send all entries as a CSV in DM"),
                    ("forget", "Delete the tab after confirmation"),
                    ("help", "How KinTab works"),
                ]
            ]
        )
        runner = await start_web(settings, service, client, notifier)
        logging.info(
            "KinTab @%s listening on %s:%s", client.me.username, settings.web_host, settings.web_port
        )
        await idle()
    finally:
        if runner:
            await runner.cleanup()
        await notifier.close()
        if started:
            await client.stop()
        await db.close()


def main():
    settings = load_settings()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            RotatingFileHandler(
                settings.data_dir / "bot.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8"
            ),
        ],
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    client = Client(
        "kintab",
        api_id=settings.api_id,
        api_hash=settings.api_hash,
        bot_token=settings.bot_token,
        workdir=str(settings.workdir),
        workers=8,
    )
    try:
        loop.run_until_complete(run(client, settings))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
