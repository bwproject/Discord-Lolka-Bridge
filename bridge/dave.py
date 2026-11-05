from __future__ import annotations

import inspect
import logging
from typing import Any

log = logging.getLogger(__name__)


def _prepared(self: Any) -> set[int]:
    value = getattr(self, "_bridge_dave_prepared", None)
    if value is None:
        value = set()
        setattr(self, "_bridge_dave_prepared", value)
    return value


def _prepare(state: Any, transition_id: int, protocol_version: int) -> bool:
    prepared = _prepared(state)
    if transition_id in prepared:
        return False
    session = getattr(state, "dave_session", None)
    if session is None:
        return False
    session.prepare_transition(transition_id, protocol_version)
    prepared.add(transition_id)
    log.info(
        "DAVE transition %s подготовлен: protocol=%s ready=%s can_encrypt=%s",
        transition_id, protocol_version,
        getattr(session, "ready", None),
        getattr(session, "can_encrypt", None),
    )
    return True


def patch_dave_transitions() -> None:
    """Forward DAVE transitions to davey.

    discord.py 2.7.x tracks DAVE transitions for protocol version negotiation
    and for transition readiness but never calls
    ``DaveSession.prepare_transition`` or ``DaveSession.execute_transition``.
    Without those calls davey keeps ``ready`` false forever, so voice-recv
    drops all incoming Discord audio and discord.py sends outgoing audio
    without E2EE. Keep this workaround until the pinned discord.py version
    handles DAVE sessions itself.
    """
    try:
        import davey  # noqa: F401
        from discord.gateway import DiscordVoiceWebSocket
        from discord.voice_state import VoiceConnectionState
    except Exception as exc:
        log.warning("DAVE-патч недоступен: %s", exc)
        return

    original_execute = VoiceConnectionState._execute_transition
    original_binary = DiscordVoiceWebSocket.received_binary_message
    if getattr(original_execute, "__bridge_dave_patched__", False):
        return
    try:
        sources = (
            inspect.getsource(original_execute)
            + inspect.getsource(original_binary)
        )
        if "prepare_transition" in sources:
            log.info("DAVE-патч не нужен: discord.py уже применяет переходы davey")
            return
    except (OSError, TypeError):
        pass

    async def _execute_transition(self: Any, transition_id: int) -> None:
        protocol_version = self.dave_pending_transitions.get(transition_id)
        await original_execute(self, transition_id)
        if protocol_version is None:
            return
        session = getattr(self, "dave_session", None)
        if session is None:
            return
        try:
            _prepare(self, transition_id, protocol_version)
            if transition_id != 0:
                session.execute_transition(transition_id)
                _prepared(self).discard(transition_id)
            log.info(
                "DAVE transition %s применён: protocol=%s ready=%s can_encrypt=%s",
                transition_id, protocol_version,
                getattr(session, "ready", None),
                getattr(session, "can_encrypt", None),
            )
        except Exception:
            log.exception("Не удалось применить DAVE transition %s", transition_id)

    async def received_binary_message(self: Any, msg: bytes) -> None:
        await original_binary(self, msg)
        if len(msg) < 5:
            return
        op = msg[2]
        if op not in (
            DiscordVoiceWebSocket.MLS_ANNOUNCE_COMMIT_TRANSITION,
            DiscordVoiceWebSocket.MLS_WELCOME,
        ):
            return

        state = self._connection
        if getattr(state, "dave_session", None) is None:
            return

        transition_id = int.from_bytes(msg[3:5], "big")
        protocol_version = state.dave_pending_transitions.get(
            transition_id, state.dave_protocol_version
        )
        try:
            _prepare(state, transition_id, protocol_version)
        except Exception:
            log.exception(
                "Не удалось подготовить DAVE transition %s", transition_id
            )

    _execute_transition.__bridge_dave_patched__ = True  # type: ignore[attr-defined]
    received_binary_message.__bridge_dave_patched__ = True  # type: ignore[attr-defined]
    VoiceConnectionState._execute_transition = _execute_transition
    DiscordVoiceWebSocket.received_binary_message = received_binary_message
    log.info("Включён DAVE-патч для применения transition в davey")
