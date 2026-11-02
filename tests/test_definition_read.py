"""The one strict definition reader shared by both native paths.

The native handoff records a definition's digest and the macOS service manager
rechecks it before a fresh bootstrap. Both used to carry their own copy of the
read, which is exactly how a Windows-only detail (``O_BINARY``) or a byte-count
check goes missing from one of them. These tests exercise both callers against
the same files.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

import pytest

from ciao import macos_service, native_handoff

#: ``(digest, bytes)``; the service manager keeps only the digest.
Reader = Callable[[Path], tuple[str, bytes | None]]


def _macos_reader(path: Path) -> tuple[str, bytes | None]:
    return macos_service._definition_digest(path), None


def _native_reader(path: Path) -> tuple[str, bytes | None]:
    read = native_handoff._read_definition(path)
    return read.sha256, read.data


READERS = [
    pytest.param(_macos_reader, id="macos_service"),
    pytest.param(_native_reader, id="native_handoff"),
]


@pytest.mark.parametrize("reader", READERS)
def test_a_utf16_definition_with_ctrl_z_and_crlf_is_read_as_exact_bytes(
    reader: Reader, tmp_path: Path
) -> None:
    """Windows text mode treats 0x1A as EOF and rewrites CRLF; both must not.

    The task definition setup writes is UTF-16, so its BOM and every ASCII
    character carry a NUL and a Ctrl-Z appears as ``1a 00``. A reader opened in
    text mode could stop at that byte or translate the line endings, producing
    a digest and bytes that are not the file launchd or Task Scheduler opens.
    """
    payload = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\n'
        "<Task>\u001a a control character that is not EOF</Task>\r\n"
    ).encode("utf-16")
    definition = tmp_path / "Ciaobot-Engine.xml"
    definition.write_bytes(payload)

    digest, data = reader(definition)

    assert digest == hashlib.sha256(payload).hexdigest()
    if data is not None:
        assert data == payload


@pytest.mark.parametrize("reader", READERS)
def test_the_open_requests_binary_mode_where_the_platform_has_it(
    reader: Reader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definition = tmp_path / "definition"
    definition.write_bytes(b"<plist/>")
    real_open = os.open
    seen: list[int] = []
    if hasattr(os, "O_BINARY"):
        binary = os.O_BINARY
        strip_fake_bit = False
    else:
        # POSIX has no O_BINARY; a fake bit proves the reader asks for it while
        # the wrapper removes it before the real open, which would reject it.
        binary = 0x8000000
        monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
        strip_fake_bit = True

    def recording_open(path, flags, *args, **kwargs):
        seen.append(flags)
        if strip_fake_bit:
            flags &= ~binary
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", recording_open)

    reader(definition)

    assert seen
    assert all(flags & binary for flags in seen)


@pytest.mark.parametrize("reader", READERS)
def test_a_read_that_does_not_cover_the_opened_size_is_refused(
    reader: Reader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An early EOF must not pass just because the metadata did not move.

    The descriptor still reports the original size and mtime, so only comparing
    the number of bytes actually read with ``fstat``'s size catches this.
    """
    definition = tmp_path / "definition"
    definition.write_bytes(b"0123456789")
    real_read = os.read
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_read(descriptor, size)[:5]
        return b""

    monkeypatch.setattr(os, "read", short_read)

    with pytest.raises(RuntimeError, match="changed while it was being read"):
        reader(definition)
