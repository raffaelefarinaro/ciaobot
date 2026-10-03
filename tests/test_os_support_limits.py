"""ciao.os_support.limits.raise_file_descriptor_limit: the startup fd budget."""

from __future__ import annotations

import sys

import pytest

from ciao.os_support import limits

# ``resource`` does not exist on Windows; the POSIX tests are skipped there.
# Import it conditionally so the Windows CI job can still collect this file
# and run the no-op assertion.
if sys.platform != "win32":
    import resource


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_raises_the_soft_limit_toward_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        limits.resource,
        "getrlimit",
        lambda _which: (256, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(limits.resource, "setrlimit", lambda _which, value: calls.append(value))

    limits.raise_file_descriptor_limit(4096)

    # Soft moves up to the target; the hard limit is left alone.
    assert calls == [(4096, resource.RLIM_INFINITY)]


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_clamps_to_the_hard_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(limits.resource, "getrlimit", lambda _which: (256, 1024))
    monkeypatch.setattr(limits.resource, "setrlimit", lambda _which, value: calls.append(value))

    limits.raise_file_descriptor_limit(4096)

    # The kernel refuses a soft limit above the hard one.
    assert calls == [(1024, 1024)]


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_leaves_an_already_sufficient_limit_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        limits.resource,
        "getrlimit",
        lambda _which: (8192, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(limits.resource, "setrlimit", lambda _which, value: calls.append(value))

    limits.raise_file_descriptor_limit(4096)

    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_an_infinite_soft_limit_is_already_sufficient(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    monkeypatch.setattr(
        limits.resource,
        "getrlimit",
        lambda _which: (resource.RLIM_INFINITY, resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(limits.resource, "setrlimit", lambda _which, value: calls.append(value))

    limits.raise_file_descriptor_limit(4096)

    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_a_read_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_which):
        raise OSError("no resource limits here")

    monkeypatch.setattr(limits.resource, "getrlimit", boom)

    limits.raise_file_descriptor_limit(4096)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX branch is not defined on Windows")
def test_a_write_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_which, _value):
        raise ValueError("above the hard limit")

    monkeypatch.setattr(limits.resource, "getrlimit", lambda _which: (256, 4096))
    monkeypatch.setattr(limits.resource, "setrlimit", boom)

    limits.raise_file_descriptor_limit(4096)


@pytest.mark.skipif(sys.platform != "win32", reason="the no-op branch is the Windows branch")
def test_windows_branch_is_a_noop() -> None:
    """Windows has no ``RLIMIT_NOFILE``; the function exists only for callers."""
    assert limits.raise_file_descriptor_limit(64) is None
