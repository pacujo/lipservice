"""Symmetric encryption for sensitive fields stored in the database.

Uses Fernet (AES-128-CBC + HMAC-SHA256) with a key derived from a
passphrase via PBKDF2-HMAC-SHA256.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

_SALT = b"lipservice-network-passwords-v1"
_ITERATIONS = 600_000


def _derive_key(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_SALT,
        iterations=_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt(plaintext: str, passphrase: str) -> str:
    return Fernet(_derive_key(passphrase)).encrypt(
        plaintext.encode(),
    ).decode()


def decrypt(ciphertext: str, passphrase: str) -> str:
    return Fernet(_derive_key(passphrase)).decrypt(
        ciphertext.encode(),
    ).decode()
