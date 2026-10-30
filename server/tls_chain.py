"""Completing a chain a server sends without its intermediates, as browsers
and Apple's platforms do: fetch them through the leaf's AIA CA Issuers URL
and add them to the store, never as trust anchors. The pin watcher and the
link checker both use it.
"""

from __future__ import annotations

import math
import socket
import ssl
import time
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, pkcs7
from cryptography.x509.oid import AuthorityInformationAccessOID

from server.tls_pins.targets import TargetRefused, resolve

# Also bounds a whole TLS handshake: CPython keeps one deadline across its reads.
CONNECT_TIMEOUT_SECONDS = 10.0
AIA_FETCH_DEADLINE_SECONDS = 20.0
AIA_MAX_BYTES = 64 * 1024
AIA_MAX_HOPS = 3
# OpenSSL's codes for a chain that stops short of a trusted root.
INCOMPLETE_CHAIN_CODES = frozenset({2, 20, 21})

IssuerFetch = Callable[[str], x509.Certificate | None]


def trust_context(cafile: str | None, extra: Sequence[bytes] = ()) -> ssl.SSLContext:
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


def verify_code(exc: BaseException | None) -> int | None:
    """The X.509 verify code of the failed certificate check behind `exc`."""
    while exc is not None and not isinstance(exc, ssl.SSLCertVerificationError):
        # httpcore drops the cause, so the ssl error is only the context.
        exc = exc.__cause__ or exc.__context__
    return exc.verify_code if exc is not None else None


def unverified_context() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _connect(addresses: list[tuple[str, int]], deadline: float = math.inf) -> socket.socket:
    """A connection to the first of `addresses` that answers before `deadline`.
    Connecting to the checked addresses, not the name, keeps a second lookup
    out. The socket's timeout, which also bounds a handshake, ends by then."""
    error: OSError = OSError("no address")
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline passed before connecting")
        try:
            return socket.create_connection(
                address, timeout=min(CONNECT_TIMEOUT_SECONDS, remaining)
            )
        except OSError as exc:
            error = exc
    raise error


def _bound(sock: socket.socket, deadline: float) -> None:
    """Lowers `sock`'s timeout to what is left before `deadline`, since the
    connect may have used some of the time its timeout was set from."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("deadline passed")
    if remaining < (sock.gettimeout() or math.inf):
        sock.settimeout(remaining)


def peer_chain(
    ctx: ssl.SSLContext,
    host: str,
    addresses: list[tuple[str, int]],
    deadline: float = math.inf,
) -> tuple[x509.Certificate, ...]:
    with _connect(addresses, deadline) as raw:
        _bound(raw, deadline)
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
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


def fetch_issuer(url: str, deadline: float = math.inf) -> x509.Certificate | None:
    """GET over HTTP/1.0 to the checked addresses, so a second lookup cannot
    rebind the name, under one deadline for headers and body alike: the
    caller's `deadline` or AIA_FETCH_DEADLINE_SECONDS, whichever is sooner."""
    request = _aia_request(url)
    if request is None:
        return None
    addresses, head = request
    deadline = min(deadline, time.monotonic() + AIA_FETCH_DEADLINE_SECONDS)
    response = b""
    try:
        with _connect(addresses, deadline) as sock:
            _bound(sock, deadline)
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


def _aia_intermediates(served: tuple[x509.Certificate, ...], fetch: IssuerFetch) -> list[bytes]:
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


def missing_intermediates(
    host: str,
    addresses: list[tuple[str, int]],
    fetch: IssuerFetch = fetch_issuer,
    deadline: float = math.inf,
) -> list[bytes]:
    """DER of the intermediates `host` leaves out of its chain, fetched
    through AIA; empty when AIA supplies none. The chain read ends by
    `deadline`; `fetch` holds to its own."""
    return _aia_intermediates(peer_chain(unverified_context(), host, addresses, deadline), fetch)
