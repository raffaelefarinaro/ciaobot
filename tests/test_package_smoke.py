from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from ciao.package_smoke import run_package_smoke


def test_package_smoke_runs_build_install_and_installed_probe(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text("{}\n", encoding="utf-8")

    commands: list[tuple[list[str], Path]] = []

    def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        commands.append((cmd, cwd))
        if cmd[:4] == [sys.executable, "-m", "pip", "wheel"]:
            outdir = Path(cmd[cmd.index("--wheel-dir") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "ciao-0.2.0-py3-none-any.whl").write_text("wheel\n", encoding="utf-8")

    run_package_smoke(repo, runner=fake_run)

    assert (["npm", "run", "build"], repo / "web") in commands
    assert (
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(repo / ".runtime" / "package-smoke" / "dist"),
            ".",
        ],
        repo,
    ) in commands
    assert any(cmd[:3] == [sys.executable, "-m", "venv"] for cmd, _cwd in commands)
    assert any(cmd[1:3] == ["-m", "pip"] and "ciao-0.2.0-py3-none-any.whl" in cmd[-1] for cmd, _cwd in commands)
    assert any(cmd[1] == "-c" and "import ciao" in cmd[2] for cmd, _cwd in commands)
    assert any(cmd[1] == "-c" and "if True:" in cmd[2] for cmd, _cwd in commands)


def test_package_smoke_can_skip_frontend_for_static_only_checks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    commands: list[list[str]] = []

    def fake_run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        commands.append(cmd)
        if cmd[:4] == [sys.executable, "-m", "pip", "wheel"]:
            outdir = Path(cmd[cmd.index("--wheel-dir") + 1])
            outdir.mkdir(parents=True, exist_ok=True)
            (outdir / "ciao-0.2.0-py3-none-any.whl").write_text("wheel\n", encoding="utf-8")

    run_package_smoke(repo, runner=fake_run, skip_frontend=True)

    assert ["npm", "run", "build"] not in commands
    assert any(cmd[:4] == [sys.executable, "-m", "pip", "wheel"] for cmd in commands)


def test_pyproject_ships_pwa_static_files() -> None:
    pyproject = Path(__file__).parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    package_data = data["tool"]["setuptools"]["package-data"]

    assert "static/**/*" in package_data["ciao.web"]
