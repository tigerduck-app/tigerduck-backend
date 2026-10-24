"""Generate the Ed25519 key pair that signs the TLS pin document.

Writes the private key to `<dir>/<key_id>.pem` with mode 0600 and refuses
to overwrite one. Prints the key_id and the raw 32-byte public key in
base64, which the apps embed; nothing else.

    uv run python scripts/generate_tls_pin_key.py td-pins-2026a
    uv run python scripts/generate_tls_pin_key.py td-pins-2026b --dir /Volumes/offline
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from server.tls_pins.document import KEY_ID_PATTERN


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("key_id", help="lower case letters, digits and hyphens")
    parser.add_argument("--dir", type=Path, default=_BACKEND_DIR / "server" / "secrets")
    args = parser.parse_args(argv)
    if not KEY_ID_PATTERN.fullmatch(args.key_id):
        parser.error("key_id must be lower case letters, digits and hyphens")

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path = args.dir / f"{args.key_id}.pem"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        print(f"{path} already exists; not overwriting it", file=sys.stderr)
        return 1
    with os.fdopen(fd, "wb") as out:
        out.write(pem)

    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    print(f"key_id: {args.key_id}")
    print(f"public_key: {base64.b64encode(public).decode()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
