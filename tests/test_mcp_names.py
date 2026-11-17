"""The claude.ai connector-name predicate: literal characterization and ownership.

``is_claudeai_connector`` is a Claude-shaped naming rule — a server named by
the claude.ai account (``claude.ai …`` prefix), connected on claude.ai and
never by ``claude mcp login``. It classifies names literally: case-sensitive,
the trailing ASCII space is required, and nothing is stripped, normalized or
parsed, so ``"claude.ai "`` itself is a connector and a URL or ``mcp__``-prefixed
tool name never is.

The definition lives once, in the neutral ``ciao.mcp_names`` — not the concrete
Claude adapter — and the adapter's catalog watcher plus every web host import
it from there. The ownership scan keeps it that way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from ciao.mcp_names import is_claudeai_connector

_REPO = Path(__file__).resolve().parents[1]
_NAMES = _REPO / "ciao" / "mcp_names.py"

# The adapter (its catalog watcher) and the three web hosts that classify
# server names: mcp_auth refusal, auth-card suppression and inventory scope.
_CONSUMERS = (
    "ciao/providers/claude.py",
    "ciao/web/mcp_inventory.py",
    "ciao/web/mcp_auth_claude.py",
    "ciao/web/chat_actions_runtime.py",
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # True: the literal "claude.ai " prefix, empty or nonempty suffix.
        ("claude.ai ", True),
        ("claude.ai Gmail", True),
        ("claude.ai incident.io", True),
        ("claude.ai  Gmail", True),
        ("claude.ai \u2603", True),
        # False: wrong case, missing/wrong whitespace, other spellings.
        ("", False),
        ("claude.ai", False),
        ("Claude.ai Gmail", False),
        ("CLAUDE.AI Gmail", False),
        (" claude.ai Gmail", False),
        ("claude.ai\tGmail", False),
        ("claude.ai\u00a0Gmail", False),
        ("claude.aix Gmail", False),
        ("https://claude.ai Gmail", False),
        ("mcp__claude.ai Gmail", False),
    ],
)
def test_is_claudeai_connector_classifies_names_literally(name: str, expected: bool) -> None:
    assert is_claudeai_connector(name) is expected


def test_mcp_names_owns_one_definition_and_no_imports() -> None:
    """The owner stays pure: one literal constant, the exact one-line predicate, zero imports."""
    tree = ast.parse(_NAMES.read_text(encoding="utf-8"), filename=str(_NAMES))

    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert imports == [], "ciao/mcp_names.py must stay import-free"

    constants = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_CLAUDEAI_PREFIX" for t in n.targets)
    ]
    assert len(constants) == 1
    value = constants[0].value
    assert isinstance(value, ast.Constant) and value.value == "claude.ai "

    functions = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "is_claudeai_connector"
    ]
    assert len(functions) == 1
    (fn,) = functions
    (fn_arg,) = fn.args.args
    arg_annotation, return_annotation = fn_arg.annotation, fn.returns
    assert arg_annotation is not None and return_annotation is not None
    assert ast.unparse(arg_annotation) == "str"
    assert ast.unparse(return_annotation) == "bool"
    # The account-managed / never-CLI-login meaning, one startswith on the constant.
    assert "claude.ai account" in (ast.get_docstring(fn) or "")
    assert "claude mcp login" in (ast.get_docstring(fn) or "")
    (stmt,) = [s for s in fn.body if not isinstance(s, ast.Expr)]
    assert isinstance(stmt, ast.Return)
    (call_value,) = [s.value for s in fn.body if isinstance(s, ast.Return)]
    assert isinstance(call_value, ast.Call)
    func = call_value.func
    assert isinstance(func, ast.Attribute) and func.attr == "startswith"
    assert isinstance(func.value, ast.Name) and func.value.id == "name"
    assert len(call_value.args) == 1
    (arg,) = call_value.args
    assert isinstance(arg, ast.Name) and arg.id == "_CLAUDEAI_PREFIX"


def test_exactly_one_definition_exists_across_ciao() -> None:
    """One predicate definition, one prefix assignment — both in the neutral owner, nowhere else."""
    found: list[str] = []
    for path in sorted((_REPO / "ciao").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "is_claudeai_connector":
                found.append(str(path.relative_to(_REPO)))
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_CLAUDEAI_PREFIX" for t in node.targets
            ):
                found.append(str(path.relative_to(_REPO)))
    assert found == [str(_NAMES.relative_to(_REPO)), str(_NAMES.relative_to(_REPO))]


@pytest.mark.parametrize("relative", _CONSUMERS)
def test_consumer_imports_the_predicate_from_the_neutral_owner(relative: str) -> None:
    """Every consumer, adapter included, imports the predicate from ``ciao.mcp_names``."""
    path = _REPO / relative
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sources = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and any(alias.name == "is_claudeai_connector" for alias in node.names)
    ]
    assert sources == ["ciao.mcp_names"]


def test_no_module_imports_the_predicate_from_the_adapter() -> None:
    """No module uses the concrete Claude adapter as the owner of this naming rule."""
    offenders: list[str] = []
    for path in sorted((_REPO / "ciao").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "ciao.providers.claude"
                and any(alias.name == "is_claudeai_connector" for alias in node.names)
            ):
                offenders.append(str(path.relative_to(_REPO)))
    assert offenders == []
