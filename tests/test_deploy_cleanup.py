from __future__ import annotations

from pathlib import Path


def test_deploy_folder_has_no_private_reverse_proxy_or_absolute_paths() -> None:
    repo = Path(__file__).parents[1]

    assert not (repo / "ciao" / "stock" / "deploy" / "Caddyfile").exists()

    deploy_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo / "ciao" / "stock" / "deploy").rglob("*")
        if path.is_file() and path.suffix not in {".icns", ".png"}
    )
    forbidden = (
        "raff" + "aelefarinaro",
        "bot." + "raff" + "aelefarinaro.com",
        "/Users/" + "raff" + "aelefarinaro",
        "sdc" + "-labs",
    )
    for marker in forbidden:
        assert marker not in deploy_text


def test_deploy_plist_points_at_packaged_cli_template() -> None:
    text = (
        Path(__file__).parents[1] / "ciao" / "stock" / "deploy" / "com.ciao.server.plist.tmpl"
    ).read_text(encoding="utf-8")

    assert "{{CIAO_PYTHON}}" in text
    assert "{{CIAO_WORKSPACE}}" in text
    assert "{{CIAO_PORT}}" in text
    assert "{{CIAO_PATH}}" in text
    assert "<string>ciao.cli</string>" in text
    assert "<string>run</string>" in text


def test_render_launchd_plist_substitutes_path() -> None:
    import os

    from ciao.cli import _render_launchd_plist

    saved = os.environ.get("PATH", "")
    os.environ["PATH"] = "/opt/homebrew/bin:/usr/bin:/bin"
    try:
        out = _render_launchd_plist(
            workspace=Path("/tmp/ciao-ws"),
            python_path="/opt/ciao/bin/python",
            port=8443,
        )
    finally:
        os.environ["PATH"] = saved

    assert "{{CIAO_PATH}}" not in out
    assert "<key>PATH</key>" in out
    assert "/opt/homebrew/bin:/usr/bin:/bin" in out


def test_run_step_reports_missing_binary_as_failed_step() -> None:
    from ciao.web.routes_api import _run_step

    result = _run_step(["definitely-not-a-real-binary-xyz"], cwd="/tmp", timeout=5)
    assert result.returncode == 127
    assert "not found on PATH" in result.stderr
    assert result.stdout == ""


def test_run_step_passes_through_success(tmp_path) -> None:
    from ciao.web.routes_api import _run_step

    result = _run_step(["true"], cwd=str(tmp_path), timeout=5)
    assert result.returncode == 0


def test_root_npm_install_skips_without_root_package_json(tmp_path) -> None:
    from ciao.web.routes_api import _run_root_npm_install

    result = _run_root_npm_install(tmp_path)

    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert "package.json" in result.stdout
