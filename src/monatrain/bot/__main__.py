"""`python -m monatrain.bot`: run the Discord app and the watcher."""

import asyncio
import logging
import os
import signal
import sys

from monatrain.bot.client import MonatrainBot
from monatrain.budget import QuotaBudget
from monatrain.config import Settings
from monatrain.db import connect, init_schema
from monatrain.repo import Repo
from monatrain.sncf.client import SncfClient

MSG_NO_TOKEN = (
    "DISCORD_BOT_TOKEN manquant : renseigne le token du bot dans le fichier .env "
    "(voir le README), puis relance `python -m monatrain.bot`."
)


def _dev_guild_id() -> int | None:
    raw = os.environ.get("DISCORD_DEV_GUILD_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        sys.exit("DISCORD_DEV_GUILD_ID doit être l'identifiant numérique d'un serveur Discord.")


def _close_on_sigterm(bot: MonatrainBot) -> None:
    """Docker stops the container with SIGTERM: close the bot so that the cleanup in `run` happens."""
    loop = asyncio.get_running_loop()

    tasks: set[asyncio.Task] = set()  # keeps the task alive until it is done

    def request_close() -> None:
        task = loop.create_task(bot.close())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        loop.add_signal_handler(signal.SIGTERM, request_close)
    except NotImplementedError:  # Windows event loops: fall back to a plain signal handler
        signal.signal(signal.SIGTERM, lambda *_: loop.call_soon_threadsafe(request_close))


async def run(settings: Settings, token: str) -> None:
    conn = connect(settings.db_path)
    init_schema(conn)
    repo = Repo(conn)
    budget = QuotaBudget(repo)
    # Two clients on the same budget: the watcher draws on its reserve, commands on the web quota.
    watcher_source = SncfClient(settings.sncf_token, repo, budget, "watcher")
    web_source = SncfClient(settings.sncf_token, repo, budget, "web")
    bot = MonatrainBot(
        repo,
        watcher_source=watcher_source,
        web_source=web_source,
        client_id=settings.discord_client_id,
        dev_guild_id=_dev_guild_id(),
    )
    _close_on_sigterm(bot)
    try:
        async with bot:
            await bot.start(token)
    finally:
        await watcher_source.aclose()
        await web_source.aclose()
        conn.close()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        sys.exit(str(exc))
    if not settings.discord_bot_token:
        sys.exit(MSG_NO_TOKEN)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    try:
        asyncio.run(run(settings, settings.discord_bot_token))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
