"""S0.5 stage 2: real chat sessions, MCP surface vs CLI surface, on a live instance.

For every (repeat, prompt, provider, surface) it creates a fresh chat in a
dedicated project, marks the chat's surface in ``.runtime/agent_surface.json``,
sends the prompt over REST, auto-approves any approval card (counting it),
waits for the turn to settle, then scores completion from real workspace state
and pulls the chat's control-plane telemetry. Everything it creates is deleted
afterwards and the memory file is restored from a snapshot taken before the run.

Usage (from the worktree, against the installed dev build):

  PYTHONPATH=. ~/repos/ciaobot/.venv/bin/python scripts/surface-compare/run_sessions.py \
      --workspace-root ~/repos/ciao/personal --runtime ~/repos/ciao/.runtime \
      --env-file ~/repos/ciao/.env --prompts-file ~/private/surface-prompts.json --repeats 5 \
      --out ~/orca/workspaces/ciaobot/plans/evidence/surface-compare/sessions
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

CONTROL_PLANE_OPS = {
    "memory_status", "memory_update", "vault_search", "vault_review", "file_surface",
    "chats_list", "chat_get", "chat_archive", "chat_delete", "schedules_list", "schedule",
    "schedule_action", "context_get",
}


@dataclass
class Prompt:
    key: str
    text: str            # may contain {run} which is replaced by the run tag
    expects: set[str]    # control-plane ops that count as "used the surface"
    check: str           # name of the completion check


def load_prompts(path: Path) -> list[Prompt]:
    """Prompts come from a local JSON file, never from the repository.

    The comparison runs against an operator's real vault, so the prompts name
    real people and facts from it; they must not be committed. The shape is
    documented by ``prompts.example.json`` beside this script (fictional
    entities). Each entry: ``key``, ``text`` (may contain ``{run}``),
    ``expects`` (operations that must all appear), ``check``
    (``memory_contains:<text>`` | ``answer_contains:<text>`` |
    ``file_surfaced:<relative path>`` | ``schedule_exists:<title>`` |
    ``chat_archived:<title>`` | ``op_seen:<operation>``).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise SystemExit(f"{path}: expected a non-empty JSON list of prompts")
    prompts = []
    for item in raw:
        prompts.append(Prompt(str(item["key"]), str(item["text"]), set(item.get("expects") or []), str(item["check"])))
    return prompts


@dataclass
class RunRecord:
    run: str
    repeat: int
    prompt: str
    provider: str
    model: str
    surface: str
    mode: str = "auto"
    chat_id: str = ""
    started_at: float = 0.0
    duration_s: float = 0.0
    completed: bool | None = None
    check_detail: str = ""
    control_plane_calls: list[dict[str, Any]] = field(default_factory=list)
    expected_ops_used: bool = False
    provider_tools: list[str] = field(default_factory=list)
    approval_cards: int = 0
    fallback_signals: list[str] = field(default_factory=list)
    answer: str = ""
    error: str = ""
    memory_changed: bool = False
    schedules_created: list[str] = field(default_factory=list)
    other_activity_seen: bool = False
    cleanup_notes: list[str] = field(default_factory=list)
    activity: list[str] = field(default_factory=list)


class Instance:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(base_url=self.base_url, timeout=30.0, follow_redirects=True)
        r = self.client.post("/api/auth", json={"token": token})
        r.raise_for_status()
        self.cookie = "; ".join(f"{k}={v}" for k, v in self.client.cookies.items())

    def get(self, path: str, **kw: Any) -> Any:
        r = self.client.get(path, **kw); r.raise_for_status(); return r.json()

    def post(self, path: str, payload: dict | None = None) -> Any:
        r = self.client.post(path, json=payload or {}); r.raise_for_status()
        return r.json() if r.content else {}

    def delete(self, path: str) -> None:
        r = self.client.delete(path)
        if r.status_code not in (200, 204, 404):
            r.raise_for_status()

    def active_chat_ids(self) -> set[str]:
        return set(self.get("/api/active-chats").get("active_chat_ids") or [])

    def chat(self, chat_id: str) -> dict[str, Any] | None:
        for c in self.get("/api/chats"):
            if c.get("chat_id") == chat_id:
                return c
        return None

    async def approve_permission(self, chat_id: str, request_id: str) -> None:
        import websockets

        ws_url = self.base_url.replace("http", "ws", 1) + f"/ws/chat/{chat_id}"
        async with websockets.connect(ws_url, additional_headers={"Cookie": self.cookie}) as ws:
            await ws.send(json.dumps({"type": "permission_response", "request_id": request_id, "approved": True}))
            await asyncio.sleep(0.5)


def read_env_token(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("PWA_AUTH_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("PWA_AUTH_TOKEN not found in env file")


def jsonl_tail(path: Path, start: int) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("rb") as fh:
        fh.seek(start)
        for line in fh.read().decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return rows


def file_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def memory_region(text: str) -> str:
    m = re.search(r"<!-- ciao:memory:start[^>]*-->(.*?)<!-- ciao:memory:end -->", text, re.S)
    return m.group(1) if m else ""


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.inst = Instance(args.base_url, read_env_token(Path(args.env_file).expanduser()))
        self.runtime = Path(args.runtime).expanduser()
        self.workspace_root = Path(args.workspace_root).expanduser()
        # The bounded-memory regions live in the workspace guide: AGENTS.md on a
        # current install, CLAUDE.md where the migration has not run yet.
        from ciao.workspace_guide import guide_path

        self.memory_file = guide_path(self.workspace_root)
        self.schedules_file = self.runtime / "schedules.json"
        self.telemetry = self.runtime / "mcp_tool_calls.jsonl"
        self.agent_tools = self.runtime / "agent_tool_calls.jsonl"
        self.surface_file = self.runtime / "agent_surface.json"
        self.out = Path(args.out).expanduser(); self.out.mkdir(parents=True, exist_ok=True)
        self.log = (self.out / "sessions.jsonl").open("a", encoding="utf-8")
        self.project_id = self._ensure_project()

    # ── project / chats ────────────────────────────────────────────────
    def _ensure_project(self) -> str:
        for p in self.inst.get("/api/projects"):
            if p.get("name") == self.args.project_name and p.get("workspace") == self.args.workspace:
                self._project_created_this_run = False
                return p["project_id"]
        created = self.inst.post("/api/projects", {"name": self.args.project_name, "workspace": self.args.workspace, "context": "Temporary project for the MCP-versus-CLI surface comparison. Safe to delete."})
        self._project_created_this_run = True
        return created["project_id"]

    def _create_chat(self, title: str, provider: str, model: str) -> str:
        chat = self.inst.post(f"/api/projects/{self.project_id}/chats", {"title": title, "provider": provider, "model": model, "mode": self.args.mode})
        return chat["chat_id"]

    def _set_surface(self, chat_id: str, surface: str | None) -> None:
        try:
            data = json.loads(self.surface_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if surface is None:
            data.pop(chat_id, None)
        else:
            data[chat_id] = surface
        self.surface_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    # ── one run ─────────────────────────────────────────────────────────
    async def run_one(self, repeat: int, prompt: Prompt, provider: str, surface: str) -> RunRecord:
        model = self.args.claude_model if provider == "claude" else self.args.opencode_model
        run = f"{prompt.key}-{provider}-{surface}-r{repeat}-{int(time.time()) % 100000}"
        rec = RunRecord(run=run, mode=self.args.mode, repeat=repeat, prompt=prompt.key, provider=provider, model=model, surface=surface)
        text = prompt.text.replace("{run}", run)
        check = prompt.check.replace("{run}", run)

        memory_before = self.memory_file.read_text(encoding="utf-8")
        schedules_before = self._schedule_ids()
        tele_pos, tools_pos = file_size(self.telemetry), file_size(self.agent_tools)
        target_chat = ""
        if prompt.key == "P5_archive_chat":
            target_chat = self._create_chat(f"Surface probe target {run}", provider, model)

        chat_id = self._create_chat(f"[surface-compare] {run}", provider, model)
        rec.chat_id = chat_id
        self._set_surface(chat_id, surface)
        rec.started_at = time.time()
        try:
            self.inst.post(f"/api/chats/{chat_id}/prompt", {"prompt": text})
            await self._wait_for_turn(rec)
        except Exception as exc:  # noqa: BLE001 - one failed run must not stop the matrix
            rec.error = f"{type(exc).__name__}: {exc}"[:300]
        rec.duration_s = round(time.time() - rec.started_at, 1)

        # Telemetry for this chat only.
        rec.control_plane_calls = [
            {"tool": r.get("tool"), "status": r.get("status"), "error_code": r.get("error_code"), "surface": r.get("surface")}
            for r in jsonl_tail(self.telemetry, tele_pos) if r.get("chat_id") == chat_id
        ]
        seen_uses: set[str] = set()
        rec.provider_tools = []
        for r in jsonl_tail(self.agent_tools, tools_pos):
            if r.get("chat_id") != chat_id:
                continue
            key = str(r.get("tool_use_id") or "") or f"{r.get('tool')}@{r.get('timestamp')}"
            if key in seen_uses:
                continue
            seen_uses.add(key)
            rec.provider_tools.append(str(r.get("tool")))
        used = {c["tool"] for c in rec.control_plane_calls if c["status"] == "ok"}
        # Every expected operation must appear: a multi-step prompt that only
        # listed, or archived by guessing an id, did not use the surface as intended.
        rec.expected_ops_used = prompt.expects <= used

        # Answer text.
        try:
            msgs = self.inst.get(f"/api/chats/{chat_id}/messages")
            if isinstance(msgs, dict):
                msgs = msgs.get("items") or []
            assistant = [m for m in msgs if isinstance(m, dict) and m.get("role") == "assistant" and isinstance(m.get("content"), str)]
            rec.answer = assistant[-1]["content"][:1500] if assistant else ""
            rec.activity = [str(m.get("content") or "")[:200] for m in msgs if isinstance(m, dict) and m.get("tool_name") == "_activity"]
            activity = " ".join(rec.activity)
            if "curl " in activity and "/api/" in activity:
                rec.fallback_signals.append("curl against the REST API")
        except Exception as exc:  # noqa: BLE001
            rec.error = (rec.error + f" | messages: {exc}")[:400]

        # Completion + fallback signals from real state.
        memory_after = self.memory_file.read_text(encoding="utf-8")
        rec.memory_changed = memory_region(memory_after) != memory_region(memory_before)
        rec.schedules_created = sorted(self._schedule_ids() - schedules_before)
        rec.completed, rec.check_detail = self._check(check, rec, target_chat)
        if rec.memory_changed and "memory_update" not in used:
            rec.fallback_signals.append("memory file changed without a memory_update call")
        if rec.schedules_created and "schedule" not in used:
            rec.fallback_signals.append("schedule created without a schedule call")
        if prompt.key in ("P2_recall_person", "P6_supersession") and "vault_search" not in used and any(t in ("Read", "read", "Grep", "grep", "Glob", "glob") for t in rec.provider_tools):
            rec.fallback_signals.append("read vault files directly instead of vault search")
        if surface == "cli":
            bash_calls = sum(1 for t in rec.provider_tools if t.lower() == "bash")
            if bash_calls > len(rec.control_plane_calls):
                rec.fallback_signals.append(f"{bash_calls - len(rec.control_plane_calls)} Bash calls not matched by a ciao record")

        # Cleanup — only artifacts this run can prove it created. The runner
        # targets a live instance, so another chat or automation may change
        # memory or add a schedule during the turn; restoring a global snapshot
        # or deleting "every new schedule" would destroy that legitimate state.
        try:
            if rec.memory_changed and not self.args.keep_memory:
                marker = check.split(":", 1)[1] if check.startswith("memory_contains:") else ""
                if marker:
                    self._remove_memory_entries_containing(marker)
                elif rec.other_activity_seen:
                    rec.cleanup_notes.append("memory changed with other chats active; left as is (inspect by hand)")
                else:
                    self.memory_file.write_text(memory_before, encoding="utf-8")
            titles = self._schedule_titles()
            for sid in rec.schedules_created:
                if run in titles.get(sid, ""):
                    self.inst.delete(f"/api/schedules/{sid}")
                else:
                    rec.cleanup_notes.append(f"schedule {sid} appeared during the turn but is not tagged with this run; left as is")
            produced = self.workspace_root / f"surface-compare/{run}.md"
            if produced.exists():
                produced.unlink()
            self.inst.delete(f"/api/chats/{chat_id}")
            if target_chat:
                self.inst.delete(f"/api/chats/{target_chat}")
            self._set_surface(chat_id, None)
        except Exception as exc:  # noqa: BLE001
            rec.error = (rec.error + f" | cleanup: {exc}")[:500]

        self.log.write(json.dumps(rec.__dict__, ensure_ascii=False) + "\n"); self.log.flush()
        return rec

    async def _wait_for_turn(self, rec: RunRecord) -> None:
        deadline = time.time() + self.args.turn_timeout
        seen_active = False
        seen_requests: set[str] = set()
        while time.time() < deadline:
            await asyncio.sleep(2.0)
            active_ids = self.inst.active_chat_ids()
            active = rec.chat_id in active_ids
            if active_ids - {rec.chat_id}:
                rec.other_activity_seen = True
            chat = self.inst.chat(rec.chat_id) or {}
            pending = chat.get("pending_permission") or ""
            if pending:
                try:
                    payload = json.loads(pending)
                    rid = str(payload.get("request_id") or "")
                except ValueError:
                    rid = ""
                if rid and rid not in seen_requests:
                    seen_requests.add(rid)
                    rec.approval_cards += 1
                    await self.inst.approve_permission(rec.chat_id, rid)
                    continue
            if active:
                seen_active = True
                continue
            if seen_active or time.time() - rec.started_at > 20:
                # settle: the stream ended; give post-turn bookkeeping a moment
                await asyncio.sleep(2.0)
                if rec.chat_id not in self.inst.active_chat_ids():
                    return
        rec.error = "turn_timeout"
        try:
            self.inst.post(f"/api/chats/{rec.chat_id}/stop")
        except Exception:  # noqa: BLE001
            pass

    def _remove_memory_entries_containing(self, marker: str) -> None:
        """Drop only the region entries this run added (they carry ``marker``)."""
        text = self.memory_file.read_text(encoding="utf-8")
        m = re.search(r"(<!-- ciao:memory:start[^>]*-->)(.*?)(<!-- ciao:memory:end -->)", text, re.S)
        if not m:
            return
        body = m.group(2)
        kept = [entry for entry in body.split("§") if marker not in entry]
        new_body = "§".join(kept)
        if new_body != body:
            self.memory_file.write_text(text[: m.start(2)] + new_body + text[m.end(2):], encoding="utf-8")

    def _schedule_ids(self) -> set[str]:
        try:
            data = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
        items = data if isinstance(data, list) else (data.get("schedules") or list(data.values()))
        if isinstance(items, dict):
            items = list(items.values())
        return {str(s.get("schedule_id")) for s in items if isinstance(s, dict) and s.get("schedule_id")}

    def _schedule_titles(self) -> dict[str, str]:
        try:
            data = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        items = data if isinstance(data, list) else (data.get("schedules") or list(data.values()))
        if isinstance(items, dict):
            items = list(items.values())
        return {str(s.get("schedule_id")): str(s.get("title") or "") for s in items if isinstance(s, dict)}

    def _check(self, check: str, rec: RunRecord, target_chat: str) -> tuple[bool, str]:
        kind, _, arg = check.partition(":")
        if kind == "memory_contains":
            ok = arg in memory_region(self.memory_file.read_text(encoding="utf-8"))
            return ok, f"'{arg}' {'found' if ok else 'absent'} in ciao:memory"
        if kind == "answer_contains":
            ok = arg.lower() in rec.answer.lower()
            return ok, f"'{arg}' {'in' if ok else 'not in'} answer"
        if kind == "file_surfaced":
            exists = (self.workspace_root / arg).exists()
            surfaced = any(c["tool"] == "file_surface" and c["status"] == "ok" for c in rec.control_plane_calls)
            return exists and surfaced, f"file {'exists' if exists else 'missing'}, surface call {'ok' if surfaced else 'absent'}"
        if kind == "schedule_exists":
            titles = self._schedule_titles()
            ok = any(t == arg for t in titles.values())
            return ok, f"schedule '{arg}' {'present' if ok else 'absent'}"
        if kind == "chat_archived":
            chat = self.inst.chat(target_chat) or {}
            ok = bool(chat.get("archived"))
            return ok, f"target chat archived={ok}"
        if kind == "op_seen":
            ok = any(c["tool"] == arg and c["status"] == "ok" for c in rec.control_plane_calls)
            return ok, f"{arg} {'called' if ok else 'not called'}"
        return False, f"unknown check {check}"

    # ── matrix ──────────────────────────────────────────────────────────
    async def run_matrix(self) -> None:
        providers = [p for p in self.args.providers.split(",") if p]
        surfaces = [s for s in self.args.surfaces.split(",") if s]
        all_prompts = load_prompts(Path(self.args.prompts_file).expanduser())
        prompts = [p for p in all_prompts if not self.args.prompts or p.key in self.args.prompts.split(",")]
        total = self.args.repeats * len(prompts) * len(providers) * len(surfaces)
        done = 0
        finished: set[tuple] = set()
        if self.args.resume and (self.out / "sessions.jsonl").exists():
            for line in (self.out / "sessions.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                    finished.add((r["repeat"], r["prompt"], r["provider"], r["surface"]))
                except (ValueError, KeyError):
                    pass
            done = len(finished)
        probe_dir = self.workspace_root / "surface-compare"
        if probe_dir.exists() and not self.args.resume:
            raise SystemExit(f"{probe_dir} already exists; refusing to reuse a directory this run did not create")
        probe_dir.mkdir(exist_ok=True)
        for repeat in range(self.args.repeats):
            for prompt in prompts:
                for provider in providers:
                    for surface in surfaces:
                        if (repeat, prompt.key, provider, surface) in finished:
                            continue
                        rec = await self.run_one(repeat, prompt, provider, surface)
                        done += 1
                        print(f"[{done}/{total}] {rec.run}: completed={rec.completed} ops={[c['tool'] for c in rec.control_plane_calls]} cards={rec.approval_cards} {rec.duration_s}s {rec.error}", flush=True)
        # Per-run files were unlinked after each session; the directory was
        # created by this run (checked above), so removing it only removes
        # what this run left behind.
        leftovers = [p for p in probe_dir.iterdir()] if probe_dir.exists() else []
        if leftovers:
            print(f"leaving {probe_dir}: unexpected files {sorted(p.name for p in leftovers)}", flush=True)
        else:
            shutil.rmtree(probe_dir, ignore_errors=True)
        if self.args.delete_project and self._project_created_this_run:
            self.inst.delete(f"/api/projects/{self.project_id}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8443")
    ap.add_argument("--env-file", default="~/repos/ciao/.env")
    ap.add_argument("--runtime", default="~/repos/ciao/.runtime")
    ap.add_argument("--workspace-root", default="~/repos/ciao/personal")
    ap.add_argument("--workspace", default="personal")
    ap.add_argument("--project-name", default="Surface compare")
    ap.add_argument("--providers", default="claude,opencode")
    ap.add_argument("--surfaces", default="mcp,cli")
    ap.add_argument("--prompts-file", required=True, help="Local JSON prompt set; see prompts.example.json. Never commit a real one.")
    ap.add_argument("--prompts", default="", help="Comma-separated prompt keys to run (default: all in the file).")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--claude-model", default="opus")
    ap.add_argument("--opencode-model", default="ollama-cloud/deepseek-v4.1-flash")
    ap.add_argument("--turn-timeout", type=float, default=300.0)
    ap.add_argument("--mode", default="auto", choices=["auto", "bypass", "normal"], help="Permission mode for the probe chats.")
    ap.add_argument("--resume", action="store_true", help="Skip (repeat, prompt, provider, surface) cells already present in sessions.jsonl.")
    ap.add_argument("--keep-memory", action="store_true")
    ap.add_argument("--delete-project", action="store_true")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    asyncio.run(Runner(args).run_matrix())
    return 0


if __name__ == "__main__":
    sys.exit(main())
