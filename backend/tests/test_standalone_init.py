"""app.cli standalone-init: first-start files for deploy/compose.standalone.yml."""

from __future__ import annotations

import io
import os
import re
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.cli import Console, main
from app.cli import standalone_init as si
from app.core.crypto import decode_key

VOLUMES = ("key", "token", "tls", "prometheus")


@pytest.fixture
def layout(tmp_path: Path) -> si.Layout:
    for name in VOLUMES:
        (tmp_path / "init" / name).mkdir(parents=True)
    (tmp_path / "data").mkdir()
    return si.Layout(root=tmp_path / "init", data=tmp_path / "data")


def run(
    layout: si.Layout, env: dict[str, str] | None = None, *, as_root: bool = False
) -> list[str]:
    lines: list[str] = []
    si.run(lines.append, layout=layout, env=env or {}, as_root=as_root)
    return lines


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def all_files(layout: si.Layout) -> list[Path]:
    return [
        layout.master_key.path,
        layout.token.path,
        layout.tls_cert.path,
        layout.tls_key.path,
        layout.prometheus_yml.path,
    ]


def test_first_start_creates_every_file_in_the_expected_format(layout: si.Layout) -> None:
    lines = run(layout, {"VANTAGE_TLS_NAMES": "vantage.home.example, 192.0.2.10"})

    assert len(decode_key(layout.master_key.path.read_text(), "test")) == 32
    assert re.fullmatch(r"[0-9a-f]{64}\n", layout.token.path.read_text())
    assert layout.prometheus_yml.path.read_text() == si.PROMETHEUS_YML

    cert = x509.load_pem_x509_certificate(layout.tls_cert.path.read_bytes())
    key = serialization.load_pem_private_key(layout.tls_key.path.read_bytes(), password=None)
    assert isinstance(key, rsa.RSAPrivateKey) and key.key_size == 2048
    assert cert.public_key().public_numbers() == key.public_key().public_numbers()
    sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert {"localhost", "vantage.home.example"} <= set(sans.get_values_for_type(x509.DNSName))
    assert {str(a) for a in sans.get_values_for_type(x509.IPAddress)} == {
        "127.0.0.1",
        "::1",
        "192.0.2.10",
    }
    assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca

    assert "Created the master key" in lines
    assert any(line.startswith("IMPORTANT: back up the master key") for line in lines)
    # No temporary file is left behind.
    assert sorted(p.name for p in layout.root.rglob("*") if p.is_file()) == sorted(
        p.name for p in all_files(layout)
    )


def test_output_never_holds_a_secret(layout: si.Layout) -> None:
    output = "\n".join(run(layout))
    key_b64 = layout.master_key.path.read_text().strip()
    token = layout.token.path.read_text().strip()
    assert key_b64 not in output and token not in output
    assert "PRIVATE KEY" not in output


def test_a_second_start_keeps_every_file(layout: si.Layout) -> None:
    run(layout)
    before = {p: p.read_bytes() for p in all_files(layout)}
    lines = run(layout)
    assert {p: p.read_bytes() for p in all_files(layout)} == before
    assert not any(line.startswith("Created") for line in lines)
    assert not any(line.startswith("IMPORTANT") for line in lines)


def test_as_root_each_file_gets_setup_sh_owner_and_mode(
    layout: si.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    owners: dict[str, tuple[int, int]] = {}

    def fake_chown(path: Path, uid: int, gid: int, *, follow_symlinks: bool = True) -> None:
        assert follow_symlinks is False
        owners[Path(path).name] = (uid, gid)

    monkeypatch.setattr(os, "chown", fake_chown)
    run(layout, as_root=True)
    assert owners == {
        "master_key": (10001, -1),
        "prometheus_token": (10001, 65534),
        "tls_key": (101, -1),
    }
    assert mode(layout.master_key.path) == 0o400
    assert mode(layout.token.path) == 0o440
    assert mode(layout.tls_key.path) == 0o400
    assert mode(layout.tls_cert.path) == 0o644
    assert mode(layout.prometheus_yml.path) == 0o644


def test_without_root_secret_files_are_readable_through_their_mode(layout: si.Layout) -> None:
    run(layout)
    for path in all_files(layout):
        assert mode(path) == 0o644


def test_existing_files_get_their_mode_again(
    layout: si.Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A key restored or a certificate copied in is fixed up, never replaced."""
    monkeypatch.setattr(os, "chown", lambda *a, **k: None)
    run(layout, as_root=True)
    own_cert = layout.tls_cert.path.read_bytes()
    layout.tls_key.path.chmod(0o644)
    run(layout, as_root=True)
    assert mode(layout.tls_key.path) == 0o400
    assert layout.tls_cert.path.read_bytes() == own_cert


def test_only_half_a_tls_pair_is_refused_before_anything_is_written(layout: si.Layout) -> None:
    layout.tls_cert.path.write_text("my certificate")
    with pytest.raises(si.InitError, match="only one of tls_cert and tls_key"):
        run(layout)
    assert not layout.master_key.path.exists()
    assert not layout.token.path.exists()


@pytest.mark.parametrize("sign", ["token", "data", "postgres"])
def test_a_missing_key_next_to_old_data_is_refused(layout: si.Layout, sign: str) -> None:
    env: dict[str, str] = {}
    if sign == "token":
        layout.token.path.write_text("0" * 64 + "\n")
    elif sign == "data":
        (layout.data / "vantage.db").write_bytes(b"")
    else:
        env["VANTAGE_DATABASE_URL"] = "postgresql+psycopg://vantage@db/vantage"
    with pytest.raises(si.InitError, match="master key is missing"):
        run(layout, env)
    assert not layout.master_key.path.exists()
    assert not layout.tls_cert.path.exists()

    env["VANTAGE_FORCE_NEW_KEY"] = "true"
    run(layout, env)
    assert layout.master_key.path.exists()


def test_sqlite_url_alone_is_not_old_data(layout: si.Layout) -> None:
    run(layout, {"VANTAGE_DATABASE_URL": "sqlite:////data/vantage.db"})
    assert layout.master_key.path.exists()


@pytest.mark.parametrize("names", ["bad name!", "x;rm", "[::1]"])
def test_bad_tls_names_are_refused_before_anything_is_written(
    layout: si.Layout, names: str
) -> None:
    with pytest.raises(si.InitError, match="VANTAGE_TLS_NAMES"):
        run(layout, {"VANTAGE_TLS_NAMES": names})
    assert not layout.master_key.path.exists()


def test_a_symlink_in_place_of_a_file_is_refused(layout: si.Layout, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("x")
    layout.tls_key.path.symlink_to(target)
    with pytest.raises(si.InitError, match="not a regular file"):
        run(layout)
    assert target.read_text() == "x"


def test_a_volume_that_isnt_mounted_is_refused(layout: si.Layout) -> None:
    layout.prometheus_yml.path.parent.rmdir()
    with pytest.raises(si.InitError, match="is not mounted"):
        run(layout)


def test_a_file_that_appears_meanwhile_is_kept(layout: si.Layout) -> None:
    layout.master_key.path.write_text("theirs")
    assert si._write_new(layout.master_key, b"ours") is False
    assert layout.master_key.path.read_text() == "theirs"
    assert not list(layout.master_key.path.parent.glob(".*.tmp"))


def test_cli_runs_it_without_settings_or_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """No VANTAGE_* environment: the command must not load the app's settings."""
    for name in list(os.environ):
        if name.startswith("VANTAGE_"):
            monkeypatch.delenv(name)
    err = io.StringIO()
    code = main(["standalone-init"], console=Console(stdout=io.StringIO(), stderr=err))
    assert code == 1  # /vantage-init isn't mounted here
    assert "is not mounted" in err.getvalue()
