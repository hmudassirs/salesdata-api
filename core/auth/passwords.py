# core/auth/passwords.py
"""Password hashing shared by the HTTP API and offline admin tooling.

Previously this lived as private (`_hash_password`/`_verify_password`)
functions inside `routes.py`. Pulling it out means the CLI bootstrap
script (`bootstrap_admin.py`) can hash a password the exact same way the
API does, instead of a second, easy-to-drift copy of the same logic.
"""

import hashlib
import secrets
from typing import Optional

# PBKDF2 iteration count. 200k is a reasonable current default for
# SHA-256; raise this over time as hardware gets faster (OWASP revises
# its recommendation periodically — check before assuming this is
# still current).
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str, salt: Optional[str] = None) -> str:
    """Hash a password with PBKDF2-HMAC-SHA256 and a random per-user salt.

    Args:
        password: Plaintext password.
        salt: Hex-encoded salt to reuse (only passed internally by
            `verify_password`). Omit to generate a fresh random salt.

    Returns:
        A string of the form "salt$hash", both hex-encoded.
    """
    salt = salt or secrets.token_hex(16)
    derived = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"{salt}${derived.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against a "salt$hash" PBKDF2 digest.

    Returns:
        True if the password matches, False for a mismatch or a
        malformed stored_hash (e.g. legacy unsalted hashes).
    """
    try:
        salt, _ = stored_hash.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_password(password, salt), stored_hash)
