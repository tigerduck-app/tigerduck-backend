"""aioapns wrapper — JWT auth + Live Activity Push-to-Start send path.

Uses the modern token-based auth (.p8 key + key_id + team_id). Same key works
for development and production; `use_sandbox` picks the APNs host.
"""

from __future__ import annotations

import asyncio
import socket
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


# TCP keepalive on an APNs connection: the first probe after a minute
# without traffic, then every 10s, and the connection is reported lost
# after 3 go unanswered.
_KEEPALIVE_IDLE_SECONDS = 60
_KEEPALIVE_INTERVAL_SECONDS = 10
_KEEPALIVE_PROBES = 3


class _KeepAliveProtocol:
    """Turns TCP keepalive on for each connection aioapns opens.

    A connection sits idle for up to `apns_connection_idle_seconds`
    between sends, and a NAT or load balancer on the way drops a flow that
    quiet without telling either end: the next send then waits out its
    whole timeout, and the delivery goes out a retry round later. A probe
    each minute keeps the route's state alive, and a peer that stops
    answering is reported lost, so the pool stops handing the connection
    out.
    """

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        sock = transport.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # TCP_KEEPIDLE on Linux, TCP_KEEPALIVE on macOS.
            idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(
                socket, "TCP_KEEPALIVE", None
            )
            if idle is not None:
                sock.setsockopt(socket.IPPROTO_TCP, idle, _KEEPALIVE_IDLE_SECONDS)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(
                    socket.IPPROTO_TCP,
                    socket.TCP_KEEPINTVL,
                    _KEEPALIVE_INTERVAL_SECONDS,
                )
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, _KEEPALIVE_PROBES)
        super().connection_made(transport)  # type: ignore[misc]


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
        # this pool's connections the configured lifetime instead, with TCP
        # keepalive to see them through it — on our pool's protocol class,
        # not aioapns's own, so nothing else using the library changes.
        base = client.pool.protocol_class
        client.pool.protocol_class = type(
            base.__name__,
            (_KeepAliveProtocol, base),
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
            #
            # Once per client: sends that timed out on it together replace
            # it once, rather than each discarding the last one's
            # replacement with its connections open. And its pool is closed
            # only once every send already on it is over, which the timeout
            # bounds — two ticks can send at once, and closing it now would
            # cut off one still receiving its answer.
            if self._client is client:
                self._client = self._build_client()
                asyncio.get_running_loop().call_later(timeout, client.pool.close)
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
        # aioapns' APNs has no close of its own, and its pool's connections
        # now stay open for `apns_connection_idle_seconds`, each with a
        # timer pending on the loop — close them rather than leave both to
        # outlive the app.
        self._client.pool.close()


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
