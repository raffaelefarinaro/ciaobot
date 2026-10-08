"""The agent turn's PATH must lead with the running engine's own bin dir (#989).

A stale ``ciao`` earlier on the user's PATH (for example an older uv-tool
install at ``~/.local/bin/ciao``) used to win every ``ciao <command>`` an agent
ran, because the providers spawned the harness with the inherited process PATH
and nothing promoted the engine's own executable directory. These tests pin the
engine-first PATH on the one chat-turn env builder (which reaches Claude,
opencode and the agent CLI transport) and on the one-shot env builder.

The engine directory is ``sysconfig.get_path("scripts")``, deliberately not the
resolved interpreter path: a venv's ``bin/python`` symlinks to the base
interpreter, and resolving it names the base interpreter's bin dir — where no
``ciao`` entry point lives. It is also not always ``Path(sys.executable).parent``:
on a global Windows install the interpreter sits in ``...\\x64\\`` while the
console script is written to the sibling ``...\\Scripts\\``. ``ciao/cli.py``
documents the same rule.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from pathlib import Path

import pytest

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.models import AgentRequest
from ciao.sessions import StateStore
from ciao.tool_path import engine_bin_dir, prepend_engine_path
from ciao.transcripts import TranscriptStore
from ciao.web.project_chats import ProjectChatManager

from tests.conftest import attach_stub_mcp


def _make_manager(tmp_path: Path) -> ProjectChatManager:
    runtime = tmp_path / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    config = CiaoConfig(
        pwa_auth_token="test-token",
        workspace_root=tmp_path,
        state_path=runtime / "state.json",
        media_root=runtime / "media",
        # These tests check the turn env, not the sandbox: whole-machine scope
        # keeps the spawn unwrapped on every platform (Linux CI has no bwrap,
        # Windows has no sandbox).
        workspaces={
            "personal": WorkspaceConfig(
                name="personal",
                vault_root="memory-vault/personal",
                agent_fs_scope="machine",
            )
        },
    )
    return attach_stub_mcp(ProjectChatManager(
        config,
        state_store=StateStore(config.state_path, tmp_path, config.media_root),
        transcript_store=TranscriptStore(runtime, tmp_path / "transcripts"),
        path=runtime / "web_projects.json",
    ))


def _chat_extra_env(tmp_path: Path, monkeypatch, *, provider: str = "claude") -> dict:
    """A real chat turn's ``extra_env`` for one provider."""
    manager = _make_manager(tmp_path)
    project = manager.create_project("path", workspace="personal")
    chat = manager.create_chat(project.project_id, provider=provider)
    return manager.build_agent_request(chat, prompt="hi").extra_env


# ── the helper ────────────────────────────────────────────────────────────


def test_engine_bin_dir_is_the_script_dir_not_the_resolved_base() -> None:
    """Guard the venv-symlink trap and the global-Windows split.

    The directory is the scripts dir, not ``Path(sys.executable).parent``: a
    global Windows install keeps the interpreter in ``...\\x64\\`` and writes the
    ``ciao`` console script to the sibling ``...\\Scripts\\``. And it is not the
    ``.resolve()``d base interpreter's dir either: a venv's ``bin/python``
    symlinks to the base interpreter, and that base dir holds no ``ciao`` entry
    point. ``sysconfig.get_path("scripts")`` is venv-aware and does not resolve
    symlinks, so both rules hold at once.
    """
    engine = Path(engine_bin_dir())
    assert engine == Path(sysconfig.get_path("scripts"))
    # A venv's bin/python symlinks to the base interpreter; the engine dir must
    # stay on the venv's own side, never the resolved base's, where no `ciao`
    # entry point lives.
    exe_dir = Path(sys.executable).parent
    if exe_dir != Path(sys.executable).resolve().parent:
        assert engine != Path(sys.executable).resolve().parent


def test_the_engine_bin_dir_holds_the_entry_point_when_it_exists() -> None:
    """The scripts dir names the ``ciao`` entry point wherever it was written.

    On a venv it always does; under a global install with no shim beside the
    interpreter this skips rather than asserting a shape the machine does not
    have, which is the very case the CI background test now covers.
    """
    engine_ciao = Path(engine_bin_dir()) / ("ciao.exe" if sys.platform == "win32" else "ciao")
    if not engine_ciao.exists():
        pytest.skip("no ciao entry point beside the test interpreter")
    assert shutil.which("ciao", path=engine_bin_dir()) is not None


def test_prepend_engine_path_puts_the_engine_first_and_keeps_the_rest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_path = os.pathsep.join(["/user/bin", "/other/bin"])
    monkeypatch.setenv("PATH", user_path)

    result = prepend_engine_path()

    entries = result.split(os.pathsep)
    assert entries[0] == engine_bin_dir()
    assert entries[1:] == ["/user/bin", "/other/bin"]


def test_prepend_engine_path_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Already-first (or repeated elsewhere) is not duplicated."""
    monkeypatch.setenv(
        "PATH", os.pathsep.join([engine_bin_dir(), "/user/bin", engine_bin_dir()])
    )

    entries = prepend_engine_path().split(os.pathsep)

    assert entries[0] == engine_bin_dir()
    assert entries[1:] == ["/user/bin"]


def test_prepend_engine_path_accepts_an_explicit_path() -> None:
    result = prepend_engine_path(os.pathsep.join(["/a", "/b"]))
    assert result.split(os.pathsep) == [engine_bin_dir(), "/a", "/b"]


# ── the chat-turn env builder (every provider) ─────────────────────────────


def test_chat_turn_env_prepends_the_engine_bin_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    decoy = tmp_path / "stale-local-bin"
    decoy.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoy), "/user/bin"]))

    extra_env = _chat_extra_env(tmp_path, monkeypatch)

    assert extra_env["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    # The user's own PATH survives behind the engine dir.
    assert extra_env["PATH"].split(os.pathsep)[1:] == [str(decoy), "/user/bin"]


@pytest.mark.parametrize("provider", ["claude", "opencode"])
def test_every_provider_chat_env_leads_with_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    monkeypatch.setenv("PATH", os.pathsep.join(["/stale", "/user/bin"]))

    extra_env = _chat_extra_env(tmp_path, monkeypatch, provider=provider)

    assert extra_env["PATH"].split(os.pathsep)[0] == engine_bin_dir()


def test_engine_ciao_resolves_first_despite_a_decoy_on_the_inherited_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale ``ciao`` earlier on the user's PATH must not win the turn.

    The decoy stands in for ``~/.local/bin/ciao`` pointing at an older install.
    """
    engine_ciao = Path(engine_bin_dir()) / (
        "ciao.exe" if sys.platform == "win32" else "ciao"
    )
    if not engine_ciao.exists():
        pytest.skip("no ciao entry point beside the test interpreter")

    decoy_dir = tmp_path / "stale-bin"
    decoy_dir.mkdir()
    decoy = decoy_dir / ("ciao.exe" if sys.platform == "win32" else "ciao")
    decoy.write_text("#!/bin/sh\necho 1.0.1\n")
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoy_dir), "/user/bin"]))

    extra_env = _chat_extra_env(tmp_path, monkeypatch)

    resolved = shutil.which("ciao", path=extra_env["PATH"])
    assert resolved is not None
    assert Path(resolved).resolve() == engine_ciao.resolve()


# ── per-provider spawn sites ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_sdk_env_carries_the_engine_first_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude passes ``request.extra_env`` straight to the SDK subprocess."""
    from ciao.providers.claude import ClaudeProvider

    captured: dict = {}

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

    monkeypatch.setenv("PATH", os.pathsep.join(["/stale", "/user/bin"]))
    monkeypatch.setattr(
        "ciao.providers.claude.get_bundled_claude_path", lambda: "/fake/claude"
    )
    monkeypatch.setattr("ciao.providers.claude.ClaudeSDKClient", FakeClient)

    manager = _make_manager(tmp_path)
    project = manager.create_project("path", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="claude")
    request = manager.build_agent_request(chat, prompt="hi")

    provider = ClaudeProvider(tmp_path)
    await provider._ensure_connected(request)

    assert captured["options"].env["PATH"].split(os.pathsep)[0] == engine_bin_dir()


@pytest.mark.asyncio
async def test_opencode_server_env_carries_the_engine_first_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opencode server subprocess inherits the turn env with PATH promoted."""
    from ciao.providers.opencode import OpencodeProvider

    monkeypatch.setenv("PATH", os.pathsep.join(["/stale", "/user/bin"]))

    class _NoOpTree:
        def __init__(self, pid: int, *, dies_with_engine: bool = False) -> None:
            self.pid = pid

        def terminate(self) -> None:
            pass

        def kill(self) -> None:
            pass

        def kill_descendants(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr("ciao.providers.opencode.ProcessTree", _NoOpTree)

    provider = OpencodeProvider(tmp_path)
    captured: dict = {}

    class FakeProcess:
        returncode = None
        pid = 4242
        stderr = None

        def terminate(self):
            self.returncode = 0

        async def wait(self):
            return 0

    async def fake_exec(*_args, **kwargs):
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(
        "ciao.providers.opencode.resolve_opencode_binary", lambda _env=None: "/bin/opencode"
    )
    monkeypatch.setattr("ciao.providers.opencode._free_port", lambda: 43123)
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    async def noop(*_args):
        return None

    monkeypatch.setattr(provider, "_await_health", noop)
    monkeypatch.setattr(provider, "_verify_contract", noop)

    manager = _make_manager(tmp_path)
    project = manager.create_project("path", workspace="personal")
    chat = manager.create_chat(project.project_id, provider="opencode")
    request = manager.build_agent_request(chat, prompt="hi")

    await provider._ensure_server(request)
    try:
        assert captured["env"]["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    finally:
        await provider.disconnect()


# ── one-shot env builder (both provider branches) ─────────────────────────


@pytest.mark.asyncio
async def test_oneshot_claude_env_carries_the_engine_first_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ciao.providers.oneshot as oneshot

    captured: dict = {}

    async def fake_query(*, prompt: str, options):
        captured["options"] = options
        if False:  # pragma: no cover - make this an async generator
            yield None

    monkeypatch.setattr(oneshot, "query", fake_query)
    monkeypatch.setenv("PATH", os.pathsep.join(["/stale", "/user/bin"]))

    await oneshot.run_oneshot("hi", system_prompt="s", model="haiku")

    assert (
        captured["options"].env["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    )


@pytest.mark.asyncio
async def test_oneshot_opencode_env_carries_the_engine_first_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import ciao.providers.opencode as opencode_mod
    import ciao.providers.oneshot as oneshot

    captured: dict = {}

    class FakeOpencodeProvider:
        def __init__(self, workspace_root, **_kw):
            pass

        async def run_streaming(self, request, register_handle):
            captured["request"] = request
            register_handle(None)
            if False:  # pragma: no cover - async generator
                yield None

        async def disconnect(self):
            pass

        async def delete_current_session(self):
            return True

    monkeypatch.setattr(
        opencode_mod, "OpencodeProvider", FakeOpencodeProvider
    )
    monkeypatch.setenv("PATH", os.pathsep.join(["/stale", "/user/bin"]))

    await oneshot.run_oneshot(
        "hi", system_prompt="s", model="x/y", provider="opencode", cwd=tmp_path
    )

    assert (
        captured["request"].extra_env["PATH"].split(os.pathsep)[0] == engine_bin_dir()
    )


# ── the shared helper is the single definition ────────────────────────────


def test_agent_request_default_extra_env_is_untouched() -> None:
    """A request built without the manager keeps an empty extra_env.

    The prepend lives in the env builders, not in the dataclass default, so a
    provider called directly in a test is not silently re-pointed.
    """
    request = AgentRequest(prompt="p", model="m", mode="normal")
    assert request.extra_env == {}


# ── bare `gws` resolves the workspace's Google account ─────────────────────


def test_chat_turn_env_points_bare_gws_at_the_linked_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    creds = tmp_path / "secrets" / "gws-personal"
    creds.mkdir(parents=True)
    (creds / "credentials.json").write_text("{}", encoding="utf-8")

    extra_env = _chat_extra_env(tmp_path, monkeypatch)

    assert extra_env["GWS_PROFILE"] == "personal"
    assert extra_env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] == str(creds.resolve())


def test_chat_turn_env_leaves_gws_config_unset_without_an_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra_env = _chat_extra_env(tmp_path, monkeypatch)

    assert extra_env["GWS_PROFILE"] == ""
    assert "GOOGLE_WORKSPACE_CLI_CONFIG_DIR" not in extra_env
