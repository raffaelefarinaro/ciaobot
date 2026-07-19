"""Paired live benchmark for Ciaobot's legacy and MCP control surfaces.

The runner starts two isolated Ciaobot servers per provider and submits each
scenario to both at the same time.  It records wall time, provider-reported
tokens, provider tool events, MCP calls, durable-state correctness, and surface
compliance.  No synthetic model responses are used.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any, Callable, Iterable, Literal

from ciao.loops import LoopStore
from ciao.memory_tool import add_entry, load_entries, memory_path, remove_entry
from ciao.schedules import ScheduleStore
from ciao.control_surfaces import load_decision, write_decision


Surface = Literal["legacy", "mcp"]
Provider = Literal["claude", "codex"]
Validator = Callable[["RunContext", str], tuple[bool, str]]
Fixture = Callable[["RunContext"], None]
Cleanup = Callable[["RunContext"], None]


@dataclass(slots=True)
class Scenario:
    name: str
    description: str
    prompt: Callable[["RunContext"], str]
    validate: Validator
    fixture: Fixture = lambda _ctx: None
    cleanup: Cleanup = lambda _ctx: None


@dataclass(slots=True)
class RunContext:
    provider: Provider
    surface: Surface
    repeat: int
    marker: str
    server: "ArmServer"
    project_id: str
    project_name: str
    chat_id: str
    chat_title: str

    @property
    def root(self) -> Path:
        return self.server.root

    @property
    def runtime(self) -> Path:
        return self.root / ".runtime"

    @property
    def vault(self) -> Path:
        return self.root / "memory-vault" / self.server.workspace_name

    @property
    def memory_file(self) -> Path:
        return memory_path(self.root / ".memory")


@dataclass(slots=True)
class RunResult:
    provider: Provider
    surface: Surface
    scenario: str
    repeat: int
    marker: str
    chat_id: str
    correct: bool
    validation: str
    surface_compliant: bool
    elapsed_ms: int
    duration_ms: int | None
    usage: dict[str, str]
    tokens: int | None
    provider_tools: list[str]
    mcp_tools: list[str]
    mcp_errors: int
    final_text: str
    error: str = ""
    provider_blocked: bool = False
    provider_block_reason: str = ""


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


class ArmServer:
    """One isolated full-stack server used by a benchmark arm."""

    def __init__(
        self,
        *,
        root: Path,
        surface: Surface,
        provider: Provider,
        workspace_name: str,
        startup_timeout: float,
    ) -> None:
        self.root = root
        self.surface = surface
        self.provider = provider
        self.workspace_name = workspace_name
        self.startup_timeout = startup_timeout
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _copy_packaged_assets(self.root)
        (self.root / "memory-vault" / self.workspace_name).mkdir(
            parents=True, exist_ok=True
        )
        env = dict(os.environ)
        python_bin = str(Path(sys.executable).parent)
        env.update(
            {
                "CIAO_WORKSPACE": str(self.root),
                "CIAO_RUNTIME_ROOT": str(self.root / ".runtime"),
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
                "CIAO_OLLAMA_LOCAL_DISCOVERY": "false",
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
            if self.process.poll() is not None:
                raise RuntimeError(
                    f"{self.surface} server exited with {self.process.returncode}; "
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
            f"Timed out starting {self.surface} server: {last_error}; see {log_path}"
        )

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
        return _json_request(
            self.base_url,
            "/api/projects",
            method="POST",
            payload={"name": name, "workspace": self.workspace_name},
        )

    def create_chat(
        self,
        project_id: str,
        *,
        title: str,
        model: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": title,
            "provider": self.provider,
            "mode": "bypass",
            "control_surface": self.surface,
        }
        if model:
            payload["model"] = model
        if self.provider == "claude":
            payload["model_bucket"] = "work"
        return _json_request(
            self.base_url,
            f"/api/projects/{project_id}/chats",
            method="POST",
            payload=payload,
        )

    def send(self, chat_id: str, prompt: str) -> None:
        _json_request(
            self.base_url,
            f"/api/chats/{chat_id}/prompt",
            method="POST",
            payload={"prompt": prompt},
        )

    def wait_for_turn(self, chat_id: str, timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        seen_active = False
        while time.monotonic() < deadline:
            active = _json_request(self.base_url, "/api/active-chats", timeout=5)
            ids = active.get("active_chat_ids") or []
            if chat_id in ids:
                seen_active = True
            elif seen_active:
                # Give the provider transcript writer one moment to flush.
                time.sleep(0.2)
                return _json_request(
                    self.base_url, f"/api/chats/{chat_id}/messages", timeout=15
                )
            elif self.process is not None and self.process.poll() is not None:
                raise RuntimeError(f"server exited with {self.process.returncode}")
            time.sleep(0.25)
        raise TimeoutError(f"chat {chat_id} did not finish within {timeout:.0f}s")


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


def _assistant_result(messages: Iterable[dict[str, Any]]) -> tuple[str, int | None, dict[str, str]]:
    assistant = [row for row in messages if row.get("role") == "assistant"]
    if not assistant:
        return "", None, {}
    terminal = next(
        (row for row in reversed(assistant) if row.get("duration_ms") is not None),
        assistant[-1],
    )
    duration = terminal.get("duration_ms")
    usage = terminal.get("usage") if isinstance(terminal.get("usage"), dict) else {}
    return (
        str(terminal.get("content") or ""),
        int(duration) if isinstance(duration, (int, float)) else None,
        {str(k): str(v) for k, v in usage.items()},
    )


def token_count(provider: Provider, usage: dict[str, str]) -> int | None:
    """Return a comparable provider-appropriate total, preserving raw usage too."""
    try:
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


_PROVIDER_BLOCK_PATTERNS = (
    ("monthly spend limit", "provider monthly spend limit"),
    ("workspace is out of credits", "provider workspace is out of credits"),
    ("organization is out of credits", "provider organization is out of credits"),
    ("org is out of credits", "provider organization is out of credits"),
)


def _provider_block_reason(ctx: RunContext, final_text: str, error: str) -> str:
    """Classify hard account/provider blocks without treating them as arm defects."""
    evidence = [final_text, error]
    transcript = ctx.runtime / "transcripts" / ctx.chat_id / f"{ctx.provider}.json"
    try:
        evidence.append(transcript.read_text(encoding="utf-8"))
    except OSError:
        pass
    combined = "\n".join(evidence).lower()
    for pattern, reason in _PROVIDER_BLOCK_PATTERNS:
        if pattern in combined:
            return reason
    return ""


def _memory_contains(ctx: RunContext, value: str) -> bool:
    return any(value == entry for entry in load_entries(ctx.memory_file))


def _memory_cleanup(ctx: RunContext) -> None:
    for entry in list(load_entries(ctx.memory_file)):
        if ctx.marker in entry:
            remove_entry(ctx.memory_file, entry, char_limit=20_000)


def _state_json(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    rows = payload.get(key, [])
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def scenarios() -> list[Scenario]:
    """The fixed representative suite used for release decisions."""

    def done(ctx: RunContext) -> str:
        return f"DONE:{ctx.marker}"

    def marker_in_output(ctx: RunContext, output: str) -> tuple[bool, str]:
        ok = ctx.marker in output
        return ok, "marker returned" if ok else "expected marker missing from final answer"

    def seed_replace(ctx: RunContext) -> None:
        add_entry(ctx.memory_file, f"old-{ctx.marker}", char_limit=20_000)

    def validate_replace(ctx: RunContext, _output: str) -> tuple[bool, str]:
        old = _memory_contains(ctx, f"old-{ctx.marker}")
        new = _memory_contains(ctx, f"new-{ctx.marker}")
        return (new and not old, f"new={new}, old={old}")

    def seed_remove(ctx: RunContext) -> None:
        add_entry(ctx.memory_file, f"remove-{ctx.marker}", char_limit=20_000)

    def validate_removed(ctx: RunContext, _output: str) -> tuple[bool, str]:
        absent = not _memory_contains(ctx, f"remove-{ctx.marker}")
        return absent, "entry removed" if absent else "entry still present"

    def seed_memory_read(ctx: RunContext) -> None:
        add_entry(ctx.memory_file, f"recall-{ctx.marker}", char_limit=20_000)

    def seed_vault(ctx: RunContext) -> None:
        path = ctx.vault / "benchmark" / f"{ctx.marker}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Benchmark\n\nneedle-{ctx.marker} alpine-orchid-{ctx.marker}\n",
            encoding="utf-8",
        )

    def validate_vault_write(ctx: RunContext, _output: str) -> tuple[bool, str]:
        path = ctx.vault / "benchmark" / f"write-{ctx.marker}.md"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        ok = f"vault-content-{ctx.marker}" in content
        return ok, "vault note written" if ok else "vault note/content missing"

    def validate_workspace_file(ctx: RunContext, output: str) -> tuple[bool, str]:
        path = ctx.root / "benchmark-output" / f"{ctx.marker}.txt"
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        ok = content == f"workspace-content-{ctx.marker}" and ctx.marker in output
        return ok, "workspace round-trip passed" if ok else "file or returned marker mismatch"

    def validate_project_chat(ctx: RunContext, _output: str) -> tuple[bool, str]:
        path = ctx.runtime / "web_projects.json"
        if not path.exists():
            return False, "project registry missing"
        payload = json.loads(path.read_text(encoding="utf-8"))
        projects = payload.get("projects", {})
        chats = payload.get("chats", {})
        project_entry = next(
            (
                (project_id, project)
                for project_id, project in projects.items()
                if project.get("name") == f"Project {ctx.marker}"
            ),
            None,
        )
        if project_entry is None:
            return False, "created project missing"
        # The durable registry stores IDs as mapping keys; ``to_dict()`` adds
        # them only at the API boundary. Validate the persisted representation
        # directly so a successfully-created child chat is not misclassified.
        project_id, _project = project_entry
        chat = next(
            (
                c
                for c in chats.values()
                if c.get("project_id") == project_id
                and c.get("title") == f"Chat {ctx.marker}"
            ),
            None,
        )
        return (chat is not None, "project and chat created" if chat else "child chat missing")

    def seed_schedule(ctx: RunContext) -> None:
        ScheduleStore(ctx.runtime, include_system=False).create(
            daily_time_utc="09:00",
            prompt=f"fixture-{ctx.marker}",
            model="",
            mode="auto",
            chat_id=0,
            frequency="manual",
            workspace=ctx.server.workspace_name,
            web_project_id=ctx.project_id,
            title=f"Schedule {ctx.marker}",
        )

    def validate_schedule_create(ctx: RunContext, _output: str) -> tuple[bool, str]:
        rows = _state_json(ctx.runtime / "schedules.json", "schedules")
        matches = [row for row in rows if row.get("title") == f"Created {ctx.marker}"]
        ok = len(matches) == 1 and matches[0].get("workspace") == ctx.server.workspace_name
        return ok, f"matching schedules={len(matches)}"

    def validate_loop_create(ctx: RunContext, _output: str) -> tuple[bool, str]:
        rows = _state_json(ctx.runtime / "loops.json", "loops")
        matches = [row for row in rows if row.get("title") == f"Loop {ctx.marker}"]
        ok = (
            len(matches) == 1
            and matches[0].get("web_chat_id") == ctx.chat_id
            and int(matches[0].get("interval_minutes") or 0) == 17
        )
        return ok, f"matching loops={len(matches)}"

    return [
        Scenario(
            "memory_add",
            "Add one bounded durable memory entry.",
            lambda ctx: (
                "Use the Ciaobot control surface assigned to this session to add exactly "
                f"this durable memory entry: add-{ctx.marker}. Do not edit files by hand "
                f"when a Ciaobot tool is available. Finish with {done(ctx)}"
            ),
            lambda ctx, _out: (
                _memory_contains(ctx, f"add-{ctx.marker}"),
                "entry present" if _memory_contains(ctx, f"add-{ctx.marker}") else "entry missing",
            ),
            cleanup=_memory_cleanup,
        ),
        Scenario(
            "memory_replace",
            "Replace a uniquely selected bounded-memory entry.",
            lambda ctx: (
                "Use the assigned Ciaobot control surface to replace memory entry "
                f"old-{ctx.marker} with new-{ctx.marker}. Finish with {done(ctx)}"
            ),
            validate_replace,
            fixture=seed_replace,
            cleanup=_memory_cleanup,
        ),
        Scenario(
            "memory_remove",
            "Remove a uniquely selected bounded-memory entry.",
            lambda ctx: (
                "Use the assigned Ciaobot control surface to remove memory entry "
                f"remove-{ctx.marker}. Finish with {done(ctx)}"
            ),
            validate_removed,
            fixture=seed_remove,
            cleanup=_memory_cleanup,
        ),
        Scenario(
            "memory_read",
            "Re-read bounded memory rather than relying on the injected snapshot.",
            lambda ctx: (
                "Re-read current Ciaobot durable memory through the assigned control surface; "
                f"do not rely only on injected context. Report recall-{ctx.marker} and finish "
                f"with {done(ctx)}"
            ),
            marker_in_output,
            fixture=seed_memory_read,
            cleanup=_memory_cleanup,
        ),
        Scenario(
            "vault_read",
            "Read a known vault-relative markdown note.",
            lambda ctx: (
                "Read the vault note at "
                f"benchmark/{ctx.marker}.md through the assigned surface and report "
                f"needle-{ctx.marker}. Finish with {done(ctx)}"
            ),
            marker_in_output,
            fixture=seed_vault,
        ),
        Scenario(
            "vault_search",
            "Search the active workspace vault.",
            lambda ctx: (
                "Search the active Ciaobot vault through the assigned surface for "
                f"alpine-orchid-{ctx.marker}; report the matching path and finish with {done(ctx)}"
            ),
            marker_in_output,
            fixture=seed_vault,
        ),
        Scenario(
            "vault_write",
            "Write a vault-relative markdown note.",
            lambda ctx: (
                "Write a Ciaobot vault note at "
                f"benchmark/write-{ctx.marker}.md containing exactly vault-content-{ctx.marker}. "
                f"Use the assigned surface and finish with {done(ctx)}"
            ),
            validate_vault_write,
        ),
        Scenario(
            "workspace_file_roundtrip",
            "Write and read a safe workspace text file.",
            lambda ctx: (
                "Through the assigned Ciaobot surface, write "
                f"benchmark-output/{ctx.marker}.txt with exactly workspace-content-{ctx.marker}, "
                f"then read it back and finish with {done(ctx)}"
            ),
            validate_workspace_file,
        ),
        Scenario(
            "project_chat_create",
            "Create PWA project and chat records through application managers.",
            lambda ctx: (
                f"Create a Ciaobot project named Project {ctx.marker} in the current workspace, "
                f"then create an empty chat titled Chat {ctx.marker} inside it. Do not send a "
                f"message to the new chat. Use the assigned surface and finish with {done(ctx)}"
            ),
            validate_project_chat,
        ),
        Scenario(
            "schedule_list",
            "List native schedules in the current workspace.",
            lambda ctx: (
                "List current Ciaobot schedules through the assigned surface and report the "
                f"schedule titled Schedule {ctx.marker}. Finish with {done(ctx)}"
            ),
            marker_in_output,
            fixture=seed_schedule,
        ),
        Scenario(
            "schedule_create",
            "Create a validated native manual schedule.",
            lambda ctx: (
                "The user has explicitly approved creation: create one Ciaobot manual schedule "
                f"titled Created {ctx.marker}, prompt schedule-prompt-{ctx.marker}, targeted to "
                "the current project, timezone UTC, archive policy manual. Use the assigned "
                f"surface and finish with {done(ctx)}"
            ),
            validate_schedule_create,
        ),
        Scenario(
            "loop_create",
            "Create a native loop bound to the current chat.",
            lambda ctx: (
                "The user has explicitly approved creation: create one stopped Ciaobot loop "
                f"titled Loop {ctx.marker}, bound to this current chat, interval 17 minutes, "
                f"prompt loop-prompt-{ctx.marker}, autostart false. Use the assigned surface and "
                f"finish with {done(ctx)}"
            ),
            validate_loop_create,
        ),
    ]


def _run_one(ctx: RunContext, scenario: Scenario, *, timeout: float) -> RunResult:
    started = time.perf_counter()
    messages: list[dict[str, Any]] = []
    error = ""
    try:
        scenario.fixture(ctx)
        ctx.server.send(ctx.chat_id, scenario.prompt(ctx))
        messages = ctx.server.wait_for_turn(ctx.chat_id, timeout)
    except Exception as exc:  # noqa: BLE001 - benchmark must preserve failed runs
        error = str(exc)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    final_text, duration_ms, usage = _assistant_result(messages)
    provider_block_reason = _provider_block_reason(ctx, final_text, error)
    provider_rows = _read_jsonl(ctx.runtime / "agent_tool_calls.jsonl", ctx.chat_id)
    mcp_rows = _read_jsonl(ctx.runtime / "mcp_tool_calls.jsonl", ctx.chat_id)
    try:
        correct, validation = scenario.validate(ctx, final_text)
    except Exception as exc:  # noqa: BLE001
        correct, validation = False, f"validator error: {exc}"
    if error:
        correct = False
        validation = f"{validation}; execution error: {error}"
    surface_compliant = not mcp_rows if ctx.surface == "legacy" else bool(mcp_rows)
    result = RunResult(
        provider=ctx.provider,
        surface=ctx.surface,
        scenario=scenario.name,
        repeat=ctx.repeat,
        marker=ctx.marker,
        chat_id=ctx.chat_id,
        correct=correct,
        validation=validation,
        surface_compliant=surface_compliant,
        elapsed_ms=elapsed_ms,
        duration_ms=duration_ms,
        usage=usage,
        tokens=token_count(ctx.provider, usage),
        provider_tools=[str(row.get("tool") or "") for row in provider_rows],
        mcp_tools=[str(row.get("tool") or "") for row in mcp_rows],
        mcp_errors=sum(row.get("status") != "ok" for row in mcp_rows),
        final_text=final_text,
        error=error,
        provider_blocked=bool(provider_block_reason),
        provider_block_reason=provider_block_reason,
    )
    try:
        scenario.cleanup(ctx)
    except Exception:
        pass
    return result


def _median(values: list[int]) -> float | None:
    return float(statistics.median(values)) if values else None


def _mean(values: list[int]) -> float | None:
    return float(statistics.fmean(values)) if values else None


def summarize(results: list[RunResult]) -> dict[str, Any]:
    """Aggregate measurements and apply the documented release rule."""
    providers = sorted({row.provider for row in results})
    report: dict[str, Any] = {"providers": {}, "decision_rule": {
        "eligibility": "correctness >= 95% and surface compliance >= 95%",
        "score": "60 correctness + 10 compliance + 15 latency + 10 tokens + 5 tool efficiency",
        "winner_margin": "3 score points; otherwise tie",
    }}
    for provider in providers:
        arms: dict[str, dict[str, Any]] = {}
        provider_rows = [row for row in results if row.provider == provider]
        blocked_pairs = {
            (row.scenario, row.repeat)
            for row in provider_rows
            if row.provider_blocked
        }
        for surface in ("legacy", "mcp"):
            rows = [row for row in provider_rows if row.surface == surface]
            evaluated = [
                row for row in rows
                if (row.scenario, row.repeat) not in blocked_pairs
            ]
            elapsed = [row.elapsed_ms for row in evaluated]
            durations = [row.duration_ms for row in evaluated if row.duration_ms is not None]
            tokens = [row.tokens for row in evaluated if row.tokens is not None]
            calls = [len(row.provider_tools) for row in evaluated]
            arms[surface] = {
                "runs": len(rows),
                "evaluated_runs": len(evaluated),
                "provider_blocked_runs": sum(row.provider_blocked for row in rows),
                "correct": sum(row.correct for row in evaluated),
                "correctness_rate": sum(row.correct for row in evaluated) / len(evaluated) if evaluated else 0.0,
                "surface_compliance_rate": sum(row.surface_compliant for row in evaluated) / len(evaluated) if evaluated else 0.0,
                "median_elapsed_ms": _median(elapsed),
                "mean_elapsed_ms": _mean(elapsed),
                "median_provider_duration_ms": _median(durations),
                "mean_tokens": _mean(tokens),
                "mean_provider_tool_calls": _mean(calls),
                "mcp_calls": sum(len(row.mcp_tools) for row in evaluated),
                "mcp_errors": sum(row.mcp_errors for row in evaluated),
                "failures": [
                    {"scenario": row.scenario, "repeat": row.repeat, "reason": row.validation}
                    for row in evaluated
                    if not row.correct or not row.surface_compliant
                ],
            }
        best_latency = min(
            value for value in (arms[s]["median_elapsed_ms"] for s in arms) if value is not None
        ) if any(arms[s]["median_elapsed_ms"] is not None for s in arms) else None
        best_tokens = min(
            value for value in (arms[s]["mean_tokens"] for s in arms) if value is not None
        ) if any(arms[s]["mean_tokens"] is not None for s in arms) else None
        best_tools = min(
            value for value in (arms[s]["mean_provider_tool_calls"] for s in arms) if value is not None
        ) if any(arms[s]["mean_provider_tool_calls"] is not None for s in arms) else None
        eligible: list[str] = []
        for surface, metrics in arms.items():
            latency_eff = (
                best_latency / metrics["median_elapsed_ms"]
                if best_latency is not None and metrics["median_elapsed_ms"]
                else 0.0
            )
            token_eff = (
                best_tokens / metrics["mean_tokens"]
                if best_tokens is not None and metrics["mean_tokens"]
                else 0.0
            )
            tool_eff = (
                best_tools / metrics["mean_provider_tool_calls"]
                if best_tools is not None and metrics["mean_provider_tool_calls"]
                else (1.0 if best_tools == metrics["mean_provider_tool_calls"] else 0.0)
            )
            metrics["score"] = round(
                60 * metrics["correctness_rate"]
                + 10 * metrics["surface_compliance_rate"]
                + 15 * latency_eff
                + 10 * token_eff
                + 5 * tool_eff,
                2,
            )
            metrics["eligible"] = (
                metrics["correctness_rate"] >= 0.95
                and metrics["surface_compliance_rate"] >= 0.95
            )
            if metrics["eligible"]:
                eligible.append(surface)
        if blocked_pairs:
            winner = "blocked"
            reason = (
                f"{len(blocked_pairs)} paired scenario run(s) were excluded by a hard "
                "provider/account block; rerun is required"
            )
        elif len(eligible) == 1:
            winner = eligible[0]
            reason = "only arm meeting correctness and surface-compliance gates"
        elif len(eligible) == 2:
            delta = arms["mcp"]["score"] - arms["legacy"]["score"]
            if abs(delta) < 3:
                winner, reason = "tie", f"score difference {abs(delta):.2f} < 3"
            else:
                winner = "mcp" if delta > 0 else "legacy"
                reason = f"score advantage {abs(delta):.2f} points"
        else:
            winner, reason = "no-decision", "neither arm met the correctness/compliance gates"
        report["providers"][provider] = {
            "arms": arms,
            "winner": winner,
            "reason": reason,
            "blocked_pairs": [
                {"scenario": scenario, "repeat": repeat}
                for scenario, repeat in sorted(blocked_pairs)
            ],
        }
    return report


def render_markdown(summary: dict[str, Any], results: list[RunResult]) -> str:
    lines = [
        "# Ciaobot control-surface benchmark",
        "",
        f"Generated: {datetime.now(UTC).isoformat()}",
        "",
        "Paired live turns compare the legacy CLI/direct-file/API path with the authenticated MCP path.",
        "",
        "| Provider | Arm | Correct | Compliance | Median wall | Mean tokens | Mean tools | Score | Decision |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for provider, provider_data in summary.get("providers", {}).items():
        for surface, metrics in provider_data["arms"].items():
            decision = provider_data["winner"] if surface == "mcp" else ""
            lines.append(
                "| {provider} | {surface} | {correct}/{runs} ({correctness:.1%}) | "
                "{compliance:.1%} | {elapsed:.0f} ms | {tokens} | {tools} | {score:.2f} | {decision} |".format(
                    provider=provider,
                    surface=surface,
                    correct=metrics["correct"],
                    runs=metrics["evaluated_runs"],
                    correctness=metrics["correctness_rate"],
                    compliance=metrics["surface_compliance_rate"],
                    elapsed=metrics["median_elapsed_ms"] or 0,
                    tokens=(
                        f"{metrics['mean_tokens']:.0f}"
                        if metrics["mean_tokens"] is not None
                        else "n/a"
                    ),
                    tools=(
                        f"{metrics['mean_provider_tool_calls']:.2f}"
                        if metrics["mean_provider_tool_calls"] is not None
                        else "n/a"
                    ),
                    score=metrics["score"],
                    decision=decision,
                )
            )
        lines.extend(
            [
                "",
                f"**{provider} decision:** `{provider_data['winner']}` — {provider_data['reason']}.",
                "",
            ]
        )
    failures = [
        row
        for row in results
        if not row.provider_blocked
        and (not row.correct or not row.surface_compliant or row.error)
    ]
    blocked = [row for row in results if row.provider_blocked]
    lines.extend(["## Failed or non-compliant runs", ""])
    if not failures:
        lines.append("None.")
    else:
        for row in failures:
            lines.append(
                f"- `{row.provider}/{row.surface}/{row.scenario}#{row.repeat}`: "
                f"{row.validation}; compliant={row.surface_compliant}"
            )
    lines.extend(
        [
            "",
            "## External provider blocks",
            "",
        ]
    )
    if not blocked:
        lines.append("None.")
    else:
        for row in blocked:
            lines.append(
                f"- `{row.provider}/{row.surface}/{row.scenario}#{row.repeat}`: "
                f"{row.provider_block_reason}"
            )
    lines.extend(
        [
            "",
            "## Decision rule",
            "",
            "An arm must reach at least 95% state/output correctness and 95% surface compliance. "
            "Eligible arms are scored out of 100: correctness 60, compliance 10, median wall-time "
            "efficiency 15, token efficiency 10, and provider-tool-call efficiency 5. A lead below "
            "3 points is a tie. A hard provider/account block excludes both members of that pair "
            "from metrics and blocks promotion until they are rerun. Raw usage fields and every "
            "run remain in `results.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _selected_scenarios(names: list[str], smoke: bool) -> list[Scenario]:
    catalog = scenarios()
    if names:
        requested = set(names)
        unknown = sorted(requested - {item.name for item in catalog})
        if unknown:
            raise ValueError(f"Unknown scenarios: {', '.join(unknown)}")
        return [item for item in catalog if item.name in requested]
    if smoke:
        smoke_names = {"memory_add", "vault_read", "schedule_create"}
        return [item for item in catalog if item.name in smoke_names]
    return catalog


def _write_report(output: Path, results: list[RunResult], summary: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "summary": summary,
                "runs": [asdict(row) for row in results],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (output / "REPORT.md").write_text(
        render_markdown(summary, results), encoding="utf-8"
    )


def promote_decision(
    *,
    workspace: Path,
    output: Path,
    summary: dict[str, Any],
    results: list[RunResult],
    selected_scenarios: int,
    repeats: int,
    smoke: bool,
) -> Path:
    """Persist only release-grade, decisive provider results."""
    if smoke or selected_scenarios != 12 or repeats < 5:
        raise ValueError(
            "Refusing to promote a partial benchmark: all 12 scenarios and at "
            "least 5 repeats are required."
        )
    provider_records: dict[str, Any] = {}
    for provider, provider_data in summary.get("providers", {}).items():
        winner = provider_data.get("winner")
        if winner not in {"legacy", "mcp"}:
            raise ValueError(
                f"Refusing to promote {provider}: result is {winner!r}, not a decisive winner."
            )
        expected_runs = selected_scenarios * repeats * 2
        actual_runs = sum(row.provider == provider for row in results)
        if actual_runs != expected_runs:
            raise ValueError(
                f"Refusing to promote {provider}: expected {expected_runs} runs, got {actual_runs}."
            )
        provider_records[provider] = {
            "winner": winner,
            "reason": provider_data.get("reason", ""),
            "legacy": provider_data["arms"]["legacy"],
            "mcp": provider_data["arms"]["mcp"],
        }
    existing = load_decision(workspace)
    existing_providers = existing.get("providers")
    merged = dict(existing_providers) if isinstance(existing_providers, dict) else {}
    merged.update(provider_records)
    payload = {
        "schema_version": 1,
        "promoted_at": datetime.now(UTC).isoformat(),
        "source_report": str(output / "results.json"),
        "suite": {"scenarios": selected_scenarios, "repeats": repeats},
        "providers": merged,
    }
    return write_decision(workspace, payload)


def run_benchmark(args: argparse.Namespace) -> int:
    selected = _selected_scenarios(args.scenario, args.smoke)
    repeats = 1 if args.smoke else args.repeats
    providers: list[Provider] = args.provider or ["claude", "codex"]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or Path("benchmark-results") / stamp).expanduser().resolve()
    results: list[RunResult] = []
    print(
        f"Running {len(selected)} scenarios x {repeats} repeats x 2 arms x "
        f"{len(providers)} providers = {len(selected) * repeats * 2 * len(providers)} turns"
    )
    for provider in providers:
        arm_root = output / "workspaces" / provider
        servers = {
            surface: ArmServer(
                root=arm_root / surface,
                surface=surface,
                provider=provider,
                workspace_name=args.workspace,
                startup_timeout=args.startup_timeout,
            )
            for surface in ("legacy", "mcp")
        }
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(server.start) for server in servers.values()]
                for future in futures:
                    future.result()
            projects = {
                surface: server.create_project(f"Benchmark {provider} {surface}")
                for surface, server in servers.items()
            }
            provider_blocked = False
            for repeat in range(1, repeats + 1):
                for scenario in selected:
                    pair_contexts: dict[Surface, RunContext] = {}
                    nonce = uuid.uuid4().hex[:6]
                    for surface, server in servers.items():
                        marker = f"ciao-{provider}-{scenario.name}-{repeat}-{nonce}"
                        model = args.claude_model if provider == "claude" else args.codex_model
                        chat = server.create_chat(
                            projects[surface]["project_id"],
                            title=f"Benchmark {scenario.name} {repeat}",
                            model=model,
                        )
                        pair_contexts[surface] = RunContext(
                            provider=provider,
                            surface=surface,
                            repeat=repeat,
                            marker=marker,
                            server=server,
                            project_id=projects[surface]["project_id"],
                            project_name=projects[surface]["name"],
                            chat_id=chat["chat_id"],
                            chat_title=chat["title"],
                        )
                    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                        future_map = {
                            pool.submit(
                                _run_one, ctx, scenario, timeout=args.turn_timeout
                            ): surface
                            for surface, ctx in pair_contexts.items()
                        }
                        pair_results = [future.result() for future in future_map]
                    pair_results.sort(key=lambda row: row.surface)
                    results.extend(pair_results)
                    status = ", ".join(
                        f"{row.surface}: "
                        f"{'BLOCKED' if row.provider_blocked else ('ok' if row.correct and row.surface_compliant else 'FAIL')} "
                        f"{row.elapsed_ms}ms/{row.tokens if row.tokens is not None else 'n/a'}tok"
                        for row in pair_results
                    )
                    print(f"[{provider} {repeat}/{repeats}] {scenario.name}: {status}", flush=True)
                    _write_report(output, results, summarize(results))
                    if pair_results and all(row.provider_blocked for row in pair_results):
                        provider_blocked = True
                        print(
                            f"[{provider}] stopping early: both arms hit a hard provider/account block",
                            flush=True,
                        )
                        break
                if provider_blocked:
                    break
        finally:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda server: server.stop(), servers.values()))
    summary = summarize(results)
    _write_report(output, results, summary)
    print(f"Report: {output / 'REPORT.md'}")
    if args.apply_to_workspace is not None:
        decision = promote_decision(
            workspace=args.apply_to_workspace.expanduser().resolve(),
            output=output,
            summary=summary,
            results=results,
            selected_scenarios=len(selected),
            repeats=repeats,
            smoke=args.smoke,
        )
        print(f"Promoted decision: {decision}")
    winners = [data["winner"] for data in summary["providers"].values()]
    return 0 if winners and all(winner in {"legacy", "mcp", "tie"} for winner in winners) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ciao benchmark-control-surfaces",
        description="Run paired live legacy-vs-MCP Ciaobot evaluations.",
    )
    parser.add_argument(
        "--provider",
        action="append",
        choices=["claude", "codex"],
        help="Provider to evaluate; repeat for both. Defaults to both.",
    )
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="Run only one named scenario; repeat to select several.",
    )
    parser.add_argument("--smoke", action="store_true", help="Run 3 scenarios once.")
    parser.add_argument("--workspace", default="work")
    parser.add_argument("--claude-model", default="sonnet")
    parser.add_argument("--codex-model", default="")
    parser.add_argument("--turn-timeout", type=float, default=600.0)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--apply-to-workspace",
        type=Path,
        help=(
            "Promote decisive winners into WORKSPACE/.runtime after the full "
            "12-scenario suite with at least 5 repeats."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.repeats < 1:
        raise SystemExit("--repeats must be at least 1")
    return run_benchmark(args)


if __name__ == "__main__":
    raise SystemExit(main())
