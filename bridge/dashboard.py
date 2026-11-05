from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)
StatusProvider = Callable[[], dict[str, Any] | Awaitable[dict[str, Any]]]


class DashboardServer:
    """Small dependency-free HTTP dashboard for bridge health/status."""

    def __init__(self, host: str, port: int, status_provider: StatusProvider) -> None:
        self.host = host
        self.port = port
        self.status_provider = status_provider
        self.server: asyncio.Server | None = None
        self.index_path = Path(__file__).resolve().parent.parent / "web" / "index.html"

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self.server.sockets or []
        bound = ", ".join(str(sock.getsockname()) for sock in sockets)
        log.info("Веб-панель запущена: http://%s:%s (%s)", self.host, self.port, bound)

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
            log.info("Веб-панель остановлена")

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            parts = request_line.decode("ascii", errors="replace").strip().split()
            if len(parts) < 2 or parts[0] != "GET":
                await self._respond(writer, "405 Method Not Allowed", "text/plain; charset=utf-8", b"Method not allowed")
                return

            # Consume headers, with a small bound to avoid unbounded client input.
            header_bytes = 0
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                header_bytes += len(line)
                if not line or line in (b"\r\n", b"\n"):
                    break
                if header_bytes > 16_384:
                    await self._respond(writer, "431 Request Header Fields Too Large", "text/plain; charset=utf-8", b"Headers too large")
                    return

            path = parts[1].split("?", 1)[0]
            if path == "/api/status":
                status = self.status_provider()
                if hasattr(status, "__await__"):
                    status = await status
                body = json.dumps(status, ensure_ascii=False).encode("utf-8")
                await self._respond(writer, "200 OK", "application/json; charset=utf-8", body, no_store=True)
            elif path in ("/", "/index.html"):
                try:
                    body = self.index_path.read_bytes()
                except OSError:
                    body = b"Dashboard file is missing"
                    await self._respond(writer, "500 Internal Server Error", "text/plain; charset=utf-8", body)
                    return
                await self._respond(writer, "200 OK", "text/html; charset=utf-8", body, no_store=True)
            else:
                await self._respond(writer, "404 Not Found", "text/plain; charset=utf-8", b"Not found")
        except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("Ошибка обработки HTTP-запроса веб-панели")
            try:
                await self._respond(writer, "500 Internal Server Error", "text/plain; charset=utf-8", b"Internal server error")
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    @staticmethod
    async def _respond(
        writer: asyncio.StreamWriter,
        status: str,
        content_type: str,
        body: bytes,
        no_store: bool = False,
    ) -> None:
        headers = [
            f"HTTP/1.1 {status}",
            f"Content-Type: {content_type}",
            f"Content-Length: {len(body)}",
            "Connection: close",
            "X-Content-Type-Options: nosniff",
            "Content-Security-Policy: default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'",
        ]
        if no_store:
            headers.append("Cache-Control: no-store")
        writer.write(("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body)
        await writer.drain()
