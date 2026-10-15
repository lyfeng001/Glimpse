import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hub"))
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402

import webpush  # noqa: E402

# RFC 8291 Section 5 / Appendix A
AS_PRIV = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
UA_PUB = "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
SALT = "DGv6ra1nlYgDCS1FRnbzlw"
PLAIN = b"When I grow up, I want to be a watermelon"
EXPECTED = ("DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIg"
            "Dll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")


def test_rfc8291_vector():
    priv = ec.derive_private_key(int.from_bytes(webpush.b64u_dec(AS_PRIV), "big"), ec.SECP256R1())
    out = webpush.encrypt(PLAIN, UA_PUB, AUTH, as_private=priv, salt=webpush.b64u_dec(SALT))
    assert webpush.b64u(out) == EXPECTED


def test_vapid_jwt_verifies(tmp_path):
    v = webpush.Vapid(str(tmp_path / "vapid.json"))
    h = v.headers("https://web.push.apple.com/abc", "mailto:a@example.com")["Authorization"]
    token = h.split("t=")[1].split(",")[0]
    k = h.split("k=")[1]
    assert k == v.public_b64
    head, claims, sig = token.split(".")
    c = json.loads(webpush.b64u_dec(claims))
    assert c["aud"] == "https://web.push.apple.com" and c["sub"] == "mailto:a@example.com"
    raw = webpush.b64u_dec(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64u_dec(k))
    pub.verify(der, ("%s.%s" % (head, claims)).encode(), ec.ECDSA(hashes.SHA256()))
    # reload keeps the same key
    assert webpush.Vapid(str(tmp_path / "vapid.json")).public_b64 == v.public_b64
