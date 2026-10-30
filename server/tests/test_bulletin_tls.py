"""Bulletin fetches verify TLS. The server is on 127.0.0.1 with certificates
the test makes: it sends its leaf without the intermediate, as NTUST's
servers may, under a root with no Subject Key Identifier, as TWCA Global
Root CA has. Name lookups other than 127.0.0.1 are refused."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server.bulletins.jobs import (
    NTUST_INTERMEDIATES,
    default_http_client_factory,
    resolve_bulletin_verify,
)
from server.config import Settings
from server.tests.test_links_transport_tls import _https_server
from server.tests.test_tls_pin_watcher import _CA_USAGE, HOST, PEM, Authority, _issue, _name


def _root_without_ski(name: str) -> Authority:
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(_name(name))
        .issuer_name(_name(name))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(_CA_USAGE, critical=True)
        .sign(key, hashes.SHA256())
    )
    return Authority(cert, key)


@dataclass(frozen=True)
class Chain:
    roots: Path
    intermediates: Path
    leaf: Authority


@pytest.fixture
def chain(tmp_path: Path) -> Chain:
    root = _root_without_ski("Test Global Root CA")
    intermediate = _issue("Test Secure SSL CA", root, ca=True)
    roots, intermediates = tmp_path / "roots.pem", tmp_path / "intermediates.pem"
    roots.write_bytes(root.cert.public_bytes(PEM))
    intermediates.write_bytes(intermediate.cert.public_bytes(PEM))
    return Chain(roots, intermediates, _issue(HOST, intermediate, ca=False))


@pytest.fixture
def port(tmp_path, monkeypatch, chain: Chain):
    key = chain.leaf.key.private_bytes(
        PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with _https_server(tmp_path, monkeypatch, chain.leaf.cert.public_bytes(PEM), key) as (
        port,
        _seen,
    ):
        yield port


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, env="development", **overrides)


async def _get(client: httpx.AsyncClient, port: int, host: str = HOST) -> httpx.Response:
    async with client:
        return await client.get(f"https://127.0.0.1:{port}/", extensions={"sni_hostname": host})


async def test_the_scheduler_client_refuses_a_chain_no_public_root_vouches_for(port):
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _get(default_http_client_factory(), port)


async def test_a_leaf_sent_without_its_intermediate_verifies_with_the_bundled_one(
    port, chain: Chain
):
    verify = resolve_bulletin_verify(
        _settings(), cafile=str(chain.roots), intermediates=chain.intermediates
    )

    response = await _get(httpx.AsyncClient(verify=verify), port)

    assert (response.status_code, response.content) == (200, b"ok")


async def test_a_name_the_certificate_lacks_is_refused(port, chain: Chain):
    verify = resolve_bulletin_verify(
        _settings(), cafile=str(chain.roots), intermediates=chain.intermediates
    )

    with pytest.raises(httpx.ConnectError, match="Hostname mismatch"):
        await _get(httpx.AsyncClient(verify=verify), port, host="other.tls.test")


async def test_without_the_intermediate_the_chain_is_refused(port, chain: Chain):
    verify = resolve_bulletin_verify(_settings(), cafile=str(chain.roots))

    with pytest.raises(httpx.ConnectError, match="unable to get local issuer certificate"):
        await _get(httpx.AsyncClient(verify=verify), port)


async def test_the_operator_bundle_adds_its_certificates(port, chain: Chain):
    settings = _settings(bulletin_ca_bundle=chain.intermediates)
    verify = resolve_bulletin_verify(settings, cafile=str(chain.roots))

    response = await _get(httpx.AsyncClient(verify=verify), port)

    assert response.status_code == 200


def test_the_bundled_intermediates_are_ntusts_and_chain_to_certifi_roots():
    bundled = x509.load_pem_x509_certificates(NTUST_INTERMEDIATES.read_bytes())
    with warnings.catch_warnings():
        # certifi has a root with a negative serial number.
        warnings.simplefilter("ignore")
        roots = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())

    names = {c.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value for c in bundled}
    assert names == {
        "TWCA Secure SSL Certification Authority",
        "TWCA SSL Certification Authority",
    }
    for cert in bundled:
        assert cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        assert cert.not_valid_after_utc > datetime.now(UTC)
        issuer = next(r for r in roots if r.subject == cert.issuer and r.issuer == r.subject)
        cert.verify_directly_issued_by(issuer)
