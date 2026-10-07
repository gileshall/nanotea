"""Web Push (VAPID) to the browsers and home-screen app the owner turned notifications on in."""

import base64
import json
import os
import threading
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class PushBook:
    """The VAPID key pair (made on first run) and the subscribed browsers."""

    def __init__(self, root: Path, subject: str | None):
        self.key_path = root / "vapid_private.pem"
        self.subs_path = root / "push_subscriptions.json"
        self.subject = subject
        self._lock = threading.Lock()
        if not self.key_path.exists():
            key = ec.generate_private_key(ec.SECP256R1())
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
        key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        self.public_key = _b64url(key.public_key().public_bytes(serialization.Encoding.X962,
                                                                serialization.PublicFormat.UncompressedPoint))

    def subscriptions(self) -> list[dict]:
        return json.loads(self.subs_path.read_text()) if self.subs_path.exists() else []

    def _write(self, subs: list[dict]) -> None:
        tmp = self.subs_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(subs, indent=2))
        os.replace(tmp, self.subs_path)

    def add(self, sub: dict) -> None:
        with self._lock:
            self._write([s for s in self.subscriptions() if s["endpoint"] != sub["endpoint"]] + [sub])

    def remove(self, endpoint: str) -> None:
        with self._lock:
            self._write([s for s in self.subscriptions() if s["endpoint"] != endpoint])

    def send(self, sub: dict, payload: dict) -> None:
        """Raises on failure. A subscription the push service reports gone (404/410) is removed."""
        try:
            webpush(sub, json.dumps(payload), vapid_private_key=str(self.key_path),
                    vapid_claims={"sub": self.subject}, ttl=86400, timeout=20)
        except WebPushException as e:
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                self.remove(sub["endpoint"])
                raise RuntimeError(f"subscription gone ({status}), removed") from e
            raise
