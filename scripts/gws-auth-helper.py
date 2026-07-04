#!/usr/bin/env python3
"""Interactive GWS OAuth re-auth for headless servers.

Usage:
    python3 scripts/gws-auth-helper.py personal
    python3 scripts/gws-auth-helper.py work

Flow:
1. Reads client_secret.json from ~/.config/gws[-personal]/
2. Prints the exact auth URL (using the registered redirect_uri)
3. Waits for you to paste the redirect URL from your browser
4. Exchanges the code and saves credentials.json
5. Removes stale encrypted files so gws uses the new credentials
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

PROFILE_CONFIGS = {
    "personal": REPO_ROOT / "secrets" / "gws-personal",
    "work": REPO_ROOT / "secrets" / "gws",
}

# Scopes requested per profile (must match or exceed what gws needs).
PERSONAL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/tasks "
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)

WORK_SCOPES = (
    "https://www.googleapis.com/auth/drive "
    "https://www.googleapis.com/auth/spreadsheets "
    "https://www.googleapis.com/auth/gmail.modify "
    "https://www.googleapis.com/auth/calendar "
    "https://www.googleapis.com/auth/documents "
    "https://www.googleapis.com/auth/presentations "
    "https://www.googleapis.com/auth/tasks "
    "openid "
    "https://www.googleapis.com/auth/userinfo.email "
    "https://www.googleapis.com/auth/userinfo.profile"
)

PROFILE_SCOPES = {
    "personal": PERSONAL_SCOPES,
    "work": WORK_SCOPES,
}


def read_client_secret(config_dir: Path) -> dict:
    path = config_dir / "client_secret.json"
    if not path.exists():
        print(f"Error: {path} not found. Run gws auth setup first.", file=sys.stderr)
        sys.exit(1)
    with open(path, "r") as f:
        return json.load(f)


def extract_installed(secret: dict) -> dict:
    if "installed" in secret:
        return secret["installed"]
    if "web" in secret:
        return secret["web"]
    print("Error: client_secret.json missing 'installed' or 'web' section.", file=sys.stderr)
    sys.exit(1)


def build_auth_url(client_id: str, redirect_uri: str, scopes: str) -> str:
    params = {
        "scope": scopes,
        "access_type": "offline",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "client_id": client_id,
        "prompt": "select_account consent",
    }
    return "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)


def exchange_code(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> dict:
    data = urllib.parse.urlencode(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        print(f"\nToken exchange failed: HTTP {e.code}")
        print(body)
        sys.exit(1)


def save_credentials(config_dir: Path, client_id: str, client_secret: str, refresh_token: str) -> None:
    creds = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "type": "authorized_user",
    }
    path = config_dir / "credentials.json"
    with open(path, "w") as f:
        json.dump(creds, f, indent=2)
    os.chmod(path, 0o600)


def clean_stale_files(config_dir: Path) -> None:
    """Remove old encrypted files so gws uses the new plain credentials."""
    for name in ("credentials.enc", "token_cache.json"):
        stale = config_dir / name
        if stale.exists():
            backup = config_dir / (name + ".old")
            stale.rename(backup)
            print(f"  Moved stale {name} -> {name}.old")


def fix_encryption_key_permissions(config_dir: Path) -> None:
    key_file = config_dir / ".encryption_key"
    if key_file.exists():
        mode = key_file.stat().st_mode & 0o777
        if mode != 0o600:
            os.chmod(key_file, 0o600)
            print(f"  Fixed {key_file} permissions to 600")


def validate_profile(profile: str) -> Path:
    config_dir = PROFILE_CONFIGS[profile]
    if not config_dir.exists():
        print(f"Creating {config_dir}")
        config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive GWS OAuth helper")
    parser.add_argument("profile", choices=["personal", "work"], help="GWS profile to authenticate")
    args = parser.parse_args()

    config_dir = validate_profile(args.profile)
    secret = read_client_secret(config_dir)
    installed = extract_installed(secret)

    client_id = installed["client_id"]
    client_secret = installed["client_secret"]
    redirect_uris = installed.get("redirect_uris", ["http://localhost"])
    redirect_uri = redirect_uris[0]

    scopes = PROFILE_SCOPES[args.profile]

    print(f"\nProfile: {args.profile}")
    print(f"Config dir: {config_dir}")
    print(f"Client ID: {client_id}")
    print(f"Redirect URI: {redirect_uri}")
    print()

    auth_url = build_auth_url(client_id, redirect_uri, scopes)
    print("Open this URL in your browser (pick the correct Google account):")
    print()
    print(auth_url)
    print()
    print("After Google redirects and the page fails to load, copy the FULL")
    print("URL from your browser's address bar and paste it below.")
    print()

    redirect_url = input("Paste redirect URL: ").strip()
    if not redirect_url:
        print("No URL provided. Exiting.")
        sys.exit(1)

    parsed = urllib.parse.urlparse(redirect_url)
    query = urllib.parse.parse_qs(parsed.query)

    if "error" in query:
        print(f"Google returned error: {query['error'][0]}")
        sys.exit(1)

    if "code" not in query:
        print("Error: no 'code' found in the redirect URL.")
        print(f"Query params: {dict(query)}")
        sys.exit(1)

    code = query["code"][0]
    print("\nExchanging code for tokens...")

    tokens = exchange_code(code, client_id, client_secret, redirect_uri)

    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        print("Warning: no refresh_token in response. Account may already be authorized.")
        print("If this is a re-auth, revoke the old grant first at https://myaccount.google.com/permissions")
        sys.exit(1)

    print("Got refresh token. Saving credentials...")
    clean_stale_files(config_dir)
    save_credentials(config_dir, client_id, client_secret, refresh_token)
    fix_encryption_key_permissions(config_dir)

    print("\nDone. Verify with:")
    print(f"  GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file scripts/gws-profile.sh {args.profile} gws auth status")
    print(f"  GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file scripts/gws-profile.sh {args.profile} gws calendar events list --params '{{\"calendarId\": \"primary\", \"maxResults\": 1}}'")


if __name__ == "__main__":
    main()
