"""The signing key: the script that makes one, loading it, and the roots a
document lists as prompt-eligible."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from server.tls_pins.document import load_signer, prompt_roots, spki_sha256

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_prompt_roots_are_the_sorted_pins_of_certifis_roots() -> None:
    import certifi

    certs = x509.load_pem_x509_certificates(Path(certifi.where()).read_bytes())

    assert list(prompt_roots()) == sorted({spki_sha256(c) for c in certs})
    assert len(prompt_roots()) > 100


def test_the_key_script_writes_a_private_key_only_the_owner_reads(tmp_path) -> None:
    run = [sys.executable, "scripts/generate_tls_pin_key.py", "td-pins-2026a", "--dir", str(tmp_path)]

    first = subprocess.run(run, cwd=REPO_ROOT, capture_output=True, text=True, check=True)
    again = subprocess.run(run, cwd=REPO_ROOT, capture_output=True, text=True, check=False)

    path = tmp_path / "td-pins-2026a.pem"
    assert os.stat(path).st_mode & 0o777 == 0o600
    lines = first.stdout.splitlines()
    assert lines[0] == "key_id: td-pins-2026a" and len(lines) == 2
    public = base64.b64decode(lines[1].removeprefix("public_key: "))
    assert len(public) == 32
    assert again.returncode == 1 and again.stdout == ""

    signer = load_signer(path)
    assert signer is not None and signer.key_id == "td-pins-2026a"
    signature = signer.key.sign(b"payload")
    Ed25519PublicKey.from_public_bytes(public).verify(signature, b"payload")


def test_a_missing_or_unusable_key_disables_signing(tmp_path) -> None:
    not_ed25519 = tmp_path / "td-pins-ec.pem"
    not_ed25519.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    garbage = tmp_path / "td-pins-x.pem"
    garbage.write_bytes(b"not a key")

    assert load_signer(None) is None
    assert load_signer(tmp_path / "td-pins-missing.pem") is None
    assert load_signer(garbage) is None
    assert load_signer(not_ed25519) is None
