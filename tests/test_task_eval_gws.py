"""The ``gws_fake`` capability: the CIAO_GWS_BIN hook, the fake gws, the gws_calls check.

Nothing here boots an engine or calls a model. The fake launcher runs as a
subprocess (POSIX only), which is the point of the fake.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import cli
from ciao import gws_auth
from ciao import gws_wrapper
from ciao import task_eval as te
from ciao.tool_path import common_tool_dirs

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake gws is a POSIX shell launcher")

M4_ID = "M4-email-draft"
THREAD = "19a0f3c2b1d40001"
OTHER_THREAD = "19a3c1d2e3f40003"


def _config(tmp_path: Path) -> SimpleNamespace:
    (tmp_path / ".runtime").mkdir(exist_ok=True)
    return SimpleNamespace(workspace_root=tmp_path, state_path=tmp_path / ".runtime" / "state.json")


def _m4() -> te.EvalTask:
    return next(task for task in te.load_tasks() if task.id == M4_ID)


def _install(tmp_path: Path) -> te.GwsFakeInstall:
    ws = tmp_path / "ws"
    ws.mkdir()
    return te.install_gws_fake(ws, _spec(), base_path=os.environ.get("PATH", ""))


def _spec() -> te.GwsFakeSpec:
    spec = _m4().gws_fake
    assert spec is not None
    return spec


def _run_fake(inst: te.GwsFakeInstall, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(inst.launcher), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=inst.env,
        timeout=120,
        check=False,
    )


def _raw(to: str, body: str, in_reply_to: str | None = None) -> str:
    lines = [f"To: {to}", "Subject: Re: Team offsite: pick a day"]
    if in_reply_to is not None:
        lines.append(f"In-Reply-To: {in_reply_to}")
    text = "\n".join(lines) + "\n\n" + body + "\n"
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _rows(log: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_task(root: Path, task_id: str, body: dict[str, Any]) -> None:
    folder = root / task_id
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps(body), encoding="utf-8", newline="")


def _body(task_id: str, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": task_id,
        "tier": "medium",
        "category": "email_draft",
        "prompt": "Reply to Tomas about the offsite.",
        "entry": {"kind": "prompt"},
        "setup": [],
        "requires": [],
        "timeout_s": 600,
        "checks": [{"kind": "answer_contains", "any": ["ok"]}],
    }
    body.update(overrides)
    return body


def _no_api(method: str, path: str, body: Any = None) -> Any:
    raise AssertionError(f"unexpected API call {method} {path}")


def _grade_ctx(root: Path, log: Path | None) -> te.GradeContext:
    return te.GradeContext(
        agent_root=root,
        answer="",
        api=_no_api,
        snapshot=te.Snapshot(task_ids=frozenset(), schedule_ids=frozenset(), project_ids=frozenset(), files=frozenset()),
        run_date=date(2026, 11, 1),
        gws_call_log=log,
    )


# --- engine hook: CIAO_GWS_BIN in ciao/gws_auth.py and ciao/gws_wrapper.py ---------


def test_gws_bin_override_wins_in_wrapper(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    fake = tmp_path / "fake-gws"
    monkeypatch.setenv("CIAO_GWS_BIN", str(fake))
    monkeypatch.setattr("ciao.config.CiaoConfig.from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(gws_wrapper, "_configured_workspace_root", lambda config: tmp_path)
    monkeypatch.setattr(
        gws_wrapper, "resolve_command", lambda name: pytest.fail("resolve_command must not run")
    )
    seen: list[tuple[str, list[str]]] = []

    def fake_hand_off(executable: str, argv: list[str], env: dict[str, str]) -> int:
        seen.append((executable, argv))
        return 0

    monkeypatch.setattr(gws_wrapper, "hand_off", fake_hand_off)

    assert gws_wrapper.main(["eval-sandbox", "gmail", "+triage"]) == 0
    assert seen == [(str(fake), [str(fake), "gmail", "+triage"])]


def test_gws_bin_override_missing_file_never_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _config(tmp_path)
    monkeypatch.setenv("CIAO_GWS_BIN", "/nonexistent/gws")
    monkeypatch.setattr("ciao.config.CiaoConfig.from_env", classmethod(lambda cls: cfg))
    monkeypatch.setattr(gws_wrapper, "_configured_workspace_root", lambda config: tmp_path)
    monkeypatch.setattr(
        gws_wrapper, "resolve_command", lambda name: pytest.fail("the real gws lookup must not run")
    )
    attempted: list[str] = []

    def failing_hand_off(executable: str, argv: list[str], env: dict[str, str]) -> int:
        attempted.append(executable)
        raise OSError("no such file")

    monkeypatch.setattr(gws_wrapper, "hand_off", failing_hand_off)

    assert gws_wrapper.main(["eval-sandbox", "gmail", "+triage"]) == 1
    assert "failed to run gws" in capsys.readouterr().err
    assert attempted == [str(Path("/nonexistent/gws"))]


def test_auth_status_uses_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import tool_path

    fake = tmp_path / "fake-gws"
    monkeypatch.setenv("CIAO_GWS_BIN", str(fake))
    monkeypatch.setattr(tool_path, "resolve_command", lambda name: pytest.fail("no PATH lookup"))
    monkeypatch.setattr(tool_path, "login_shell_path", lambda: "/usr/bin")
    recorded: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.append(argv)
        out = '{"token_valid": true, "has_refresh_token": true, "token_error": ""}'
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    status = gws_auth.auth_status(_config(tmp_path), "eval-sandbox", runner=runner)

    assert recorded[0][0] == str(fake)
    assert status["token_valid"] is True


def test_override_unset_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CIAO_GWS_BIN", raising=False)

    assert gws_auth.gws_bin_override() is None


# --- catalog: the M4 task and the loader's checks ----------------------------------


def test_load_tasks_m4_has_gws_fake_spec() -> None:
    task = _m4()

    assert task.requires == ["gws_fake"]
    assert task.gws_fake is not None
    assert task.gws_fake.profile == "eval-sandbox"
    thread_rows = [m for m in task.gws_fake.messages if m["threadId"] == THREAD]
    assert len(thread_rows) == 2


def test_catalog_rejects_gws_fake_without_spec(tmp_path: Path) -> None:
    required = tmp_path / "required"
    _write_task(required, "needs-spec", _body("needs-spec", requires=["gws_fake"]))
    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(required)

    stray = tmp_path / "stray"
    stray_body = _body("stray-spec")
    stray_body["gws_fake"] = {"profile": "eval-sandbox", "account": "a@example.com", "messages": []}
    _write_task(stray, "stray-spec", stray_body)
    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(stray)


def test_catalog_rejects_bad_gws_calls_regex(tmp_path: Path) -> None:
    body = _body("bad-regex", checks=[{"kind": "gws_calls", "must": ["(unclosed"]}])
    _write_task(tmp_path, "bad-regex", body)

    with pytest.raises(te.TaskCatalogError):
        te.load_tasks(tmp_path)


# --- install: files, registry, health cache, PATH --------------------------------


@posix_only
def test_install_gws_fake_links_profile_on_disk(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    inst = te.install_gws_fake(ws, _spec(), base_path="/usr/bin:/bin")

    config = SimpleNamespace(workspace_root=str(ws), state_path=ws / ".runtime" / "state.json")
    assert "eval-sandbox" in gws_auth.known_profiles(config)
    cred_dir = ws / "secrets" / "gws-eval-sandbox"
    assert (cred_dir / "credentials.json").is_file()
    health = gws_auth.read_health_cache(ws / ".runtime")
    assert health["eval-sandbox"]["token_valid"] is True
    assert os.access(inst.launcher, os.X_OK)
    assert inst.launcher.parent == cred_dir / "eval-gws"
    assert inst.env["CIAO_GWS_BIN"] == str(inst.launcher)
    path_entries = inst.env["PATH"].split(os.pathsep)
    assert path_entries[0] == str(inst.bin_dir)
    for tool_dir in common_tool_dirs():
        assert tool_dir in path_entries


# --- the fake gws: launcher and operations ---------------------------------------


@posix_only
def test_fake_gws_triage_read_and_reply_draft(tmp_path: Path) -> None:
    inst = _install(tmp_path)

    triage = _run_fake(inst, "gmail", "+triage", "--query", "from:tomas offsite", "--format", "json")
    assert triage.returncode == 0, triage.stderr
    assert [row["id"] for row in json.loads(triage.stdout)] == ["19a2b7e4c9f60002", THREAD]

    read = _run_fake(inst, "gmail", "+read", "--id", "19a2b7e4c9f60002")
    assert read.returncode == 0
    assert "Thursday" in read.stdout

    reply = _run_fake(
        inst, "gmail", "+reply", "--message-id", "19a2b7e4c9f60002", "--body", "Thursday works", "--draft"
    )
    assert reply.returncode == 0, reply.stderr
    assert json.loads(reply.stdout)["message"]["threadId"] == THREAD

    rows = _rows(inst.log)
    assert len(rows) == 3
    spec = _m4().checks[0]
    last = rows[-1]["summary"]
    assert re.search(spec["must"][0], last, re.IGNORECASE)
    for pattern in spec["must_not"]:
        assert re.search(pattern, last, re.IGNORECASE) is None


@posix_only
def test_fake_gws_raw_drafts_create_resolves_thread_from_in_reply_to(tmp_path: Path) -> None:
    inst = _install(tmp_path)
    raw = _raw("tomas.berg@example.com", "Thursday works", in_reply_to="<offsite-2@mail.example.com>")

    result = _run_fake(
        inst,
        "gmail", "users", "drafts", "create",
        "--params", '{"userId":"me"}',
        "--json", json.dumps({"message": {"raw": raw}}),
    )

    assert result.returncode == 0, result.stderr
    row = _rows(inst.log)[-1]
    assert row["effect"] == "draft_created"
    assert row["thread_id"] == THREAD
    assert f"effect=draft_created thread={THREAD}" in row["summary"]


@posix_only
def test_fake_gws_send_is_logged_as_sent(tmp_path: Path) -> None:
    inst = _install(tmp_path)

    sent = _run_fake(inst, "gmail", "+reply", "--message-id", "19a2b7e4c9f60002", "--body", "Thursday works")
    assert sent.returncode == 0, sent.stderr
    assert _rows(inst.log)[-1]["effect"] == "sent"

    raw = _raw("tomas.berg@example.com", "Thursday works")
    api_send = _run_fake(
        inst, "gmail", "users", "messages", "send",
        "--params", '{"userId":"me"}',
        "--json", json.dumps({"raw": raw, "threadId": THREAD}),
    )
    assert api_send.returncode == 0, api_send.stderr
    assert _rows(inst.log)[-1]["effect"] == "sent"

    dry = _run_fake(
        inst, "gmail", "+reply", "--message-id", "19a2b7e4c9f60002", "--body", "Thursday works", "--dry-run"
    )
    assert dry.returncode == 0, dry.stderr
    assert _rows(inst.log)[-1]["effect"] == "dry_run"


@posix_only
def test_fake_gws_unknown_command_logged_and_fails(tmp_path: Path) -> None:
    inst = _install(tmp_path)

    result = _run_fake(inst, "gmail", "users", "settings", "frobnicate")

    assert result.returncode == 2
    assert json.loads(result.stderr)["error"]["code"] == 2
    assert _rows(inst.log)[-1]["effect"] == "unknown"


@posix_only
def test_fake_gws_auth_status_and_version(tmp_path: Path) -> None:
    inst = _install(tmp_path)

    status = _run_fake(inst, "auth", "status")
    assert status.returncode == 0
    assert json.loads(status.stdout)["token_valid"] is True

    version = _run_fake(inst, "--version")
    assert version.returncode == 0
    assert version.stdout.strip() == "gws 0.22.5"


# --- the gws_calls check -----------------------------------------------------------


def _call_row(summary: str) -> dict[str, Any]:
    return {
        "ts": 0.0,
        "argv": [],
        "op": "x",
        "effect": "read",
        "thread_id": None,
        "message_id": None,
        "to": None,
        "body": None,
        "summary": summary,
    }


def _gws_result(root: Path, log: Path | None, rows: list[dict[str, Any]] | None) -> te.CheckResult:
    if log is not None and rows is not None:
        log.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="")
    results = te.grade(_m4(), _grade_ctx(root, log))
    return next(result for result in results if result.kind == "gws_calls")


_READ = _call_row("op=gmail +triage effect=read thread=- message=- to=- body=-")
_GOOD_DRAFT = _call_row(
    "op=gmail users drafts create effect=draft_created thread=" + THREAD
    + " message=eval-draft-msg-2 to=tomas.berg@example.com body=Thursday works for me"
)


def test_gws_calls_check_pass_and_fail(tmp_path: Path) -> None:
    log = tmp_path / "calls.jsonl"

    assert _gws_result(tmp_path, log, [_READ, _GOOD_DRAFT]).ok is True

    sent = _call_row(
        "op=gmail +reply effect=sent thread=" + THREAD + " message=- to=tomas.berg@example.com body=Thursday"
    )
    assert _gws_result(tmp_path, log, [_READ, _GOOD_DRAFT, sent]).ok is False

    wrong_thread = _call_row(
        "op=gmail users drafts create effect=draft_created thread=" + OTHER_THREAD
        + " message=eval-draft-msg-3 to=tomas.berg@example.com body=Thursday works for me"
    )
    assert _gws_result(tmp_path, log, [_READ, wrong_thread]).ok is False

    missing = _gws_result(tmp_path, None, None)
    assert missing.ok is False
    assert missing.detail == "no gws fake in this run"


# --- runner: the log reaches the grader -------------------------------------------


class _FakeGwsSession:
    """An ``EngineSession`` stand-in with a call log, as an IsolatedEngine boot would leave it."""

    def __init__(self, root: Path, log: Path | None) -> None:
        self.agent_root = root
        self.gws_call_log = log
        self.closed = False

    def api(self, method: str, path: str, body: Any = None) -> Any:
        if path.startswith("/api/tasks?"):
            return {"tasks": []}
        if path in ("/api/schedules", "/api/projects"):
            return []
        raise AssertionError(f"unexpected {method} {path}")

    def start_prompt(self, prompt: str, *, provider: str, model: str) -> str:
        return "chat-1"

    def delegate(self, task_id: str, revision: str, *, provider: str, model: str) -> str:
        raise AssertionError("prompt entry must not delegate")

    def wait_settled(self, timeout_s: float) -> bool:
        return True

    def stop_chat(self, chat_id: str) -> None:
        pass

    def answer_text(self) -> str:
        return "Saved a draft reply in that thread."

    def usage(self) -> dict[str, Any]:
        return {
            "tokens": {"input": 1, "output": 1, "cache_read": 0, "cache_write": 0},
            "tool_calls": 0,
            "tool_errors": 0,
            "ciao_calls": 0,
            "ciao_errors": 0,
            "cost_usd": None,
            "chat_ids": ["chat-1"],
        }

    def close(self) -> None:
        self.closed = True


def test_run_task_passes_gws_log_to_grader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(te, "provider_version", lambda provider: "fake 1.0")
    monkeypatch.setattr(te, "code_revision", lambda: "abc123def456-dirty")
    log = tmp_path / "calls.jsonl"
    log.write_text(json.dumps(_GOOD_DRAFT) + "\n", encoding="utf-8", newline="")
    session = _FakeGwsSession(tmp_path, log)

    record = te.run_task(
        _m4(),
        provider="claude",
        model="haiku",
        label="candidate",
        repeat=1,
        session_factory=lambda task: session,
        capabilities=frozenset({"gws_fake"}),
    )

    assert record["status"] == "passed", record
    assert session.closed is True


# --- capabilities and the CLI wiring ----------------------------------------------


def test_available_capabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    assert te.available_capabilities() == {"gws_fake"}

    monkeypatch.setattr(sys, "platform", "win32")
    assert te.available_capabilities() == frozenset()


def test_cli_eval_tasks_run_passes_capabilities(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run_suite(tasks: Any, **kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(te, "cli_package_mismatch", lambda: None)
    monkeypatch.setattr(te, "run_suite", fake_run_suite)

    rc = cli.main(
        [
            "eval", "tasks", "run",
            "--provider", "claude",
            "--model", "haiku",
            "--include", M4_ID,
            "--out", str(tmp_path / "out.jsonl"),
        ]
    )

    assert rc == 0
    assert captured["capabilities"] == te.available_capabilities()
