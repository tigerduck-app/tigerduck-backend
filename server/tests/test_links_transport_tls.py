"""The checker's transport over a real TLS connection, to a server on
127.0.0.1 with a certificate from a CA the test makes. Name lookups are
refused, so nothing leaves the machine."""

from __future__ import annotations

import socket
import ssl
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from server.links.checker import _PinnedTransport

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


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    ca_pem, cert_pem, key_pem = _certificates()
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
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    real_getaddrinfo = socket.getaddrinfo

    def loopback_only(host, *args, **kwargs):
        assert host == "127.0.0.1", f"looked up {host}"
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", loopback_only)
    try:
        yield server.server_address[1], ca_pem.decode(), seen
    finally:
        server.shutdown()
        server.server_close()


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
