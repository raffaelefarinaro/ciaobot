"""Wheel smoke-test target for public packaging work."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path


Runner = Callable[[list[str]], None]


class PackageSmokeError(RuntimeError):
    """Raised when the package smoke check cannot complete."""


def _default_runner(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(cmd, cwd=str(cwd), env=env, check=True)


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _installed_probe(require_stock: bool) -> str:
    return f"""
import sys
from pathlib import Path
from importlib import resources

import ciao
import ciao.window  # menu bar "Open Ciaobot" launches this; must import
if sys.platform == "darwin":
    # The native window backend must ship on macOS, or the tray/app opens
    # nothing. This is the regression class that left the window unopenable.
    import webview  # noqa: F401
from ciao.config import CiaoConfig
from ciao.web.app import create_app
from starlette.testclient import TestClient

static_root = resources.files("ciao.web").joinpath("static")
index = static_root.joinpath("index.html")
if not index.is_file():
    raise SystemExit(f"missing packaged PWA index: {{index}}")

workspace = Path.cwd()
cfg = CiaoConfig(
    pwa_auth_token="package-smoke",
    workspace_root=workspace,
    state_path=workspace / ".runtime" / "state.json",
    media_root=workspace / ".runtime" / "media",
)
response = TestClient(create_app(cfg)).get("/")
if response.status_code != 200:
    raise SystemExit(f"GET / returned {{response.status_code}}")

if {require_stock!r}:
    stock_root = resources.files("ciao.stock")
    required = ("agents", "commands", "workspace", "schedules.json")
    missing = [name for name in required if not stock_root.joinpath(name).is_file() and not stock_root.joinpath(name).is_dir()]
    if missing:
        raise SystemExit(f"missing stock package data: {{', '.join(missing)}}")
""".strip()


def run_package_smoke(
    repo_root: Path | str,
    *,
    runner: Callable[[list[str]], None] = _default_runner,
    skip_frontend: bool = False,
    require_stock: bool = True,
) -> None:
    """Build a wheel, install it into a clean venv, and probe the installed app."""

    root = Path(repo_root).expanduser().resolve()
    smoke_root = root / ".runtime" / "package-smoke"
    dist_dir = smoke_root / "dist"
    venv_dir = smoke_root / "venv"
    workspace_dir = smoke_root / "workspace"

    if smoke_root.exists():
        shutil.rmtree(smoke_root)
    dist_dir.mkdir(parents=True)
    workspace_dir.mkdir(parents=True)

    web_dir = root / "web"
    if not skip_frontend and (web_dir / "package.json").exists():
        runner(["npm", "run", "build"], cwd=web_dir)

    runner(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(dist_dir),
            ".",
        ],
        cwd=root,
    )
    wheels = sorted(dist_dir.glob("*.whl"))
    if not wheels:
        raise PackageSmokeError(f"no wheel produced in {dist_dir}")

    runner([sys.executable, "-m", "venv", str(venv_dir)], cwd=root)
    python = _venv_python(venv_dir)
    runner([str(python), "-m", "pip", "install", str(wheels[-1])], cwd=root)

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "PWA_AUTH_TOKEN": "package-smoke",
            "CIAO_WORKSPACE": str(workspace_dir),
            "CIAO_VAULT_ROOT": str(workspace_dir / "memory-vault"),
        }
    )
    runner([str(python), "-c", _installed_probe(require_stock)], cwd=workspace_dir, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build, install, and smoke-test the Ciaobot wheel in isolation."
    )
    parser.add_argument("repo_root", nargs="?", default=".")
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="Skip `npm run build` and use the existing ciao/web/static output.",
    )
    parser.add_argument(
        "--skip-stock",
        action="store_true",
        help="Skip ciao.stock package-data checks.",
    )
    args = parser.parse_args(argv)
    run_package_smoke(
        args.repo_root,
        skip_frontend=args.skip_frontend,
        require_stock=not args.skip_stock,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
