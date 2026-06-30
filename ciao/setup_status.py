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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Mapping, Any


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
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "auth": auth,
        "command": command,
    }
    if detail:
        row["detail"] = detail
    return row


def _ollama_daemon_ready(local_url: str) -> bool:
    url = local_url.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=0.4) as response:
            return 200 <= getattr(response, "status", 200) < 300
    except (OSError, urllib.error.URLError, TimeoutError):
        return False


def _pi_auth_ready() -> bool:
    if (Path.home() / ".pi").exists():
        return True
    if shutil.which("pi") is None:
        return False
    try:
        proc = subprocess.run(
            ["pi", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=0.8,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, TimeoutError):
        return False
    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    return proc.returncode == 0 and "login" not in combined and "not" not in combined


def _claude_status(
    env: Mapping[str, str],
    credentials_path: Path,
) -> dict[str, Any]:
    if env.get("ANTHROPIC_API_KEY", "").strip():
        return _provider(
            name="claude",
            ok=True,
            auth="api_key",
            command="ciao auth claude",
            detail="ANTHROPIC_API_KEY is set.",
        )
    if credentials_path.is_file():
        return _provider(
            name="claude",
            ok=True,
            auth="oauth",
            command="ciao auth claude",
            detail=str(credentials_path),
        )
    return _provider(
        name="claude",
        ok=False,
        auth="missing",
        command="ciao auth claude",
        detail="Run Claude OAuth or set ANTHROPIC_API_KEY.",
    )


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


def _pi_status() -> dict[str, Any]:
    if _pi_auth_ready():
        return _provider(
            name="pi",
            ok=True,
            auth="oauth",
            command="ciao auth pi",
            detail="pi auth status succeeded.",
        )
    return _provider(
        name="pi",
        ok=False,
        auth="missing",
        command="ciao auth pi",
        detail="Install Pi and run pi auth.",
    )


def setup_status(
    config: Any,
    *,
    env: Mapping[str, str] | None = None,
    claude_credentials_path: Path | None = None,
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
            check_id="pwa_auth_token",
            label="PWA auth token",
            ok=bool(getattr(config, "pwa_auth_token", "")),
            required=True,
        ),
        _check(
            check_id="push_contact",
            label="Push contact",
            ok=bool(source.get("CIAO_PUSH_CONTACT", "").strip()),
            required=True,
            detail="CIAO_PUSH_CONTACT",
        ),
    ]
    providers = {
        "claude": _claude_status(source, credentials_path),
        "pi": _pi_status(),
        "ollama": _ollama_status(config, source),
    }
    configured = all(row["ok"] for row in checks)
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
