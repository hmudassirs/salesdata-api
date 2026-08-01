#!/usr/bin/env python3
"""Create (or promote) the first admin user, outside the HTTP API —
and issue them an API key, since nothing in this system can do that
for them otherwise.

Why this exists
----------------
`register_user` always creates a plain "user" account, and the only way
to grant admin is `PATCH /api/auth/users/{user_id}/role` — which itself
requires an existing admin's API key. That's correct for every user
*after* the first, but it leaves a chicken-and-egg problem for the very
first admin the system ever has. This script is the deliberate, narrow
escape hatch: it talks to the service database directly, run by whoever
has shell/deploy access to the box — not by anyone over the network.

There is a second, easy-to-miss chicken-and-egg problem underneath the
first one: the API-key middleware in app.py only ever validates API
keys (`x-api-key` header, checked against the `api_keys` table) — it
never validates the JWT that `/api/auth/users/login` returns. So even a
freshly bootstrapped admin, with a real username/password and a JWT
from logging in, still cannot call `POST /api/auth/keys` (the only way
to obtain a key) because that endpoint sits behind the same
API-key-only middleware. Creating the user is not enough; this script
also has to mint their first API key directly against the service
database, the same way it mints their first user record.

Usage
-----
Run from anywhere — this script locates the project root itself:

    python scripts/bootstrap_admin.py --username admin --email admin@example.com
    python scripts/bootstrap_admin.py --username admin --email admin@example.com --promote-existing

By default this also prints a freshly issued API key for the account —
the only time it will ever be shown. Pass --skip-api-key if you already
have a valid key for this account and don't want to mint another one.

If --password is omitted, you'll be prompted (input hidden, not echoed
to the terminal, not stored in shell history). You can also set it via
the ADMIN_BOOTSTRAP_PASSWORD environment variable for non-interactive
use (e.g. a container entrypoint) — prefer piping it in over passing
--password on the command line, since command-line args are visible to
other processes on the same machine (`ps`, /proc).

Safety properties
------------------
- Refuses to create a duplicate username; use --promote-existing to
  instead call update_role() on an already-registered account.
- Reuses UserCreate's pydantic validation (min password length, etc.)
  so this script can't create an account the API itself would reject.
- Uses the same core.auth.passwords.hash_password the API uses, so
  there's exactly one password-hashing implementation in the codebase.
- Issues API keys through the same core.auth.api_key_service.APIKeyService
  the HTTP API uses (generation, hashing, storage) — one implementation,
  not a second copy that could quietly drift from it.
- Never prints the password or its hash back to the terminal or logs.
  The raw API key is the one secret this script does print, since
  there is no other way to retrieve it later — the API itself only
  ever returns it once, at creation time, same as here.
"""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
from typing import Optional

# This script lives at <project_root>/scripts/bootstrap_admin.py. When
# run directly (`python scripts/bootstrap_admin.py`), Python only adds
# the script's own directory (scripts/) to sys.path — not the project
# root that actually contains the `core` package, which is why a bare
# `from core.auth... import ...` raises ModuleNotFoundError. Insert the
# parent of this file's directory so the import resolves regardless of
# the current working directory or how the script was launched.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.auth.api_key_service import APIKeyService
from core.auth.models import UserCreate
from core.auth.passwords import hash_password
from core.service_registry import ServiceManager
from core.storage.service_db import ServiceDatabase


def _read_password(cli_password: Optional[str]) -> str:
    """Resolve the password from --password, env var, or an interactive prompt.

    Precedence: --password > ADMIN_BOOTSTRAP_PASSWORD env var > prompt.
    """
    if cli_password:
        print(
            "Warning: passing --password on the command line is visible to "
            "other processes on this machine (ps, /proc). Prefer "
            "ADMIN_BOOTSTRAP_PASSWORD or the interactive prompt.",
            file=sys.stderr,
        )
        return cli_password

    env_password = os.getenv("ADMIN_BOOTSTRAP_PASSWORD")
    if env_password:
        return env_password

    password = getpass.getpass("Admin password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Error: passwords do not match.", file=sys.stderr)
        sys.exit(1)
    return password


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create or promote the first admin user."
    )
    parser.add_argument("--username", required=True, help="Admin username")
    parser.add_argument("--email", required=True, help="Admin email address")
    parser.add_argument(
        "--password",
        default=None,
        help="Admin password (prefer ADMIN_BOOTSTRAP_PASSWORD env var or the prompt instead)",
    )
    parser.add_argument(
        "--service-db-path",
        default=os.getenv("SERVICE_DB_PATH", "data/service.db"),
        help="Path to the SQLite service database (default: data/service.db, "
        "or $SERVICE_DB_PATH)",
    )
    parser.add_argument(
        "--promote-existing",
        action="store_true",
        help="If the username already exists, promote it to admin instead of "
        "failing. Does not change the existing password.",
    )
    parser.add_argument(
        "--skip-api-key",
        action="store_true",
        help="Don't issue a new API key for this account (e.g. it already "
        "has a valid one you intend to keep using).",
    )
    parser.add_argument(
        "--scopes",
        default=None,
        help="Comma-separated scopes for the issued API key (default: none, "
        "meaning unrestricted).",
    )
    args = parser.parse_args()

    password = _read_password(args.password)

    # Reuse the API's own validation (password length, required fields,
    # etc.) so this script can't create an account the HTTP API itself
    # would have rejected as invalid.
    try:
        validated = UserCreate(
            username=args.username,
            email=args.email,
            password=password,
            role="admin",
        )
    except Exception as e:
        print(f"Error: invalid input: {e}", file=sys.stderr)
        sys.exit(1)

    service_db = ServiceDatabase(args.service_db_path)
    service_db.connect()
    service_db.create_tables()

    def issue_api_key(owner_id: str) -> None:
        """Mint an API key the same way the HTTP API would, and print it once.

        This is the actual fix for "a registered user gets Unauthorized
        trying to create their own key": nobody — not even the admin
        this script just created — has any way to authenticate to the
        API at all until an API key exists for them. The JWT from
        /login is never checked by the middleware; only API keys are.
        """
        if args.skip_api_key:
            return

        service_manager = ServiceManager(service_db)
        api_key_service = APIKeyService(service_manager)
        result = asyncio.run(
            api_key_service.create_api_key(owner_id=owner_id, scopes=args.scopes)
        )

        print()
        print("=" * 70)
        print("API KEY (shown once — save it now, it cannot be retrieved again):")
        print(f"  {result['api_key']}")
        print("=" * 70)
        print(
            "Use it as the x-api-key header (or 'Authorization: Bearer "
            "<key>') on requests. Store it in a secrets manager or "
            "password manager, not in a file that gets committed."
        )
        print()

    try:
        existing = service_db.get_user_by_username(validated.username)

        if existing:
            if not args.promote_existing:
                print(
                    f"Error: user '{validated.username}' already exists. "
                    "Re-run with --promote-existing to grant it admin instead "
                    "of creating a new account.",
                    file=sys.stderr,
                )
                sys.exit(1)

            success = service_db.update_user_role(existing["user_id"], "admin")
            if not success:
                print("Error: failed to update role.", file=sys.stderr)
                sys.exit(1)

            print(f"Promoted existing user '{validated.username}' to admin.")
            issue_api_key(existing["user_id"])
            return

        import secrets
        import time

        user_id = f"user_{int(time.time())}_{secrets.token_hex(4)}"
        password_hash = hash_password(validated.password)

        success = service_db.create_user(
            user_id=user_id,
            username=validated.username,
            email=validated.email,
            password_hash=password_hash,
            role="admin",
        )
        if not success:
            print("Error: failed to create user.", file=sys.stderr)
            sys.exit(1)

        print(f"Created admin user '{validated.username}' (user_id={user_id}).")
        issue_api_key(user_id)
    finally:
        service_db.disconnect()


if __name__ == "__main__":
    main()
