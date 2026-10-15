"""Minimal Web Push sender: RFC 8291 (aes128gcm message encryption) + RFC 8292 (VAPID)."""
import base64
import hashlib
import hmac
import json
import os
import struct
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_X962 = serialization.Encoding.X962
_UNCOMP = serialization.PublicFormat.UncompressedPoint


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def b64u_dec(s):
    s = "".join(s.split())
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _hmac(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


def public_bytes(priv):
    return priv.public_key().public_bytes(_X962, _UNCOMP)


def encrypt(plaintext, ua_public_b64, auth_b64, as_private=None, salt=None, rs=4096):
    """Encrypt one push message body (single record) per RFC 8291."""
    ua_pub = b64u_dec(ua_public_b64)
    auth = b64u_dec(auth_b64)
    if as_private is None:
        as_private = ec.generate_private_key(ec.SECP256R1())
    as_pub = public_bytes(as_private)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    ecdh = as_private.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(auth, ecdh)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_pub + as_pub + b"\x01")
    salt = salt if salt is not None else os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    if len(plaintext) + 17 + 86 > rs:
        raise ValueError("push payload too large")
    ct = AESGCM(cek).encrypt(nonce, plaintext + b"\x02", None)
    return salt + struct.pack(">I", rs) + bytes([len(as_pub)]) + as_pub + ct


class Vapid:
    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            with open(path) as f:
                d = json.load(f)
            self.priv = ec.derive_private_key(int.from_bytes(b64u_dec(d["private"]), "big"), ec.SECP256R1())
        else:
            self.priv = ec.generate_private_key(ec.SECP256R1())
            raw = self.priv.private_numbers().private_value.to_bytes(32, "big")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"private": b64u(raw)}, f)
        self.public_b64 = b64u(public_bytes(self.priv))

    def headers(self, endpoint, sub, exp=None):
        u = urlparse(endpoint)
        aud = "%s://%s" % (u.scheme, u.netloc)
        h = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        c = b64u(json.dumps({"aud": aud, "exp": int(exp or time.time() + 12 * 3600), "sub": sub},
                            separators=(",", ":")).encode())
        der = self.priv.sign(("%s.%s" % (h, c)).encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        return {"Authorization": "vapid t=%s.%s.%s, k=%s" % (h, c, sig, self.public_b64)}


async def send(http, vapid, sub, payload, subject, ttl=6 * 3600, urgency="high"):
    """POST one notification. Returns the HTTP status (201 = accepted, 404/410 = subscription gone)."""
    body = encrypt(json.dumps(payload, ensure_ascii=False).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    headers = vapid.headers(sub["endpoint"], subject)
    headers.update({"TTL": str(ttl), "Urgency": urgency, "Content-Encoding": "aes128gcm",
                    "Content-Type": "application/octet-stream"})
    async with http.post(sub["endpoint"], data=body, headers=headers) as r:
        text = await r.text()
        return r.status, text[:300]
