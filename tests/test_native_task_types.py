"""``is_process_task``: the Claude-native process-versus-agent task classification.

Pure-function characterization — the case-sensitive ``"agent"`` substring
truth table, the single no-default ``get("task_type")`` read, input
immutability and exception propagation — plus source-ownership assertions
that the remaining caller (the host Processes projection) takes the
predicate from its neutral owner.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from ciao.native_task_types import is_process_task


class _SentinelGetError(Exception):
    """Raised by the get seam so propagation can be pinned by identity."""


_TRUE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("no task_type key at all", {}),
    ("None task_type", {"task_type": None}),
    ("empty task_type", {"task_type": ""}),
    ("shell-flavoured local_bash", {"task_type": "local_bash"}),
    ("monitor-flavoured monitor_mcp", {"task_type": "monitor_mcp"}),
    ("upper-case AGENT is not the substring", {"task_type": "AGENT"}),
    ("mixed-case Agent is not the substring", {"task_type": "Agent"}),
    ("False task_type stringifies to the falsey stand-in", {"task_type": False}),
    ("zero task_type stringifies to the falsey stand-in", {"task_type": 0}),
    ("a plain int task_type never contains the substring", {"task_type": 42}),
]

_FALSE_CASES: list[tuple[str, dict[str, Any]]] = [
    ("lower-case agent", {"task_type": "agent"}),
    ("local_agent carries the substring", {"task_type": "local_agent"}),
    ("remote_agent carries the substring", {"task_type": "remote_agent"}),
    ("nonagent carries the substring", {"task_type": "nonagent"}),
    ("agent_monitor carries the substring", {"task_type": "agent_monitor"}),
    ("any task_type value is str()ed first", {"task_type": ["agent"]}),
]


@pytest.mark.parametrize(("name", "info"), _TRUE_CASES, ids=[name for name, _ in _TRUE_CASES])
def test_process_task_is_true_for_missing_falsey_and_process_values(
    name: str, info: dict[str, Any]
) -> None:
    assert is_process_task(info) is True


@pytest.mark.parametrize(("name", "info"), _FALSE_CASES, ids=[name for name, _ in _FALSE_CASES])
def test_process_task_is_false_when_the_task_type_string_contains_agent(
    name: str, info: dict[str, Any]
) -> None:
    assert is_process_task(info) is False


def test_predicate_leaves_the_task_dictionary_untouched() -> None:
    agent_row: dict[str, Any] = {
        "id": "task-1",
        "task_type": "local_agent",
        "command": "python run.py",
        "started_at": 1730000000.0,
        "metadata": {"turn": 3, "tags": ["resume"], "depth": {"nested": {"leaf": True}}},
    }
    agent_snapshot: dict[str, Any] = {
        "id": "task-1",
        "task_type": "local_agent",
        "command": "python run.py",
        "started_at": 1730000000.0,
        "metadata": {"turn": 3, "tags": ["resume"], "depth": {"nested": {"leaf": True}}},
    }
    shell_row: dict[str, Any] = {
        "id": "task-2",
        "task_type": "local_bash",
        "command": "sleep 5",
        "extra": {"unused": [1, 2]},
    }
    shell_snapshot: dict[str, Any] = {
        "id": "task-2",
        "task_type": "local_bash",
        "command": "sleep 5",
        "extra": {"unused": [1, 2]},
    }

    assert is_process_task(agent_row) is False
    assert is_process_task(shell_row) is True

    # The classification reads a task row it does not own: compare the whole
    # input afterwards with independently written snapshots.
    assert agent_row == agent_snapshot
    assert shell_row == shell_snapshot


class _RecordingMapping(Mapping[str, Any]):
    """Mapping stub that records the exact ``get`` call it receives."""

    def __init__(
        self,
        *,
        items: dict[str, Any] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self._items: dict[str, Any] = {} if items is None else items
        self._raises = raises
        # (args, kwargs) as passed: an omitted default keeps args singleton.
        self.get_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def get(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.get_calls.append((args, kwargs))
        if self._raises is not None:
            raise self._raises
        return self._items.get(*args, **kwargs)

    def __getitem__(self, key: str) -> Any:
        return self._items[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


def test_predicate_reads_task_type_exactly_once_with_no_default() -> None:
    spy = _RecordingMapping(items={"task_type": "local_bash"})

    assert is_process_task(spy) is True

    # Exactly one read, positional key, default omitted.
    assert spy.get_calls == [(("task_type",), {})]


def test_predicate_propagates_a_mapping_get_error_unwrapped() -> None:
    marker = _SentinelGetError("custom mapping exploded")
    spy = _RecordingMapping(raises=marker)

    with pytest.raises(_SentinelGetError) as excinfo:
        is_process_task(spy)

    assert excinfo.value is marker


# ── neutral ownership ───────────────────────────────────────────────────
#
# The predicate is defined once, in ciao.native_task_types. The Claude
# adapter's process-task stop guard left with the background-stop route, so
# the host Processes projection in routes_api.py is the only remaining
# caller and the only module these guards assert. AST checks are pinned to
# the parsed source trees, not line numbers.

_NEUTRAL_MODULE = "native_task_types.py"
_PREDICATE = "is_process_task"
# Production modules that still import and call the predicate.
_REMAINING_CALLERS: tuple[tuple[str, ...], ...] = (("web", "routes_api.py"),)


def _ciao_source(*parts: str) -> str:
    path = (Path(__file__).resolve().parent.parent / "ciao").joinpath(*parts)
    assert path.exists(), f"{path} does not exist: the neutral owner has not been created"
    return path.read_text(encoding="utf-8")


def _all_function_names(source: str) -> list[str]:
    return [
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _imports_by_module(source: str) -> dict[str, set[str]]:
    modules: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.setdefault(node.module, set()).update(a.name for a in node.names)
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.setdefault(alias.name, set())
    return modules


def test_predicate_is_defined_once_in_the_neutral_module_only() -> None:
    neutral = _all_function_names(_ciao_source(_NEUTRAL_MODULE))

    assert neutral.count(_PREDICATE) == 1
    for parts in _REMAINING_CALLERS:
        assert _PREDICATE not in _all_function_names(_ciao_source(*parts))


def test_remaining_callers_import_the_predicate_from_the_neutral_module() -> None:
    for parts in _REMAINING_CALLERS:
        imports = _imports_by_module(_ciao_source(*parts))
        assert _PREDICATE in imports["ciao.native_task_types"]
        # Classification stays in the neutral module: a caller must not take
        # the predicate from the Claude adapter.
        assert _PREDICATE not in imports.get("ciao.providers.claude", set())


def test_native_task_types_imports_only_the_declared_stdlib_typing() -> None:
    imports = _imports_by_module(_ciao_source(_NEUTRAL_MODULE))

    # Only the two required type imports: no application, provider, SDK,
    # configuration, filesystem, process or network dependency at all.
    assert imports == {"collections.abc": {"Mapping"}, "typing": {"Any"}}


def _called_names(source: str) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def test_remaining_callers_still_call_the_neutral_predicate() -> None:
    for parts in _REMAINING_CALLERS:
        assert _PREDICATE in _called_names(_ciao_source(*parts))
