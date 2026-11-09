from __future__ import annotations

import asyncio
import logging
from typing import Any


async def run_client_with_retry(
    name: str,
    client: Any,
    token: str,
    logger: logging.Logger,
    *,
    initial_delay: float = 5.0,
    max_delay: float = 60.0,
) -> None:
    """Run one platform client without allowing its failures to stop other clients.

    The platform library continues to manage ordinary Gateway reconnects.
    This wrapper handles fatal start/login errors (for example, API timeouts)
    with exponential backoff until the client is closed or the task is cancelled.
    """
    delay = initial_delay
    while not client.is_closed():
        try:
            logger.info("%s: запускаю клиент", name)
            await client.start(token)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "%s: ошибка запуска/подключения; повторная попытка через %.0f сек.",
                name,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)
