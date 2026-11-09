from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _optional_id(name: str) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a numeric ID") from exc


def _optional_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return parsed


def _optional_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean (true/false)")


@dataclass(frozen=True)
class Config:
    discord_token: str
    lolka_token: str
    discord_voice_channel_id: int
    lolka_voice_channel_id: int
    discord_guild_id: int | None
    lolka_guild_id: int | None
    command_prefix: str
    discord_log_channel_id: int | None
    lolka_log_channel_id: int | None
    diagnostic_tone: bool
    max_frames_per_speaker: int
    dashboard_host: str
    dashboard_port: int
    audio_processor_enabled: bool
    audio_processor_host: str
    audio_processor_port: int
    audio_processor_secret: str

    @classmethod
    def from_env(cls) -> "Config":
        processor_host = os.getenv("AUDIO_PROCESSOR_IP", "").strip()
        processor_secret = os.getenv("AUDIO_PROCESSOR_SECRET", "").strip()
        if bool(processor_host) != bool(processor_secret):
            raise RuntimeError("AUDIO_PROCESSOR_IP and AUDIO_PROCESSOR_SECRET must both be set")
        return cls(
            discord_token=_required("DISCORD_BOT_TOKEN"),
            lolka_token=_required("LOLKA_BOT_TOKEN"),
            discord_voice_channel_id=int(_required("DISCORD_VOICE_CHANNEL_ID")),
            lolka_voice_channel_id=int(_required("LOLKA_VOICE_CHANNEL_ID")),
            discord_guild_id=_optional_id("DISCORD_GUILD_ID"),
            lolka_guild_id=_optional_id("LOLKA_GUILD_ID"),
            command_prefix=os.getenv("COMMAND_PREFIX", "!").strip() or "!",
            discord_log_channel_id=_optional_id("DISCORD_LOG_CHANNEL_ID"),
            lolka_log_channel_id=_optional_id("LOLKA_LOG_CHANNEL_ID"),
            diagnostic_tone=_optional_bool("DIAGNOSTIC_TONE"),
            max_frames_per_speaker=_optional_int("MAX_FRAMES_PER_SPEAKER", default=2),
            dashboard_host=os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1",
            dashboard_port=_optional_int("DASHBOARD_PORT", default=8080, minimum=1),
            audio_processor_enabled=bool(processor_host and processor_secret),
            audio_processor_host=processor_host,
            audio_processor_port=_optional_int("AUDIO_PROCESSOR_PORT", default=9443, minimum=1),
            audio_processor_secret=processor_secret,
        )
