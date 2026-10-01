"""Every text read, write and decoded subprocess in ``ciao/`` names its encoding.

Without ``encoding=`` Python falls back to the locale's encoding, which is
UTF-8 on macOS and Linux and the ANSI code page (cp1252 and friends) on
Windows: a UTF-8 note, config or tool output then fails to decode, or is
written in an encoding nothing else reads (#696). The maintainer's decision is
an explicit ``encoding=`` at every call site rather than relying on Python's
UTF-8 mode, so this scan fails on any new call without one.

It flags, when no ``encoding=`` keyword is given:

- ``open()`` / ``io.open()`` / ``Path.open()`` in a text mode, and
  ``os.fdopen()`` / the ``tempfile`` text-file constructors in a text mode;
- ``Path.read_text()`` / ``Path.write_text()``;
- ``subprocess.run`` / ``Popen`` / ``check_output`` / ``call`` /
  ``check_call`` asked to decode (``text=True`` or ``universal_newlines=True``).

A mode that is not a string literal counts as text: the scan cannot prove it
binary, and naming the encoding there costs nothing.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_PACKAGE = Path(__file__).resolve().parent.parent / "ciao"
_SUBPROCESS = {"run", "Popen", "check_output", "call", "check_call"}
_TEMPFILE = {"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"}


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def _is_text_mode(mode: ast.expr | None, *, default_text: bool) -> bool:
    if mode is None:
        return default_text
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return "b" not in mode.value
    return True


def _truthy(value: ast.expr | None) -> bool:
    return value is not None and not (isinstance(value, ast.Constant) and not value.value)


def _problem(call: ast.Call) -> str | None:
    keywords = _keywords(call)
    if "encoding" in keywords:
        return None
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    owner = func.value.id if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) else ""

    if name in {"read_text", "write_text"} and isinstance(func, ast.Attribute):
        return f"{name}() without encoding="
    if name == "open":
        if isinstance(func, ast.Name) or owner == "io":
            # builtins/io.open(file, mode, ...)
            mode = keywords.get("mode", call.args[1] if len(call.args) > 1 else None)
        elif not call.args or (
            isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str)
        ) or "mode" in keywords:
            # Path.open(mode, ...): no positional, or a literal mode first. Any
            # other `.open(x, ...)` is someone else's (os.open, tarfile.open,
            # ZipFile.open, urllib's opener, webbrowser) and is not text I/O.
            mode = keywords.get("mode", call.args[0] if call.args else None)
        else:
            return None
        if _is_text_mode(mode, default_text=True):
            return "text-mode open() without encoding="
        return None
    if name == "fdopen" and owner == "os":
        mode = keywords.get("mode", call.args[1] if len(call.args) > 1 else None)
        if _is_text_mode(mode, default_text=True):
            return "text-mode os.fdopen() without encoding="
        return None
    if name in _TEMPFILE:
        mode = keywords.get("mode", call.args[0] if call.args else None)
        if _is_text_mode(mode, default_text=False):
            return f"text-mode tempfile.{name}() without encoding="
        return None
    if name in _SUBPROCESS and owner in {"subprocess", ""}:
        if _truthy(keywords.get("text")) or _truthy(keywords.get("universal_newlines")):
            return f"subprocess {name}() decodes text without encoding="
    return None


def _findings() -> list[str]:
    found = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                problem = _problem(node)
                if problem:
                    rel = path.relative_to(_PACKAGE.parent).as_posix()
                    found.append(f"{rel}:{node.lineno}: {problem}")
    return found


def test_every_text_io_in_ciao_names_its_encoding() -> None:
    findings = _findings()
    assert not findings, "name the encoding (see this module's docstring):\n" + "\n".join(findings)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ("open(p)", True),
        ("open(p, 'w')", True),
        ("open(p, 'rb')", False),
        ("open(p, encoding='utf-8')", False),
        ("open(p, mode)", True),
        ("io.open(p, 'a')", True),
        ("p.open('w')", True),
        ("p.open('ab')", False),
        ("p.open()", True),
        ("os.open(p, os.O_RDONLY)", False),
        ("tarfile.open(tmp, 'w:gz')", False),
        ("zf.open(info)", False),
        ("opener.open(request, timeout=5)", False),
        ("webbrowser.open(url)", False),
        ("os.fdopen(fd, 'w')", True),
        ("os.fdopen(fd, 'wb')", False),
        ("p.read_text()", True),
        ("p.write_text(t, encoding='utf-8')", False),
        ("tempfile.NamedTemporaryFile('w', delete=False)", True),
        ("tempfile.NamedTemporaryFile(delete=False)", False),
        ("subprocess.run(cmd, text=True)", True),
        ("subprocess.run(cmd, capture_output=True)", False),
        ("subprocess.run(cmd, text=True, encoding='utf-8')", False),
        ("subprocess.check_output(cmd, universal_newlines=True)", True),
        ("subprocess.Popen(cmd, text=False)", False),
    ],
)
def test_the_scan_itself(source: str, flagged: bool) -> None:
    call = ast.parse(source, mode="eval").body
    assert isinstance(call, ast.Call)
    assert (_problem(call) is not None) is flagged
