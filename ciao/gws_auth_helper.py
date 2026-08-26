"""``ciao gws-auth-helper`` — interactive headless OAuth re-auth for gws.

Moved from ``scripts/gws-auth-helper.py`` into the package so it ships inside
the installed app. Unlike the old script it reuses ``ciao.gws_auth`` for the
token exchange and credential write (no duplicated scope/code/save logic).

Flow:
1. Reads ``client_secret.json`` from the profile's credential dir.
2. Prints the exact auth URL (using the registered redirect_uri).
3. Waits for the redirect URL to be pasted back (or ``--redirect-url``).
4. Exchanges the code and saves ``credentials.json``.
5. Retires stale encrypted files so gws uses the new credentials.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from ciao import gws_auth
from ciao.gws_auth import fingerprint

_PERSONAL_SCOPES = gws_auth._PERSONAL_SCOPES  # noqa: SLF001


def _build_auth_url(*, client_id: str, redirect_uri: str, scopes: str) -> str:
    return gws_auth.build_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
    )


def _read_client_secret(config_dir) -> dict:
    return gws_auth.load_client_secret(config_dir)


def _clean_stale_files(config_dir) -> None:
    """Retire old encrypted files so gws uses the new plain credentials."""
    for name in ("credentials.enc", "token_cache.json"):
        stale = config_dir / name
        if stale.exists():
            backup = config_dir / (name + ".old")
            stale.rename(backup)
            print(f"  Moved stale {name} -> {name}.old")


def _fix_encryption_key_permissions(config_dir) -> None:
    key_file = config_dir / ".encryption_key"
    if key_file.exists():
        mode = key_file.stat().st_mode & 0o777
        if mode != 0o600:
            os.chmod(key_file, 0o600)
            print(f"  Fixed {key_file} permissions to 600")


def main_entry(argv: Sequence[str] | None = None) -> int:
    from ciao.config import CiaoConfig

    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        prog="ciao gws-auth-helper",
        description="Interactive headless GWS OAuth re-auth",
    )
    parser.add_argument("profile", help="GWS profile (Google account name) to authenticate")
    parser.add_argument(
        "--redirect-url",
        help="Full redirect URL from the browser. When given, skip the interactive "
        "prompt and exchange the code directly (for headless/non-TTY use).",
    )
    parser.add_argument(
        "--scopes",
        help="Space-separated OAuth scopes to request instead of the full default set "
        "(e.g. 'https://www.googleapis.com/auth/keep https://www.googleapis.com/auth/chat.messages').",
    )
    args = parser.parse_args(args_list)

    config = CiaoConfig.from_env()
    config_dir = gws_auth.profile_config_dir(config, args.profile)
    if config_dir is None:
        print(f"Error: '{args.profile}' is not a usable profile name", file=sys.stderr)
        return 2
    config_dir.mkdir(parents=True, exist_ok=True)

    try:
        installed = _read_client_secret(config_dir)
    except ValueError as exc:
        print(f"Error: {exc}. Run `ciao gws {args.profile} auth setup` first.", file=sys.stderr)
        return 1

    client_id = installed.get("client_id")
    client_secret = installed.get("client_secret")
    if not client_id or not client_secret:
        print("Error: client_secret.json missing client_id or client_secret", file=sys.stderr)
        return 1
    redirect_uris = installed.get("redirect_uris", ["http://localhost"])
    redirect_uri = redirect_uris[0]

    scopes = args.scopes.strip() if args.scopes else _PERSONAL_SCOPES

    print(f"\nProfile: {args.profile}")
    print(f"Config dir: {config_dir}")
    print(f"Client ID: sha256:{fingerprint(client_id)}")
    print(f"Redirect URI: sha256:{fingerprint(redirect_uri)}")
    print()

    auth_url = _build_auth_url(client_id=client_id, redirect_uri=redirect_uri, scopes=scopes)
    print("Open this URL in your browser (pick the correct Google account):")
    print()
    print(auth_url)
    print()
    print("After Google redirects and the page fails to load, copy the FULL")
    print("URL from your browser's address bar and paste it below.")
    print()

    if args.redirect_url:
        redirect_url = args.redirect_url.strip()
        print(f"Using redirect URL from --redirect-url flag.")
    else:
        redirect_url = input("Paste redirect URL: ").strip()
    if not redirect_url:
        print("No URL provided. Exiting.")
        return 1

    try:
        code = gws_auth.extract_code_from_input(redirect_url)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    print("\nExchanging code for tokens...")
    try:
        tokens = gws_auth.exchange_code(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Warning: no refresh_token in response. Account may already be authorized.")
        print("If this is a re-auth, revoke the old grant first at https://myaccount.google.com/permissions")
        return 1

    print("Got refresh token. Saving credentials...")
    _clean_stale_files(config_dir)
    email = gws_auth.extract_email_from_id_token(tokens.get("id_token"))
    granted_scopes = tokens.get("scope") or ""
    gws_auth.store_credentials(
        config_dir,
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        email=email,
        scopes=granted_scopes,
    )
    _fix_encryption_key_permissions(config_dir)

    print("\nDone. Verify with:")
    print(f"  GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file ciao gws {args.profile} calendar events list --params '{{\"calendarId\": \"primary\", \"maxResults\": 1}}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_entry(sys.argv[1:]))
