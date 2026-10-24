"""Signing and publishing the pin document.

`publish` is the only code that signs. It runs inside its caller's
transaction, under the advisory lock every change to the pin state takes,
so concurrent callers get distinct, increasing serials.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
import structlog
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from server import metrics
from server.tls_pins.models import TlsPinDocument, TlsPinSet

logger = structlog.get_logger(__name__)

DOCUMENT_FORMAT = 1
DOCUMENT_VALIDITY = timedelta(days=30)
# A document with this little left is signed again under a new serial.
RESIGN_BEFORE = timedelta(days=7)
# "TD_PINS"
PIN_STATE_LOCK_KEY = 0x54445F50494E53
KEY_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
KEY_FILE_PREFIX = "tls-pins-"


def key_file_name(key_id: str) -> str:
    return f"{KEY_FILE_PREFIX}{key_id}.pem"


@dataclass(frozen=True)
class PinSigner:
    key_id: str
    key: Ed25519PrivateKey = field(repr=False)


def load_signer(path: Path | None) -> PinSigner | None:
    """The PKCS #8 PEM key at `path`, named `tls-pins-<key_id>.pem`, or None.
    Without a key the backend runs and publishes nothing."""
    if path is None or not path.is_file():
        logger.warning("tls_pins.signing_disabled", reason="no_key_file", path=str(path))
        return None
    key_id = path.name.removeprefix(KEY_FILE_PREFIX).removesuffix(".pem")
    if path.name != key_file_name(key_id) or not KEY_ID_PATTERN.fullmatch(key_id):
        logger.error("tls_pins.signing_disabled", reason="bad_key_file_name", path=str(path))
        return None
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        logger.error(
            "tls_pins.signing_disabled",
            reason="key_unreadable",
            path=str(path),
            error=type(exc).__name__,
        )
        return None
    if not isinstance(key, Ed25519PrivateKey):
        logger.error("tls_pins.signing_disabled", reason="not_ed25519", path=str(path))
        return None
    return PinSigner(key_id=key_id, key=key)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def spki_sha256(cert: x509.Certificate) -> str:
    """The pin of `cert`: padded base64 of SHA-256 over its SPKI DER."""
    spki = cert.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()


@functools.cache
def prompt_roots() -> tuple[str, ...]:
    """The pins of every root in certifi's bundle, Mozilla's TLS roots."""
    certs = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())
    return tuple(sorted({spki_sha256(c) for c in certs}))


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def published_pins(pin_set: TlsPinSet) -> list[str]:
    """The pins a document may carry: pending ones wait for an operator."""
    return [pin["spki"] for pin in pin_set.pins if not pin.get("pending")]


def set_entry(pin_set: TlsPinSet) -> dict:
    """`pin_set` as the document lists it."""
    return {
        "id": pin_set.id,
        "domains": pin_set.domains,
        "pins": published_pins(pin_set),
        "expires_at": timestamp(pin_set.expires_at),
    }


def document_sets(document: TlsPinDocument) -> list[dict]:
    return json.loads(document.payload)["pin_sets"]


async def lock_pin_state(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(PIN_STATE_LOCK_KEY)))


async def current_document(session: AsyncSession) -> TlsPinDocument | None:
    return (
        await session.execute(
            select(TlsPinDocument).order_by(TlsPinDocument.serial.desc()).limit(1)
        )
    ).scalar_one_or_none()


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


async def publish(
    session: AsyncSession,
    signer: PinSigner,
    *,
    reason: str,
    pin_sets: list[dict] | None = None,
    now: datetime | None = None,
) -> TlsPinDocument:
    """Sign and store a new document: `pin_sets` when given (a re-sign
    passes the current document's), otherwise every set as stored."""
    await lock_pin_state(session)
    previous = await current_document(session)
    if pin_sets is None:
        rows = (await session.execute(select(TlsPinSet).order_by(TlsPinSet.id))).scalars()
        pin_sets = [set_entry(row) for row in rows]
    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    expires = issued + DOCUMENT_VALIDITY
    # A restored database can hold an older highest serial; the clock keeps
    # the new one above every serial the apps have seen.
    serial = max(previous.serial + 1 if previous else 0, int(issued.timestamp()))
    payload = _json_bytes(
        {
            "format": DOCUMENT_FORMAT,
            "serial": serial,
            "issued_at": timestamp(issued),
            "expires_at": timestamp(expires),
            "pin_sets": pin_sets,
            "prompt_roots": list(prompt_roots()),
        }
    )
    signature = signer.key.sign(payload)
    envelope = _json_bytes(
        {
            "format": DOCUMENT_FORMAT,
            "key_id": signer.key_id,
            "payload": b64url(payload),
            "signature": b64url(signature),
        }
    )
    document = TlsPinDocument(
        serial=serial,
        key_id=signer.key_id,
        payload=payload,
        signature=signature,
        envelope=envelope,
        issued_at=issued,
        expires_at=expires,
        reason=reason,
    )
    session.add(document)
    await session.flush()

    before = {s["id"]: s for s in document_sets(previous)} if previous else {}
    for entry in pin_sets:
        if before.get(entry["id"]) != entry:
            metrics.TLS_PINS_PUBLISHED.labels(set=entry["id"]).inc()
    metrics.TLS_PIN_DOCUMENT_EXPIRY.set(expires.timestamp())
    logger.info("tls_pins.published", serial=serial, reason=reason, key_id=signer.key_id)
    return document


async def ensure_current_document(
    session: AsyncSession, signer: PinSigner | None, *, now: datetime | None = None
) -> TlsPinDocument | None:
    """Publish the first document, or re-sign the current one's sets once
    `RESIGN_BEFORE` or less is left. Returns the document it signed."""
    if signer is None:
        return None
    now = now or datetime.now(UTC)
    await lock_pin_state(session)
    document = await current_document(session)
    if document is None:
        return await publish(session, signer, reason="initial", now=now)
    if document.expires_at - now <= RESIGN_BEFORE:
        return await publish(
            session, signer, reason="resign", pin_sets=document_sets(document), now=now
        )
    return None


async def refresh_gauges(session: AsyncSession) -> None:
    """The expiry gauges from the database, and every set's publish counter
    at zero, so its first publish is an increase the alert sees."""
    document = await current_document(session)
    if document is not None:
        metrics.TLS_PIN_DOCUMENT_EXPIRY.set(document.expires_at.timestamp())
    for set_id, expires_at in await session.execute(
        select(TlsPinSet.id, TlsPinSet.expires_at)
    ):
        metrics.TLS_PIN_SET_EXPIRY.labels(set=set_id).set(expires_at.timestamp())
        metrics.TLS_PINS_PUBLISHED.labels(set=set_id)
