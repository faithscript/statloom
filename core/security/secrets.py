"""core/security/secrets.py: encrypt and decrypt connection credentials (Fernet).

Navigation:
  SecretsConfigError / SecretDecryptionError   typed failures (never carry a key, token, or secret)
  load_keys     read STATLOOM_SECRET_KEY: one or more comma-separated Fernet keys
  check_secret_key   startup check: raise now (not at first use) if the key is missing or malformed
  encrypt       plaintext -> token, using the FIRST key
  decrypt       token -> masked SecretStr, trying every key

Invariants: credentials are stored only encrypted; a decrypted secret is a masked
SecretStr, so repr/str/logs show "**********"; decrypt only where a connector is built
(services/connections.py); a missing or malformed key fails loudly and names the
variable, never the key. Several keys make rotation possible without extra code: put
the new key first, keep the old one until stored tokens are re-encrypted.
Key management beyond an environment variable is deferred (Q9).
"""
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import SecretStr

ENV_VAR = "STATLOOM_SECRET_KEY"


class SecretsConfigError(Exception):
    """The encryption key is missing or malformed."""


class SecretDecryptionError(Exception):
    """A stored token could not be decrypted (wrong key or corrupted data)."""


def load_keys() -> list[Fernet]:
    raw = os.environ.get(ENV_VAR, "")
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise SecretsConfigError(f"{ENV_VAR} is not set")
    keys = []
    for part in parts:
        try:
            keys.append(Fernet(part.encode()))
        except Exception:  # noqa: BLE001  ValueError / binascii errors; never echo the key
            raise SecretsConfigError(f"{ENV_VAR} contains a malformed Fernet key") from None
    return keys


def encrypt(plaintext: SecretStr) -> str:
    return MultiFernet(load_keys()).encrypt(plaintext.get_secret_value().encode()).decode()


def decrypt(token: str) -> SecretStr:
    try:
        return SecretStr(MultiFernet(load_keys()).decrypt(token.encode()).decode())
    except InvalidToken:
        raise SecretDecryptionError("stored credential could not be decrypted") from None


def check_secret_key() -> int:
    """Startup validation. Call where the services are wired (the API's create_app with a database):
    a missing or malformed key then stops startup instead of surfacing on the first encrypt or
    decrypt. Returns the number of keys configured. The message names the variable, never a key."""
    return len(load_keys())
