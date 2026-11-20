"""The OpenCode CLI helpers that outlive the HTTP driver: the version floor and the binary.

The chat runtime is `opencode acp` (see test_acp_agents.py). These guards cover what
Settings, the releaser, the conversation importer and the ACP start gate all share.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ciao import opencode_cli


@pytest.fixture(autouse=True)
def _stub_auth_list(monkeypatch):
    """Tests must never query the operator's OpenCode credentials."""
    monkeypatch.setattr(opencode_cli, "bounded_output", lambda *a, **k: (0, b"[]"))


@pytest.mark.parametrize(
    ("version", "supported"),
    [
        ("opencode v2.3.2", True),
        ("2.3.2", True),
        ("2.10.0", True),
        ("2.3.1", False),
        ("2.0.16", False),
        ("1.14.0", False),
        ("3.0.0", False),
        ("unknown", False),
        ("", False),
    ],
)
def test_the_floor_is_2_3_2_within_major_2(version: str, supported: bool) -> None:
    # Mutation check: lowering the floor back to (2, 0, 16) makes "2.3.1" and "2.0.16" pass.
    assert opencode_cli.opencode_start_supported(version) is supported
    assert (opencode_cli.cli_version_error({"version": version}) is None) is supported


def test_a_payload_that_is_not_a_mapping_is_refused_with_the_floor_message() -> None:
    error = opencode_cli.cli_version_error("2.3.2")
    assert error == opencode_cli.OPENCODE_V2_REQUIRED


def test_a_refused_version_names_the_installed_version() -> None:
    error = opencode_cli.cli_version_error({"version": "2.0.16"})
    assert error is not None
    assert error.endswith("Installed CLI version: 2.0.16.")


def test_version_number_reads_the_dotted_prefix_and_ignores_suffixes() -> None:
    assert opencode_cli.version_number("opencode v2.3.2") == (2, 3, 2)
    assert opencode_cli.version_number("2.0.16-beta.3") == (2, 0, 16)
    assert opencode_cli.version_number("unknown") is None


def test_default_model_reads_the_operator_override_and_defaults_to_empty() -> None:
    from types import SimpleNamespace

    assert opencode_cli.opencode_default_model(SimpleNamespace()) == ""
    config = SimpleNamespace(opencode=opencode_cli.OpencodeSettings(default_model="x/y"))
    assert opencode_cli.opencode_default_model(config) == "x/y"


def test_the_binary_override_must_point_at_a_file(tmp_path, monkeypatch) -> None:
    binary = tmp_path / "opencode"
    binary.write_text("#!/bin/sh\n")
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(binary))
    assert opencode_cli.resolve_opencode_binary() == str(binary.resolve())
    monkeypatch.setenv("CIAO_OPENCODE_BIN", str(tmp_path / "missing"))
    assert opencode_cli.resolve_opencode_binary() is None


def test_safe_credential_inventory_projection(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    rows = [
        {"id": "ollama", "name": "Ollama", "connections": [
            {"type": "credential", "method": "key", "token": "SECRET"},
            {"type": "env", "name": "SECRET"},
        ]},
        {"id": "custom", "name": " Custom ", "connections": [
            {"type": "credential", "method": "oauth"},
            {"type": "credential", "method": "oauth"},
        ]},
    ]
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: (0, json.dumps(rows).encode()))
    assert mod.opencode_credential_inventory() == {
        "status": "complete",
        "providers": [
            {"id": "custom", "name": "Custom", "auth_types": ["oauth"], "configured": True, "verified": None},
            {"id": "ollama", "name": "Ollama", "auth_types": ["api_key", "environment"], "configured": True, "verified": None},
        ],
    }

def test_safe_credential_inventory_rejects_bad_data(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    rows = [
        {"id": "a", "name": "bad\nname", "connections": [{"type": "env"}]},
        {"id": "a", "name": "good", "connections": []},
        {"id": "a", "name": "good", "connections": [{"type": "env"}]},
        {"id": "a", "name": "duplicate", "connections": [{"type": "env"}]},
        {"id": "b", "name": "B", "connections": [None, {"type": "other"}]},
    ]
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: (0, json.dumps(rows).encode()))
    assert mod.opencode_credential_inventory() == {
        "status": "partial",
        "providers": [
            {"id": "a", "name": "good", "auth_types": ["environment"], "configured": True, "verified": None},
            {"id": "b", "name": "B", "auth_types": ["unknown"], "configured": True, "verified": None},
        ],
    }


@pytest.mark.parametrize("result", [None, (1, b"[]"), (0, b"\xff"), (0, b"{}"), (0, b"[")])
def test_safe_credential_inventory_probe_failures_are_unknown(monkeypatch, result):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: result)
    assert mod.opencode_credential_inventory() == {"status": "unknown", "providers": []}


def test_safe_credential_inventory_limits_and_reservation(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    rows = [
        {"id": "x", "name": "X", "connections": [None]},
        {"id": "x", "name": "X", "connections": [{"type": "env"}] * 33},
        {"id": "é" * 128, "name": "有效", "connections": [{"type": "env"}]},
        {"id": "y" * 129, "name": "long", "connections": [{"type": "env"}]},
        {"id": "bad\u200b", "name": "bad", "connections": [{"type": "env"}]},
    ] + [{"id": f"empty{i}", "name": "Empty", "connections": []} for i in range(96)]
    calls = []
    def collect(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0, json.dumps(rows).encode()
    monkeypatch.setattr(mod, "bounded_output", collect)
    result = mod.opencode_credential_inventory()
    assert result == {
        "status": "partial",
        "providers": [
            {"id": "x", "name": "X", "auth_types": ["environment"], "configured": True, "verified": None},
            {"id": "é" * 128, "name": "有效", "auth_types": ["environment"], "configured": True, "verified": None},
        ],
    }
    assert calls == [(["fake", "auth", "list", "--format", "json", "--standalone"],
                      {"timeout": 5.0, "max_stdout": 1024 * 1024})]


def test_safe_credential_inventory_deep_json_and_missing_binary(monkeypatch, caplog):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: (0, b"[" * 100000 + b"]" * 100000))
    result = mod.opencode_credential_inventory()
    # Python's JSON decoder may either reject the nesting or decode a non-row.
    assert result["status"] in {"unknown", "partial"}
    assert result["providers"] == []
    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: None)
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: pytest.fail("unexpected probe"))
    assert mod.opencode_credential_inventory() == {"status": "unknown", "providers": []}
    assert not caplog.records


def test_safe_credential_inventory_never_projects_credential_fields(monkeypatch, caplog):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda: "fake")
    rows = [{"id": "custom", "name": "visible-name", "label": "label-sentinel",
             "connections": [{"type": "credential", "method": "key", "key": "key-sentinel",
                              "token": "token-sentinel", "account": "account-sentinel",
                              "path": "path-sentinel", "nested": {"value": "nested-sentinel"}},
                             {"type": "env", "name": "env-name-sentinel", "value": "env-value-sentinel"}]}]
    monkeypatch.setattr(mod, "bounded_output", lambda *args, **kwargs: (0, json.dumps(rows).encode()))
    output = json.dumps(mod.opencode_credential_inventory()) + caplog.text
    for word in ("label", "key", "token", "account", "path", "nested", "env-name", "env-value"):
        assert f"{word}-sentinel" not in output
    assert "visible-name" in output
    monkeypatch.setattr(mod, "bounded_output",
                        lambda *args, **kwargs: (0, b'[{"id":"empty","name":"Empty","connections":[]}]'))
    assert mod.opencode_credential_inventory() == {"status": "complete", "providers": []}


def test_credential_count_bounded_failures_and_legacy_fallback(monkeypatch, caplog):
    import re
    import sys
    import ciao.opencode_cli as mod

    outputs = iter([(7, b'[{"connections":[1]}]')])
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: next(outputs))
    assert mod._credential_count("synthetic", timeout=1) == 1
    for text in ["1credential", "٢\u2003credentıals", "3 credential 4 credentials",
                 "42 CREDENTIALS!", "5 credentialsx", "7 credential"]:
        match = re.search(r"(\d+)\s+credentials?\b", text, re.IGNORECASE)
        expected = int(match.group(1)) if match else None
        outputs = iter([(0, b"not json"), (0, text.encode())])
        assert mod._credential_count("synthetic", timeout=1) == expected
    old_limit = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(640)
        outputs = iter([(0, b"not json"), (0, b"9" * 10000 + b" credentials")])
        assert mod._credential_count("synthetic", timeout=1) is None
    finally:
        sys.set_int_max_str_digits(old_limit)
    outputs = iter([(0, b"[" * 2000), (0, b"7 credentials")])
    assert mod._credential_count("synthetic", timeout=1) == 7
    outputs = iter([(0, b'{"not":"a list"}'), (9, b"8 credentials")])
    assert mod._credential_count("synthetic", timeout=1) == 8
    outputs = iter([(0, b"not json"), (0, b"\xff")])
    assert mod._credential_count("synthetic", timeout=1) is None
    outputs = iter([(0, b"\xff")])
    assert mod._credential_count("synthetic", timeout=1) is None
    outputs = iter([None])
    assert mod._credential_count("synthetic", timeout=1) is None
    assert not caplog.records


def test_credential_count_digit_scan_is_linear(monkeypatch):
    import ciao.opencode_cli as mod

    class CountedText(str):
        visits = 0

        def __getitem__(self, key):
            type(self).visits += 1
            return super().__getitem__(key)

    class Ansi:
        def sub(self, replacement, text):
            return CountedText(text)

    outputs = iter([(0, b"invalid"), (0, b"9" * (1024 * 1024 - 1))])
    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: next(outputs))
    monkeypatch.setattr(mod, "_ANSI_RE", Ansi())
    assert mod._credential_count("synthetic", timeout=1) is None
    assert CountedText.visits <= 3 * (1024 * 1024)


def test_credential_count_reads_the_reported_number(monkeypatch):
    """Captured verbatim from `opencode auth list` with an empty store.

    Regression: counting non-empty lines counted the ANSI reset and the
    box-drawing characters, so an empty store was reported as several
    authenticated providers.
    """
    import ciao.opencode_cli as mod

    output = "\x1b[0m\n┌  Credentials \x1b[90m~/.local/share/opencode/auth.json\n│\n└  0 credentials\n"
    monkeypatch.setattr(
        mod, "bounded_output", lambda *a, **k: (0, output.encode())
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 0


def test_credential_count_parses_a_populated_store(monkeypatch):
    import ciao.opencode_cli as mod

    output = "┌  Credentials\n│  anthropic\n│  openai\n└  2 credentials\n"
    monkeypatch.setattr(
        mod, "bounded_output", lambda *a, **k: (0, output.encode())
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 2


def test_credential_count_reads_the_v2_json_listing(monkeypatch):
    """2.0.16 dropped the `N credentials` footer for a plain table; the JSON
    listing is what gets counted. Shape captured from `auth list --format json`
    with identifiers removed."""
    import json as _json
    import ciao.opencode_cli as mod

    listing = [
        {"id": "openai", "name": "OpenAI", "connections": [
            {"type": "credential", "label": "OAuth", "method": "oauth"},
            {"type": "env", "name": "OPENAI_API_KEY"},
        ]},
        {"id": "openrouter", "name": "OpenRouter", "connections": [
            {"type": "credential", "label": "API key", "method": "key"},
        ]},
    ]
    calls = []

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return (0, _json.dumps(listing).encode())

    monkeypatch.setattr(mod, "bounded_output", fake_run)
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 2
    assert calls == [["/bin/opencode", "auth", "list", "--format", "json", "--standalone"]]

    monkeypatch.setattr(
        mod, "bounded_output", lambda *a, **k: (0, b"[]")
    )
    assert mod._credential_count("/bin/opencode", timeout=1.0) == 0


def test_status_reports_skills_and_plugins_from_the_server(monkeypatch):
    """Settings lists what the opencode CLI brings; the V2 server's list routes
    are the source, minus built-in and failed plugins.

    Rows are trimmed from OpenCode 2.3.2's ``/api/skill`` (paths sanitised). Built-in
    skills are the ``embedded`` source, which the server reports as ``skill://<id>/SKILL.md``;
    user and plugin skills are absolute file paths.
    """
    from types import SimpleNamespace

    import ciao.opencode_cli as mod

    routes = {
        "/api/skill": [
            {"id": "opencode", "name": "opencode", "autoinvoke": True,
             "path": "skill://opencode/SKILL.md"},
            {"id": "report", "name": "report", "autoinvoke": True,
             "path": "skill://report/SKILL.md"},
            {"id": "browser", "name": "browser", "autoinvoke": True,
             "path": "skill://browser/SKILL.md"},
            {"id": "ego-browser", "name": "ego-browser", "autoinvoke": True,
             "path": "/home/user/.agents/skills/ego-browser/SKILL.md"},
            {"id": "pdf", "name": "pdf", "autoinvoke": True,
             "path": "/home/user/.claude/skills/synced/pdf/SKILL.md"},
        ],
        "/api/plugin": [
            {"id": "opencode.config.worktree", "source": {"type": "builtin"}, "state": {"status": "active"}},
            {"id": "review", "source": {"type": "local"}, "state": {"status": "active"}},
            {"id": "dupe", "source": {"type": "local"}, "state": {"status": "failed"}},
        ],
    }

    run_calls = []

    def fake_run(cmd, *a, **k):
        run_calls.append(cmd)
        if cmd[1] == "--version":
            return SimpleNamespace(stdout="opencode v2.3.2\n", returncode=0)
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(mod, "_credential_count", lambda *a, **k: 0)
    monkeypatch.setattr(
        mod,
        "_fetch_inventory_rows",
        lambda binary, **_k: (routes["/api/skill"], routes["/api/plugin"]),
    )

    status = mod.opencode_login_status()

    assert status["skills"] == ["ego-browser", "pdf", "review"]
    assert status["bundled_skills"] == ["opencode", "report", "browser"]
    assert "mcps" not in status
    assert not any(len(cmd) > 1 and cmd[1] == "api" for cmd in run_calls)


def test_status_leaves_out_lists_the_server_could_not_report(monkeypatch):
    """A failed list route must not read as "nothing installed"."""
    from types import SimpleNamespace

    import ciao.opencode_cli as mod

    def fake_run(cmd, *a, **k):
        if cmd[1] == "--version":
            return SimpleNamespace(stdout="opencode v2.3.2\n", returncode=0)
        return SimpleNamespace(stdout="[]", returncode=0)

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(mod, "_credential_count", lambda *a, **k: 0)
    monkeypatch.setattr(mod, "_fetch_inventory_rows", lambda binary, **_k: (None, []))

    status = mod.opencode_login_status()

    assert status["ok"] is True
    assert "skills" not in status
    assert "bundled_skills" not in status
    assert "mcps" not in status


def _inventory_client(handler):
    import httpx

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://t")


def test_inventory_waits_until_plugins_load(monkeypatch):
    import time
    from types import SimpleNamespace

    import httpx

    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "_INVENTORY_POLL", 0)
    seen: list[httpx.Request] = []
    plugin_calls = 0
    builtin = {"id": "opencode", "path": "/builtin/opencode.md"}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal plugin_calls
        seen.append(request)
        if request.url.path == "/api/plugin":
            plugin_calls += 1
            if plugin_calls <= 2:
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": [
                {"id": "opencode.x", "source": {"type": "builtin"}, "state": {"status": "active"}}
            ]})
        if request.url.path == "/api/skill":
            if plugin_calls <= 2:
                return httpx.Response(200, json={"data": [builtin, {"id": "opencode2"}]})
            return httpx.Response(200, json={"data": [builtin, {"id": "pdf"}]})
        return httpx.Response(404)

    with _inventory_client(handler) as client:
        skills, plugins = mod._read_loaded_inventory(
            client, SimpleNamespace(poll=lambda: None),
            deadline=time.monotonic() + 5, location="/home/x",
        )

    assert any(row["id"] == "pdf" for row in skills)
    assert len(plugins) == 1
    assert not any(r.url.path == "/api/mcp" for r in seen)
    assert all(r.url.params["location[directory]"] == "/home/x" for r in seen)
    assert sum(1 for r in seen if r.url.path == "/api/plugin") == 3


def test_inventory_is_unknown_when_the_server_never_loads(monkeypatch):
    import time
    from types import SimpleNamespace

    import httpx

    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "_INVENTORY_POLL", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with _inventory_client(handler) as client:
        rows = mod._read_loaded_inventory(
            client, SimpleNamespace(poll=lambda: None),
            deadline=time.monotonic() + 0.05, location="/home/x",
        )

    assert rows == (None, None)


def test_inventory_is_unknown_when_the_server_exits():
    import time
    from types import SimpleNamespace

    import httpx

    import ciao.opencode_cli as mod

    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, json={"data": []})

    with _inventory_client(handler) as client:
        rows = mod._read_loaded_inventory(
            client, SimpleNamespace(poll=lambda: 1),
            deadline=time.monotonic() + 5, location="/home/x",
        )

    assert rows == (None, None)
    assert called == []


def _recorded_serve_spawn(monkeypatch) -> tuple[list[str], dict]:
    import subprocess

    import ciao.opencode_cli as mod
    from ciao.providers.acp import connection

    recorded = []

    def fake_popen(argv, **kwargs):
        recorded.append((argv, kwargs))
        raise OSError("no spawn in tests")

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # The login-shell PATH probe spawns a shell of its own; keep it out of this test.
    monkeypatch.setattr(connection, "terminal_path", lambda: "")

    assert mod._fetch_inventory_rows("/bin/opencode") == (None, None)
    return next(
        (argv, kwargs) for argv, kwargs in recorded if argv[1:2] == ["serve"]
    )


def test_inventory_spawns_a_private_server_in_home_not_the_service(monkeypatch):
    argv, kwargs = _recorded_serve_spawn(monkeypatch)
    assert argv[:2] == ["/bin/opencode", "serve"]
    assert "--port" in argv
    assert argv[-2:] == ["--hostname", "127.0.0.1"]
    assert "api" not in argv
    assert "--service" not in argv
    assert kwargs["cwd"] == str(Path.home())
    assert "OPENCODE_SERVER_PASSWORD" in kwargs["env"]


def test_inventory_server_env_withholds_engine_secrets(monkeypatch):
    """The private server gets the agent base env, never CIAO_* or the PWA token.

    Mutation check: building the env from os.environ instead of agent_base_env(())
    makes CIAO_SECRET and PWA_AUTH_TOKEN reach the child and fails this test.
    """
    monkeypatch.setenv("CIAO_SECRET_PROBE", "ciao-sentinel")
    monkeypatch.setenv("PWA_AUTH_TOKEN", "pwa-sentinel")
    monkeypatch.setenv("OPENCODE_PROBE_KEEP", "keep-sentinel")
    _argv, kwargs = _recorded_serve_spawn(monkeypatch)
    env = kwargs["env"]
    assert "CIAO_SECRET_PROBE" not in env
    assert "PWA_AUTH_TOKEN" not in env
    assert env["OPENCODE_PROBE_KEEP"] == "keep-sentinel"
    assert env["OPENCODE_SERVER_PASSWORD"]


def test_login_status_without_inventory_starts_no_server(monkeypatch):
    from types import SimpleNamespace

    import ciao.opencode_cli as mod

    def no_server(*_a, **_k):
        raise AssertionError("inventory=False must not start a server")

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    monkeypatch.setattr(
        "subprocess.run", lambda *a, **k: SimpleNamespace(stdout="opencode v2.3.2\n", returncode=0)
    )
    monkeypatch.setattr(mod, "_credential_count", lambda *a, **k: 1)
    monkeypatch.setattr(mod, "_fetch_inventory_rows", no_server)

    status = mod.opencode_login_status(inventory=False)

    assert status["ok"] is True
    assert "skills" not in status


def test_system_skills_lists_every_skill_row(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "_system_skills_cache", None)
    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    skills = [
        {"name": "loop-engineering", "path": "/Users/x/.claude/skills/loop-engineering/SKILL.md"},
        {"name": "opencode", "path": "/builtin/opencode"},
    ]
    monkeypatch.setattr(
        mod,
        "_fetch_inventory_rows",
        lambda binary, **_k: (skills, [{"id": "review"}]),
    )

    assert mod.opencode_system_skills() == ["loop-engineering", "opencode"]


def test_system_skills_cached_within_ttl(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "_system_skills_cache", None)
    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    calls = []

    def fetch(binary, **_k):
        calls.append(binary)
        return ([{"name": "pdf"}], [])

    monkeypatch.setattr(mod, "_fetch_inventory_rows", fetch)
    start = 1000.0
    monkeypatch.setattr(mod.time, "monotonic", lambda: start)

    assert mod.opencode_system_skills() == ["pdf"]
    assert mod.opencode_system_skills() == ["pdf"]
    assert len(calls) == 1

    monkeypatch.setattr(mod.time, "monotonic", lambda: start + 301)
    assert mod.opencode_system_skills() == ["pdf"]
    assert len(calls) == 2


def test_system_skills_empty_without_binary_or_server(monkeypatch):
    import ciao.opencode_cli as mod

    calls = []

    def fetch(binary, **_k):
        calls.append(binary)
        return (None, None)

    monkeypatch.setattr(mod, "_fetch_inventory_rows", fetch)

    monkeypatch.setattr(mod, "_system_skills_cache", None)
    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: None)
    assert mod.opencode_system_skills() == []
    assert calls == []

    monkeypatch.setattr(mod, "_system_skills_cache", None)
    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: "/bin/opencode")
    assert mod.opencode_system_skills() == []
    assert mod.opencode_system_skills() == []
    assert len(calls) == 1


def test_system_skills_binary_error_is_empty(monkeypatch):
    import ciao.opencode_cli as mod

    def broken(_env=None):
        raise OSError("broken")

    monkeypatch.setattr(mod, "_system_skills_cache", None)
    monkeypatch.setattr(mod, "resolve_opencode_binary", broken)

    assert mod.opencode_system_skills() == []


def test_credential_count_is_unknown_when_the_cli_fails(monkeypatch):
    import ciao.opencode_cli as mod

    monkeypatch.setattr(mod, "bounded_output", lambda *a, **k: None)
    assert mod._credential_count("/bin/opencode", timeout=1.0) is None

def test_login_status_reports_a_missing_binary_as_an_install_step(
    tmp_path: Path, monkeypatch
):
    """No binary means no chats, so the row asks for an install, not a login.

    The install branch is shared with Claude: `auth="not_installed"` with a
    one-line installer, the download page, and a PATH line when the installer
    drops the binary off the default PATH. An empty hint is omitted, the same
    way Claude's row omits it. Before this the row said `auth="missing"` with
    a bare `opencode` command, so the provider page and the wizard showed
    nothing a user could run.
    """
    from ciao import opencode_cli as mod

    monkeypatch.setattr(mod, "resolve_opencode_binary", lambda _env=None: None)

    row = mod.opencode_login_status()

    assert row["ok"] is False
    assert row["auth"] == "not_installed"
    assert row["install_url"] == mod.OPENCODE_INSTALL_DOCS_URL
    assert "opencode" in row["command"]
    hint = mod.opencode_path_hint()
    if hint:
        assert row["path_command"] == hint
    else:
        assert "path_command" not in row
    assert "not installed" in row["detail"]


