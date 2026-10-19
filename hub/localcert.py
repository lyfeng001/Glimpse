"""A certificate for the hub's 127.0.0.1 listener (`gli local-cert`), so a Glimpse window installed from
https://127.0.0.1:8443 opens without "此服务器无法证明它是127.0.0.1" (the Tailscale certificate only names the
.ts.net host).

    gli local-cert          create it and trust it in this Mac's login keychain (macOS asks for your password)
    gli local-cert remove   untrust and delete it

What gets trusted is a one-off CA that can only vouch for 127.0.0.1, ::1 and localhost (critical name
constraints, path length 0). Its private key exists only in memory while it signs the hub's certificate and is
then dropped, so nothing, not even this program, can sign anything else with it. Files (data/certs, 0600 / 0700):
local.crt (the hub's certificate + the CA), local.key (the hub's key), local-ca.crt (the CA, for trusting)."""
import datetime
import ipaddress
import os
import socket
import subprocess
import sys

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DAYS = 820                    # Apple / Chrome accept at most 825 days for a TLS server certificate
LOOPBACK = [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address("::1")),
            x509.DNSName("localhost")]


def paths(data):
    d = os.path.join(data, "certs")
    return os.path.join(d, "local.crt"), os.path.join(d, "local.key"), os.path.join(d, "local-ca.crt")


def make(now=None):
    """(server cert PEM incl. the CA, server key PEM, CA cert PEM). The CA key is never returned."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    start, end = now - datetime.timedelta(minutes=5), now + datetime.timedelta(days=DAYS)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    host = socket.gethostname().split(".")[0][:32] or "mac"
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Glimpse 127.0.0.1 (%s, %s)" % (host, now.strftime("%Y-%m-%d"))),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Glimpse local")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(start).not_valid_after(end)
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.NameConstraints(permitted_subtrees=[
              x509.IPAddress(ipaddress.ip_network("127.0.0.1/32")), x509.IPAddress(ipaddress.ip_network("::1/128")),
              x509.DNSName("localhost")], excluded_subtrees=None), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
            .issuer_name(ca_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.SubjectAlternativeName(LOOPBACK), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    del ca_key                                       # nothing else can ever be signed by this CA
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM)
    kpem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem(leaf) + pem(ca), kpem, pem(ca)


def write(data, now=None):
    crt, key, ca = paths(data)
    os.makedirs(os.path.dirname(crt), mode=0o700, exist_ok=True)
    c, k, a = make(now)
    for p, b, mode in ((key, k, 0o600), (crt, c, 0o644), (ca, a, 0o644)):
        tmp = p + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(b)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    return crt, key, ca


def fingerprint(path):
    with open(path, "rb") as f:
        return x509.load_pem_x509_certificate(f.read()).fingerprint(hashes.SHA1()).hex().upper()


def login_keychain():
    return os.path.expanduser("~/Library/Keychains/login.keychain-db")


def untrust(ca):
    """Remove an earlier CA of ours from the login keychain (its trust setting and the certificate)."""
    if not os.path.isfile(ca):
        return
    subprocess.run(["security", "remove-trusted-cert", ca], capture_output=True)
    subprocess.run(["security", "delete-certificate", "-Z", fingerprint(ca), login_keychain()], capture_output=True)


def main(argv):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import config as C
    crt, key, ca = paths(C.DATA)
    if argv[:1] == ["remove"]:
        if sys.platform == "darwin":
            untrust(ca)
        for p in (crt, key, ca):
            if os.path.exists(p):
                os.remove(p)
        print("removed. Restart the hub (gli hub): 127.0.0.1 gets the Tailscale certificate again")
        return
    if argv:
        raise SystemExit("usage: gli local-cert [remove]")
    if sys.platform == "darwin":
        untrust(ca)                                  # an older one, if any
    write(C.DATA)
    print("created %s (127.0.0.1 / ::1 / localhost, valid %d days)" % (crt, DAYS))
    if sys.platform != "darwin":
        print("trust %s in your system / browser certificate store, then restart the hub" % ca)
        return
    print("trusting it in your login keychain; macOS asks for your password ...")
    r = subprocess.run(["security", "add-trusted-cert", "-r", "trustRoot", "-p", "ssl", "-k", login_keychain(), ca])
    if r.returncode:
        raise SystemExit("not trusted (security exited %d); run `gli local-cert` again to retry" % r.returncode)
    print("trusted. Now restart the hub, then reopen the Glimpse window")


if __name__ == "__main__":
    main(sys.argv[1:])
