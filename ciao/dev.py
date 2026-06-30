"""Local development runner for the Ciao backend and Vite frontend."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class DevEnvironment:
    workspace: Path
    web_dir: Path
    env: dict[str, str]
    backend_url: str
    frontend_url: str


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def build_dev_environment(
    workspace: Path | str,
    *,
    backend_port: int = 8543,
    frontend_port: int = 5173,
    base_env: Mapping[str, str] | None = None,
) -> DevEnvironment:
    root = Path(workspace).expanduser().resolve()
    web_dir = root / "web"
    if not web_dir.is_dir():
        raise RuntimeError(f"{web_dir} does not exist; run ciao dev from an app checkout.")

    env = dict(base_env or os.environ)
    for key, value in _load_env_file(root / ".env").items():
        env.setdefault(key, value)
    if not env.get("PWA_AUTH_TOKEN"):
        raise RuntimeError("Set PWA_AUTH_TOKEN in .env first.")

    env["CIAO_AUTO_SYNC_ON_START"] = "false"
    env["PWA_PORT"] = str(env.get("PWA_PORT") or backend_port)
    env.setdefault("VITE_BACKEND_URL", f"http://127.0.0.1:{env['PWA_PORT']}")
    env.setdefault("CIAO_WORKSPACE", str(root))

    return DevEnvironment(
        workspace=root,
        web_dir=web_dir,
        env=env,
        backend_url=f"http://127.0.0.1:{env['PWA_PORT']}",
        frontend_url=f"http://localhost:{frontend_port}",
    )


def _wait_for_backend(url: str, *, timeout_s: int = 30) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    raise RuntimeError(f"Backend did not become ready at {url}")


def _terminate(processes: list[subprocess.Popen]) -> None:
    for proc in processes:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    for proc in processes:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def run_dev_stack(
    workspace: Path | str,
    *,
    backend_port: int = 8543,
    frontend_port: int = 5173,
    install_node: bool = True,
) -> int:
    dev_env = build_dev_environment(
        workspace,
        backend_port=backend_port,
        frontend_port=frontend_port,
    )
    dev_env.workspace.joinpath(".runtime").mkdir(parents=True, exist_ok=True)

    if install_node and not (dev_env.web_dir / "node_modules").is_dir():
        print("Installing npm dependencies...")
        subprocess.run(["npm", "install"], cwd=dev_env.web_dir, check=True)

    processes: list[subprocess.Popen] = []
    try:
        print(f"Starting backend on {dev_env.backend_url} ...")
        backend = subprocess.Popen(
            [sys.executable, "-m", "ciao.main"],
            cwd=dev_env.workspace,
            env=dev_env.env,
        )
        processes.append(backend)
        _wait_for_backend(dev_env.backend_url)

        print(f"Starting frontend on {dev_env.frontend_url} ...")
        frontend = subprocess.Popen(
            ["npx", "vite", "--host", "0.0.0.0", "--port", str(frontend_port)],
            cwd=dev_env.web_dir,
            env=dev_env.env,
        )
        processes.append(frontend)

        print("")
        print(f"  Backend:  {dev_env.backend_url}")
        print(f"  Frontend: {dev_env.frontend_url}  (use this one)")
        print(f"  Proxy:    {dev_env.env['VITE_BACKEND_URL']}")
        print("")
        print("  Press Ctrl+C to stop both.")
        while True:
            for proc in processes:
                code = proc.poll()
                if code is not None:
                    return int(code)
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        _terminate(processes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path("."),
        help="App checkout/workspace root. Defaults to current directory.",
    )
    parser.add_argument("--backend-port", type=int, default=8543)
    parser.add_argument("--frontend-port", type=int, default=5173)
    parser.add_argument(
        "--no-install",
        action="store_true",
        help="Do not run npm install when web/node_modules is missing.",
    )
    args = parser.parse_args(argv)
    try:
        return run_dev_stack(
            args.workspace,
            backend_port=args.backend_port,
            frontend_port=args.frontend_port,
            install_node=not args.no_install,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
