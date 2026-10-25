"""The pin watcher: checks the chain every monitored host serves.

A validated chain that shares no pin with its set gets its intermediates
and root added, never its leaf, and published when the set allows it. A
chain that fails system trust is reported and changes nothing. Pins are
never removed here; an operator removes them in the portal.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import structlog
from cryptography import x509
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from server import metrics
from server.db import session_scope
from server.tls_chain import (
    INCOMPLETE_CHAIN_CODES,
    IssuerFetch,
    fetch_issuer,
    missing_intermediates,
    peer_chain,
    trust_context,
    unverified_context,
)
from server.tls_pins.document import (
    PinSigner,
    current_document,
    document_sets,
    lock_pin_state,
    publish,
    published_pins,
    set_entry,
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

# A host that stalls holds one of these threads, not the default executor's.
PROBE_THREADS = 8
_probe_pool = ThreadPoolExecutor(max_workers=PROBE_THREADS, thread_name_prefix="tls-probe")
# Held until a probe's thread is done, so a host's timeout starts when a thread
# takes it, not while it waits behind slow hosts.
_probe_slots = asyncio.Semaphore(PROBE_THREADS)


@dataclass(frozen=True)
class Probe:
    status: Literal["trusted", "untrusted", "error"]
    # Trusted: the validated chain, leaf first and anchor last. Untrusted:
    # the chain as served.
    chain: tuple[x509.Certificate, ...] = ()
    detail: str | None = None
    via_aia: bool = False


def probe(
    target: str,
    *,
    cafile: str | None = None,
    fetch: IssuerFetch = fetch_issuer,
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
            return Probe("trusted", peer_chain(trust_context(cafile), host, addresses))
        except ssl.SSLCertVerificationError as exc:
            if exc.verify_code not in INCOMPLETE_CHAIN_CODES:
                raise
            extra = missing_intermediates(host, addresses, fetch)
            if not extra:
                raise
            chain = peer_chain(trust_context(cafile, extra), host, addresses)
            return Probe("trusted", chain, via_aia=True)
    except ssl.SSLCertVerificationError as exc:
        try:
            served = peer_chain(unverified_context(), host, addresses)
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


def forget_host(set_id: str, target: str) -> None:
    """Drop a host's series, so a host no longer monitored stops alerting."""
    for gauge in (
        metrics.TLS_CHAIN_TRUSTED, metrics.TLS_CHAIN_MATCHES, metrics.TLS_HOST_LAST_SUCCESS
    ):
        gauge.remove(set_id, target)
    label = _chain_labels.pop((set_id, target), None)
    if label is not None:
        metrics.TLS_CHAIN_INFO.remove(set_id, target, label)


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
    previous = pin_set.watch_results.get(target) or {}
    record = {
        "checked_at": seen_at,
        "status": result.status,
        "detail": result.detail,
        "via_aia": result.via_aia,
        "chain": described,
        "added": [],
        "last_success_at": (
            seen_at if result.status != "error" else previous.get("last_success_at") or seen_at
        ),
    }
    labels = {"set": pin_set.id, "host": target}
    # An error keeps the last chain's values, as the series do.
    gauges = previous.get("gauges")
    if result.status == "untrusted":
        gauges = {
            "trusted": 0,
            "matches": (gauges or {}).get("matches"),
            "chain": _chain_label(described, f"untrusted ({result.detail}): "),
        }
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
        gauges = {
            "trusted": 1,
            "matches": 1 if effective & seen else 0,
            "chain": _chain_label(described),
        }
        if added:
            pin_set.updated_at = now
            pin_set.updated_by = "watcher"
            logger.warning(
                "tls_pins.chain_changed",
                **labels,
                added=record["added"],
                chain=[(c["subject"], c["not_after"]) for c in described],
            )
    record["gauges"] = gauges
    _export(pin_set.id, target, record)
    pin_set.watch_results = {**pin_set.watch_results, target: record}
    return record["added"]


def _export(set_id: str, target: str, record: dict) -> None:
    """A host's series from its watch result, after each check and again at
    startup, so a restart does not resolve an alert no check cleared."""
    labels = {"set": set_id, "host": target}
    if record.get("last_success_at"):
        metrics.TLS_HOST_LAST_SUCCESS.labels(**labels).set(
            datetime.fromisoformat(record["last_success_at"]).timestamp()
        )
    gauges = record.get("gauges")
    if gauges is None:
        return
    metrics.TLS_CHAIN_TRUSTED.labels(**labels).set(gauges["trusted"])
    if gauges["matches"] is not None:
        metrics.TLS_CHAIN_MATCHES.labels(**labels).set(gauges["matches"])
    _set_chain_info(set_id, target, gauges["chain"])


async def restore_host_gauges(session: AsyncSession) -> None:
    """Every monitored host's series as its last saved check left them."""
    for pin_set in (await session.execute(select(TlsPinSet))).scalars():
        for target in pin_set.monitored_hosts:
            _export(pin_set.id, target, pin_set.watch_results.get(target) or {})


async def run_pin_watch(
    session_factory: async_sessionmaker[AsyncSession],
    signer: PinSigner | None,
    *,
    timeout: float,
    probe_fn: Callable[[str], Probe] = probe,
    now: datetime | None = None,
) -> None:
    """Check every monitored host, record the results, and publish what the
    sets allow."""
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
        served = {s["id"]: s for s in document_sets(document)} if document else {}
        sets = {s.id: s for s in (await session.execute(select(TlsPinSet))).scalars()}
        for (set_id, host, _), result in zip(targets, results, strict=True):
            pin_set = sets.get(set_id)
            # An operator may have removed the host while it was probed.
            if pin_set is None or host not in pin_set.monitored_hosts:
                continue
            entry = served.get(set_id)
            _apply(pin_set, host, result, set(entry["pins"]) if entry else None, now)
        # Compared with the served document, not this run's additions, so a
        # change kept while the key was missing goes out once it is back.
        should_publish = document is not None and any(
            s.auto_publish and served.get(s.id) != set_entry(s) for s in sets.values()
        )
        if should_publish and signer is not None:
            await publish(session, signer, reason="watcher", now=now)
