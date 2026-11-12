"""One-time sealed handoff of a saved login from the dashboard that read it to the browser task that types it.

The browser task makes a key pair for one ask and keeps the private half in memory only. It publishes the public
half, signed with the app secret both processes already share, so the dashboard can tell the ask came from its own
task. The dashboard seals the login to that public key. Only that task, while it is still running, can open it. The
sealed box is removed from the database when it is read, and it is useless once the task ends.

X25519 key agreement, HKDF-SHA256, ChaCha20-Poly1305. The ask id, the site and the step are bound in as associated
data, so a box made for one ask cannot be opened as another.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_INFO = b"site sign-in handoff v1"


class SealError(Exception):
    """The box could not be made or opened. The message never includes its contents."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _raw(text: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(str(text or "").encode())
    except Exception:
        raise SealError("bad encoding") from None


def _key(shared: bytes, context: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_INFO + b"|" + context.encode()).derive(shared)


def sign_public(app_secret: str, public: str, context: str) -> str:
    return hmac.new((app_secret or "").encode(), f"{context}|{public}".encode(), hashlib.sha256).hexdigest()


def public_is_ours(app_secret: str, public: str, context: str, signature: str) -> bool:
    if not app_secret or not public or not signature:
        return False
    return hmac.compare_digest(sign_public(app_secret, public, context), str(signature))


class HandoffKey:
    """The browser task's half. The private key never leaves this object."""

    def __init__(self) -> None:
        self._private = X25519PrivateKey.generate()

    def __repr__(self) -> str:
        return "HandoffKey()"

    @property
    def public(self) -> str:
        return _b64(self._private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))

    def open(self, box: Any, context: str) -> dict:
        if not isinstance(box, dict):
            raise SealError("no box")
        try:
            sender = X25519PublicKey.from_public_bytes(_raw(box.get("epk")))
            key = _key(self._private.exchange(sender), context)
            plain = ChaCha20Poly1305(key).decrypt(_raw(box.get("nonce")), _raw(box.get("ct")), context.encode())
            data = json.loads(plain.decode())
        except (InvalidTag, ValueError, TypeError):
            raise SealError("could not open") from None
        if not isinstance(data, dict):
            raise SealError("could not open")
        return data


def seal(public: str, payload: dict, context: str) -> dict:
    """Seal `payload` so only the holder of the matching private key can read it."""
    try:
        receiver = X25519PublicKey.from_public_bytes(_raw(public))
    except ValueError:
        raise SealError("bad key") from None
    sender = X25519PrivateKey.generate()
    key = _key(sender.exchange(receiver), context)
    nonce = os.urandom(12)
    sealed = ChaCha20Poly1305(key).encrypt(nonce, json.dumps(payload).encode(), context.encode())
    return {"epk": _b64(sender.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)), "nonce": _b64(nonce), "ct": _b64(sealed)}
