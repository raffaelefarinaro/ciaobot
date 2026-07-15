"""First-run setup readiness probes.

The setup wizard needs one stable endpoint that answers "what is already
configured?" without scraping files in the browser. Keep checks bounded and
fail-closed: missing or unreachable optional providers report ``ok=false``
with the command the user can run next.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import json
import sys
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Any

from ciao.providers.codex import codex_login_status


# macOS TCC (privacy) protects these home subfolders. A launchd-spawned
# background agent has no access grant for them, so a workspace placed inside
# one fails at runtime with EPERM ("Operation not permitted") reading its own
# .runtime files — the server and menu bar die (exit 78). Steer setup away.
_TCC_PROTECTED_DIRS = ("Desktop", "Documents", "Downloads")


def tcc_protected_location(path: Path | str) -> str | None:
    """Return the protected home folder (Desktop/Documents/Downloads) that
    contains ``path`` on macOS, or None. Non-macOS platforms return None."""
    if sys.platform != "darwin":
        return None
    resolved = Path(path).expanduser().resolve()
    home = Path.home()
    for name in _TCC_PROTECTED_DIRS:
        base = (home / name).resolve()
        if resolved == base or base in resolved.parents:
            return name
    return None


def _check(
    *,
    check_id: str,
    label: str,
    ok: bool,
    required: bool,
    detail: str = "",
    command: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": check_id,
        "label": label,
        "ok": bool(ok),
        "required": bool(required),
    }
    if detail:
        row["detail"] = detail
    if command:
        row["command"] = command
    return row


def _provider(
    *,
    name: str,
    ok: bool,
    auth: str,
    command: str,
    detail: str = "",
    version: str = "",
    account: str = "",
    protocol: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "auth": auth,
        "command": command,
    }
    if detail:
        row["detail"] = detail
    if version:
        row["version"] = version
    if account:
        row["account"] = account
    if protocol:
        row["protocol"] = protocol
    return row


@lru_cache(maxsize=4)
def _cli_version(binary: str) -> str:
    try:
        run = subprocess.run(
            [binary, "--version"], capture_output=True, text=True,
            timeout=3, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "installed"
    lines = (run.stdout or run.stderr).strip().splitlines()
    return lines[-1] if lines else "installed"


def _ollama_daemon_ready(local_url: str) -> bool:
    url = local_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=0.4) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _claude_status(
    env: Mapping[str, str],
    credentials_path: Path,
    config_path: Path,
) -> dict[str, Any]:
    from ciao.providers.claude import get_bundled_claude_path

    binary = get_bundled_claude_path() or shutil.which("claude") or ""
    version = _cli_version(binary) if binary else "not installed"
    if env.get("ANTHROPIC_API_KEY", "").strip():
        return _provider(
            name="claude",
            ok=True,
            auth="api_key",
            command="ciao auth claude",
            detail="ANTHROPIC_API_KEY is set.",
            version=version,
            account="Anthropic API",
            protocol="Agent SDK ready",
        )
    if credentials_path.is_file():
        return _provider(
            name="claude",
            ok=True,
            auth="oauth",
            command="ciao auth claude",
            detail=str(credentials_path),
            version=version,
            account="OAuth credentials",
            protocol="Agent SDK ready",
        )
    # Claude Code on macOS stores the OAuth token in the Keychain and writes
    # account metadata to ~/.claude.json. The token itself is not on disk, so
    # treat a populated ``oauthAccount`` block as evidence of a logged-in
    # session without attempting to read the Keychain (which may be locked or
    # unavailable to the server process).
    account = _claude_oauth_account(config_path)
    if account:
        return _provider(
            name="claude",
            ok=True,
            auth="oauth",
            command="ciao auth claude",
            detail=account,
            version=version,
            account=account.removeprefix("oauthAccount: "),
            protocol="Agent SDK ready",
        )
    return _provider(
        name="claude",
        ok=False,
        auth="missing",
        command="ciao auth claude",
        detail="Run Claude OAuth or set ANTHROPIC_API_KEY.",
        version=version,
    )


def _claude_oauth_account(config_path: Path) -> str:
    """Return a short identifier from ``~/.claude.json``'s ``oauthAccount``.

    Returns an empty string when the file is missing, unparseable, or has no
    usable account metadata. We deliberately avoid touching the Keychain: the
    server process often cannot unlock it, and the account block is enough to
    confirm a completed OAuth login.
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    account = data.get("oauthAccount")
    if not isinstance(account, dict) or not account:
        return ""
    email = str(account.get("emailAddress", "")).strip()
    if email:
        return f"oauthAccount: {email}"
    uuid_ = str(account.get("accountUuid", "")).strip()
    if uuid_:
        return f"oauthAccount: {uuid_}"
    return "oauthAccount present"


def _ollama_status(config: Any, env: Mapping[str, str]) -> dict[str, Any]:
    if env.get("CIAO_OLLAMA_API_KEY", "").strip():
        return _provider(
            name="ollama",
            ok=True,
            auth="api_key",
            command="ciao auth ollama",
            detail="CIAO_OLLAMA_API_KEY is set.",
        )
    local_url = getattr(getattr(config, "ollama", None), "local_url", "http://localhost:11434")
    if _ollama_daemon_ready(local_url):
        return _provider(
            name="ollama",
            ok=True,
            auth="local_daemon",
            command="ciao auth ollama",
            detail=f"{local_url.rstrip('/')}/api/tags responded.",
        )
    return _provider(
        name="ollama",
        ok=False,
        auth="missing",
        command="ciao auth ollama",
        detail="Set CIAO_OLLAMA_API_KEY or sign in to a local Ollama daemon.",
    )


def _openrouter_status(env: Mapping[str, str]) -> dict[str, Any]:
    if env.get("OPENROUTER_API_KEY", "").strip():
        return _provider(
            name="openrouter",
            ok=True,
            auth="api_key",
            command="OPENROUTER_API_KEY=sk-or-...",
            detail="OPENROUTER_API_KEY is set.",
        )
    return _provider(
        name="openrouter",
        ok=False,
        auth="missing",
        command="OPENROUTER_API_KEY=sk-or-...",
        detail="Create an OpenRouter key and add OPENROUTER_API_KEY to .env.",
    )


def _workspace_guides_linked(workspace_root: Path) -> bool:
    """True when AGENTS.md resolves to CLAUDE.md so both CLIs share one guide."""
    claude_guide = workspace_root / "CLAUDE.md"
    codex_guide = workspace_root / "AGENTS.md"
    try:
        return claude_guide.is_file() and codex_guide.resolve() == claude_guide.resolve()
    except OSError:
        return False


def setup_status(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
    claude_credentials_path: Path | None = None,
    claude_config_path: Path | None = None,
) -> dict[str, Any]:
    """Return setup readiness for the wizard and expert CLI.

    ``env`` is injectable for tests and lets the API route pass the live
    process environment without exposing secret values in the response.
    """
    source = env if env is not None else os.environ
    workspace_root = Path(getattr(config, "workspace_root", Path.cwd())).resolve()
    vault_root = Path(getattr(config, "vault_root", workspace_root / "memory-vault")).resolve()
    raw_credentials_path = source.get("CLAUDE_CREDENTIALS_PATH", "").strip()
    credentials_path = (
        claude_credentials_path
        or (Path(raw_credentials_path).expanduser() if raw_credentials_path else None)
        or Path.home() / ".claude" / ".credentials.json"
    )
    raw_config_path = source.get("CLAUDE_CONFIG_PATH", "").strip()
    config_path = (
        claude_config_path
        or (Path(raw_config_path).expanduser() if raw_config_path else None)
        or Path.home() / ".claude.json"
    )

    checks = [
        _check(
            check_id="workspace",
            label="Workspace folder",
            ok=workspace_root.is_dir(),
            required=True,
            detail=str(workspace_root),
        ),
        _check(
            check_id="vault",
            label="Vault folder",
            ok=vault_root.is_dir(),
            required=True,
            detail=str(vault_root),
        ),
        _check(
            check_id="workspace_guides",
            label="Linked workspace guides",
            ok=_workspace_guides_linked(workspace_root),
            # Optional: a custom AGENTS.md is preserved on purpose, but then
            # Claude Code and Codex read different workspace instructions.
            required=False,
            detail=str(workspace_root / "AGENTS.md"),
        ),
        _check(
            check_id="pwa_auth_token",
            label="PWA auth token",
            ok=bool(getattr(config, "pwa_auth_token", "")),
            required=True,
        ),
        _check(
            check_id="push_contact",
            label="Push contact",
            ok=bool(source.get("CIAO_PUSH_CONTACT", "").strip()),
            # Optional: without it Web Push stays disabled, nothing else breaks.
            required=False,
            detail="CIAO_PUSH_CONTACT",
        ),
    ]
    providers = {
        "claude": _claude_status(source, credentials_path, config_path),
        "codex": codex_login_status(source),
        "ollama": _ollama_status(config, source),
        "openrouter": _openrouter_status(source),
    }
    configured = all(row["ok"] for row in checks if row["required"])
    provider_ready = any(row["ok"] for row in providers.values())
    bootstrap = bool(getattr(config, "bootstrap_mode", False))
    return {
        "configured": configured and provider_ready,
        "bootstrap": bootstrap,
        "mode": "bootstrap" if bootstrap else "configured",
        "workspace_root": str(workspace_root),
        "vault_root": str(vault_root),
        "checks": checks,
        "providers": providers,
        "provider_ready": provider_ready,
    }
