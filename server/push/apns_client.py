"""aioapns wrapper — JWT auth + Live Activity Push-to-Start send path.

Uses the modern token-based auth (.p8 key + key_id + team_id). Same key works
for development and production; `use_sandbox` picks the APNs host.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog
from aioapns import APNs, NotificationRequest, PushType

from server.config import Settings
from server.push.payload import ApnsRequest, PushKind

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SendResult:
    success: bool
    status: str
    description: str | None = None
    notification_id: str | None = None


class PushSender(Protocol):
    async def send(self, request: ApnsRequest) -> SendResult: ...
    async def close(self) -> None: ...


class AioApnsSender:
    """Real APNs sender — requires p8 key file, key_id, team_id."""

    def __init__(self, settings: Settings) -> None:
        if not settings.apns_key_id or not settings.apns_team_id:
            raise ValueError(
                "APNs not configured: set TIGERDUCK_APNS_KEY_ID and _APNS_TEAM_ID"
            )
        key_path: Path = settings.apns_key_path
        if not key_path.exists():
            raise FileNotFoundError(f"APNs auth key not found: {key_path}")

        # aioapns' `key` parameter wants the PEM *content*, not a file path.
        # Passing the path makes PyJWT try to parse the path string as PEM
        # and fail with MalformedFraming.
        self._key_pem = key_path.read_text(encoding="utf-8")
        self._settings = settings
        self._client = self._build_client()

    def _build_client(self) -> APNs:
        client = APNs(
            key=self._key_pem,
            key_id=self._settings.apns_key_id,
            team_id=self._settings.apns_team_id,
            # per-request topic overrides this default
            topic=self._settings.apns_bundle_id,
            use_sandbox=self._settings.apns_env == "development",
        )
        # aioapns closes a connection after INACTIVITY_TIME (10s) idle. Give
        # this pool's connections the configured lifetime instead — on our
        # pool's protocol class, not aioapns's own, so nothing else using
        # the library changes.
        base = client.pool.protocol_class
        client.pool.protocol_class = type(
            base.__name__,
            (base,),
            {"INACTIVITY_TIME": self._settings.apns_connection_idle_seconds},
        )
        return client

    async def send(self, request: ApnsRequest) -> SendResult:
        if request.kind is PushKind.live_activity:
            push_type = PushType.LIVEACTIVITY
        elif request.kind is PushKind.background:
            push_type = PushType.BACKGROUND
        else:
            push_type = PushType.ALERT
        notification = NotificationRequest(
            device_token=request.device_token,
            message=request.message,
            priority=request.priority,
            time_to_live=max(0, request.expiration - _now_seconds()),
            push_type=push_type,
            apns_topic=request.topic,
            collapse_key=request.collapse_id,
        )
        client = self._client
        timeout = self._settings.apns_send_timeout_seconds
        try:
            result = await asyncio.wait_for(
                client.send_notification(notification), timeout=timeout
            )
        except asyncio.TimeoutError:
            # Most likely a connection that died without a FIN, which aioapns
            # would keep handing out; replace the client so the next send
            # reconnects. Not "unregistered", so the pipeline retries the
            # delivery.
            self._client = self._build_client()
            client.pool.close()
            logger.warning("apns.send_timeout", timeout_seconds=timeout)
            return SendResult(
                success=False,
                status="TIMEOUT",
                description=f"APNs send exceeded {timeout}s",
            )
        return SendResult(
            success=result.is_successful,
            status=result.status,
            description=result.description,
            notification_id=result.notification_id,
        )

    async def close(self) -> None:
        # aioapns APNs has no public close; connections GC when client dereferenced.
        pass


class RecordingSender:
    """Test double — captures requests instead of hitting Apple.

    Use in unit tests and in the `TIGERDUCK_APNS_KEY_ID=''` dev path to avoid
    sending accidental pushes during local iteration.
    """

    def __init__(self) -> None:
        self.requests: list[ApnsRequest] = []

    async def send(self, request: ApnsRequest) -> SendResult:
        self.requests.append(request)
        logger.info(
            "apns.recorded",
            topic=request.topic,
            priority=request.priority,
            token_head=request.device_token[:8],
            expiration=request.expiration,
            aps_event=request.message["aps"].get("event"),
        )
        return SendResult(success=True, status="200", description="recorded")

    async def close(self) -> None:
        pass


def build_sender(settings: Settings) -> PushSender:
    """Return the real sender when APNs credentials exist, else a recording stub."""
    if settings.apns_key_id and settings.apns_team_id and settings.apns_key_path.exists():
        logger.info("apns.using_real_sender", env=settings.apns_env)
        return AioApnsSender(settings)
    logger.warning(
        "apns.using_recording_sender",
        reason="APNs credentials missing; pushes will not be delivered",
    )
    return RecordingSender()


def _now_seconds() -> int:
    from datetime import datetime, timezone

    return int(datetime.now(timezone.utc).timestamp())
