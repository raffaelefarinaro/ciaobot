"""Reusable full-chat runner for live Ciaobot evaluations."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import IO, Any, Iterable, Literal

# Validated at runtime against ciao.provider_registry; a plain str so adding a
# provider does not require editing a Literal.
Provider = str
Surface = Literal["legacy", "mcp"]


@dataclass(frozen=True, slots=True)
class ChatRunSpec:
    scenario: str
    prompt: str
    provider: Provider
    model: str
    surface: Surface
    turn_timeout_s: float


@dataclass(frozen=True, slots=True)
class ChatObservation:
    scenario: str
    selected_model: str
    effective_model: str
    final_text: str
    error: str
    elapsed_ms: int
    provider_duration_ms: int | None
    usage: dict[str, str]
    tokens: int | None
    provider_tools: tuple[str, ...]
    mcp_tools: tuple[str, ...]
    mcp_errors: int
    mcp_result_paths: tuple[str, ...] = ()
    mcp_tool_durations_ms: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedChat:
    """A chat prepared before benchmark fixtures, with their timing origin."""

    project_id: str
    project_name: str
    chat_id: str
    chat_title: str
    prepared_at: float


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> Any:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": base_url,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {path}: {exc}") from exc
    return json.loads(raw) if raw else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_packaged_assets(root: Path) -> None:
    """Install just the agent assets a normal Ciaobot workspace receives."""
    stock = resources.files("ciao.stock")
    for agent_dir in (".claude", ".agents"):
        destination = root / agent_dir / "skills"
        with resources.as_file(stock.joinpath("skills")) as source:
            shutil.copytree(source, destination, dirs_exist_ok=True)
    with resources.as_file(stock.joinpath("commands")) as source:
        shutil.copytree(source, root / ".claude" / "commands", dirs_exist_ok=True)


class IsolatedChatServer:
    """An isolated full-stack server used for live chat evaluation."""

    def __init__(
        self,
        *,
        root: Path,
        surface: Surface,
        provider: Provider,
        workspace_name: str,
        startup_timeout: float,
        install_packaged_assets: bool = True,
        require_subagent_synthesis: bool = False,
        subagent_discovery_polls: int = 3,
    ) -> None:
        self.root = root
        self.surface = surface
        self.provider = provider
        self.workspace_name = workspace_name
        self.startup_timeout = startup_timeout
        self.install_packaged_assets = install_packaged_assets
        self.require_subagent_synthesis = require_subagent_synthesis
        self.subagent_discovery_polls = max(1, subagent_discovery_polls)
        self.port = 0
        self.base_url = ""
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle: IO[bytes] | None = None

    def __enter__(self) -> IsolatedChatServer:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    @property
    def runtime(self) -> Path:
        return self.root / ".runtime"

    @property
    def is_running(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    def start(self) -> None:
        if self.is_running:
            return
        if self.process is not None or self._log_handle is not None:
            self.stop()
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.install_packaged_assets:
            _copy_packaged_assets(self.root)
        (self.root / "memory-vault" / self.workspace_name).mkdir(
            parents=True, exist_ok=True
        )
        env = dict(os.environ)
        python_bin = str(Path(sys.executable).parent)
        env.update(
            {
                "CIAO_WORKSPACE": str(self.root),
                "CIAO_RUNTIME_ROOT": str(self.runtime),
                "CIAO_VAULT_ROOT": str(self.root / "memory-vault"),
                "CIAO_MEMORY_DIR": str(self.root / ".memory"),
                "PWA_HOST": "127.0.0.1",
                "PWA_PORT": str(self.port),
                "PWA_AUTH_REQUIRED": "false",
                "CIAO_MCP_ENABLED": "true",
                "CIAO_CONTROL_SURFACE": self.surface,
                "CIAO_BENCHMARK_MODE": "true",
                "CIAO_AUTO_SYNC_ON_START": "false",
                "CIAO_AUTO_VAULT_INDEX": "false",
                "CIAO_INSIGHTS_DISABLED": "1",
                "CIAO_TRAJECTORIES_DISABLED": "1",
                "CIAO_SKILL_EVOLUTION_DISABLED": "1",
                "CIAO_GWS_HEALTH_INTERVAL": "0",
                "CIAO_NO_BROWSER": "1",
                "PATH": os.pathsep.join(
                    [python_bin, env.get("PATH", "")]
                ).rstrip(os.pathsep),
            }
        )
        log_path = self.root / "server.log"
        self._log_handle = log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "ciao.cli", "run"],
                cwd=self.root,
                env=env,
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            deadline = time.monotonic() + self.startup_timeout
            last_error = ""
            while time.monotonic() < deadline:
                process = self.process
                if process is not None and process.poll() is not None:
                    raise RuntimeError(
                        f"{self.surface} server exited with {process.returncode}; "
                        f"see {log_path}"
                    )
                try:
                    status = _json_request(self.base_url, "/api/mcp/status", timeout=2)
                    if status.get("enabled") and status.get("bound"):
                        return
                except RuntimeError as exc:
                    last_error = str(exc)
                time.sleep(0.25)
            raise RuntimeError(
                f"Timed out starting {self.surface} server: {last_error}; "
                f"see {log_path}"
            )
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)
        if self._log_handle is not None:
            self._log_handle.close()
        self.process = None
        self._log_handle = None

    def create_project(self, name: str) -> dict[str, Any]:
        result: dict[str, Any] = _json_request(
            self.base_url,
            "/api/projects",
            method="POST",
            payload={"name": name, "workspace": self.workspace_name},
        )
        return result

    def create_chat(
        self,
        project_id: str,
        *,
        title: str,
        provider: Provider,
        model: str,
        surface: Surface,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "provider": provider,
            "model": model,
            "mode": "bypass",
            "control_surface": surface,
        }
        result: dict[str, Any] = _json_request(
            self.base_url,
            f"/api/projects/{project_id}/chats",
            method="POST",
            payload=payload,
        )
        return result

    def prepare_chat(self, spec: ChatRunSpec) -> PreparedChat:
        label = f"Eval {spec.scenario}"
        project = self.create_project(label)
        chat = self.create_chat(
            project["project_id"],
            title=label,
            provider=spec.provider,
            model=spec.model,
            surface=spec.surface,
        )
        return PreparedChat(
            project_id=str(project["project_id"]),
            project_name=str(project["name"]),
            chat_id=str(chat["chat_id"]),
            chat_title=str(chat["title"]),
            prepared_at=time.perf_counter(),
        )

    def send(self, chat_id: str, prompt: str) -> None:
        _json_request(
            self.base_url,
            f"/api/chats/{chat_id}/prompt",
            method="POST",
            payload={"prompt": prompt},
        )

    def wait_for_turn(self, chat_id: str, timeout: float) -> list[dict[str, Any]]:
        if self.require_subagent_synthesis:
            return self._wait_for_subagent_synthesis(chat_id, timeout)

        deadline = time.monotonic() + timeout
        seen_active = False
        while time.monotonic() < deadline:
            active = _json_request(self.base_url, "/api/active-chats", timeout=5)
            ids = active.get("active_chat_ids") or []
            if chat_id in ids:
                seen_active = True
            else:
                if seen_active:
                    time.sleep(0.2)
                messages: list[dict[str, Any]] = _json_request(
                    self.base_url, f"/api/chats/{chat_id}/messages", timeout=15
                )
                if _has_terminal_assistant(messages):
                    return messages
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}")
            time.sleep(0.25)
        raise TimeoutError(f"chat {chat_id} did not finish within {timeout:.0f}s")

    def _wait_for_subagent_synthesis(
        self,
        chat_id: str,
        timeout: float,
    ) -> list[dict[str, Any]]:
        """Wait for persisted subagent completion and the parent follow-up.

        ``active-chats`` alone has an intentional watcher-to-drain gap. The
        subagent endpoint persists dispatch status, while chat history places
        the completion notification before the synthesis assistant message.
        Those two observable facts form the completion contract.
        """
        deadline = time.monotonic() + timeout
        assistant_baseline: tuple[str, ...] = ()
        saw_subagent = False
        stable_parent_signatures: tuple[str, ...] | None = None
        stable_parent_polls = 0
        while time.monotonic() < deadline:
            active = _json_request(self.base_url, "/api/active-chats", timeout=5)
            is_active = chat_id in (active.get("active_chat_ids") or [])
            messages: list[dict[str, Any]] = _json_request(
                self.base_url, f"/api/chats/{chat_id}/messages", timeout=15
            )
            subagents_raw = _json_request(
                self.base_url, f"/api/chats/{chat_id}/subagents", timeout=15
            )
            subagents = (
                [row for row in subagents_raw if isinstance(row, dict)]
                if isinstance(subagents_raw, list)
                else []
            )
            if subagents:
                saw_subagent = True
            async_subagents = [
                row for row in subagents if bool(row.get("is_async"))
            ]
            signatures = _assistant_signatures(messages)

            # A model may ignore the delegation instruction. Once the parent
            # is terminal and unchanged across a bounded set of polls, with no
            # persisted subagent record ever observed, return that response so
            # deterministic assertions can report the routing failure.
            if (
                not saw_subagent
                and not is_active
                and _has_terminal_assistant(messages)
            ):
                if signatures == stable_parent_signatures:
                    stable_parent_polls += 1
                else:
                    stable_parent_signatures = signatures
                    stable_parent_polls = 1
                if stable_parent_polls >= self.subagent_discovery_polls:
                    return messages
            else:
                stable_parent_signatures = None
                stable_parent_polls = 0

            # Foreground delegation completes inside the parent turn. There
            # is no between-turn synthesis to wait for. Require explicit
            # metadata on every entry so a briefly unclassified background
            # dispatch cannot be mistaken for foreground work.
            explicitly_foreground = bool(subagents) and all(
                row.get("is_async") is False
                and str(row.get("status") or "").casefold()
                in {"completed", "failed", "errored", "interrupted"}
                for row in subagents
            )
            if explicitly_foreground:
                if not is_active and _has_terminal_assistant(messages):
                    return messages

            if async_subagents:
                running = any(
                    str(row.get("status") or "").casefold()
                    not in {"completed", "failed", "errored", "interrupted"}
                    for row in async_subagents
                )
                if running:
                    # This is the interim parent sequence. A valid synthesis
                    # must add a later assistant response.
                    assistant_baseline = signatures
                elif not is_active:
                    if _has_assistant_after_subagent_completion(messages):
                        return messages
                    if (
                        assistant_baseline
                        and signatures != assistant_baseline
                        and len(signatures) > len(assistant_baseline)
                    ):
                        return messages
                    # Codex keeps the parent collaboration turn open until its
                    # children settle; it does not use Claude's nudge/drain.
                    if self.provider == "codex" and _has_terminal_assistant(messages):
                        return messages

            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}")
            time.sleep(0.25)
        raise TimeoutError(
            f"chat {chat_id} did not produce a post-subagent synthesis "
            f"within {timeout:.0f}s"
        )


def _read_jsonl(path: Path, chat_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("chat_id") == chat_id:
            rows.append(row)
    return rows


def _read_jsonl_after(path: Path, chat_id: str, *, offset: int) -> list[dict[str, Any]]:
    """Return chat_id-matching rows appended to ``path`` after ``offset``.

    Used to isolate telemetry to a single measured turn: warm and multi-turn
    scenarios reuse the same chat, so reading from byte zero would mix in
    rows from warm-up and earlier turns and let required-tool or
    memory-source assertions pass even when the measured turn never acted.
    The telemetry writers append one JSON object per line, so byte offset
    matches line offset for these files.
    """
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if offset < 0 or offset >= size:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("chat_id") == chat_id:
                    rows.append(row)
    except OSError:
        return []
    return rows


def _jsonl_offset(path: Path) -> int:
    """Return the byte offset where new ``path`` writes would start, or 0."""
    if not path.exists():
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _has_terminal_assistant(messages: Iterable[dict[str, Any]]) -> bool:
    return any(
        row.get("role") == "assistant"
        and (
            row.get("duration_ms") is not None
            or bool(row.get("is_error"))
            or bool(row.get("effective_model"))
            or isinstance(row.get("usage"), dict)
        )
        for row in messages
    )


def _assistant_signatures(
    messages: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    return tuple(
        str(row.get("content") or "")
        for row in messages
        if row.get("role") == "assistant"
    )


def _has_assistant_after_subagent_completion(
    messages: Iterable[dict[str, Any]],
) -> bool:
    completion_seen = False
    for row in messages:
        content = str(row.get("content") or "")
        if (
            row.get("role") == "system"
            and content.startswith("🤖 ")
            and re.search(
                r"\b(?:completed|finished|done|succeeded|failed)\b",
                content,
                re.IGNORECASE,
            )
        ):
            completion_seen = True
            continue
        if completion_seen and row.get("role") == "assistant" and content.strip():
            return True
    return False


def _assistant_result(
    messages: Iterable[dict[str, Any]],
) -> tuple[str, str, int | None, dict[str, str], bool]:
    assistant = [row for row in messages if row.get("role") == "assistant"]
    if not assistant:
        return "", "", None, {}, False
    terminal = assistant[-1]
    duration = terminal.get("duration_ms")
    usage_raw = terminal.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    return (
        str(terminal.get("content") or ""),
        str(terminal.get("effective_model") or ""),
        int(duration) if isinstance(duration, (int, float)) else None,
        {str(k): str(v) for k, v in usage.items()},
        bool(terminal.get("is_error")),
    )


def token_count(provider: Provider, usage: dict[str, str]) -> int | None:
    """Return a comparable provider-appropriate total, preserving raw usage."""
    try:
        keys: tuple[str, ...]
        if provider == "claude":
            keys = (
                "input_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                "output_tokens",
            )
        else:
            # Codex cached input is a subset of input_tokens.
            keys = ("input_tokens", "output_tokens")
        values = [int(usage[key]) for key in keys if key in usage]
    except (TypeError, ValueError):
        return None
    return sum(values) if values else None


def run_chat_turn(
    server: IsolatedChatServer,
    spec: ChatRunSpec,
    *,
    prepared_chat: PreparedChat | None = None,
) -> ChatObservation:
    """Run and observe one turn, preserving direct and benchmark timing contracts.

    Declarative two-argument runs measure prompt dispatch through terminal
    observation. Prepared benchmark runs begin at ``PreparedChat.prepared_at``
    so their existing wall-time metric continues to include fixture work.
    """
    owns_server = not server.is_running
    started: float | None = None
    messages: list[dict[str, Any]] = []
    error = ""
    chat_id = ""
    provider_error = False
    completed = False
    agent_offset = 0
    mcp_offset = 0
    try:
        try:
            if owns_server:
                server.start()
            prepared = prepared_chat or server.prepare_chat(spec)
            chat_id = prepared.chat_id
            started = (
                prepared.prepared_at
                if prepared_chat is not None
                else time.perf_counter()
            )
            runtime = server.runtime
            agent_offset = _jsonl_offset(runtime / "agent_tool_calls.jsonl")
            mcp_offset = _jsonl_offset(runtime / "mcp_tool_calls.jsonl")
            server.send(chat_id, spec.prompt)
            messages = server.wait_for_turn(chat_id, spec.turn_timeout_s)
        except Exception as exc:  # noqa: BLE001 - evaluations preserve failed runs
            error = str(exc)

        observed_at = time.perf_counter()
        final_text, effective_model, duration_ms, usage, provider_error = (
            _assistant_result(messages)
        )
        if provider_error:
            error = error or final_text or "provider error"
        runtime = server.runtime
        provider_rows = _read_jsonl_after(
            runtime / "agent_tool_calls.jsonl", chat_id, offset=agent_offset
        )
        mcp_rows = _read_jsonl_after(
            runtime / "mcp_tool_calls.jsonl", chat_id, offset=mcp_offset
        )
        elapsed_ms = (
            int((observed_at - started) * 1000)
            if started is not None
            else 0
        )
        observation = ChatObservation(
            scenario=spec.scenario,
            selected_model=spec.model,
            effective_model=effective_model or spec.model,
            final_text=final_text,
            error=error,
            elapsed_ms=elapsed_ms,
            provider_duration_ms=duration_ms,
            usage=usage,
            tokens=token_count(spec.provider, usage),
            provider_tools=tuple(
                str(row.get("tool") or "") for row in provider_rows
            ),
            mcp_tools=tuple(str(row.get("tool") or "") for row in mcp_rows),
            mcp_errors=sum(row.get("status") != "ok" for row in mcp_rows),
            mcp_result_paths=tuple(
                str(path)
                for row in mcp_rows
                for path in (row.get("result_paths") or [])
                if isinstance(path, str)
            ),
            mcp_tool_durations_ms=tuple(
                int(row.get("duration_ms") or 0)
                for row in mcp_rows
                if isinstance(row.get("duration_ms"), (int, float))
            ),
        )
        completed = True
        return observation
    finally:
        if owns_server or error or provider_error or not completed:
            server.stop()
