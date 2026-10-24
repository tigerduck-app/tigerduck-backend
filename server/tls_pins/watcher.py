"""The pin watcher: checks the chain every monitored host serves.

A validated chain that shares no pin with its set gets its intermediates
and root added, never its leaf, and published when the set allows it. A
chain that fails system trust is reported and changes nothing. Pins are
never removed here; an operator removes them in the portal.
"""

from __future__ import annotations

import asyncio
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
import structlog
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server import metrics
from server.db import session_scope
from server.tls_pins.document import (
    PinSigner,
    current_document,
    document_sets,
    ensure_current_document,
    lock_pin_state,
    publish,
    published_pins,
    spki_sha256,
    timestamp,
)
from server.tls_pins.models import TlsPinSet
from server.tls_pins.targets import (
    TargetRefused,
    address_allowed,
    check_target,
    parse_target,
    resolve,
    under_domains,
)

logger = structlog.get_logger(__name__)

CONNECT_TIMEOUT_SECONDS = 10.0
AIA_MAX_BYTES = 64 * 1024
AIA_MAX_HOPS = 3
# OpenSSL's codes for a chain that stops short of a trusted root.
_INCOMPLETE_CHAIN_CODES = frozenset({2, 20, 21})


@dataclass(frozen=True)
class Probe:
    status: Literal["trusted", "untrusted", "error"]
    # Trusted: the validated chain, leaf first and anchor last. Untrusted:
    # the chain as served.
    chain: tuple[x509.Certificate, ...] = ()
    detail: str | None = None
    via_aia: bool = False


def _trust_context(cafile: str | None, extra: Sequence[bytes] = ()) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if cafile is None:
        ctx.load_default_certs()
    else:
        ctx.load_verify_locations(cafile=cafile)
    # Lenient as server/_ssl_compat.py is, since the apps' platforms skip
    # OpenSSL's strict RFC 5280 checks. No partial chains: an intermediate
    # fetched through AIA joins the store but never becomes an anchor.
    ctx.verify_flags = ssl.VERIFY_X509_TRUSTED_FIRST
    for der in extra:
        ctx.load_verify_locations(cadata=der)
    return ctx


def _unverified_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect(addresses: list[tuple[str, int]]) -> socket.socket:
    """A connection to the first of `addresses` that answers. Connecting to
    the checked addresses, not the name, keeps a second lookup out."""
    error: OSError = OSError("no address")
    for address in addresses:
        try:
            return socket.create_connection(address, timeout=CONNECT_TIMEOUT_SECONDS)
        except OSError as exc:
            error = exc
    raise error


def _chain(
    ctx: ssl.SSLContext, host: str, addresses: list[tuple[str, int]]
) -> tuple[x509.Certificate, ...]:
    with _connect(addresses) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
        # The chain as served when the context does not verify.
        ders = tls.get_verified_chain()
    return tuple(x509.load_der_x509_certificate(der) for der in ders)


def _ca_issuers_url(cert: x509.Certificate) -> str | None:
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess)
    except x509.ExtensionNotFound:
        return None
    for desc in aia.value:
        if desc.access_method == AuthorityInformationAccessOID.CA_ISSUERS and isinstance(
            desc.access_location, x509.UniformResourceIdentifier
        ):
            return desc.access_location.value
    return None


def _is_public_url(url: str) -> bool:
    """An http(s) URL whose host resolves to public addresses only, so a
    certificate cannot point the fetch at the stack's own services."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 80)
    except OSError:
        return False
    return all(address_allowed(info[4][0]) for info in infos)


def parse_issuer(body: bytes) -> x509.Certificate | None:
    """A caIssuers answer: DER, PEM or a PKCS #7 bundle."""
    for load in (x509.load_der_x509_certificate, x509.load_pem_x509_certificate):
        try:
            return load(body)
        except ValueError:
            pass
    try:
        certs = pkcs7.load_der_pkcs7_certificates(body)
    except ValueError:
        return None
    return certs[0] if certs else None


def fetch_issuer(url: str) -> x509.Certificate | None:
    if not _is_public_url(url):
        return None
    try:
        with (
            httpx.Client(timeout=CONNECT_TIMEOUT_SECONDS) as client,
            client.stream("GET", url) as resp,
        ):
            if resp.status_code != 200:
                return None
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > AIA_MAX_BYTES:
                    return None
    except httpx.HTTPError:
        return None
    return parse_issuer(body)


def _aia_intermediates(
    served: tuple[x509.Certificate, ...], fetch: Callable[[str], x509.Certificate | None]
) -> list[bytes]:
    """The intermediates the served chain leaves out, fetched through AIA.
    Stops at a root: only the system store may supply one."""
    found: list[bytes] = []
    cert = served[-1]
    for _ in range(AIA_MAX_HOPS):
        url = _ca_issuers_url(cert)
        issuer = fetch(url) if url and cert.issuer != cert.subject else None
        if issuer is None or issuer.subject != cert.issuer or issuer.issuer == issuer.subject:
            break
        found.append(issuer.public_bytes(Encoding.DER))
        cert = issuer
    return found


def probe(
    target: str,
    *,
    cafile: str | None = None,
    fetch: Callable[[str], x509.Certificate | None] = fetch_issuer,
    resolve: Callable[[str, int], list[tuple[str, int]]] = resolve,
) -> Probe:
    """Check `target` against system trust (`cafile` and `resolve` stand in
    for it and for DNS in tests). Blocking; the watcher runs it in a worker
    thread."""
    host, port = parse_target(target)
    try:
        addresses = resolve(host, port)
    except TargetRefused as exc:
        return Probe("error", detail=f"refused: {exc}")
    try:
        try:
            return Probe("trusted", _chain(_trust_context(cafile), host, addresses))
        except ssl.SSLCertVerificationError as exc:
            if exc.verify_code not in _INCOMPLETE_CHAIN_CODES:
                raise
            extra = _aia_intermediates(_chain(_unverified_context(), host, addresses), fetch)
            if not extra:
                raise
            chain = _chain(_trust_context(cafile, extra), host, addresses)
            return Probe("trusted", chain, via_aia=True)
    except ssl.SSLCertVerificationError as exc:
        try:
            served = _chain(_unverified_context(), host, addresses)
        except OSError:
            served = ()
        return Probe("untrusted", served, detail=exc.verify_message)
    except (OSError, ValueError) as exc:
        return Probe("error", detail=f"{type(exc).__name__}: {exc}")


async def _probe_in_thread(probe_fn: Callable[[str], Probe], target: str, timeout: float) -> Probe:
    try:
        return await asyncio.wait_for(asyncio.to_thread(probe_fn, target), timeout)
    except TimeoutError:
        return Probe("error", detail="timeout")
    except Exception as exc:  # one host's bug must not end the run
        logger.exception("tls_pins.probe_failed", host=target)
        return Probe("error", detail=type(exc).__name__)


def _refusal(target: str, domains: list[dict]) -> str | None:
    """Why the watcher will not connect to `target`, if it will not."""
    try:
        host, _ = check_target(target)
    except TargetRefused as exc:
        return str(exc)
    return None if under_domains(host, domains) else "not under the set's domains"


async def _refused(reason: str) -> Probe:
    return Probe("error", detail=f"refused: {reason}")


def _describe(chain: tuple[x509.Certificate, ...]) -> list[dict]:
    return [
        {
            "subject": cert.subject.rfc4514_string(),
            "issuer": cert.issuer.rfc4514_string(),
            "not_before": timestamp(cert.not_valid_before_utc),
            "not_after": timestamp(cert.not_valid_after_utc),
            "spki": spki_sha256(cert),
        }
        for cert in chain
    ]


# The chain label each host's info series carries, to remove it on change.
_chain_labels: dict[tuple[str, str], str] = {}


def _set_chain_info(set_id: str, target: str, label: str) -> None:
    old = _chain_labels.get((set_id, target))
    if old is not None and old != label:
        metrics.TLS_CHAIN_INFO.remove(set_id, target, old)
    _chain_labels[(set_id, target)] = label
    metrics.TLS_CHAIN_INFO.labels(set=set_id, host=target, chain=label).set(1)


def _chain_label(described: list[dict], prefix: str = "") -> str:
    return prefix + "; ".join(
        f"{c['subject']} until {c['not_after'][:10]} pin {c['spki']}" for c in described
    )


def _apply(
    pin_set: TlsPinSet,
    target: str,
    result: Probe,
    served: set[str] | None,
    now: datetime,
) -> list[str]:
    """Record one host's result on its set; returns the pins it added.
    `served` holds the set's pins in the current document, if there is one."""
    seen_at = timestamp(now)
    described = _describe(result.chain)
    record = {
        "checked_at": seen_at,
        "status": result.status,
        "detail": result.detail,
        "via_aia": result.via_aia,
        "chain": described,
        "added": [],
    }
    labels = {"set": pin_set.id, "host": target}
    if result.status == "untrusted":
        metrics.TLS_CHAIN_TRUSTED.labels(**labels).set(0)
        _set_chain_info(pin_set.id, target, _chain_label(described, f"untrusted ({result.detail}): "))
        logger.warning("tls_pins.chain_untrusted", **labels, detail=result.detail)
    elif result.status == "trusted":
        known = {pin["spki"] for pin in pin_set.pins}
        seen = {c["spki"] for c in described}
        added = []
        if not known & seen:
            # The leaf is never pinned: the sets hold CAs only.
            for cert in described[1:]:
                if cert["spki"] not in known and cert["spki"] not in {p["spki"] for p in added}:
                    added.append(
                        {
                            "spki": cert["spki"],
                            "label": cert["subject"],
                            "first_seen_at": seen_at,
                            "last_seen_at": seen_at,
                            "source": "watcher",
                            # Left out of every document until Publish now.
                            "pending": not pin_set.auto_publish,
                        }
                    )
        pin_set.pins = [
            {
                **pin,
                "first_seen_at": pin.get("first_seen_at") or seen_at,
                "last_seen_at": seen_at,
            }
            if pin["spki"] in seen
            else pin
            for pin in pin_set.pins
        ] + added
        record["added"] = [pin["spki"] for pin in added]
        effective = served if served is not None else set(published_pins(pin_set))
        metrics.TLS_CHAIN_MATCHES.labels(**labels).set(1 if effective & seen else 0)
        metrics.TLS_CHAIN_TRUSTED.labels(**labels).set(1)
        _set_chain_info(pin_set.id, target, _chain_label(described))
        if added:
            pin_set.updated_at = now
            pin_set.updated_by = "watcher"
            logger.warning(
                "tls_pins.chain_changed",
                **labels,
                added=record["added"],
                chain=[(c["subject"], c["not_after"]) for c in described],
            )
    pin_set.watch_results = {**pin_set.watch_results, target: record}
    return record["added"]


async def run_pin_watch(
    session_factory: async_sessionmaker[AsyncSession],
    signer: PinSigner | None,
    *,
    timeout: float,
    probe_fn: Callable[[str], Probe] = probe,
    now: datetime | None = None,
) -> None:
    """Check every monitored host, record the results, publish what the
    sets allow, and keep the document from expiring."""
    async with session_factory() as session:
        rows = await session.execute(
            select(TlsPinSet.id, TlsPinSet.domains, TlsPinSet.monitored_hosts)
        )
        targets = [
            (set_id, host, _refusal(host, domains))
            for set_id, domains, hosts in rows
            for host in hosts
        ]
    # The probes reach the school's servers, so no transaction is open yet.
    results = await asyncio.gather(
        *(
            _refused(refusal) if refusal else _probe_in_thread(probe_fn, host, timeout)
            for _, host, refusal in targets
        )
    )
    now = now or datetime.now(UTC)
    async with session_scope(session_factory) as session:
        await lock_pin_state(session)
        document = await current_document(session)
        published = (
            {s["id"]: set(s["pins"]) for s in document_sets(document)} if document else {}
        )
        sets = {s.id: s for s in (await session.execute(select(TlsPinSet))).scalars()}
        should_publish = False
        for (set_id, host, _), result in zip(targets, results, strict=True):
            pin_set = sets.get(set_id)
            if pin_set is None:
                continue
            added = _apply(pin_set, host, result, published.get(set_id), now)
            should_publish |= bool(added) and pin_set.auto_publish
        if should_publish and signer is not None:
            await publish(session, signer, reason="watcher", now=now)
        await ensure_current_document(session, signer, now=now)
    if any(result.status != "error" for result in results):
        metrics.TLS_WATCH_LAST_SUCCESS.set(time.time())
