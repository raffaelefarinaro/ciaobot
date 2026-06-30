from __future__ import annotations

from pathlib import Path


def test_deploy_folder_has_no_private_reverse_proxy_or_absolute_paths() -> None:
    repo = Path(__file__).parents[1]

    assert not (repo / "deploy" / "Caddyfile").exists()

    deploy_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (repo / "deploy").rglob("*")
        if path.is_file()
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
        Path(__file__).parents[1] / "deploy" / "com.ciao.server.plist"
    ).read_text(encoding="utf-8")

    assert "{{CIAO_PYTHON}}" in text
    assert "{{CIAO_WORKSPACE}}" in text
    assert "{{CIAO_PORT}}" in text
    assert "<string>ciao.cli</string>" in text
    assert "<string>run</string>" in text
