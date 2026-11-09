from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import patch

from bridge.retry import run_client_with_retry


class FakeClient:
    def __init__(self) -> None:
        self.closed = False
        self.calls = 0

    def is_closed(self) -> bool:
        return self.closed

    async def start(self, token: str) -> None:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError("temporary API timeout")
        self.closed = True


class ClientRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_client_retries_after_temporary_start_failure(self) -> None:
        client = FakeClient()
        delays: list[float] = []

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)

        with patch("bridge.retry.asyncio.sleep", new=fake_sleep):
            await run_client_with_retry(
                "Lolka",
                client,
                "test-token",
                logging.getLogger("test"),
                initial_delay=5,
                max_delay=60,
            )

        self.assertEqual(client.calls, 2)
        self.assertEqual(delays, [5])

    async def test_client_does_not_start_when_already_closed(self) -> None:
        class ClosedClient:
            def is_closed(self) -> bool:
                return True

            async def start(self, token: str) -> None:
                raise AssertionError("closed client must not start")

        await run_client_with_retry(
            "Discord",
            ClosedClient(),
            "test-token",
            logging.getLogger("test"),
        )


if __name__ == "__main__":
    unittest.main()
