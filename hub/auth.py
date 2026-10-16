"""Login for the phone app: a password, one-time pairing links, and per-device sessions.

data/auth.json (0600) holds only hashes:
  {"password": {"salt", "hash", "n", "r", "p"} | null,
   "devices": [{"id", "name", "hash", "created", "last_seen", "ip"}],
   "pairs": [{"hash", "expires"}]}
A device keeps a random 256-bit session token (cookie + localStorage); the hub stores its sha256 only, so a
leaked auth.json does not let anyone in. Pairing links are single use and expire after PAIR_TTL seconds.
"""
import hashlib
import hmac
import json
import os
import secrets
import time

PAIR_TTL = 600
MAX_DEVICES = 50
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=n, r=r, p=p, dklen=32).hex()


class Auth:
    def __init__(self, path, write_secret):
        self.path = path
        self.write_secret = write_secret
        self.fail = {}            # ip -> (count, until)
        self._mtime = None
        self.load()
        try:
            self._mtime = os.stat(path).st_mtime_ns
        except OSError:
            self._mtime = None

    # --- storage
    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        self.password = d.get("password") if isinstance(d.get("password"), dict) else None
        self.devices = [x for x in d.get("devices") or [] if isinstance(x, dict) and x.get("hash")]
        self.pairs = [x for x in d.get("pairs") or [] if isinstance(x, dict) and x.get("expires", 0) > time.time()]

    def save(self):
        self.write_secret(self.path, json.dumps({"password": self.password, "devices": self.devices,
                                                 "pairs": self.pairs}, indent=1))
        try:
            self._mtime = os.stat(self.path).st_mtime_ns
        except OSError:
            self._mtime = None

    def refresh(self):
        """Pick up changes another process made (`./gli url` / `./gli password` while the hub runs)."""
        try:
            m = os.stat(self.path).st_mtime_ns
        except OSError:
            m = None
        if m != getattr(self, "_mtime", None):
            self.load()
            self._mtime = m

    # --- password
    def has_password(self):
        return bool(self.password)

    def set_password(self, pw):
        self.refresh()
        pw = str(pw or "")
        if len(pw) < 8:
            raise ValueError("密码至少 8 位")
        salt = secrets.token_hex(16)
        self.password = dict(SCRYPT, salt=salt, hash=_scrypt(pw, salt, **SCRYPT))
        self.save()

    def check_password(self, pw):
        self.refresh()
        p = self.password
        if not p or not isinstance(pw, str) or not pw:
            return False
        try:
            h = _scrypt(pw, p["salt"], p["n"], p["r"], p["p"])
        except (ValueError, KeyError, TypeError):
            return False
        return hmac.compare_digest(h, p["hash"])

    # --- pairing links
    def new_pair(self):
        self.refresh()
        code = secrets.token_urlsafe(18)
        now = time.time()
        self.pairs = [x for x in self.pairs if x["expires"] > now][-20:]
        self.pairs.append({"hash": _sha(code), "expires": now + PAIR_TTL})
        self.save()
        return code

    def use_pair(self, code):
        if not isinstance(code, str) or not code:
            return False
        self.refresh()
        h, now = _sha(code), time.time()
        for x in self.pairs:
            if x["expires"] > now and hmac.compare_digest(x["hash"], h):
                self.pairs.remove(x)
                self.save()
                return True
        return False

    # --- device sessions
    def new_device(self, name, ip):
        self.refresh()
        tok = secrets.token_urlsafe(32)
        now = time.time()
        dev = {"id": secrets.token_hex(6), "name": str(name or "设备")[:60], "hash": _sha(tok),
               "created": now, "last_seen": now, "ip": str(ip or "")[:64]}
        self.devices = (self.devices + [dev])[-MAX_DEVICES:]
        self.save()
        return tok, dev

    def device_for(self, tok):
        if not isinstance(tok, str) or not tok:
            return None
        self.refresh()          # a device revoked by the CLI stops working at once
        h = _sha(tok)
        for d in self.devices:
            if hmac.compare_digest(d["hash"], h):
                return d
        return None

    def touch(self, dev, ip):
        now = time.time()
        if now - dev.get("last_seen", 0) > 300 or dev.get("ip") != ip:
            dev["last_seen"], dev["ip"] = now, str(ip or "")[:64]
            self.save()

    def revoke(self, dev_id):
        self.refresh()
        n = len(self.devices)
        self.devices = [d for d in self.devices if d["id"] != dev_id]
        if len(self.devices) != n:
            self.save()
            return True
        return False

    def revoke_all(self, keep=None):
        self.refresh()
        self.devices = [d for d in self.devices if keep and d["id"] == keep]
        self.save()

    def public_devices(self, current=None):
        return [{"id": d["id"], "name": d["name"], "created": d["created"], "last_seen": d["last_seen"],
                 "ip": d.get("ip", ""), "current": d["id"] == current} for d in reversed(self.devices)]

    # --- brute force protection: per client, doubling lockout after 5 failures
    def blocked(self, ip):
        c, until = self.fail.get(ip, (0, 0))
        return until > time.time()

    def failed(self, ip):
        c, _ = self.fail.get(ip, (0, 0))
        c += 1
        until = time.time() + (min(3600, 30 * 2 ** (c - 5)) if c >= 5 else 0)
        self.fail[ip] = (c, until)
        if len(self.fail) > 1000:
            self.fail = dict(list(self.fail.items())[-500:])

    def ok(self, ip):
        self.fail.pop(ip, None)
