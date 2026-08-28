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
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

from ciao import gws_auth, gws_wrapper
from ciao.gws_auth import fingerprint

_PERSONAL_SCOPES = gws_auth._PERSONAL_SCOPES  # noqa: SLF001


def _build_auth_url(
    *, client_id: str, redirect_uri: str, scopes: str, code_challenge: str | None = None
) -> str:
    return gws_auth.build_auth_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        code_challenge=code_challenge,
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


# Where the interactive run parks its PKCE verifier for a follow-up
# --redirect-url invocation. Short-lived: written 0600, deleted as soon as it
# is spent, and refused once older than the window below (an authorization code
# has expired well before then anyway, so a stale file can only mislead).
_PENDING_VERIFIER_NAME = ".pkce_verifier"
_PENDING_VERIFIER_MAX_AGE_S = 900.0


def _pending_verifier_path(config_dir) -> Path:
    return Path(config_dir) / _PENDING_VERIFIER_NAME


def _store_pending_verifier(config_dir, code_verifier: str) -> None:
    path = _pending_verifier_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create with 0600 rather than writing then chmod-ing: the verifier must
    # never exist group/world-readable, even briefly.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code_verifier)
    os.chmod(path, 0o600)


def _load_pending_verifier(config_dir) -> str:
    path = _pending_verifier_path(config_dir)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return ""
    if age > _PENDING_VERIFIER_MAX_AGE_S:
        _clear_pending_verifier(config_dir)
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _clear_pending_verifier(config_dir) -> None:
    try:
        _pending_verifier_path(config_dir).unlink()
    except OSError:
        pass


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
        "--code-verifier",
        help="PKCE verifier from the invocation that produced the authorization "
        "URL. Only needed with --redirect-url, and only when the parked "
        "verifier is unavailable (different machine, or expired).",
    )
    parser.add_argument(
        "--scopes",
        help="Space-separated OAuth scopes to request instead of the full default set "
        "(e.g. 'https://www.googleapis.com/auth/keep https://www.googleapis.com/auth/chat.messages').",
    )
    args = parser.parse_args(args_list)

    config = CiaoConfig.from_env()
    workspace_root = gws_wrapper._configured_workspace_root(config) or Path(
        config.workspace_root
    )
    root_config: SimpleNamespace = SimpleNamespace(workspace_root=str(workspace_root))
    config_dir = gws_auth.profile_config_dir(root_config, args.profile)
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

    # PKCE (issue #354). In the interactive flow the verifier is just a local
    # variable carried from the URL build to the exchange below. The
    # --redirect-url flow is two processes, though: the code in that URL was
    # issued against the *previous* invocation's challenge, so generating a
    # fresh verifier here would guarantee `invalid_grant`. The interactive run
    # therefore parks its verifier next to the profile, and the non-interactive
    # one picks it up. Never printed or logged.
    if args.redirect_url:
        code_verifier = args.code_verifier or _load_pending_verifier(config_dir)
        if not code_verifier:
            print(
                "Error: --redirect-url needs the verifier from the run that "
                "produced the authorization URL.\n"
                "Run this command without --redirect-url first (it prints the "
                "URL and parks the verifier), then re-run with --redirect-url, "
                "or pass --code-verifier explicitly.",
                file=sys.stderr,
            )
            return 1
        redirect_url = args.redirect_url.strip()
        print("Using redirect URL from --redirect-url flag.")
    else:
        code_verifier = gws_auth.generate_code_verifier()
        code_challenge = gws_auth.code_challenge_s256(code_verifier)
        auth_url = _build_auth_url(
            client_id=client_id,
            redirect_uri=redirect_uri,
            scopes=scopes,
            code_challenge=code_challenge,
        )
        # Parked before the URL is shown: the operator may complete the paste
        # in a second, non-interactive invocation.
        _store_pending_verifier(config_dir, code_verifier)
        print("Open this URL in your browser (pick the correct Google account):")
        print()
        print(auth_url)
        print()
        print("After Google redirects and the page fails to load, copy the FULL")
        print("URL from your browser's address bar and paste it below.")
        print("(Or re-run this command with --redirect-url '<the URL>'.)")
        print()

        if not sys.stdin.isatty():
            print(
                "Not a TTY: re-run with --redirect-url once you have the URL.",
                file=sys.stderr,
            )
            return 1
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
            code_verifier=code_verifier,
        )
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    finally:
        # The code is spent either way — a verifier left on disk could only be
        # picked up by a later run it does not belong to.
        _clear_pending_verifier(config_dir)

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
