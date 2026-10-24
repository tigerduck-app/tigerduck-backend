"""The pin watcher against in-process TLS servers with hand-made chains.

The root each test trusts stands in for the system store, and AIA fetches
are answered from memory; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import socket
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import (
    AuthorityInformationAccessOID,
    ExtendedKeyUsageOID,
    NameOID,
)
from prometheus_client import REGISTRY
from sqlalchemy import select

from server.db import build_session_factory
from server.tls_pins.document import PinSigner, document_sets, publish, spki_sha256
from server.tls_pins.models import TlsPinDocument, TlsPinSet
from server.tls_pins.watcher import (
    Probe,
    _is_public_url,
    parse_issuer,
    probe,
    run_pin_watch,
)

AIA_URL = "http://aia.invalid/intermediate.cer"
HOST = "tls.test"
UNRELATED_PIN = "A" * 43 + "="
NOW = datetime(2026, 10, 25, 4, 0, tzinfo=UTC)
PEM = serialization.Encoding.PEM


Resolve = Callable[[str, int], list[tuple[str, int]]]


@dataclass(frozen=True)
class Authority:
    cert: x509.Certificate
    key: ec.EllipticCurvePrivateKey

    @property
    def pin(self) -> str:
        return spki_sha256(self.cert)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


_CA_USAGE = x509.KeyUsage(
    digital_signature=True, content_commitment=False, key_encipherment=False,
    data_encipherment=False, key_agreement=False, key_cert_sign=True,
    crl_sign=True, encipher_only=False, decipher_only=False,
)


def _issue(subject: str, issuer: Authority | None, *, ca: bool) -> Authority:
    key = ec.generate_private_key(ec.SECP256R1())
    signer_key = issuer.key if issuer else key
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject))
        .issuer_name(issuer.cert.subject if issuer else _name(subject))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_key.public_key()),
            critical=False,
        )
    )
    if ca:
        builder = builder.add_extension(_CA_USAGE, critical=True)
    else:
        builder = (
            builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
            )
            .add_extension(
                x509.AuthorityInformationAccess(
                    [
                        x509.AccessDescription(
                            AuthorityInformationAccessOID.CA_ISSUERS,
                            x509.UniformResourceIdentifier(AIA_URL),
                        )
                    ]
                ),
                critical=False,
            )
        )
    return Authority(builder.sign(signer_key, hashes.SHA256()), key)


@dataclass(frozen=True)
class Pki:
    root: Authority
    intermediate: Authority
    leaf: Authority
    cafile: str


def _pki(tmp_path: Path, name: str) -> Pki:
    root = _issue(f"{name} Root CA", None, ca=True)
    intermediate = _issue(f"{name} Issuing CA", root, ca=True)
    leaf = _issue(HOST, intermediate, ca=False)
    cafile = tmp_path / f"{name}-roots.pem"
    cafile.write_bytes(root.cert.public_bytes(PEM))
    return Pki(root, intermediate, leaf, str(cafile))


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    return _pki(tmp_path, "Test")


@contextlib.contextmanager
def tls_server(tmp_path: Path, leaf: Authority, *chain: Authority) -> Iterator[Resolve]:
    """Serves `leaf` and `chain` on 127.0.0.1; yields the stand-in for DNS
    that sends HOST there."""
    certfile, keyfile = tmp_path / "server-chain.pem", tmp_path / "server-key.pem"
    certfile.write_bytes(b"".join(a.cert.public_bytes(PEM) for a in (leaf, *chain)))
    keyfile.write_bytes(
        leaf.key.private_bytes(
            PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile, keyfile)
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except TimeoutError:
                continue
            with conn, contextlib.suppress(OSError):
                conn.settimeout(5)
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        port = listener.getsockname()[1]
        yield lambda host, _port: [("127.0.0.1", port)]
    finally:
        stop.set()
        thread.join()
        listener.close()


def _aia(*answers: Authority):
    """An AIA fetch that answers AIA_URL with the first of `answers`."""
    def fetch(url: str) -> x509.Certificate | None:
        return answers[0].cert if url == AIA_URL and answers else None

    return fetch


# --- The probe -------------------------------------------------------------


def test_a_full_chain_validates_up_to_a_root_the_server_does_not_send(tmp_path, pki) -> None:
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        result = probe(HOST, cafile=pki.cafile, fetch=_aia(), resolve=resolve)

    assert result.status == "trusted" and not result.via_aia
    assert [spki_sha256(c) for c in result.chain] == [
        pki.leaf.pin, pki.intermediate.pin, pki.root.pin
    ]


def test_an_incomplete_chain_is_completed_through_aia(tmp_path, pki) -> None:
    with tls_server(tmp_path, pki.leaf) as resolve:
        result = probe(HOST, cafile=pki.cafile, fetch=_aia(pki.intermediate), resolve=resolve)

    assert result.status == "trusted" and result.via_aia
    assert [spki_sha256(c) for c in result.chain] == [
        pki.leaf.pin, pki.intermediate.pin, pki.root.pin
    ]


def test_an_aia_intermediate_never_becomes_a_trust_anchor(tmp_path, pki) -> None:
    other = _pki(tmp_path, "Other")
    with tls_server(tmp_path, pki.leaf) as resolve:
        unrooted = probe(HOST, cafile=other.cafile, fetch=_aia(pki.intermediate), resolve=resolve)
        without_aia = probe(HOST, cafile=pki.cafile, fetch=_aia(), resolve=resolve)

    assert unrooted.status == "untrusted"
    assert [spki_sha256(c) for c in unrooted.chain] == [pki.leaf.pin]
    assert without_aia.status == "untrusted"


def test_a_chain_from_an_untrusted_root_is_untrusted(tmp_path, pki) -> None:
    other = _pki(tmp_path, "Other")
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        result = probe(HOST, cafile=other.cafile, fetch=_aia(pki.intermediate), resolve=resolve)

    assert result.status == "untrusted"
    assert result.detail


def test_an_unreachable_host_is_an_error() -> None:
    with socket.create_server(("127.0.0.1", 0)) as closed:
        port = closed.getsockname()[1]
    result = probe(HOST, resolve=lambda host, _port: [("127.0.0.1", port)])

    assert result.status == "error" and "ConnectionRefusedError" in result.detail


def test_a_host_resolving_to_a_private_address_is_never_connected(monkeypatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("10.0.0.7", 443))]
    )
    connected = []
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: connected.append(a))

    result = probe(HOST)

    assert result.status == "error" and result.detail.startswith("refused:")
    assert connected == []


def test_an_aia_answer_parses_as_der_pem_or_pkcs7(pki) -> None:
    cert = pki.intermediate.cert
    for body in (
        cert.public_bytes(serialization.Encoding.DER),
        cert.public_bytes(PEM),
        pkcs7.serialize_certificates([cert], serialization.Encoding.DER),
    ):
        assert parse_issuer(body) == cert
    assert parse_issuer(b"<html>") is None


def test_aia_is_never_fetched_from_the_stacks_own_addresses() -> None:
    assert not _is_public_url("http://127.0.0.1/ca.cer")
    assert not _is_public_url("http://10.0.0.5/ca.cer")
    assert not _is_public_url("http://169.254.169.254/latest")
    assert not _is_public_url("ftp://8.8.8.8/ca.cer")
    assert _is_public_url("http://8.8.8.8/ca.cer")


# --- The watcher -----------------------------------------------------------


@pytest.fixture
def signer() -> PinSigner:
    return PinSigner(key_id="td-pins-test", key=Ed25519PrivateKey.generate())


@pytest_asyncio.fixture(loop_scope="session")
async def factory(db_session, prepared_engine):
    return build_session_factory(prepared_engine)


async def _store_set(db_session, hosts: list[str], pins: list[str], *, auto_publish: bool) -> None:
    db_session.add(
        TlsPinSet(
            id="ntust",
            domains=[{"name": HOST, "include_subdomains": True}],
            pins=[
                {"spki": p, "label": "built in", "first_seen_at": None,
                 "last_seen_at": None, "source": "builtin"}
                for p in pins
            ],
            expires_at=datetime(2027, 8, 18, tzinfo=UTC),
            monitored_hosts=hosts,
            auto_publish=auto_publish,
        )
    )
    await db_session.commit()


async def _stored_set(factory) -> TlsPinSet:
    async with factory() as session:
        return await session.get(TlsPinSet, "ntust")


async def _documents(factory) -> list[TlsPinDocument]:
    async with factory() as session:
        return list(
            (await session.execute(select(TlsPinDocument).order_by(TlsPinDocument.serial))).scalars()
        )


def _gauge(name: str, target: str) -> float | None:
    return REGISTRY.get_sample_value(name, {"set": "ntust", "host": target})


@pytest.mark.asyncio(loop_scope="session")
async def test_a_changed_chain_adds_its_intermediate_and_root_but_never_its_leaf(
    tmp_path, pki, db_session, factory, signer
) -> None:
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        await _store_set(db_session, [HOST], [UNRELATED_PIN], auto_publish=True)
        async with factory() as session:
            first = await publish(session, signer, reason="initial", now=NOW)
            await session.commit()

        await run_pin_watch(
            factory, signer, timeout=10,
            probe_fn=functools.partial(probe, cafile=pki.cafile, fetch=_aia(), resolve=resolve),
            now=NOW,
        )

    pin_set = await _stored_set(factory)
    assert [p["spki"] for p in pin_set.pins] == [
        UNRELATED_PIN, pki.intermediate.pin, pki.root.pin
    ]
    added = pin_set.pins[1:]
    assert {p["source"] for p in added} == {"watcher"}
    assert [p["label"] for p in added] == ["CN=Test Issuing CA", "CN=Test Root CA"]
    assert pki.leaf.pin not in {p["spki"] for p in pin_set.pins}
    result = pin_set.watch_results[HOST]
    assert result["status"] == "trusted"
    assert result["added"] == [pki.intermediate.pin, pki.root.pin]
    assert [c["subject"] for c in result["chain"]] == [
        "CN=tls.test", "CN=Test Issuing CA", "CN=Test Root CA"
    ]

    documents = await _documents(factory)
    assert [d.serial for d in documents] == [first.serial, first.serial + 1]
    assert documents[-1].reason == "watcher"
    assert document_sets(documents[-1])[0]["pins"] == [
        UNRELATED_PIN, pki.intermediate.pin, pki.root.pin
    ]
    # Measured against the document the apps held when the check ran.
    assert _gauge("tigerduck_tls_chain_matches", HOST) == 0
    assert _gauge("tigerduck_tls_chain_trusted", HOST) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_with_auto_publish_off_the_pins_are_added_but_not_published(
    tmp_path, pki, db_session, factory, signer
) -> None:
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        await _store_set(db_session, [HOST], [UNRELATED_PIN], auto_publish=False)
        async with factory() as session:
            first = await publish(session, signer, reason="initial", now=NOW)
            await session.commit()

        await run_pin_watch(
            factory, signer, timeout=10,
            probe_fn=functools.partial(probe, cafile=pki.cafile, fetch=_aia(), resolve=resolve),
            now=NOW,
        )

    pins = (await _stored_set(factory)).pins
    assert [p.get("pending", False) for p in pins] == [False, True, True]
    assert [d.serial for d in await _documents(factory)] == [first.serial]
    assert _gauge("tigerduck_tls_chain_matches", HOST) == 0

    # An unrelated publish, such as another set's auto publish, leaves them out.
    async with factory() as session:
        later = await publish(session, signer, reason="watcher", now=NOW + timedelta(hours=1))
        await session.commit()
    assert document_sets(later)[0]["pins"] == [UNRELATED_PIN]


@pytest.mark.asyncio(loop_scope="session")
async def test_an_untrusted_chain_changes_nothing_and_is_reported(
    tmp_path, pki, db_session, factory, signer
) -> None:
    other = _pki(tmp_path, "Other")
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        await _store_set(db_session, [HOST], [UNRELATED_PIN], auto_publish=True)
        async with factory() as session:
            first = await publish(session, signer, reason="initial", now=NOW)
            await session.commit()

        await run_pin_watch(
            factory, signer, timeout=10,
            probe_fn=functools.partial(probe, cafile=other.cafile, fetch=_aia(), resolve=resolve),
            now=NOW,
        )

    pin_set = await _stored_set(factory)
    assert [p["spki"] for p in pin_set.pins] == [UNRELATED_PIN]
    assert pin_set.watch_results[HOST]["status"] == "untrusted"
    assert [d.serial for d in await _documents(factory)] == [first.serial]
    assert _gauge("tigerduck_tls_chain_trusted", HOST) == 0


@pytest.mark.asyncio(loop_scope="session")
async def test_a_matching_chain_marks_its_pins_seen_and_removes_none(
    tmp_path, pki, db_session, factory, signer
) -> None:
    with tls_server(tmp_path, pki.leaf, pki.intermediate) as resolve:
        await _store_set(
            db_session, [HOST], [pki.intermediate.pin, UNRELATED_PIN], auto_publish=True
        )
        await run_pin_watch(
            factory, None, timeout=10,
            probe_fn=functools.partial(probe, cafile=pki.cafile, fetch=_aia(), resolve=resolve),
            now=NOW,
        )

    pins = (await _stored_set(factory)).pins
    assert [p["spki"] for p in pins] == [pki.intermediate.pin, UNRELATED_PIN]
    assert pins[0]["first_seen_at"] == pins[0]["last_seen_at"] == "2026-10-25T04:00:00Z"
    assert pins[1]["last_seen_at"] is None
    assert _gauge("tigerduck_tls_chain_matches", HOST) == 1


@pytest.mark.asyncio(loop_scope="session")
async def test_the_watcher_resigns_a_document_with_seven_days_left(
    db_session, factory, signer
) -> None:
    await _store_set(db_session, [], [UNRELATED_PIN], auto_publish=True)
    async with factory() as session:
        first = await publish(session, signer, reason="initial", now=NOW - timedelta(days=22))
        await session.commit()

    await run_pin_watch(factory, signer, timeout=10, now=NOW - timedelta(hours=1))
    await run_pin_watch(factory, signer, timeout=10, now=NOW + timedelta(days=1))

    documents = await _documents(factory)
    assert [d.reason for d in documents] == ["initial", "resign"]
    assert document_sets(documents[1]) == document_sets(first)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_hung_probe_is_cut_off_and_is_no_success(db_session, factory) -> None:
    await _store_set(db_session, ["hung.tls.test"], [UNRELATED_PIN], auto_publish=True)
    before = REGISTRY.get_sample_value("tigerduck_tls_watch_last_success_timestamp")

    def hang(target: str) -> Probe:
        time.sleep(1)
        return Probe("trusted")

    started = time.monotonic()
    await run_pin_watch(factory, None, timeout=0.1, probe_fn=hang, now=NOW)

    assert time.monotonic() - started < 1
    result = (await _stored_set(factory)).watch_results["hung.tls.test"]
    assert result["status"] == "error" and result["detail"] == "timeout"
    assert REGISTRY.get_sample_value("tigerduck_tls_watch_last_success_timestamp") == before
    await asyncio.sleep(1)


@pytest.mark.asyncio(loop_scope="session")
async def test_a_refused_host_is_never_probed(db_session, factory) -> None:
    db_session.add(
        TlsPinSet(
            id="exact",
            domains=[{"name": "exact.test", "include_subdomains": False}],
            pins=[{"spki": UNRELATED_PIN, "label": "x", "first_seen_at": None,
                   "last_seen_at": None, "source": "builtin"}],
            expires_at=datetime(2027, 8, 18, tzinfo=UTC),
            monitored_hosts=[
                "203.0.113.5", "exact.test:8443", "other.example", "sub.exact.test", "exact.test",
            ],
        )
    )
    await db_session.commit()
    probed: list[str] = []

    def record(target: str) -> Probe:
        probed.append(target)
        return Probe("error", detail="stub")

    await run_pin_watch(factory, None, timeout=10, probe_fn=record, now=NOW)

    assert probed == ["exact.test"]
    async with factory() as session:
        results = (await session.get(TlsPinSet, "exact")).watch_results
    assert {t: r["detail"] for t, r in results.items() if t != "exact.test"} == {
        "203.0.113.5": "refused: an IP address, not a host name",
        "exact.test:8443": "refused: port must be 443, 465 or 993",
        "other.example": "refused: not under the set's domains",
        "sub.exact.test": "refused: not under the set's domains",
    }
