import base64
import os
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.crypto import (
    KEY_ENV,
    KEY_FILE_ENV,
    DecryptionError,
    MasterKeyError,
    SecretBox,
    load_master_key,
    row_context,
)
from app.main import create_app
from app.models import Instance
from tests.conftest import TEST_MASTER_KEY

OTHER_KEY = b"another-fake-key-for-tests-only!"
SECRET = "example-token-value"
CTX = row_context("instances", "00000000-0000-0000-0000-000000000001")


def b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


# --- master key loading -------------------------------------------------------


def test_key_from_env() -> None:
    assert load_master_key({KEY_ENV: b64(TEST_MASTER_KEY)}) == TEST_MASTER_KEY


def test_key_from_file_with_trailing_newline(tmp_path: Path) -> None:
    # `openssl rand -base64 32 > master.key` writes a trailing newline.
    key_file = tmp_path / "master.key"
    key_file.write_text(b64(TEST_MASTER_KEY) + "\n")
    assert load_master_key({KEY_FILE_ENV: str(key_file)}) == TEST_MASTER_KEY


def test_key_file_wins_over_env(tmp_path: Path) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_text(b64(TEST_MASTER_KEY))
    env = {KEY_FILE_ENV: str(key_file), KEY_ENV: b64(OTHER_KEY)}
    assert load_master_key(env) == TEST_MASTER_KEY


@pytest.mark.parametrize("env", [{}, {KEY_ENV: ""}, {KEY_ENV: "   "}, {KEY_FILE_ENV: ""}])
def test_missing_key_is_rejected(env: dict[str, str]) -> None:
    with pytest.raises(MasterKeyError, match=KEY_FILE_ENV):
        load_master_key(env)


def test_unreadable_key_file_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(MasterKeyError, match=KEY_FILE_ENV):
        load_master_key({KEY_FILE_ENV: str(tmp_path / "missing.key")})


def test_binary_key_file_is_rejected(tmp_path: Path) -> None:
    key_file = tmp_path / "master.key"
    key_file.write_bytes(b"\xff\xfe" + TEST_MASTER_KEY)
    with pytest.raises(MasterKeyError, match=KEY_FILE_ENV):
        load_master_key({KEY_FILE_ENV: str(key_file)})


@pytest.mark.parametrize(
    "raw",
    [
        "not-base64!!",
        b64(b"too-short"),
        b64(TEST_MASTER_KEY + b"x"),
        TEST_MASTER_KEY.decode(),  # raw text, not base64
    ],
)
def test_malformed_key_is_rejected_without_echoing_it(raw: str) -> None:
    with pytest.raises(MasterKeyError) as exc:
        load_master_key({KEY_ENV: raw})
    assert raw not in str(exc.value)
    assert KEY_ENV in str(exc.value)


# --- app startup --------------------------------------------------------------


def test_app_refuses_to_start_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_ENV, raising=False)
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)
    with pytest.raises(MasterKeyError):
        create_app(Settings())


def test_app_refuses_to_start_with_bad_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(KEY_ENV, "not-a-key")
    with pytest.raises(MasterKeyError):
        create_app(Settings())


def test_app_starts_with_key_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(KEY_FILE_ENV, raising=False)
    monkeypatch.setenv(KEY_ENV, b64(TEST_MASTER_KEY))
    app = create_app(Settings())
    assert isinstance(app.state.secret_box, SecretBox)


# --- encryption ---------------------------------------------------------------


def test_round_trip(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    assert secret_box.decrypt(blob, context=CTX) == SECRET.encode()


def test_round_trip_with_new_box_from_same_key(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    assert SecretBox(TEST_MASTER_KEY).decrypt(blob, context=CTX) == SECRET.encode()


def test_secrets_mapping_round_trip(secret_box: SecretBox) -> None:
    values = {"token": SECRET, "password": "example-pässword", "empty": ""}
    blob = secret_box.encrypt_secrets(values, context=CTX)
    assert secret_box.decrypt_secrets(blob, context=CTX) == values


def test_ciphertext_does_not_contain_plaintext(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt_secrets({"token": SECRET}, context=CTX)
    assert SECRET.encode() not in blob
    assert b"token" not in blob


def test_same_plaintext_gives_different_blobs(secret_box: SecretBox) -> None:
    a = secret_box.encrypt(SECRET.encode(), context=CTX)
    b = secret_box.encrypt(SECRET.encode(), context=CTX)
    assert a != b
    assert a[1:13] != b[1:13]  # fresh nonce each time


def test_wrong_key_fails(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    with pytest.raises(DecryptionError):
        SecretBox(OTHER_KEY).decrypt(blob, context=CTX)


def test_wrong_context_fails(secret_box: SecretBox) -> None:
    """A blob copied to another row or table must not decrypt."""
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    for other in (row_context("instances", "other-id"), row_context("channels", "1")):
        with pytest.raises(DecryptionError):
            secret_box.decrypt(blob, context=other)


def test_every_tampered_byte_fails(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    for i in range(len(blob)):
        tampered = bytearray(blob)
        tampered[i] ^= 0x01
        with pytest.raises(DecryptionError):
            secret_box.decrypt(bytes(tampered), context=CTX)


@pytest.mark.parametrize("blob", [b"", b"\x01", b"\x01" + b"\x00" * 27, b"\x02" + b"\x00" * 40])
def test_short_or_unknown_version_blob_fails(secret_box: SecretBox, blob: bytes) -> None:
    with pytest.raises(DecryptionError):
        secret_box.decrypt(blob, context=CTX)


@pytest.mark.parametrize("plaintext", [b"not json", b"[1, 2]", b'{"token": 1}'])
def test_decrypt_secrets_rejects_wrong_shape(secret_box: SecretBox, plaintext: bytes) -> None:
    blob = secret_box.encrypt(plaintext, context=CTX)
    with pytest.raises(DecryptionError):
        secret_box.decrypt_secrets(blob, context=CTX)


def test_empty_context_is_refused(secret_box: SecretBox) -> None:
    with pytest.raises(ValueError):
        secret_box.encrypt(b"x", context="")


def test_errors_and_repr_leak_nothing(secret_box: SecretBox) -> None:
    blob = secret_box.encrypt(SECRET.encode(), context=CTX)
    with pytest.raises(DecryptionError) as exc:
        SecretBox(OTHER_KEY).decrypt(blob, context=CTX)
    text = str(exc.value) + repr(exc.value) + repr(secret_box)
    for needle in (SECRET, b64(TEST_MASTER_KEY), TEST_MASTER_KEY.decode(), blob.hex()):
        assert needle not in text
    assert exc.value.__cause__ is None
    assert exc.value.__suppress_context__


def test_master_key_must_be_32_bytes() -> None:
    with pytest.raises(ValueError):
        SecretBox(os.urandom(16))


# --- storage ------------------------------------------------------------------


def test_blob_round_trips_through_database(session: Session, secret_box: SecretBox) -> None:
    instance = Instance(type_id="http_check", name="example", config={})
    session.add(instance)
    session.flush()
    ctx = row_context("instances", instance.id)
    instance.secrets = secret_box.encrypt_secrets({"token": SECRET}, context=ctx)
    session.commit()
    session.expire_all()

    stored = session.get(Instance, instance.id)
    assert stored is not None and stored.secrets is not None
    assert SECRET.encode() not in stored.secrets
    assert secret_box.decrypt_secrets(stored.secrets, context=ctx) == {"token": SECRET}
