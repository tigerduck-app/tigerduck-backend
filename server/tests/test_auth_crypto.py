"""Unit tests for the credential envelope encryption."""

from __future__ import annotations

import base64

import pytest

from server.auth.crypto import (
    CredentialCipher,
    CredentialCipherError,
    EncryptedBlob,
    build_credential_aad,
)

KEY_V1 = base64.b64encode(b"1" * 32).decode()
KEY_V2 = base64.b64encode(b"2" * 32).decode()


def make_cipher(active: str = "v1") -> CredentialCipher:
    return CredentialCipher(keys={"v1": KEY_V1, "v2": KEY_V2}, active_key_id=active)


def test_round_trip() -> None:
    cipher = make_cipher()
    payload = {"ntust_password": "hunter2", "password_verified": False}
    aad = build_credential_aad(42, "ntust_sso")

    blob = cipher.encrypt(payload, aad)
    assert blob.key_id == "v1"
    assert len(blob.nonce) == 12
    assert blob.aad == aad
    assert cipher.decrypt(blob) == payload


def test_tampered_ciphertext_raises() -> None:
    cipher = make_cipher()
    blob = cipher.encrypt({"x": 1}, "aad")
    tampered = EncryptedBlob(
        key_id=blob.key_id,
        nonce=blob.nonce,
        ciphertext=blob.ciphertext[:-1] + bytes([blob.ciphertext[-1] ^ 0xFF]),
        aad=blob.aad,
    )
    with pytest.raises(CredentialCipherError):
        cipher.decrypt(tampered)


def test_wrong_aad_raises() -> None:
    cipher = make_cipher()
    blob = cipher.encrypt({"x": 1}, build_credential_aad(1, "ntust_sso"))
    moved = EncryptedBlob(
        key_id=blob.key_id,
        nonce=blob.nonce,
        ciphertext=blob.ciphertext,
        aad=build_credential_aad(2, "ntust_sso"),
    )
    with pytest.raises(CredentialCipherError):
        cipher.decrypt(moved)


def test_decrypt_uses_blob_key_id_not_active_key() -> None:
    # Encrypt under v1, then "rotate" the active key to v2 — old blobs must
    # still decrypt via their recorded key_id.
    blob = make_cipher(active="v1").encrypt({"x": 1}, "aad")
    rotated = make_cipher(active="v2")
    assert rotated.decrypt(blob) == {"x": 1}
    assert rotated.encrypt({"x": 1}, "aad").key_id == "v2"


def test_unknown_key_id_raises() -> None:
    blob = make_cipher().encrypt({"x": 1}, "aad")
    lost_keys = CredentialCipher(keys={"v2": KEY_V2}, active_key_id="v2")
    with pytest.raises(CredentialCipherError):
        lost_keys.decrypt(blob)


def test_missing_active_key_raises() -> None:
    with pytest.raises(CredentialCipherError):
        CredentialCipher(keys={"v1": KEY_V1}, active_key_id="v9")


def test_bad_key_length_raises() -> None:
    short = base64.b64encode(b"1" * 16).decode()
    with pytest.raises(CredentialCipherError):
        CredentialCipher(keys={"v1": short}, active_key_id="v1")


def test_build_credential_aad_format() -> None:
    assert build_credential_aad(7, "ntust_sso") == (
        "external_account:7:provider:ntust_sso"
    )
