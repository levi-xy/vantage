"""First-start files for the standalone compose install (``deploy/compose.standalone.yml``).

Run by that file's one-shot ``init`` service, as root, with no network::

    python -m app.cli standalone-init

It does what ``deploy/setup.sh`` does for the bundle install, but in named volumes
instead of host folders, so a stack manager (Portainer, Dockge) needs no files on the
host. Each volume is mounted under ``/vantage-init`` and holds what one or two services
read:

* ``key/master_key``: the master key (backend only), uid 10001, mode 0400.
* ``token/prometheus_token``: the bearer token (backend and Prometheus), uid 10001,
  gid 65534, mode 0440.
* ``tls/tls_cert`` and ``tls/tls_key``: a self-signed certificate for the frontend; the
  key uid 101, mode 0400, the certificate 0644.
* ``prometheus/prometheus.yml``: the fixed bootstrap config (Contract 4), 0644.

Owners and modes are the ones setup.sh gives the same files when it runs as root.

An existing file is never replaced, only given its owner and mode again (so a key
restored or a certificate copied into a volume ends up readable by the right container
and nobody else). New files are written to a temporary name and hard-linked into place,
so a crash never leaves a half-written key and a file that appeared meanwhile is never
overwritten. A missing master key is refused, as setup.sh refuses it, when data from an
earlier install may exist, unless ``VANTAGE_FORCE_NEW_KEY=true``. Nothing secret is ever
printed: not the key, the token or the private key.
"""

from __future__ import annotations

import base64
import datetime
import ipaddress
import os
import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_ROOT = Path("/vantage-init")
BACKEND_UID = 10001  # backend container user (backend/Dockerfile)
PROMETHEUS_GID = 65534  # prometheus container user/group (nobody)
FRONTEND_UID = 101  # frontend container user (nginx-unprivileged image)
CERT_DAYS = 825  # as setup.sh

# A byte-for-byte copy of deploy/prometheus/prometheus.yml (the backend image can't read
# deploy/); tests/test_standalone_compose.py fails when the two differ.
PROMETHEUS_YML = """\
# Fixed bootstrap config (AGENTS.md Contract 4). Never add anything
# device-specific here: devices reach Prometheus through /sd/targets.

global:
  scrape_interval: 30s
  evaluation_interval: 30s

scrape_configs:
  # Exporter targets for every enabled instance, served by the backend.
  # Each target carries its own __metrics_path__ and __scheme__ labels.
  # Generic per-scrape limits (not device-specific; AGENTS.md Contract 4, approved
  # 2026-11-01): a misbehaving or hostile exporter can't flood Prometheus. A scrape over
  # a limit fails as a whole and its target shows as down.
  - job_name: vantage-targets
    sample_limit: 50000
    label_limit: 64
    label_value_length_limit: 2048
    body_size_limit: 16MB
    http_sd_configs:
      - url: http://backend:8000/sd/targets
        refresh_interval: 60s
        authorization:
          type: Bearer
          credentials_file: /run/secrets/prometheus_token

  # Metrics polled by the backend (API/SNMP sources) plus its own health.
  - job_name: vantage-backend
    static_configs:
      - targets: ["backend:8000"]
    authorization:
      type: Bearer
      credentials_file: /run/secrets/prometheus_token

  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
"""

_NAME_CHARS = re.compile(r"[A-Za-z0-9.:*-]+")


class InitError(Exception):
    """A reason to stop before writing anything. The message never holds a secret."""


@dataclass(frozen=True)
class _File:
    path: Path
    label: str
    uid: int | None  # None = keep the owner (root, the creator)
    gid: int | None
    mode: int  # as root
    secret: bool  # mode 0644 without root, like setup.sh


@dataclass(frozen=True)
class Layout:
    root: Path = DEFAULT_ROOT
    # The backend's data volume, read-only, only to see whether it holds data. At /data,
    # the backend's own path: a volume's first mount takes the owner of the image's
    # folder there (vantage, from backend/Dockerfile), so mounting it anywhere else
    # first would leave it owned by root and unwritable for the backend.
    data: Path = Path("/data")

    @property
    def master_key(self) -> _File:
        return _File(self.root / "key/master_key", "master key", BACKEND_UID, None, 0o400, True)

    @property
    def token(self) -> _File:
        return _File(
            self.root / "token/prometheus_token",
            "Prometheus token",
            BACKEND_UID,
            PROMETHEUS_GID,
            0o440,
            True,
        )

    @property
    def tls_cert(self) -> _File:
        return _File(self.root / "tls/tls_cert", "TLS certificate", None, None, 0o644, False)

    @property
    def tls_key(self) -> _File:
        return _File(self.root / "tls/tls_key", "TLS key", FRONTEND_UID, None, 0o400, True)

    @property
    def prometheus_yml(self) -> _File:
        return _File(
            self.root / "prometheus/prometheus.yml", "prometheus.yml", None, None, 0o644, False
        )


def _exists(f: _File) -> bool:
    """True for a file already there. Anything but a regular file (a symlink, a folder)
    is refused: init runs as root and must not be pointed at another path."""
    if f.path.is_symlink() or (f.path.exists() and not f.path.is_file()):
        raise InitError(f"{f.label}: {f.path} is not a regular file; remove it and start again")
    return f.path.exists()


def _data_may_exist(layout: Layout, env: Mapping[str, str]) -> bool:
    """Signs that an earlier install stored data encrypted with another key (setup.sh's
    test): the token, a non-empty data volume, or an external database."""
    if layout.token.path.exists():
        return True
    try:
        if any(layout.data.iterdir()):
            return True
    except FileNotFoundError:
        pass
    url = env.get("VANTAGE_DATABASE_URL", "").strip()
    return bool(url) and not url.startswith("sqlite")  # Postgres: can't tell, assume data


def _write_new(f: _File, content: bytes) -> bool:
    """Write ``content`` to a new file. False when the file appeared meanwhile (kept)."""
    tmp = f.path.with_name(f".{f.path.name}.tmp")
    tmp.unlink(missing_ok=True)  # left by a crash on an earlier start
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        try:
            os.link(tmp, f.path)  # unlike rename, never replaces an existing file
        except FileExistsError:
            return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def _secure(f: _File, as_root: bool) -> None:
    """Give the file its owner and mode (chmod first: root keeps it until the chown)."""
    if as_root:
        os.chmod(f.path, f.mode, follow_symlinks=False)
        if f.uid is not None or f.gid is not None:
            uid = -1 if f.uid is None else f.uid
            gid = -1 if f.gid is None else f.gid
            os.chown(f.path, uid, gid, follow_symlinks=False)
    else:
        # Not root: can't chown to the container users, so readable through the mode,
        # as setup.sh does without root.
        os.chmod(f.path, 0o644, follow_symlinks=False)


def _san_entries(names: str) -> list[x509.GeneralName]:
    entries: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    for name in re.split(r"[,\s]+", names.strip()):
        if not name:
            continue
        if not _NAME_CHARS.fullmatch(name):
            raise InitError(f"VANTAGE_TLS_NAMES: {name!r} is not a host name or IP address")
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            entries.append(x509.DNSName(name))
    return entries


def self_signed_pair(names: str) -> tuple[bytes, bytes]:
    """A self-signed certificate and its unencrypted key (PEM), as setup.sh makes them."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "vantage"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Vantage self-signed"),
        ]
    )
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=CERT_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.SubjectAlternativeName(_san_entries(names)), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def run(
    out: Callable[[str], None],
    *,
    layout: Layout | None = None,
    env: Mapping[str, str] | None = None,
    as_root: bool | None = None,
) -> None:
    """Create what's missing and fix owners and modes. Raises ``InitError`` before writing
    anything when a precondition fails."""
    layout = layout or Layout()
    env = os.environ if env is None else env
    as_root = os.geteuid() == 0 if as_root is None else as_root
    files = [
        layout.master_key,
        layout.token,
        layout.tls_cert,
        layout.tls_key,
        layout.prometheus_yml,
    ]
    for f in files:
        if not f.path.parent.is_dir():
            raise InitError(f"{f.path.parent} is not mounted")
    have = {f.path: _exists(f) for f in files}

    # Every check first, so a refusal leaves the volumes as they were.
    if have[layout.tls_cert.path] != have[layout.tls_key.path]:
        raise InitError(
            "only one of tls_cert and tls_key exists in the TLS volume. Add the missing one "
            "(your certificate and its key), or remove the other to have a self-signed pair "
            "made."
        )
    names = env.get("VANTAGE_TLS_NAMES", "")
    _san_entries(names)  # a bad name stops here, before anything is written
    force = env.get("VANTAGE_FORCE_NEW_KEY", "").strip().lower() == "true"
    if force:
        out(
            "Warning: VANTAGE_FORCE_NEW_KEY=true. Remove it after this start, or a lost key "
            "volume next to existing data is no longer caught."
        )
    if not have[layout.master_key.path] and _data_may_exist(layout, env) and not force:
        raise InitError(
            "the master key is missing, but data from an earlier install may exist (the "
            "Prometheus token, the backend data volume, or an external database). Stored "
            "device credentials can only be read with the original key: copy it from your "
            "backup into the key volume (docs/OPERATIONS.md). If that data is gone or you "
            "want to start over, set VANTAGE_FORCE_NEW_KEY=true for one start."
        )

    created_key = False
    new: dict[Path, bytes] = {}
    if not have[layout.master_key.path]:
        new[layout.master_key.path] = base64.b64encode(secrets.token_bytes(32)) + b"\n"
    if not have[layout.token.path]:
        new[layout.token.path] = secrets.token_hex(32).encode() + b"\n"
    if not have[layout.tls_cert.path]:
        cert, key = self_signed_pair(names)
        # The key first: a crash between the two leaves only the key, which the next
        # start reports instead of serving a certificate without it.
        new[layout.tls_key.path], new[layout.tls_cert.path] = key, cert
    if not have[layout.prometheus_yml.path]:
        new[layout.prometheus_yml.path] = PROMETHEUS_YML.encode()

    write_order = [
        layout.master_key,
        layout.token,
        layout.tls_key,
        layout.tls_cert,
        layout.prometheus_yml,
    ]
    for f in write_order:
        if f.path in new and _write_new(f, new[f.path]):
            out(f"Created the {f.label}")
            created_key = created_key or f == layout.master_key
        else:
            out(f"Keeping the existing {f.label}")
        _secure(f, as_root)

    if created_key:
        out(
            "IMPORTANT: back up the master key now, somewhere other than this server "
            "(docs/OPERATIONS.md, 'Back up the master key'). Without it, the device "
            "credentials Vantage stores can't be decrypted."
        )
