from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands as discord_commands
import lolka
from lolka.ext import commands as lolka_commands

from bridge.config import Config
from bridge.dashboard import DashboardServer
from bridge.dave import patch_dave_transitions
from bridge.manager import VoiceBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voice-bridge")

# Discord's default INFO logs hide Gateway close codes and reconnect details.
# DEBUG is enabled only for the Gateway logger to diagnose invalid sessions.
logging.getLogger("discord.gateway").setLevel(logging.DEBUG)

# RTCP Sender Reports (packet type 200) are normal voice traffic. The
# voice_recv reader currently labels them as unexpected at INFO level.
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)


async def main() -> None:
    patch_dave_transitions()
    config = Config.from_env()
    log.info("Запуск Discord ↔ Lolka Voice Bridge")
    log.info(
        "Настройки каналов: Discord channel=%s, Lolka channel=%s",
        config.discord_voice_channel_id,
        config.lolka_voice_channel_id,
    )

    discord_intents = discord.Intents.default()
    discord_intents.guilds = True
    discord_intents.voice_states = True
    discord_intents.message_content = True

    lolka_intents = lolka.Intents.default()
    lolka_intents.guilds = True
    lolka_intents.voice_states = True
    lolka_intents.message_content = True

    discord_bot = discord_commands.Bot(command_prefix=config.command_prefix, intents=discord_intents)
    lolka_bot = lolka_commands.Bot(command_prefix=config.command_prefix, intents=lolka_intents)
    bridge = VoiceBridge(discord_bot, lolka_bot, config)
    process_started_at = asyncio.get_running_loop().time()

    def dashboard_status() -> dict[str, Any]:
        discord_voice = bridge.discord_voice
        lolka_voice = bridge.lolka_voice
        discord_channel = getattr(discord_voice, "channel", None)
        lolka_channel = getattr(lolka_voice, "channel", None)
        discord_connected = bool(discord_voice and discord_voice.is_connected())
        lolka_connected = bool(lolka_voice and lolka_voice.is_connected())
        return {
            "uptime_seconds": asyncio.get_running_loop().time() - process_started_at,
            "bridge": {"connected": bridge.connected},
            "discord": {
                "ready": discord_bot.is_ready() and not discord_bot.is_closed(),
                "name": str(discord_bot.user) if discord_bot.user else None,
                "voice_connected": discord_connected,
                "channel": getattr(discord_channel, "name", None),
                "audio_playing": bool(discord_voice and discord_voice.is_playing()),
            },
            "lolka": {
                "ready": lolka_bot.is_ready() and not lolka_bot.is_closed(),
                "name": str(lolka_bot.user) if lolka_bot.user else None,
                "voice_connected": lolka_connected,
                "channel": getattr(lolka_channel, "name", None),
                "audio_playing": bool(lolka_voice and lolka_voice.is_playing()),
            },
        }

    dashboard = DashboardServer(config.dashboard_host, config.dashboard_port, dashboard_status)
    await dashboard.start()

    async def send_voice_log(platform: str, member: Any, joined: bool) -> None:
        """Send voice join/leave events to the configured text channels on both platforms."""
        if getattr(member, "bot", False):
            return
        member_id = getattr(member, "id", None)
        name = (
            getattr(member, "display_name", None)
            or getattr(member, "name", None)
            or str(member)
        )
        action = "зашёл в голосовой канал" if joined else "вышел из голосового канала"
        emoji = "🟢" if joined else "🔴"
        message = (
            f"{emoji} **{platform}: {name}** {action}."
            + (f" (ID: {member_id})" if member_id is not None else "")
        )

        if config.discord_log_channel_id is not None:
            try:
                channel = discord_bot.get_channel(config.discord_log_channel_id)
                if channel is None:
                    channel = await discord_bot.fetch_channel(config.discord_log_channel_id)
                if not hasattr(channel, "send"):
                    raise RuntimeError("Configured Discord log channel is not a text channel")
                await channel.send(
                    message,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except Exception:
                log.exception("Не удалось отправить событие %s в текстовый канал Discord", platform)

        if config.lolka_log_channel_id is not None:
            try:
                channel = lolka_bot.get_channel(config.lolka_log_channel_id)
                if channel is None:
                    fetch_channel = getattr(lolka_bot, "fetch_channel", None)
                    if fetch_channel is None:
                        raise RuntimeError("Lolka client does not expose fetch_channel()")
                    channel = await fetch_channel(config.lolka_log_channel_id)
                if not hasattr(channel, "send"):
                    raise RuntimeError("Configured Lolka log channel is not a text channel")
                await channel.send(message)
            except Exception:
                log.exception("Не удалось отправить событие %s в текстовый канал Lolka", platform)

    async def clear_old_voice_logs(client: Any, channel_id: int | None, platform: str) -> None:
        """Delete only this bot's previous voice join/leave log messages."""
        if channel_id is None:
            return

        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                fetch_channel = getattr(client, "fetch_channel", None)
                if fetch_channel is None:
                    raise RuntimeError("Client does not expose fetch_channel()")
                channel = await fetch_channel(channel_id)

            history = getattr(channel, "history", None)
            if history is None:
                raise RuntimeError("Configured log channel does not expose message history()")

            bot_user = getattr(client, "user", None)
            bot_id = getattr(bot_user, "id", None)
            if bot_id is None:
                raise RuntimeError("Bot user is not ready")

            deleted = 0
            async for message in history(limit=None):
                author_id = getattr(getattr(message, "author", None), "id", None)
                content = getattr(message, "content", "") or ""
                is_voice_log = (
                    "зашёл в голосовой канал." in content
                    or "вышел из голосового канала." in content
                )
                if author_id == bot_id and is_voice_log:
                    await message.delete()
                    deleted += 1

            log.info("%s: очищено старых записей о голосовых событиях: %s", platform, deleted)
        except Exception:
            # A missing permission or an unavailable history endpoint must not
            # prevent either bot from starting the voice bridge.
            log.exception("%s: не удалось очистить старые записи в текстовом канале", platform)

    ready_clients: set[str] = set()
    both_bots_ready = asyncio.Event()

    def mark_client_ready(name: str) -> None:
        ready_clients.add(name)
        if {"discord", "lolka"}.issubset(ready_clients):
            both_bots_ready.set()

    @discord_bot.event
    async def on_connect() -> None:
        log.info("Discord: Gateway-соединение установлено, ожидаю READY")

    @discord_bot.event
    async def on_disconnect() -> None:
        log.warning("Discord: Gateway отключился; discord.py пытается восстановить соединение")

    @discord_bot.event
    async def on_resumed() -> None:
        log.info("Discord: Gateway-сессия успешно восстановлена (RESUMED)")

    @discord_bot.event
    async def on_ready() -> None:
        log.info("Discord bot готов: %s (ID: %s)", discord_bot.user, discord_bot.user.id)
        mark_client_ready("discord")

    @lolka_bot.event
    async def on_ready() -> None:
        log.info("Lolka bot готов: %s (ID: %s)", lolka_bot.user, lolka_bot.user.id)
        mark_client_ready("lolka")

    voice_activity_changed = asyncio.Event()
    discord_rejoin_task: asyncio.Task[None] | None = None

    async def refresh_bridge_after_discord_join() -> None:
        """Rebuild voice sessions after a user re-enters the configured Discord channel.

        Discord can keep the bot's VoiceClient connected while its receive/DAVE
        session no longer delivers decoded audio after a participant leaves and
        rejoins. Reconnecting both sides gives voice-recv and DAVE a clean session.
        """
        try:
            # Let Discord finish applying the member voice-state update first.
            await asyncio.sleep(1.5)
            channel = discord_bot.get_channel(config.discord_voice_channel_id)
            if channel is None:
                channel = await discord_bot.fetch_channel(config.discord_voice_channel_id)
            members = getattr(channel, "members", ())
            if not any(not getattr(member, "bot", False) for member in members):
                log.info("Discord: после события входа в канале нет пользователей; перезапуск моста отменён")
                return
            if not bridge.connected:
                log.info("Discord: голосовое подключение уже потеряно; супервизор восстановит его")
                return
            log.info("Discord: пользователь вернулся в канал; переинициализирую голосовые сессии и DAVE")
            await bridge.stop()
            await bridge.start()
            log.info("Discord: голосовые сессии успешно переинициализированы после повторного входа")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Discord: не удалось переинициализировать мост после повторного входа; супервизор повторит подключение")

    @discord_bot.event
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
        if discord_bot.user and member.id == discord_bot.user.id:
            if before.channel is not None and after.channel is None:
                log.warning("Discord: бот отключился от голосового канала")
            elif before.channel != after.channel and after.channel is not None:
                log.info("Discord: голосовой канал бота изменён на %s", after.channel.id)
        elif before.channel != after.channel:
            log.info(
                "Discord: изменился состав голосового канала (%s -> %s), участник=%s",
                getattr(before.channel, "id", None),
                getattr(after.channel, "id", None),
                member,
            )
            target_id = config.discord_voice_channel_id
            before_id = getattr(before.channel, "id", None)
            after_id = getattr(after.channel, "id", None)
            if after_id == target_id:
                await send_voice_log("Discord", member, True)
            elif before_id == target_id:
                await send_voice_log("Discord", member, False)
            voice_activity_changed.set()

            # A user rejoining can leave the Discord VoiceClient technically
            # connected while voice-recv/DAVE has stopped delivering audio.
            # Debounce bursts of gateway events and rebuild the session once.
            if after_id == target_id and not getattr(member, "bot", False):
                nonlocal discord_rejoin_task
                if discord_rejoin_task is not None and not discord_rejoin_task.done():
                    discord_rejoin_task.cancel()
                discord_rejoin_task = asyncio.create_task(
                    refresh_bridge_after_discord_join(),
                    name="discord-voice-rejoin-refresh",
                )

    @lolka_bot.event
    async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
        if before.channel != after.channel:
            log.info(
                "Lolka: изменился состав голосового канала (%s -> %s), участник=%s",
                getattr(before.channel, "id", None),
                getattr(after.channel, "id", None),
                getattr(member, "name", member),
            )
            target_id = config.lolka_voice_channel_id
            before_id = getattr(before.channel, "id", None)
            after_id = getattr(after.channel, "id", None)
            if after_id == target_id:
                await send_voice_log("Lolka", member, True)
            elif before_id == target_id:
                await send_voice_log("Lolka", member, False)
            voice_activity_changed.set()

    @discord_bot.command(name="bridge")
    @discord_commands.has_guild_permissions(manage_guild=True)
    async def discord_bridge(ctx: discord_commands.Context, action: str = "status") -> None:
        action = action.lower()
        if action in {"start", "connect"}:
            await bridge.start()
            await ctx.reply("🔊 Подключаю оба бота к настроенным голосовым каналам.")
        elif action == "stop":
            await ctx.reply("Постоянное подключение включено: боты остаются в голосовых каналах.")
        elif action == "status":
            await ctx.reply("🟢 Оба бота подключены." if bridge.connected else "🟠 Подключение восстанавливается.")
        else:
            await ctx.reply("Использование: !bridge start|status")

    @lolka_bot.command(name="bridge")
    async def lolka_bridge(ctx: lolka_commands.Context, action: str = "status") -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        if permissions is not None and not getattr(permissions, "manage_guild", False):
            await ctx.send("Для управления мостом нужны права управления сервером.")
            return
        action = action.lower()
        if action in {"start", "connect"}:
            await bridge.start()
            await ctx.send("🔊 Подключаю оба бота к настроенным голосовым каналам.")
        elif action == "stop":
            await ctx.send("Постоянное подключение включено: боты остаются в голосовых каналах.")
        elif action == "status":
            await ctx.send("🟢 Оба бота подключены." if bridge.connected else "🟠 Подключение восстанавливается.")
        else:
            await ctx.send("Использование: !bridge start|status")

    async def keep_bots_in_voice() -> None:
        """Keep both bots connected so the DAVE session is ready before users join."""
        retry_delay = 10
        while not discord_bot.is_closed() and not lolka_bot.is_closed():
            try:
                await discord_bot.wait_until_ready()
                await lolka_bot.wait_until_ready()
                if not bridge.connected:
                    log.info("Подключаю ботов к голосовым каналам")
                    await bridge.start()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка проверки голосовых подключений; следующая попытка через %s сек.", retry_delay)
                await asyncio.sleep(retry_delay)
                continue

            voice_activity_changed.clear()
            try:
                await asyncio.wait_for(voice_activity_changed.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def start_supervisor_after_ready() -> None:
        # Do not call wait_until_ready() before Client.start() has initialized
        # the clients. on_ready fires only after each gateway client is initialized.
        await both_bots_ready.wait()
        log.info("Оба бота авторизованы и готовы; очищаю старые записи в лог-каналах")
        await asyncio.gather(
            clear_old_voice_logs(discord_bot, config.discord_log_channel_id, "Discord"),
            clear_old_voice_logs(lolka_bot, config.lolka_log_channel_id, "Lolka"),
        )
        log.info("Запускаю контроль голосовых подключений")
        await keep_bots_in_voice()

    supervisor_task = asyncio.create_task(
        start_supervisor_after_ready(),
        name="voice-connection-supervisor",
    )
    try:
        await asyncio.gather(
            discord_bot.start(config.discord_token),
            lolka_bot.start(config.lolka_token),
        )
    finally:
        supervisor_task.cancel()
        if discord_rejoin_task is not None and not discord_rejoin_task.done():
            discord_rejoin_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass
        await dashboard.close()
        await bridge.stop()
        await discord_bot.close()
        await lolka_bot.close()
        log.info("Voice Bridge остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("voice-bridge").info("Получен сигнал завершения")
