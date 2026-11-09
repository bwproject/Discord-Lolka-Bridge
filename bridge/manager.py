from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import av
import discord
import lolka
from aiortc.mediastreams import MediaStreamError
from discord.ext import voice_recv

from .audio import LolkaMixerAudioSource, MixerAudioSource, PCMFrameMixer
from .remote_audio import RemoteAudioClient, RemotePCMFrameMixer

log = logging.getLogger(__name__)


class DiscordPCMReceiveSink(voice_recv.AudioSink):
    """Receive decoded Discord PCM and forward it by speaker."""

    def __init__(self, mixer: PCMFrameMixer, own_user_id: int | None) -> None:
        super().__init__()
        self._mixer = mixer
        self._own_user_id = own_user_id
        self._seen_speakers: set[int] = set()
        self._unknown_user_logged = False
        self._missing_pcm_logged = False
        self._rtcp_sender_report_logged = False
        self._last_transfer_log_at = 0.0
        self._received_frames = 0
        self._received_bytes = 0

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_state(self, member: Any, ssrc: int, state: Any) -> None:
        user_id = getattr(member, "id", None)
        log.info(
            "Discord: gateway сообщил speaking-state user=%s ssrc=%s state=%s",
            user_id, ssrc, getattr(state, "name", state),
        )

    @voice_recv.AudioSink.listener()
    def on_voice_member_speaking_start(self, member: Any) -> None:
        log.info(
            "Discord: обнаружена RTP-активность от %s (%s)",
            member, getattr(member, "id", None),
        )

    @voice_recv.AudioSink.listener()
    def on_rtcp_packet(self, packet: Any, guild: Any) -> None:
        # Sender Reports are normal RTCP control traffic, not audio payloads.
        if getattr(packet, "type", None) == 200 and not self._rtcp_sender_report_logged:
            self._rtcp_sender_report_logged = True
            log.debug("Discord: получен обычный RTCP Sender Report (type=200)")

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.User | discord.Member | None, data: Any) -> None:
        if user is None:
            if not self._unknown_user_logged:
                self._unknown_user_logged = True
                log.warning("Discord: voice receive прислал аудио без объекта пользователя")
            return
        if data.pcm is None:
            if not self._missing_pcm_logged:
                self._missing_pcm_logged = True
                log.warning("Discord: voice receive прислал пакет без PCM для пользователя %s", user.id)
            return
        # Other bots may be the audio source in a bot-to-bot bridge.
        # Ignore only this bridge's own Discord account to prevent feedback.
        if user.id == self._own_user_id:
            return
        if user.id not in self._seen_speakers:
            self._seen_speakers.add(user.id)
            log.info(
                "Discord: принимаю аудио от %s (%s), bot=%s",
                user, user.id, user.bot,
            )
        pcm = data.pcm
        self._mixer.push(user.id, pcm)
        self._received_frames += 1
        self._received_bytes += len(pcm)
        now = time.monotonic()
        if self._last_transfer_log_at == 0.0 or now - self._last_transfer_log_at >= 5.0:
            self._last_transfer_log_at = now
            log.info(
                "Discord -> Lolka: звук получен от %s; PCM поставлен в очередь передачи "
                "(вызовов=%s, данных=%s байт)",
                user, self._received_frames, self._received_bytes,
            )
            self._received_frames = 0
            self._received_bytes = 0

    def cleanup(self) -> None:
        log.info("Discord: приём аудио остановлен")


class VoiceBridge:
    def __init__(self, discord_bot: Any, lolka_bot: Any, config: Any) -> None:
        self.discord_bot = discord_bot
        self.lolka_bot = lolka_bot
        self.config = config
        self.remote_audio_client = (
            RemoteAudioClient(
                config.audio_processor_host,
                config.audio_processor_port,
                config.audio_processor_secret,
            )
            if config.audio_processor_enabled else None
        )
        self.discord_to_lolka = self._new_mixer(0)
        self.lolka_to_discord = self._new_mixer(1)
        self.discord_voice: discord.VoiceClient | None = None
        self.lolka_voice: Any | None = None
        self._lock = asyncio.Lock()
        self._track_tasks: set[asyncio.Task[None]] = set()
        self._seen_lolka_speakers: set[str] = set()
        self._logged_resolved_channels: set[tuple[str, int]] = set()

    def _new_mixer(self, bus: int) -> PCMFrameMixer | RemotePCMFrameMixer:
        local = PCMFrameMixer(self.config.max_frames_per_speaker)
        if self.remote_audio_client is None:
            return local
        return RemotePCMFrameMixer(local, self.remote_audio_client, bus)

    def close(self) -> None:
        if self.remote_audio_client is not None:
            self.remote_audio_client.close()

    @property
    def connected(self) -> bool:
        try:
            return bool(
                self.discord_voice is not None
                and self.discord_voice.is_connected()
                and self.lolka_voice is not None
                and self.lolka_voice.is_connected()
            )
        except Exception:
            log.exception("Не удалось проверить состояние голосовых подключений")
            return False

    async def _resolve_channel(self, bot: Any, channel_id: int, guild_id: int | None, label: str) -> Any:
        if guild_id is not None:
            guild = bot.get_guild(guild_id)
            if guild is None:
                raise RuntimeError(f"{label}: server {guild_id} is not available to the bot")
            channel = guild.get_channel(channel_id)
        else:
            channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception as exc:
                raise RuntimeError(f"{label}: cannot find channel {channel_id}: {exc}") from exc
        if not hasattr(channel, "connect"):
            raise RuntimeError(f"{label}: channel {channel_id} is not a voice channel")
        channel_log_key = (label, int(channel_id))
        if channel_log_key not in self._logged_resolved_channels:
            self._logged_resolved_channels.add(channel_log_key)
            log.info("%s: найден голосовой канал '%s' (ID: %s)", label, getattr(channel, "name", channel_id), channel_id)
        return channel

    async def start(self) -> None:
        async with self._lock:
            if self.connected:
                log.debug("Оба бота уже подключены к голосовым каналам")
                return
            log.info("Подключение ботов к настроенным голосовым каналам...")
            await self._disconnect_unlocked()
            discord_channel = await self._resolve_channel(
                self.discord_bot, self.config.discord_voice_channel_id,
                self.config.discord_guild_id, "Discord",
            )
            lolka_channel = await self._resolve_channel(
                self.lolka_bot, self.config.lolka_voice_channel_id,
                self.config.lolka_guild_id, "Lolka",
            )
            try:
                self.discord_voice = await discord_channel.connect(cls=voice_recv.VoiceRecvClient)
                log.info("Discord: бот подключён к '%s' (%s)", discord_channel.name, discord_channel.id)
                connection = getattr(self.discord_voice, "_connection", None)
                dave_session = getattr(connection, "dave_session", None)
                log.info(
                    "Discord DAVE: protocol=%s can_encrypt=%s session_ready=%s",
                    getattr(connection, "dave_protocol_version", None),
                    getattr(connection, "can_encrypt", None),
                    getattr(dave_session, "ready", None),
                )
                discord_sink = DiscordPCMReceiveSink(
                    self.discord_to_lolka, getattr(self.discord_bot.user, "id", None)
                )
                self.discord_voice.listen(discord_sink)
                log.info(
                    "Discord: приём аудио запущен; is_listening=%s",
                    self.discord_voice.is_listening(),
                )
                self.discord_voice.play(
                    MixerAudioSource(
                        self.lolka_to_discord, self.config.diagnostic_tone
                    ),
                    after=lambda error: log.error(
                        "Discord: ошибка воспроизведения входящего аудио: %s", error
                    ) if error else log.info("Discord: источник воспроизведения завершился"),
                )

                # Register the Lolka receive callback in the VoiceClient constructor.
                # Setting on_receive_track after await channel.connect() can miss
                # producers announced during the WebRTC connection handshake.
                bridge = self

                class BridgeLolkaVoiceClient(lolka.VoiceClient):
                    def __init__(self, client: Any, channel: Any) -> None:
                        super().__init__(client, channel)
                        self.on_receive_track = bridge._on_lolka_track

                self.lolka_voice = await lolka_channel.connect(cls=BridgeLolkaVoiceClient)
                log.info("Lolka: бот подключён к '%s' (%s)", lolka_channel.name, lolka_channel.id)
                self.lolka_voice.play(
                    LolkaMixerAudioSource(
                        self.discord_to_lolka, self.config.diagnostic_tone
                    ),
                    after=lambda error: log.error(
                        "Lolka: ошибка воспроизведения входящего аудио: %s", error
                    ) if error else log.info("Lolka: источник воспроизведения завершился"),
                )
                log.info(
                    "Состояние воспроизведения: Discord=%s, Lolka=%s",
                    self.discord_voice.is_playing(),
                    self.lolka_voice.is_playing(),
                )
            except Exception:
                log.exception("Ошибка подключения к голосовым каналам; будет выполнена повторная попытка")
                await self._disconnect_unlocked()
                raise
            log.info("Голосовой мост активен: Discord %s <-> Lolka %s", discord_channel.id, lolka_channel.id)

    def _on_lolka_track(self, track: Any, user_id: Any, producer_id: Any) -> None:
        if self.lolka_bot.user and str(user_id) == str(self.lolka_bot.user.id):
            return
        speaker = str(user_id)
        if speaker not in self._seen_lolka_speakers:
            self._seen_lolka_speakers.add(speaker)
            log.info(
                "Lolka: принимаю входящий аудиопоток user=%s producer=%s",
                user_id, producer_id,
            )
        # The callback may fire while channel.connect() is still completing,
        # before self.lolka_voice has been assigned. The track itself is valid.
        task = self.lolka_bot.loop.create_task(
            self._forward_lolka_track(track, user_id),
            name=f"lolka-audio-{user_id}",
        )
        self._track_tasks.add(task)
        task.add_done_callback(self._track_tasks.discard)

    async def _forward_lolka_track(self, track: Any, user_id: Any) -> None:
        resampler = av.AudioResampler(format="s16", layout="stereo", rate=48_000)
        first_frame = True
        first_input_frame = True
        last_transfer_log_at = 0.0
        received_frames = 0
        received_bytes = 0
        try:
            # Do not gate this loop on self.lolka_voice: the callback can be
            # delivered before connect() returns and assigns that attribute.
            while True:
                frame = await track.recv()
                if first_input_frame:
                    first_input_frame = False
                    log.info(
                        "Lolka: исходный аудиокадр user=%s rate=%s format=%s layout=%s samples=%s",
                        user_id, getattr(frame, "sample_rate", None),
                        getattr(getattr(frame, "format", None), "name", None),
                        getattr(getattr(frame, "layout", None), "name", None),
                        getattr(frame, "samples", None),
                    )
                converted_frames = resampler.resample(frame)
                for converted in converted_frames:
                    pcm = converted.to_ndarray().tobytes()
                    if first_frame:
                        first_frame = False
                        log.info(
                            "Lolka: получен первый PCM-кадр user=%s rate=%s format=%s layout=%s bytes=%s",
                            user_id, converted.sample_rate, converted.format.name,
                            converted.layout.name, len(pcm),
                        )
                    self.lolka_to_discord.push(user_id, pcm)
                    received_frames += 1
                    received_bytes += len(pcm)
                    now = time.monotonic()
                    if last_transfer_log_at == 0.0 or now - last_transfer_log_at >= 5.0:
                        last_transfer_log_at = now
                        log.info(
                            "Lolka -> Discord: звук получен от user=%s и поставлен в очередь передачи "
                            "(кадров за интервал=%s, данных=%s байт)",
                            user_id, received_frames, received_bytes,
                        )
                        received_frames = 0
                        received_bytes = 0
        except MediaStreamError:
            log.info("Lolka: аудиопоток пользователя %s завершён", user_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка приёма аудио Lolka от пользователя %s", user_id)
        finally:
            try:
                track.stop()
            except Exception:
                pass

    async def stop(self) -> None:
        async with self._lock:
            await self._disconnect_unlocked()

    async def _disconnect_unlocked(self) -> None:
        for task in tuple(self._track_tasks):
            task.cancel()
        self._track_tasks.clear()
        self._seen_lolka_speakers.clear()
        discord_voice, self.discord_voice = self.discord_voice, None
        lolka_voice, self.lolka_voice = self.lolka_voice, None
        if discord_voice is not None:
            try:
                if discord_voice.is_listening():
                    discord_voice.stop_listening()
                discord_voice.stop()
                await discord_voice.disconnect(force=True)
                log.info("Discord: голосовое подключение закрыто")
            except Exception:
                log.exception("Ошибка отключения Discord voice")
        if lolka_voice is not None:
            try:
                lolka_voice.stop()
                await lolka_voice.disconnect()
                log.info("Lolka: голосовое подключение закрыто")
            except Exception:
                log.exception("Ошибка отключения Lolka voice")
        self.discord_to_lolka = self._new_mixer(0)
        self.lolka_to_discord = self._new_mixer(1)
