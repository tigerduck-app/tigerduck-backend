"""The checker's transport over a real TLS connection, to a server on
127.0.0.1 with a certificate from a CA the test makes. Name lookups are
refused and AIA fetches are answered from memory, so nothing leaves the
machine."""

from __future__ import annotations

import contextlib
import socket
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from server import tls_chain
from server.links.checker import _PinnedTransport
from server.tests.test_tls_pin_watcher import AIA_URL, Authority, Pki, _pki
from server.tests.test_tls_pin_watcher import HOST as PKI_HOST

HOST = "www.links.test"
TIMEOUT = {"connect": 5.0, "read": 5.0, "write": 5.0, "pool": 5.0}


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _certificates() -> tuple[bytes, bytes, bytes]:
    """A CA certificate, and a certificate for HOST it signed with its key,
    both PEM, with the extensions Python's strict verification asks for."""
    now = datetime.now(UTC)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = (
        x509.CertificateBuilder()
        .subject_name(_name("links test CA"))
        .issuer_name(_name("links test CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(_name(HOST))
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(HOST)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    key_pem = key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return ca.public_bytes(pem), leaf.public_bytes(pem), key_pem


@contextlib.contextmanager
def _https_server(tmp_path, monkeypatch, cert_pem: bytes, key_pem: bytes):
    """Serves `cert_pem`, the leaf and the chain sent with it, on 127.0.0.1;
    yields the port and the SNI and Host values each connection sent."""
    (tmp_path / "cert.pem").write_bytes(cert_pem)
    (tmp_path / "key.pem").write_bytes(key_pem)
    seen: dict[str, list[str]] = {"sni": [], "host": []}

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    context.sni_callback = lambda _sock, name, _context: seen["sni"].append(name)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["host"].append(self.headers["Host"])
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    # The handshake that only reads the chain closes without a request.
    server.handle_error = lambda _request, _address: None
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    real_getaddrinfo = socket.getaddrinfo

    def loopback_only(host, *args, **kwargs):
        assert host == "127.0.0.1", f"looked up {host}"
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", loopback_only)
    try:
        yield server.server_address[1], seen
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    ca_pem, cert_pem, key_pem = _certificates()
    with _https_server(tmp_path, monkeypatch, cert_pem, key_pem) as (port, seen):
        yield port, ca_pem.decode(), seen


def _get(transport: _PinnedTransport, host: str, port: int) -> httpx.Response:
    request = httpx.Request(
        "GET",
        f"https://{host}:{port}/",
        extensions={"address": "127.0.0.1", "timeout": TIMEOUT},
    )
    response = transport.handle_request(request)
    response.read()
    return response


def test_the_transport_reaches_the_address_by_tls_as_the_host_name(tls_server) -> None:
    port, ca_pem, seen = tls_server
    transport = _PinnedTransport(
        httpx.HTTPTransport(verify=ssl.create_default_context(cadata=ca_pem))
    )

    with transport:
        response = _get(transport, HOST, port)
        assert (response.status_code, response.content) == (200, b"ok")
        assert seen == {"sni": [HOST], "host": [f"{HOST}:{port}"]}

        # The same server, asked for under a name its certificate lacks.
        with pytest.raises(httpx.ConnectError, match="Hostname mismatch"):
            _get(transport, "other.links.test", port)
    assert seen["host"] == [f"{HOST}:{port}"]


# --- A chain sent without its intermediate -----------------------------------


@pytest.fixture
def pki(tmp_path) -> Pki:
    return _pki(tmp_path, "Test")


@pytest.fixture
def incomplete_chain_server(tmp_path, monkeypatch, pki):
    """Sends the leaf twice and the root, but not the intermediate, as
    www.academic.ntust.edu.tw does."""
    pem = serialization.Encoding.PEM
    chain = b"".join(a.cert.public_bytes(pem) for a in (pki.leaf, pki.leaf, pki.root))
    key = pki.leaf.key.private_bytes(
        pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with _https_server(tmp_path, monkeypatch, chain, key) as server:
        yield server


class _Aia:
    """An AIA fetch that answers every URL with `answer`, noting each URL."""

    def __init__(self, answer: Authority | None = None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    def __call__(self, url: str) -> x509.Certificate | None:
        self.asked.append(url)
        return self.answer.cert if self.answer else None


def test_a_chain_without_its_intermediate_is_completed_through_aia(
    incomplete_chain_server, pki
) -> None:
    port, seen = incomplete_chain_server
    aia = _Aia(pki.intermediate)

    with _PinnedTransport(cafile=pki.cafile, fetch=aia) as transport:
        response = _get(transport, PKI_HOST, port)

    assert (response.status_code, response.content) == (200, b"ok")
    assert aia.asked == [AIA_URL]
    # The handshake that failed, the one that read the chain, and the request's.
    assert seen == {"sni": [PKI_HOST] * 3, "host": [f"{PKI_HOST}:{port}"]}


def test_a_chain_aia_cannot_complete_still_fails(incomplete_chain_server, pki) -> None:
    port, seen = incomplete_chain_server
    aia = _Aia()

    with (
        _PinnedTransport(cafile=pki.cafile, fetch=aia) as transport,
        pytest.raises(httpx.ConnectError, match="unable to get local issuer certificate"),
    ):
        _get(transport, PKI_HOST, port)

    assert aia.asked == [AIA_URL] and seen["host"] == []


def test_an_aia_intermediate_never_becomes_a_trust_anchor(
    tmp_path, incomplete_chain_server, pki
) -> None:
    port, seen = incomplete_chain_server
    # Another root is trusted, so only as an anchor could the intermediate
    # vouch for the leaf.
    other = _pki(tmp_path, "Other")
    aia = _Aia(pki.intermediate)

    with (
        _PinnedTransport(cafile=other.cafile, fetch=aia) as transport,
        pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"),
    ):
        _get(transport, PKI_HOST, port)

    assert aia.asked == [AIA_URL] and seen["host"] == []


def test_nothing_is_sent_once_the_check_is_out_of_time(incomplete_chain_server, pki) -> None:
    port, seen = incomplete_chain_server
    aia = _Aia(pki.intermediate)

    with (
        _PinnedTransport(cafile=pki.cafile, fetch=aia, deadline=time.monotonic() - 1) as transport,
        pytest.raises(httpx.ConnectTimeout),
    ):
        _get(transport, PKI_HOST, port)

    assert aia.asked == [] and seen == {"sni": [], "host": []}



class _SlowAia(_Aia):
    """Answers once the check's deadline has passed."""

    def __init__(self, answer: Authority, deadline: float) -> None:
        super().__init__(answer)
        self.deadline = deadline

    def __call__(self, url: str) -> x509.Certificate | None:
        time.sleep(max(0.0, self.deadline - time.monotonic()) + 0.05)
        return super().__call__(url)


def test_no_second_try_starts_once_the_aia_fetch_used_up_the_check(
    incomplete_chain_server, pki
) -> None:
    port, seen = incomplete_chain_server
    deadline = time.monotonic() + 2
    aia = _SlowAia(pki.intermediate, deadline)

    with (
        _PinnedTransport(cafile=pki.cafile, fetch=aia, deadline=deadline) as transport,
        pytest.raises(httpx.ConnectTimeout),
    ):
        _get(transport, PKI_HOST, port)

    # The handshake that failed and the one that read the chain, no third.
    assert aia.asked == [AIA_URL] and seen == {"sni": [PKI_HOST] * 2, "host": []}


class _TimedAia(_Aia):
    """Answers after a second."""

    def __call__(self, url: str) -> x509.Certificate | None:
        time.sleep(1)
        return super().__call__(url)


def test_each_try_waits_no_longer_than_the_check_has_left(incomplete_chain_server, pki) -> None:
    port, _ = incomplete_chain_server
    deadline = time.monotonic() + 4
    timeouts: list[dict] = []

    with _PinnedTransport(
        cafile=pki.cafile, fetch=_TimedAia(pki.intermediate), deadline=deadline
    ) as transport:
        inner = transport._transport

        class Spy(httpx.BaseTransport):
            def handle_request(self, request: httpx.Request) -> httpx.Response:
                timeouts.append(request.extensions["timeout"])
                return inner.handle_request(request)

        transport._transport = Spy()
        assert _get(transport, PKI_HOST, port).status_code == 200

    # Half of what was left: under 4 seconds before the AIA fetch, under 3 after it.
    assert all(v <= 2 for v in timeouts[0].values())
    assert all(v <= 1.5 for v in timeouts[1].values())


def test_a_chain_read_or_aia_fetch_past_its_deadline_makes_no_connection(monkeypatch) -> None:
    connects: list[tuple] = []

    def refuse(*args, **kwargs):
        connects.append(args)
        raise OSError("no connection in this test")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(tls_chain, "resolve", lambda host, port: [("203.0.113.1", port)])
    past = time.monotonic() - 1

    assert tls_chain.fetch_issuer(AIA_URL, deadline=past) is None
    with pytest.raises(TimeoutError):
        tls_chain.peer_chain(tls_chain.unverified_context(), PKI_HOST, [("203.0.113.1", 443)], past)
    assert connects == []


def _connected_pair(monkeypatch, timeout: float) -> socket.socket:
    """Makes `_connect` hand over one end of a socket pair with `timeout`, as
    after a connect that used up part of it; returns the other end."""
    ours, server = socket.socketpair()
    ours.settimeout(timeout)
    monkeypatch.setattr(tls_chain, "_connect", lambda addresses, deadline: ours)
    return server


def test_no_chain_read_handshake_starts_once_the_connect_used_up_the_deadline(
    monkeypatch,
) -> None:
    server = _connected_pair(monkeypatch, 5)

    with pytest.raises(TimeoutError):
        tls_chain.peer_chain(
            tls_chain.unverified_context(), PKI_HOST, [("203.0.113.1", 443)], time.monotonic() - 1
        )

    server.settimeout(1)
    assert server.recv(4096) == b""
    server.close()


def test_the_chain_read_handshake_gets_only_the_time_left(monkeypatch) -> None:
    server = _connected_pair(monkeypatch, 60)
    timeouts: list[float | None] = []

    class Recording:
        def wrap_socket(self, raw: socket.socket, server_hostname: str) -> ssl.SSLSocket:
            timeouts.append(raw.gettimeout())
            raise ssl.SSLError("no handshake in this test")

    with pytest.raises(ssl.SSLError):
        tls_chain.peer_chain(Recording(), PKI_HOST, [("203.0.113.1", 443)], time.monotonic() + 30)

    assert len(timeouts) == 1 and 0 < timeouts[0] <= 30
    server.close()
