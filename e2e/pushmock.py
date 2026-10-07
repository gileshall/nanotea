"""A Web Push service of our own: an https endpoint the service pushes to, which decrypts each push (RFC 8291)
and checks its VAPID signature (RFC 8292), as a browser's push service and the browser would between them."""

import base64
import json
import os
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import http_ece
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_vapid(header: str, audience: str) -> dict:
    """The claims of an `Authorization: vapid t=..., k=...` header, once its ES256 signature checks out against k.
    Raises ValueError saying what is wrong."""
    if not header.startswith("vapid "):
        raise ValueError(f"Authorization is not vapid: {header[:40]!r}")
    parts = dict(p.strip().split("=", 1) for p in header[6:].split(","))
    token, public = parts["t"], unb64url(parts["k"])
    head, body, sig = token.split(".")
    if json.loads(unb64url(head)).get("alg") != "ES256":
        raise ValueError("the VAPID JWT is not ES256")
    raw = unb64url(sig)
    if len(raw) != 64:
        raise ValueError(f"an ES256 signature is 64 bytes, not {len(raw)}")
    key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    try:
        key.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        raise ValueError("the VAPID signature does not verify") from None
    claims = json.loads(unb64url(body))
    if claims.get("aud") != audience:
        raise ValueError(f"VAPID aud is {claims.get('aud')!r}, not {audience!r}")
    if not claims.get("exp", 0) > time.time():
        raise ValueError("the VAPID JWT has expired")
    return {**claims, "k": parts["k"]}


class PushService:
    """Listens on host:port over https; endpoint() is a subscription for a browser, as pushManager.subscribe
    returns it. Each push is kept, decrypted, in received."""

    def __init__(self, host: str, port: int, cert: str, key: str, endpoint_host: str):
        self.received: list[dict] = []
        self.errors: list[str] = []
        self._cond = threading.Condition()
        self._subs: dict[str, tuple[ec.EllipticCurvePrivateKey, bytes]] = {}
        self.origin = f"https://{endpoint_host}:{port}"
        service = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                name = self.path.rsplit("/", 1)[-1]
                try:
                    if name not in service._subs:
                        raise ValueError(f"no subscription {name!r}")
                    private, auth = service._subs[name]
                    if self.headers.get("Content-Encoding") != "aes128gcm":
                        raise ValueError(f"Content-Encoding is {self.headers.get('Content-Encoding')!r}")
                    claims = verify_vapid(self.headers.get("Authorization", ""), service.origin)
                    plain = http_ece.decrypt(body, private_key=private, auth_secret=auth, version="aes128gcm")
                    got = {"sub": name, "payload": json.loads(plain), "vapid": claims,
                           "ttl": self.headers.get("TTL"), "at": time.time()}
                except Exception as err:  # the sender gets a 400; the run reports it
                    with service._cond:
                        service.errors.append(f"{type(err).__name__}: {err}")
                        service._cond.notify_all()
                    self.send_response(400)
                    self.end_headers()
                    return
                with service._cond:
                    service.received.append(got)
                    service._cond.notify_all()
                self.send_response(201)
                self.send_header("Location", f"{service.origin}/m/{len(service.received)}")
                self.end_headers()

        self.server = ThreadingHTTPServer((host, port), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def endpoint(self, name: str) -> dict:
        private = ec.generate_private_key(ec.SECP256R1())
        auth = os.urandom(16)
        self._subs[name] = (private, auth)
        public = private.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        return {"endpoint": f"{self.origin}/push/{name}", "expirationTime": None,
                "keys": {"p256dh": b64url(public), "auth": b64url(auth)}}

    def wait(self, test, timeout_s: float, what: str) -> dict:
        """The first push received that test accepts. A push that failed to decrypt or verify fails at once."""
        deadline = time.time() + timeout_s
        with self._cond:
            while True:
                if self.errors:
                    raise AssertionError(f"a push failed its checks: {self.errors}")
                if hit := next((r for r in self.received if test(r)), None):
                    return hit
                left = deadline - time.time()
                if left <= 0:
                    raise AssertionError(f"no push {what} within {timeout_s} s; got {len(self.received)}")
                self._cond.wait(left)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
