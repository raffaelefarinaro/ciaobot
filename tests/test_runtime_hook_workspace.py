"""Regression tests for the UserPromptSubmit runtime-context hook.

The hook callback runs in the ciao server process, so ``os.environ`` only
holds global defaults. The active chat's workspace arrives via ``extra_env``
(the per-request env the provider builds). These tests pin that ``extra_env``
wins, so the injected ``<ciao-runtime>`` block tracks the PWA workspace toggle
instead of always printing the server default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ciao.observability.hooks import _runtime_lines, build_user_prompt_submit_hook


def test_runtime_lines_prefers_extra_env_workspace(monkeypatch) -> None:
    # Server default is personal; the active chat is work.
    monkeypatch.setenv("CIAO_WORKSPACE", "/repo")
    monkeypatch.setenv("GWS_PROFILE", "personal")
    lines = _runtime_lines(
        Path("/repo"),
        {
            "CIAO_WORKSPACE": "/repo",
            "CIAO_ACTIVE_WORKSPACE": "work",
            "GWS_PROFILE": "work",
            "CIAO_ACTIVE_PROJECT": "general",
        },
    )
    assert "workspace=work" in lines
    assert "workspace=personal" not in lines
    assert "workspace=/repo" not in lines
    assert "active_project=general" in lines


def test_runtime_lines_falls_back_to_legacy_workspace_context(monkeypatch) -> None:
    # Back-compat for callers that still pass the old active-context env.
    monkeypatch.setenv("CIAO_WORKSPACE", "personal")
    monkeypatch.delenv("GWS_PROFILE", raising=False)
    lines = _runtime_lines(Path("/repo"))
    assert "workspace=personal" in lines


def test_runtime_lines_omits_path_workspace_without_active_context(monkeypatch) -> None:
    monkeypatch.setenv("CIAO_WORKSPACE", "/repo")
    monkeypatch.delenv("CIAO_ACTIVE_WORKSPACE", raising=False)
    monkeypatch.delenv("GWS_PROFILE", raising=False)
    lines = _runtime_lines(Path("/repo"))
    assert "workspace=/repo" not in lines


def test_runtime_lines_always_includes_today_and_cwd(monkeypatch) -> None:
    monkeypatch.delenv("CIAO_WORKSPACE", raising=False)
    monkeypatch.delenv("GWS_PROFILE", raising=False)
    lines = _runtime_lines(Path("/repo/x"))
    assert any(ln.startswith("today=") for ln in lines)
    assert "cwd=/repo/x" in lines


@pytest.mark.asyncio
async def test_user_prompt_hook_filters_entities_to_active_workspace(tmp_path: Path) -> None:
    (tmp_path / "INDEX.md").write_text(
        "\n".join([
            "- `personal/Projects/Apollo` (aliases: Apollo)",
            "- `work/Projects/Apollo` (aliases: Apollo)",
            "- `shared/People/Alba` (aliases: Alba)",
            "- `personal/People/Defne` (aliases: Defne)",
        ]),
        encoding="utf-8",
    )
    hook = build_user_prompt_submit_hook(
        tmp_path,
        {
            "CIAO_WORKSPACE": "/repo",
            "CIAO_ACTIVE_WORKSPACE": "work",
            "GWS_PROFILE": "work",
        },
    )

    out = await hook(
        {"prompt": "Apollo update with Alba and Defne", "cwd": "/repo"},
        None,
        None,
    )

    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "[[work/Projects/Apollo]]" in ctx
    assert "[[shared/People/Alba]]" in ctx
    assert "[[personal/Projects/Apollo]]" not in ctx
    assert "[[personal/People/Defne]]" not in ctx
