"""Invariant tests for core/security/secrets.py. No database needed.

Navigation: round trip, stored form, masking, wrong key, malformed/missing key, rotation.
"""
import logging

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from core.security import SecretDecryptionError, SecretsConfigError, decrypt, encrypt, load_keys
from core.security.secrets import ENV_VAR

SECRET = "correct-horse-battery-staple-7731"


@pytest.fixture
def key(monkeypatch):
    k = Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_VAR, k)
    return k


def test_round_trip_and_the_token_is_not_the_plaintext(key):
    token = encrypt(SecretStr(SECRET))
    assert SECRET not in token and token != SECRET
    assert decrypt(token).get_secret_value() == SECRET


def test_encryption_is_randomized_per_call(key):
    assert encrypt(SecretStr(SECRET)) != encrypt(SecretStr(SECRET))   # Fernet includes a fresh IV


def test_decrypted_secret_is_masked_everywhere(key, caplog):
    s = decrypt(encrypt(SecretStr(SECRET)))
    assert isinstance(s, SecretStr)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("x").info("value=%s repr=%r", s, s)
    assert SECRET not in repr(s) and SECRET not in str(s) and SECRET not in caplog.text


def test_wrong_key_fails_without_leaking_the_token_or_secret(key, monkeypatch):
    token = encrypt(SecretStr(SECRET))
    monkeypatch.setenv(ENV_VAR, Fernet.generate_key().decode())
    with pytest.raises(SecretDecryptionError) as e:
        decrypt(token)
    assert token not in str(e.value) and SECRET not in str(e.value)


def test_corrupted_token_fails(key):
    token = encrypt(SecretStr(SECRET))
    with pytest.raises(SecretDecryptionError):
        decrypt(token[:-4] + "AAAA")
    with pytest.raises(SecretDecryptionError):
        decrypt("not-a-token")


def test_missing_key_fails_loudly(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    with pytest.raises(SecretsConfigError, match=ENV_VAR):
        encrypt(SecretStr(SECRET))
    monkeypatch.setenv(ENV_VAR, " , ")
    with pytest.raises(SecretsConfigError):
        load_keys()


def test_malformed_key_fails_without_echoing_it(monkeypatch):
    bad = "this-is-not-a-fernet-key-but-looks-secret"
    monkeypatch.setenv(ENV_VAR, bad)
    with pytest.raises(SecretsConfigError) as e:
        encrypt(SecretStr(SECRET))
    assert bad not in str(e.value) and ENV_VAR in str(e.value)


def test_rotation_new_first_key_old_key_still_decrypts(monkeypatch):
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    monkeypatch.setenv(ENV_VAR, old)
    old_token = encrypt(SecretStr(SECRET))
    monkeypatch.setenv(ENV_VAR, f"{new},{old}")
    assert decrypt(old_token).get_secret_value() == SECRET          # old data still readable
    new_token = encrypt(SecretStr(SECRET))
    monkeypatch.setenv(ENV_VAR, new)                                # old key retired
    assert decrypt(new_token).get_secret_value() == SECRET          # new tokens use the new key
    with pytest.raises(SecretDecryptionError):
        decrypt(old_token)                                          # old tokens need the old key
