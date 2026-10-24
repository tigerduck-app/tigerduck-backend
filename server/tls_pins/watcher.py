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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

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
    check_target,
    parse_target,
    resolve,
    under_domains,
)

logger = structlog.get_logger(__name__)

# Also bounds a whole TLS handshake: CPython keeps one deadline across its reads.
CONNECT_TIMEOUT_SECONDS = 10.0
AIA_FETCH_DEADLINE_SECONDS = 20.0
AIA_MAX_BYTES = 64 * 1024
AIA_MAX_HOPS = 3
# A host that stalls holds one of these threads, not the default executor's.
PROBE_THREADS = 8
_probe_pool = ThreadPoolExecutor(max_workers=PROBE_THREADS, thread_name_prefix="tls-probe")
# Held until a probe's thread is done, so a host's timeout starts when a thread
# takes it, not while it waits behind slow hosts.
_probe_slots = asyncio.Semaphore(PROBE_THREADS)
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


def _aia_request(url: str) -> tuple[list[tuple[str, int]], bytes] | None:
    """The addresses to fetch an http AIA URL from and the request to send,
    if its host resolves to public addresses only: a certificate must not
    point the fetch at the stack's own services."""
    if not url.isascii() or not url.isprintable() or " " in url:
        return None
    parts = urlsplit(url)
    try:
        if parts.scheme != "http" or not parts.hostname:
            return None
        addresses = resolve(parts.hostname, parts.port or 80)
    except (ValueError, TargetRefused):
        return None
    target = parts.path or "/"
    if parts.query:
        target += "?" + parts.query
    host = parts.netloc.rpartition("@")[2]
    return addresses, f"GET {target} HTTP/1.0\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()


def _recv(sock: socket.socket, deadline: float) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("AIA fetch deadline")
    sock.settimeout(remaining)
    return sock.recv(16384)


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
    """GET over HTTP/1.0 to the checked addresses, so a second lookup cannot
    rebind the name, under one deadline for headers and body alike."""
    request = _aia_request(url)
    if request is None:
        return None
    addresses, head = request
    deadline = time.monotonic() + AIA_FETCH_DEADLINE_SECONDS
    response = b""
    try:
        with _connect(addresses) as sock:
            sock.sendall(head)
            while chunk := _recv(sock, deadline):
                response += chunk
                if len(response) > AIA_MAX_BYTES:
                    return None
    except OSError:
        return None
    head, sep, body = response.partition(b"\r\n\r\n")
    if not sep or head.split(b"\r\n", 1)[0].split()[1:2] != [b"200"]:
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


def _free_slot(call: asyncio.Future) -> None:
    _probe_slots.release()
    if not call.cancelled():
        call.exception()  # a probe that fails after its timeout was already reported


async def _probe_in_thread(probe_fn: Callable[[str], Probe], target: str, timeout: float) -> Probe:
    await _probe_slots.acquire()
    call = asyncio.get_running_loop().run_in_executor(_probe_pool, probe_fn, target)
    call.add_done_callback(_free_slot)
    try:
        # A timed-out probe keeps its slot until its socket timeouts end it.
        return await asyncio.wait_for(asyncio.shield(call), timeout)
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
            # An operator may have removed the host while it was probed.
            if pin_set is None or host not in pin_set.monitored_hosts:
                continue
            added = _apply(pin_set, host, result, published.get(set_id), now)
            should_publish |= bool(added) and pin_set.auto_publish
        if should_publish and signer is not None:
            await publish(session, signer, reason="watcher", now=now)
        await ensure_current_document(session, signer, now=now)
    if any(result.status != "error" for result in results):
        metrics.TLS_WATCH_LAST_SUCCESS.set(time.time())
