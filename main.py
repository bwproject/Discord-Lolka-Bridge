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
from bridge.retry import run_client_with_retry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("voice-bridge")

# Discord's default INFO logs hide Gateway close codes and reconnect details.
logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
# RTCP Sender Reports are normal voice traffic, not audio payloads.
logging.getLogger("discord.ext.voice_recv.reader").setLevel(logging.WARNING)

# Wait this long after the last human leaves before disconnecting both voice
# clients. This absorbs short channel switches and avoids connect/disconnect flaps.
VOICE_IDLE_TIMEOUT_SECONDS = 10
VOICE_PRESENCE_RECHECK_SECONDS = 30


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
            "bridge": {"connected": bridge.connected, "mode": "automatic-presence"},
            "audio_processor": {
                "enabled": bridge.remote_audio_client is not None,
                "connected": bool(bridge.remote_audio_client and bridge.remote_audio_client.connected),
                "fallback": "local",
            },
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
        """Send human voice join/leave events to configured text channels."""
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
                await channel.send(message, allowed_mentions=discord.AllowedMentions.none())
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
            bot_id = getattr(getattr(client, "user", None), "id", None)
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
        voice_activity_changed.set()

    @lolka_bot.event
    async def on_ready() -> None:
        log.info("Lolka bot готов: %s (ID: %s)", lolka_bot.user, lolka_bot.user.id)
        mark_client_ready("lolka")
        voice_activity_changed.set()

    voice_activity_changed = asyncio.Event()

    async def get_human_members(client: Any, channel_id: int, platform: str) -> list[Any]:
        """Return cached non-bot members of a configured voice channel."""
        channel = client.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(client, "fetch_channel", None)
            if fetch_channel is None:
                raise RuntimeError(f"{platform}: client cannot fetch voice channel {channel_id}")
            channel = await fetch_channel(channel_id)
        members = getattr(channel, "members", None)
        if members is None:
            raise RuntimeError(f"{platform}: member list is unavailable for voice channel {channel_id}")
        return [member for member in members if not getattr(member, "bot", False)]

    async def has_human_activity() -> bool:
        """The bridge is needed while at least one human is in either target channel."""
        discord_members = await get_human_members(
            discord_bot, config.discord_voice_channel_id, "Discord"
        )
        lolka_members = await get_human_members(
            lolka_bot, config.lolka_voice_channel_id, "Lolka"
        )
        log.debug(
            "Участники целевых каналов: Discord=%s, Lolka=%s",
            len(discord_members), len(lolka_members),
        )
        return bool(discord_members or lolka_members)

    @discord_bot.event
    async def on_voice_state_update(
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if discord_bot.user and member.id == discord_bot.user.id:
            if before.channel is not None and after.channel is None:
                log.warning("Discord: бот отключился от голосового канала")
            elif before.channel != after.channel and after.channel is not None:
                log.info("Discord: голосовой канал бота изменён на %s", after.channel.id)
            return

        if before.channel == after.channel:
            return

        target_id = config.discord_voice_channel_id
        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)
        log.info(
            "Discord: изменился состав голосового канала (%s -> %s), участник=%s",
            before_id, after_id, member,
        )
        if after_id == target_id:
            await send_voice_log("Discord", member, True)
        if before_id == target_id:
            await send_voice_log("Discord", member, False)
        if target_id in {before_id, after_id}:
            voice_activity_changed.set()

    @lolka_bot.event
    async def on_voice_state_update(member: Any, before: Any, after: Any) -> None:
        if before.channel == after.channel:
            return
        target_id = config.lolka_voice_channel_id
        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)
        log.info(
            "Lolka: изменился состав голосового канала (%s -> %s), участник=%s",
            before_id, after_id, getattr(member, "name", member),
        )
        if after_id == target_id:
            await send_voice_log("Lolka", member, True)
        if before_id == target_id:
            await send_voice_log("Lolka", member, False)
        if target_id in {before_id, after_id}:
            voice_activity_changed.set()

    info_message = (
        "🌉 **Discord ↔ Lolka Voice Bridge**\n"
        "Двусторонняя передача голоса между Discord и Lolka в реальном времени.\n"
        "• Автоматическое подключение ботов, когда в целевых каналах есть пользователи.\n"
        "• Автоматическое отключение, когда оба канала пусты.\n"
        "• Веб-панель показывает состояние ботов и аудиомоста.\n\n"
        "🔗 GitHub: https://github.com/bwproject/Discord-Lolka-Bridge"
    )

    @discord_bot.command(name="info")
    async def discord_info(ctx: discord_commands.Context) -> None:
        await ctx.reply(info_message, mention_author=False)

    @lolka_bot.command(name="info")
    async def lolka_info(ctx: lolka_commands.Context) -> None:
        await ctx.send(info_message)

    @discord_bot.command(name="bridge")
    @discord_commands.has_guild_permissions(manage_guild=True)
    async def discord_bridge(ctx: discord_commands.Context, action: str = "status") -> None:
        action = action.lower()
        if action in {"start", "connect"}:
            await bridge.start()
            await ctx.reply("🔊 Принудительно запускаю голосовой мост. Автоматика отключит его при отсутствии участников.")
        elif action == "stop":
            await bridge.stop()
            await ctx.reply("⏹️ Голосовой мост отключён. Автоматика подключит его при появлении участников.")
        elif action == "status":
            await ctx.reply("🟢 Оба бота подключены." if bridge.connected else "⚪ Мост ожидает участников.")
        else:
            await ctx.reply("Использование: !bridge start|stop|status")

    @lolka_bot.command(name="bridge")
    async def lolka_bridge(ctx: lolka_commands.Context, action: str = "status") -> None:
        permissions = getattr(ctx.author, "guild_permissions", None)
        if permissions is not None and not getattr(permissions, "manage_guild", False):
            await ctx.send("Для управления мостом нужны права управления сервером.")
            return
        action = action.lower()
        if action in {"start", "connect"}:
            await bridge.start()
            await ctx.send("🔊 Принудительно запускаю голосовой мост. Автоматика отключит его при отсутствии участников.")
        elif action == "stop":
            await bridge.stop()
            await ctx.send("⏹️ Голосовой мост отключён. Автоматика подключит его при появлении участников.")
        elif action == "status":
            await ctx.send("🟢 Оба бота подключены." if bridge.connected else "⚪ Мост ожидает участников.")
        else:
            await ctx.send("Использование: !bridge start|stop|status")

    async def wait_for_voice_activity(timeout: float) -> None:
        try:
            await asyncio.wait_for(voice_activity_changed.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def supervise_voice_presence() -> None:
        """Connect both bots only while at least one human is in either target channel."""
        while not discord_bot.is_closed() and not lolka_bot.is_closed():
            # Clear before reading member lists so an event arriving during the
            # lookup remains set and is handled immediately on the next wait.
            voice_activity_changed.clear()
            try:
                await discord_bot.wait_until_ready()
                await lolka_bot.wait_until_ready()
                active = await has_human_activity()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Не удалось проверить участников голосовых каналов; повтор через 10 секунд")
                await wait_for_voice_activity(10)
                continue

            if active:
                if not bridge.connected:
                    try:
                        log.info("Обнаружены участники в Discord/Lolka; подключаю оба голосовых клиента")
                        await bridge.start()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        log.exception("Не удалось запустить голосовой мост; повтор через 10 секунд")
                        await wait_for_voice_activity(10)
                        continue
                await wait_for_voice_activity(VOICE_PRESENCE_RECHECK_SECONDS)
                continue

            if bridge.connected:
                log.info(
                    "В целевых каналах нет пользователей; отключение через %s секунд, если никто не вернётся",
                    VOICE_IDLE_TIMEOUT_SECONDS,
                )
                await wait_for_voice_activity(VOICE_IDLE_TIMEOUT_SECONDS)
                # Any voice-state event means the member list must be checked
                # again before disconnecting (including channel-to-channel moves).
                if voice_activity_changed.is_set():
                    continue
                try:
                    if await has_human_activity():
                        continue
                    log.info("Каналы всё ещё пусты; останавливаю аудиомост и отключаю обоих ботов")
                    await bridge.stop()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("Не удалось подтвердить пустоту каналов; оставляю мост подключённым")
                continue

            # No users and no voice connections: sleep until a relevant event,
            # with a slow periodic recheck as protection against missed gateway events.
            await wait_for_voice_activity(VOICE_PRESENCE_RECHECK_SECONDS)

    async def start_supervisor_after_ready() -> None:
        await both_bots_ready.wait()
        log.info("Оба бота авторизованы и готовы; очищаю старые записи в лог-каналах")
        await asyncio.gather(
            clear_old_voice_logs(discord_bot, config.discord_log_channel_id, "Discord"),
            clear_old_voice_logs(lolka_bot, config.lolka_log_channel_id, "Lolka"),
        )
        log.info(
            "Запускаю контроль присутствия: мост активен при наличии хотя бы одного пользователя "
            "в Discord или Lolka; idle timeout=%s сек.",
            VOICE_IDLE_TIMEOUT_SECONDS,
        )
        await supervise_voice_presence()

    supervisor_task = asyncio.create_task(
        start_supervisor_after_ready(),
        name="voice-presence-supervisor",
    )
    try:
        # Keep the platform clients isolated: a fatal Lolka API timeout must not
        # cancel the Discord client (and vice versa). Each client retries with
        # exponential backoff; normal Gateway reconnects remain library-managed.
        await asyncio.gather(
            run_client_with_retry("Discord", discord_bot, config.discord_token, log),
            run_client_with_retry("Lolka", lolka_bot, config.lolka_token, log),
        )
    finally:
        supervisor_task.cancel()
        try:
            await supervisor_task
        except asyncio.CancelledError:
            pass
        await dashboard.close()
        await bridge.stop()
        bridge.close()
        await discord_bot.close()
        await lolka_bot.close()
        log.info("Voice Bridge остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.getLogger("voice-bridge").info("Получен сигнал завершения")
