"""The strict macOS check keeps its native ACL dependency injectable by a harness.

The host-installer tests model macOS with ``sys.platform = "darwin"`` in a
fresh interpreter, including on a Linux CI host. The extended ACL question is
native to macOS, so that simulation supplies its own answer through
``tests.macos_acl_simulation`` only when the host really has no macOS ACL API:
it must neither require ``acl_get_fd_np`` from a Linux libc nor replace the real
ACL on a Mac. These tests pin both halves of that separation, the callers that
must refuse when the API is missing, and that the mode/owner rules still apply.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from tests.macos_acl_simulation import _MACOS_ACL_SYMBOLS, install_hermetic_macos_acl

ROOT = Path(__file__).resolve().parent.parent

requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="the simulated host installer is a POSIX path"
)

#: A stand-in libc with no macOS ACL calls, for a child that models Darwin.
_NO_NATIVE_ACL = """
class _NoAcl:
    def __getattr__(self, name):
        raise AttributeError(name)


def no_native_acl(*args, **kwargs):
    return _NoAcl()
"""

#: Make the simulated Darwin child also look like a non-Mac host: the harness
#: decides from the real host, not from the spoofed ``sys.platform``.
_LINUX_HOST = """
import collections

_uname_result = collections.namedtuple(
    "uname_result", "sysname nodename release version machine"
)


def linux_uname():
    return _uname_result("Linux", "ci", "6.0", "#1", "x86_64")


os.uname = linux_uname
"""

_IMPORT_ONLY_CHILD = """
import ctypes
import sys

sys.platform = "darwin"
""" + _NO_NATIVE_ACL + """
ctypes.CDLL = no_native_acl

# The import itself must not bind a macOS ACL call: a simulated Darwin child
# has to be able to load the module on a host that has no such API. The
# darwin-only names prove the macOS branch was selected, not the Linux answer.
from ciao.os_support import private

assert private._ACL_TYPE_EXTENDED == 0x100
assert callable(private._macos_acl_calls)
assert private._macos_acl is None, "binding must stay deferred until a check"
print("darwin-branch")
"""

_MISSING_API_CHILD = """
import ctypes
import os
import sys

sys.platform = "darwin"
""" + _NO_NATIVE_ACL + """
ctypes.CDLL = no_native_acl

from ciao.os_support import private

descriptor = private.open_private(sys.argv[1], os.O_WRONLY)
try:
    try:
        verdict = private.is_private_descriptor_strict(descriptor)
    except OSError:
        print("refused")
    else:
        print("accepted" if verdict else "rejected")
finally:
    os.close(descriptor)
"""

_INJECTED_CHILD = """
import ctypes
import os
import sys

sys.platform = "darwin"
""" + _NO_NATIVE_ACL + _LINUX_HOST + """
ctypes.CDLL = no_native_acl

from ciao.os_support import private
from tests.macos_acl_simulation import install_hermetic_macos_acl

descriptor = private.open_private(sys.argv[1], os.O_WRONLY)
try:
    install_hermetic_macos_acl()
    assert private.descriptor_acl_grants_another_account(descriptor) is False
    assert private.is_private_descriptor_strict(descriptor) is True

    install_hermetic_macos_acl(grants_another_account=True)
    assert private.descriptor_acl_grants_another_account(descriptor) is True
    assert private.is_private_descriptor_strict(descriptor) is False
finally:
    os.close(descriptor)
print("injected")
"""

_MISSING_API_SCRATCH_CHILD = """
import ctypes
import os
import sys

sys.platform = "darwin"
""" + _NO_NATIVE_ACL + """
ctypes.CDLL = no_native_acl

from ciao import native_handoff

try:
    native_handoff.prepare_bootstrap_scratch()
except native_handoff.NativeHandoffError as exc:
    print(f"refused: {exc}")
else:
    print("prepared")
"""

_MISSING_API_READ_CHILD = """
import ctypes
import os
import sys
from pathlib import Path

sys.platform = "darwin"
""" + _NO_NATIVE_ACL + """
ctypes.CDLL = no_native_acl

from ciao.os_support.definition import DefinitionReadError, read_bounded_file

try:
    read_bounded_file(
        Path(sys.argv[1]), max_bytes=64, label="handoff", require_private_owner=True
    )
except DefinitionReadError as exc:
    print(f"refused: {exc}")
else:
    print("read")
"""

_UNAME = namedtuple("uname_result", "sysname nodename release version machine")


class _PresentAcl:
    """A libc that really exports the macOS ACL calls, for the probe."""

    def __getattr__(self, name):
        if name in _MACOS_ACL_SYMBOLS:
            return lambda *args, **kwargs: 0
        raise AttributeError(name)


class _NoAcl:
    """A libc with no macOS ACL calls."""

    def __getattr__(self, name):
        raise AttributeError(name)


def _run(
    child: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", child, *args],
        env={**os.environ, "PYTHONPATH": str(ROOT), **(env or {})},
        capture_output=True,
        text=True,
        check=False,
    )


@requires_posix
def test_a_simulated_darwin_import_needs_no_native_acl() -> None:
    """A spoofed platform flag must not make an import reach for macOS symbols."""
    child = _run(_IMPORT_ONLY_CHILD)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "darwin-branch"


@requires_posix
def test_a_missing_native_acl_fails_the_strict_check_closed(tmp_path: Path) -> None:
    """No macOS ACL API means "could not inspect", never "no ACL"."""
    child = _run(_MISSING_API_CHILD, str(tmp_path / "secret"))
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "refused"


@requires_posix
def test_the_simulated_mac_harness_injects_the_acl_answer(tmp_path: Path) -> None:
    """A non-Mac host without the API gets the injected answer, both ways."""
    child = _run(_INJECTED_CHILD, str(tmp_path / "secret"))
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "injected"


@requires_posix
def test_native_handoff_scratch_refuses_a_missing_native_acl(tmp_path: Path) -> None:
    """The scratch chain check must not skip the ACL question."""
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    child = _run(_MISSING_API_SCRATCH_CHILD, env={"HOME": str(home)})
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("refused:"), child.stdout
    assert "Could not inspect" in child.stdout
    assert not (home / ".ciao").exists(), "it refused instead of creating the chain"


@requires_posix
def test_read_bounded_file_refuses_a_missing_native_acl(tmp_path: Path) -> None:
    """The owner-strict reader must not return bytes it could not verify."""
    path = tmp_path / "handoff.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    child = _run(_MISSING_API_READ_CHILD, str(path))
    assert child.returncode == 0, child.stderr
    assert child.stdout.startswith("refused:"), child.stdout
    assert "Could not inspect" in child.stdout


@requires_posix
def test_the_harness_does_not_override_a_present_native_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real API is never replaced, even if a test asks for an answer."""
    from ciao.os_support import private

    before = private._acl_grants_another_account
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _PresentAcl())
    install_hermetic_macos_acl(grants_another_account=True)
    assert private._acl_grants_another_account is before


@requires_posix
def test_the_harness_never_overrides_a_real_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real Mac with a missing API must keep failing closed, not be masked."""
    from ciao.os_support import private

    before = private._acl_grants_another_account
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _NoAcl())
    monkeypatch.setattr(os, "uname", lambda: _UNAME("Darwin", "mac", "24", "", ""))
    install_hermetic_macos_acl(grants_another_account=True)
    assert private._acl_grants_another_account is before


@requires_posix
def test_the_harness_injects_on_a_non_mac_without_a_native_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the non-Mac, API-less combination gets the hermetic reader."""
    from ciao.os_support import private

    # Record the original even though the helper assigns over it, so this
    # in-process simulation cannot leak its stand-in into later tests.
    monkeypatch.setattr(
        private, "_acl_grants_another_account", private._acl_grants_another_account
    )
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: _NoAcl())
    monkeypatch.setattr(os, "uname", lambda: _UNAME("Linux", "ci", "6", "", ""))
    install_hermetic_macos_acl(grants_another_account=True)
    assert private._acl_grants_another_account(0) is True


@requires_posix
def test_an_injected_acl_answer_does_not_bypass_the_mode_and_owner_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection stands in for the native call, not for the strict verdict."""
    from ciao.os_support import private

    monkeypatch.setattr(private, "_acl_grants_another_account", lambda fd: False)
    path = tmp_path / "shared.txt"
    path.write_text("token", encoding="utf-8")
    path.chmod(0o644)
    descriptor = os.open(path, os.O_RDONLY)
    try:
        assert private.is_private_descriptor_strict(descriptor) is False
    finally:
        os.close(descriptor)
