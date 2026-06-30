import os
import re
import shutil
import subprocess
from pathlib import Path
import pytest

from ciao.local_session import LocalSessionManager


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.com",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.com",
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
        "HOME": str(repo),
    }
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _identify(repo: Path) -> None:
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@e.com")


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _identify(repo)
    _write(repo / "README.md", "hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    return repo


@pytest.mark.asyncio
async def test_preflight_clean_repo(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = LocalSessionManager(workspace=repo, runtime_root=tmp_path / "rt", device_name="mini")
    
    result = await mgr.preflight()
    assert result["dirty"] is False
    assert result["blockers"] == []
    assert result["warnings"] == []
    assert result["deploy_needed"] is False
    assert all(len(v) == 0 for v in result["changed_files"].values())


@pytest.mark.asyncio
async def test_preflight_categorization_without_deploy_signal(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = LocalSessionManager(workspace=repo, runtime_root=tmp_path / "rt", device_name="mini")
    
    # Create files in various categories
    _write(repo / "ciao/web/routes_api.py", "# code change\n")
    _write(repo / "memory-vault/personal/People/john.md", "# person\n")
    _write(repo / "scripts/test-script.py", "# script\n")
    _write(repo / "pyproject.toml", "# config\n")
    _write(repo / "other-file.txt", "# other\n")
    
    result = await mgr.preflight()
    assert result["dirty"] is True
    
    # Categorization checks
    assert "ciao/web/routes_api.py" in result["changed_files"]["code"]
    assert "memory-vault/personal/People/john.md" in result["changed_files"]["vault"]
    assert "scripts/test-script.py" in result["changed_files"]["scripts"]
    assert "pyproject.toml" in result["changed_files"]["config"]
    assert "other-file.txt" in result["changed_files"]["other"]
    
    # Workspace commits never request an app deploy after the package split.
    assert result["deploy_needed"] is False


@pytest.mark.asyncio
async def test_preflight_blockers_and_warnings(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    mgr = LocalSessionManager(workspace=repo, runtime_root=tmp_path / "rt", device_name="mini")
    
    # 1. Blocker: .env file
    _write(repo / ".env", "API_KEY=secret\n")
    
    # 2. Blocker: private key file by extension
    _write(repo / "id_rsa.key", "ssh-rsa ...")
    
    # 3. Blocker: private key content
    _write(repo / "cert.txt", "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n-----END RSA PRIVATE KEY-----\n")
    
    # 4. Blocker: Google Service Account
    _write(repo / "service-account.json", '{"type": "service_account", "private_key": "somekey", "client_email": "sa@google.com"}')
    
    # 5. Blocker: OpenAI key (constructed dynamically to bypass GitHub secret scan)
    mock_openai = "sk-" + "1234567890abcdef1234567890abcdef1234567890abcdef"
    _write(repo / "openai.py", f'key = "{mock_openai}"\n')
    
    # 6. Blocker: Slack token (constructed dynamically to bypass GitHub secret scan)
    mock_slack = "xoxb-" + "123456789012-" + "123456789012-" + "abcdefghijklmnopqrstuvwx"
    _write(repo / "slack.py", f'token = "{mock_slack}"\n')

    # 7. Warning: suspicious file name
    _write(repo / "credentials.json", '{"normal": "settings"}')
    
    result = await mgr.preflight()
    
    # Verify blockers and warnings
    assert len(result["blockers"]) == 6
    assert any("env" in b.lower() for b in result["blockers"])
    assert any("cryptographic key" in b.lower() for b in result["blockers"])
    assert any("private key structure" in b.lower() for b in result["blockers"])
    assert any("service account" in b.lower() for b in result["blockers"])
    assert any("openai api key" in b.lower() for b in result["blockers"])
    assert any("slack api token" in b.lower() for b in result["blockers"])
    
    assert len(result["warnings"]) == 1
    assert "credentials.json" in result["warnings"][0]
