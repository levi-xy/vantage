"""deploy/setup.sh (M8.5, M8.1): creates missing secrets, HTTPS certificate and .env,
never overwrites the key or a certificate, never prints a secret.
Runs the real script with /bin/sh against a stubbed docker."""

from __future__ import annotations

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"

pytestmark = pytest.mark.skipif(
    shutil.which("openssl") is None or not Path("/bin/sh").exists(),
    reason="needs /bin/sh and openssl",
)

DOCKER_STUB = """#!/bin/sh
echo "docker $*" >> "$STUB_LOG"
[ "$1" = info ] && [ -n "${STUB_DOCKER_DOWN:-}" ] && exit 1
case "$1 $2" in
  "volume inspect") [ -n "${STUB_VOLUME_EXISTS:-}" ] && exit 0 || exit 1 ;;
esac
exit 0
"""


@pytest.fixture
def deploy(tmp_path: Path) -> Path:
    d = tmp_path / "deploy"
    d.mkdir()
    for name in ("setup.sh", "docker-compose.yml", ".env.example"):
        shutil.copy2(DEPLOY / name, d / name)
    stubs = tmp_path / "bin"
    stubs.mkdir()
    (stubs / "docker").write_text(DOCKER_STUB)
    (stubs / "docker").chmod(0o755)
    return d


def run(
    deploy: Path, *args: str, volume_exists: bool = False, docker_down: bool = False
) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": f"{deploy.parent / 'bin'}:/usr/bin:/bin",
        "STUB_LOG": str(deploy.parent / "stub.log"),
    }
    if volume_exists:
        env["STUB_VOLUME_EXISTS"] = "1"
    if docker_down:
        env["STUB_DOCKER_DOWN"] = "1"
    return subprocess.run(  # noqa: S603 -- fixed argv, test-only script under deploy/
        ["/bin/sh", str(deploy / "setup.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def stub_log(deploy: Path) -> str:
    log = deploy.parent / "stub.log"
    return log.read_text() if log.exists() else ""


def assert_no_secret_in_output(deploy: Path, result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    for name in ("master.key", "prometheus_token"):
        value = (deploy / "secrets" / name).read_text().strip()
        assert value and value not in output
    key = deploy / "certs" / "tls.key"
    if key.exists():
        for line in key.read_text().splitlines():
            assert "PRIVATE KEY" in line or line not in output


def test_fresh_no_start_creates_files_only(deploy: Path) -> None:
    result = run(deploy, "--no-start")
    assert result.returncode == 0, result.stderr

    key = (deploy / "secrets" / "master.key").read_text().strip()
    assert len(base64.b64decode(key, validate=True)) == 32
    token = (deploy / "secrets" / "prometheus_token").read_text().strip()
    assert len(token) == 64 and int(token, 16) >= 0
    assert (deploy / ".env").read_text() == (deploy / ".env.example").read_text()

    # Nobody but the owner can enter secrets/; as root the files belong to the containers.
    assert stat.S_IMODE((deploy / "secrets").stat().st_mode) == 0o700
    if os.geteuid() == 0:
        key_stat = (deploy / "secrets" / "master.key").stat()
        assert (key_stat.st_uid, stat.S_IMODE(key_stat.st_mode)) == (10001, 0o400)

    assert "docker compose up" not in stub_log(deploy)
    assert "back up deploy/secrets/master.key" in result.stdout
    assert_no_secret_in_output(deploy, result)


def test_rerun_keeps_key_and_token(deploy: Path) -> None:
    assert run(deploy, "--no-start").returncode == 0
    key = (deploy / "secrets" / "master.key").read_bytes()
    token = (deploy / "secrets" / "prometheus_token").read_bytes()

    result = run(deploy, "--no-start")
    assert result.returncode == 0, result.stderr
    assert (deploy / "secrets" / "master.key").read_bytes() == key
    assert (deploy / "secrets" / "prometheus_token").read_bytes() == token
    assert "Keeping existing secrets/master.key" in result.stdout
    assert "back up" not in result.stdout  # reminder only on the run that made the key


def test_full_run_starts_stack_and_prints_url(deploy: Path) -> None:
    result = run(deploy)
    assert result.returncode == 0, result.stderr
    log = stub_log(deploy)
    assert "docker compose up -d" in log
    assert "exec -T frontend wget" in log and "/healthz" in log
    assert "Vantage is running: https://127.0.0.1:8443" in result.stdout
    assert "self-signed" in result.stdout
    assert_no_secret_in_output(deploy, result)


def test_old_env_port_name_still_used(deploy: Path) -> None:
    (deploy / ".env").write_text("VANTAGE_HTTP_PORT=8080\n")
    result = run(deploy)
    assert result.returncode == 0, result.stderr
    assert "Vantage is running: https://127.0.0.1:8080" in result.stdout


def test_plain_http_opt_out_prints_http_url(deploy: Path) -> None:
    (deploy / ".env").write_text("VANTAGE_HTTPS=false\n")
    result = run(deploy)
    assert result.returncode == 0, result.stderr
    assert "Vantage is running: http://127.0.0.1:8443" in result.stdout
    assert "self-signed" not in result.stdout.split("Vantage is running")[1]
    assert "VANTAGE_PUBLIC_HOSTS is empty" in result.stdout
    (deploy / ".env").write_text("VANTAGE_HTTPS=false\nVANTAGE_PUBLIC_HOSTS=192.0.2.10\n")
    assert "VANTAGE_PUBLIC_HOSTS" not in run(deploy).stdout


def openssl_text(cert: Path) -> str:
    return subprocess.run(  # noqa: S603 -- fixed argv
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_generates_self_signed_cert_for_configured_names(deploy: Path) -> None:
    (deploy / ".env").write_text(
        "VANTAGE_BIND_ADDRESS=192.0.2.10\nVANTAGE_TLS_NAMES=vantage.example.internal, 10.0.0.5\n"
    )
    result = run(deploy, "--no-start")
    assert result.returncode == 0, result.stderr

    certs = deploy / "certs"
    assert stat.S_IMODE(certs.stat().st_mode) == 0o700
    text = openssl_text(certs / "tls.crt")
    for san in (
        "DNS:localhost",
        "IP Address:127.0.0.1",
        "IP Address:192.0.2.10",
        "DNS:vantage.example.internal",
        "IP Address:10.0.0.5",
    ):
        assert san in text
    assert "CA:FALSE" in text
    assert "PRIVATE KEY" in (certs / "tls.key").read_text()
    assert_no_secret_in_output(deploy, result)
    if os.geteuid() == 0:
        key_stat = (certs / "tls.key").stat()
        assert (key_stat.st_uid, stat.S_IMODE(key_stat.st_mode)) == (101, 0o400)


def test_keeps_admin_cert_and_refuses_half_pair(deploy: Path) -> None:
    certs = deploy / "certs"
    certs.mkdir()
    (certs / "tls.crt").write_text("example-admin-cert\n")
    result = run(deploy, "--no-start")
    assert result.returncode != 0
    assert "only one of" in result.stderr
    assert not (certs / "tls.key").exists()

    (certs / "tls.key").write_text("example-admin-key\n")
    result = run(deploy, "--no-start")
    assert result.returncode == 0, result.stderr
    assert "Keeping existing certs/tls.crt" in result.stdout
    assert (certs / "tls.crt").read_text() == "example-admin-cert\n"
    assert (certs / "tls.key").read_text() == "example-admin-key\n"


def test_rejects_malformed_tls_name(deploy: Path) -> None:
    (deploy / ".env").write_text("VANTAGE_TLS_NAMES=bad]name\n")
    result = run(deploy, "--no-start")
    assert result.returncode != 0
    assert "VANTAGE_TLS_NAMES" in result.stderr
    assert not (deploy / "certs" / "tls.crt").exists()


def test_refuses_new_key_when_data_may_exist(deploy: Path) -> None:
    result = run(deploy, "--no-start", volume_exists=True)
    assert result.returncode != 0
    assert "--force-new-key" in result.stderr
    assert not (deploy / "secrets" / "master.key").exists()

    result = run(deploy, "--no-start", "--force-new-key", volume_exists=True)
    assert result.returncode == 0, result.stderr
    assert (deploy / "secrets" / "master.key").exists()


def test_never_overwrites_key_even_with_force(deploy: Path) -> None:
    assert run(deploy, "--no-start").returncode == 0
    key = (deploy / "secrets" / "master.key").read_bytes()
    assert run(deploy, "--no-start", "--force-new-key").returncode == 0
    assert (deploy / "secrets" / "master.key").read_bytes() == key


def test_backup_reminder_even_when_docker_fails(deploy: Path) -> None:
    result = run(deploy, docker_down=True)
    assert result.returncode != 0
    assert "can't reach the Docker daemon" in result.stderr
    assert "back up deploy/secrets/master.key" in result.stdout
    assert "docker compose up" not in stub_log(deploy)
    assert_no_secret_in_output(deploy, result)
