"""Cross-host strict decision test; native Windows tests remain authoritative."""
import ast
from pathlib import Path

import pytest


def test_windows_strict_checks_protection_without_changing_legacy_answers():
    source = Path(__file__).parents[1] / "ciao/os_support/private.py"
    tree = ast.parse(source.read_text())
    branch = next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare))
    functions = [n for n in branch.body if isinstance(n, ast.FunctionDef) and n.name in {
        "is_private_strict", "is_private_descriptor_strict"
    }]
    calls = []
    namespace = {
        "os": __import__("os"),
        "is_owned_descriptor": lambda fd: True,
        "is_private_descriptor": lambda fd: True,
        "is_private": lambda path: True,
        "_named_owner_is_current_user": lambda path: True,
        "_descriptor_dacl_is_protected": lambda fd: calls.append(fd) or False,
        "_named_dacl_is_protected": lambda path: calls.append(path) or False,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    assert namespace["is_private_descriptor_strict"](42) is False
    assert namespace["is_private_strict"]("owned") is False
    assert calls == [42, "owned"]


def test_windows_control_and_cleanup_with_injected_api():
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace

    source = Path(__file__).parents[1] / "ciao/os_support/private.py"
    tree = ast.parse(source.read_text())
    branch = next(n for n in tree.body if isinstance(n, ast.If) and isinstance(n.test, ast.Compare))
    names = {"_security_descriptor_is_protected", "_descriptor_dacl_is_protected", "_named_dacl_is_protected"}
    functions = [n for n in branch.body if isinstance(n, ast.FunctionDef) and n.name in names]
    freed = []
    state = {"control": 0, "read_ok": True, "error": 0, "pointer": 123}
    def get_security(*args):
        args[-1]._obj.value = state["pointer"]
        return state["error"]
    def get_control(descriptor, control, revision):
        control._obj.value = state["control"]
        return state["read_ok"]
    def check(ok):
        if not ok:
            raise OSError("control read failed")
    def address(pointer):
        if not pointer.value:
            raise OSError("null descriptor")
        return pointer.value
    ctypes_fake = SimpleNamespace(**{name: getattr(ctypes, name) for name in ("c_void_p", "byref")},
                                  WinError=lambda error: OSError(error, "native error"))
    namespace = {"ctypes": ctypes_fake, "wintypes": wintypes, "os": __import__("os"),
                 "_PSECURITY_DESCRIPTOR": ctypes.c_void_p, "_PACL": ctypes.c_void_p, "_SE_FILE_OBJECT": 1,
                 "_DACL_SECURITY_INFORMATION": 4, "_SE_DACL_PROTECTED": 0x1000,
                 "_GetSecurityInfo": get_security, "_GetNamedSecurityInfoW": get_security,
                 "_GetSecurityDescriptorControl": get_control, "_LocalFree": lambda p: freed.append(p.value),
                 "_address": address, "_check": check, "msvcrt": SimpleNamespace(get_osfhandle=lambda fd: fd)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(source), "exec"), namespace)
    for name, arg in (("_descriptor_dacl_is_protected", 42), ("_named_dacl_is_protected", "x")):
        call = namespace[name]
        for control, expected in ((0, False), (0x1000, True)):
            state.update(control=control, read_ok=True, error=0, pointer=123)
            assert call(arg) is expected
            assert freed.pop() == 123
        state.update(read_ok=False)
        with pytest.raises(OSError):
            call(arg)
        assert freed.pop() == 123
        state.update(read_ok=True, error=5)
        with pytest.raises(OSError):
            call(arg)
        assert freed.pop() == 123
        state.update(error=0, pointer=None)
        with pytest.raises(OSError):
            call(arg)
    assert not freed
