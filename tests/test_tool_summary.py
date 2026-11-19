from collections import UserDict
import ast
import importlib
from pathlib import Path

import pytest

from ciao.tool_summary import (
    opencode_file_touches,
    summarize_claude_tool_input,
    summarize_opencode_tool_input,
)


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("Read", {}, ""),
        ("Read", {"path": "x"}, ""),
        ("read", {"file_path": "x"}, "x"),
        ("Edit", {"file_path": "a"}, "a"),
        ("edit", {"path": "x"}, "x"),
        ("Edit", {"path": "x"}, "x"),
        ("write", {"path": "x"}, "x"),
        ("Write", {"file_path": "a"}, "a"),
        ("Write", {"path": "x"}, "x"),
        ("grep", {"pattern": "p"}, '"p"'),
        ("Grep", {"pattern": "p", "path": "x"}, '"p" in x'),
        ("Glob", {"pattern": "p", "path": "x"}, "p in x"),
        ("glob", {"pattern": "p"}, "p"),
        ("Glob", {"pattern": "p"}, "p"),
        ("bash", {"command": "cmd"}, "cmd"),
        ("Bash", {"command": "x", "description": "d"}, "d"),
        ("Bash", {"command": "x" * 120}, "x" * 120),
        ("Bash", {"command": "x" * 121}, "x" * 120 + "..."),
        ("Bash", {"command": "x", "description": "d" * 121}, "d" * 121),
        ("Agent", {"subagent_type": "a"}, "a"),
        ("agent", {"description": "d"}, "d"),
        ("Agent", {"description": "d"}, "d"),
        ("Agent", {}, ""),
        ("Agent", {"subagent_type": "a", "description": "d"}, "[a] d"),
        ("TaskCreate", {"subject": "task"}, "task"),
        ("TaskUpdate", {"taskId": 2}, "#2"),
        ("TaskUpdate", {"taskId": 2, "status": "done"}, "#2 → done"),
        ("Skill", {"skill": "s"}, "s"),
        ("WebSearch", {"query": "q"}, "q"),
        ("WebFetch", {"url": "x" * 100}, "x" * 100),
        ("WebFetch", {"url": "x" * 101}, "x" * 100),
        ("NotebookEdit", {"cell_id": "c"}, "c"),
        ("mcp__x", {"query": "", "text": "t"}, "t"),
        ("mcp__x", {"query": 3}, "3"),
        ("mcp__x", {"query": "q" * 100}, "q" * 100),
        ("mcp__x", {"query": "q" * 101}, "q" * 100),
        ("mcp__x", {"query": "first", "text": "second"}, "first"),
        ("unknown", {"a": "first", "b": "second"}, "first"),
        ("unknown", {"a": "x" * 100}, "x" * 100),
        ("unknown", {"a": "x" * 101}, "x" * 100),
    ],
)
def test_claude_tool_summary_preserves_tool_specific_rules(name, data, expected):
    assert summarize_claude_tool_input(name, data) == expected


def test_claude_question_summary_preserves_json_and_errors():
    assert summarize_claude_tool_input("AskUserQuestion", {"questions": ["é"]}) == (
        '{"questions": ["é"]}'
    )
    assert summarize_claude_tool_input("AskUserQuestion", {}) == ""
    assert summarize_claude_tool_input(
        "AskUserQuestion", {"questions": [{"options": [{"label": "é"}]}]}
    ) == '{"questions": [{"options": [{"label": "é"}]}]}'
    assert summarize_claude_tool_input("AskUserQuestion", {"questions": []}) == ""
    assert summarize_claude_tool_input(
        "AskUserQuestion", {"questions": [{"value": {1}}]}
    ) == '{"questions": [{"value": "{1}"}]}'
    cycle = []
    cycle.append(cycle)
    assert summarize_claude_tool_input(
        "AskUserQuestion", {"questions": cycle}
    ) == ""


def test_opencode_tool_summary_preserves_selection_and_fallback():
    assert summarize_opencode_tool_input("x", None) == ""
    assert summarize_opencode_tool_input("x", "text") == ""
    assert summarize_opencode_tool_input("x", ["text"]) == ""
    assert summarize_opencode_tool_input("x", {}) == ""
    fields = ("filePath", "path", "file", "pattern", "query", "command", "description")
    for index, expected in enumerate(fields):
        raw = {field: field for field in fields[index:]}
        assert summarize_opencode_tool_input("x", raw) == expected
    assert summarize_opencode_tool_input(
        "x", {"filePath": 1, "path": " \n ", "file": "chosen"}
    ) == "chosen"
    assert summarize_opencode_tool_input(
        "x", {"command": "\n  first  \n\n second\n third \n"}
    ) == "first (+2 lines)"
    assert summarize_opencode_tool_input(
        "x", UserDict({"command": "pwd"})
    ) == "pwd"
    assert summarize_opencode_tool_input("x", {"id": "loop-engineering"}) == (
        '{"id": "loop-engineering"}'
    )
    assert summarize_opencode_tool_input("x", {"command": " \n "}) == (
        '{"command": " \\n "}'
    )
    assert summarize_opencode_tool_input("x", {"id": "é"}) == '{"id": "é"}'


def test_opencode_tool_summary_preserves_multiline_and_limits():
    assert summarize_opencode_tool_input("x", {"command": "x" * 400}) == "x" * 400
    assert summarize_opencode_tool_input("x", {"command": "x" * 401}) == "x" * 400
    assert summarize_opencode_tool_input("x", {"command": "x" * 401 + "\nsecond"}) == (
        "x" * 400 + " (+1 lines)"
    )
    fallback = summarize_opencode_tool_input("x", {"id": "x" * 210})
    assert fallback == '{"id": "' + "x" * 192


def test_opencode_summary_keeps_serialization_errors():
    with pytest.raises(TypeError):
        summarize_opencode_tool_input("x", {"id": object()})
    raw = {}
    raw["self"] = raw
    with pytest.raises(ValueError):
        summarize_opencode_tool_input("x", raw)


@pytest.mark.parametrize(
    ("tool", "raw", "expected"),
    [
        ("unknown", {}, None),
        ("write", None, None),
        ("read", {"path": "x"}, None),
        ("unknown", {"path": "x"}, None),
        ("write", {}, None),
        ("write", {"filePath": " a.md ", "path": "b.md"}, [{"file_path": "a.md", "action": "write"}]),
        ("WRITE", {"path": "b.md"}, [{"file_path": "b.md", "action": "write"}]),
        ("edit", {"path": "b.md"}, [{"file_path": "b.md", "action": "edit"}]),
        ("PATCH", {"path": "b.md"}, [{"file_path": "b.md", "action": "edit"}]),
        ("write", {"filePath": "", "path": "b.md"}, [{"file_path": "b.md", "action": "write"}]),
        ("write", {"filePath": 1, "path": "b.md"}, None),
        ("write", {"filePath": True, "path": "b.md"}, None),
        ("write", {"path": "  "}, None),
        ("write", UserDict({"path": "x"}), [{"file_path": "x", "action": "write"}]),
    ],
)
def test_opencode_file_touches_preserve_actions_and_path_selection(tool, raw, expected):
    assert opencode_file_touches(tool, raw) == expected


def test_opencode_shell_touches_delegate_raw_input(monkeypatch):
    from ciao.web import chat_broker

    raw = {"command": "pwd", "description": "ignored"}
    sentinel = [{"file_path": "x", "action": "created"}]
    seen = []

    def extract(tool, value):
        seen.append((tool, value))
        return sentinel

    monkeypatch.setattr(chat_broker, "extract_file_touches", extract)
    for tool in ("shell", "SHELL", "bash", "Bash"):
        assert opencode_file_touches(tool, raw) is sentinel
    assert len(seen) == 4
    assert all(tool == "bash" and value is raw for tool, value in seen)
    monkeypatch.setattr(chat_broker, "extract_file_touches", lambda *_: [])
    assert opencode_file_touches("bash", raw) is None


def test_opencode_shell_touches_keep_existing_parser_results():
    assert opencode_file_touches(
        "shell", {"command": "ciao file surface memory-vault/x.html"}
    ) == [{"file_path": "memory-vault/x.html", "action": "surfaced"}]
    assert opencode_file_touches(
        "bash", {"command": "echo hi > out/a.md"}
    ) == [{"file_path": "out/a.md", "action": "created"}]
    assert opencode_file_touches("shell", {"command": "pwd"}) is None


def test_tool_summary_ownership():
    source = Path(__file__).parents[1]
    public_names = {
        "summarize_claude_tool_input",
        "summarize_opencode_tool_input",
        "opencode_file_touches",
    }
    module = ast.parse((source / "ciao/tool_summary.py").read_text())
    definitions = [
        node.name for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert {name: definitions.count(name) for name in public_names} == {
        name: 1 for name in public_names
    }
    assert not any(
        (isinstance(node, ast.ImportFrom)
         and (
             (node.module or "").startswith("ciao.providers")
             or (
                 node.module == "ciao"
                 and any(alias.name == "providers" for alias in node.names)
             )
         ))
        or (isinstance(node, ast.Import)
            and any(alias.name.startswith("ciao.providers") for alias in node.names))
        for node in ast.walk(module)
    )
    old_names = {
        "_summarize_tool_input",
        "_file_touches",
        "_opencode_file_touches",
        "_summarize_opencode_tool_input",
    }
    for relative in (
        ("ciao/providers/opencode.py", "_summarize_tool_input"),
        ("ciao/providers/opencode.py", "_file_touches"),
        ("ciao/web/transcript_service.py", "_summarize_tool_input"),
        ("ciao/web/transcript_service.py", "_opencode_file_touches"),
        ("ciao/web/transcript_service.py", "_summarize_opencode_tool_input"),
    ):
        tree = ast.parse((source / relative[0]).read_text())
        assert not any(
            (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in old_names)
            or (isinstance(node, (ast.Import, ast.ImportFrom))
                and any(alias.name in old_names or alias.asname in old_names
                        for alias in node.names))
            or (isinstance(node, ast.Name)
                and node.id in old_names and isinstance(node.ctx, ast.Store))
            for node in ast.walk(tree)
        )
    from ciao.providers import opencode
    from ciao.web import transcript_service

    exports = {
        "ciao.tool_summary": importlib.import_module("ciao.tool_summary"),
        "ciao.providers.opencode": opencode,
        "ciao.web.transcript_service": transcript_service,
    }
    bindings = {
        ("ciao.providers.opencode", "summarize_opencode_tool_input"),
        ("ciao.providers.opencode", "opencode_file_touches"),
        ("ciao.web.transcript_service", "summarize_claude_tool_input"),
        ("ciao.web.transcript_service", "summarize_opencode_tool_input"),
        ("ciao.web.transcript_service", "opencode_file_touches"),
    }
    for owner, name in bindings:
        function = getattr(exports["ciao.tool_summary"], name)
        assert function.__module__ == "ciao.tool_summary"
        assert hasattr(exports[owner], name)
        assert getattr(exports[owner], name) is function
