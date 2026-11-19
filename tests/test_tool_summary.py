import ast
import importlib
from pathlib import Path

import pytest

from ciao.tool_summary import (
    summarize_claude_tool_input,
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


def test_tool_summary_ownership():
    source = Path(__file__).parents[1]
    public_names = {
        "summarize_claude_tool_input",
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
    from ciao.web import transcript_service

    exports = {
        "ciao.tool_summary": importlib.import_module("ciao.tool_summary"),
        "ciao.web.transcript_service": transcript_service,
    }
    bindings = {
        ("ciao.web.transcript_service", "summarize_claude_tool_input"),
    }
    for owner, name in bindings:
        function = getattr(exports["ciao.tool_summary"], name)
        assert function.__module__ == "ciao.tool_summary"
        assert hasattr(exports[owner], name)
        assert getattr(exports[owner], name) is function
