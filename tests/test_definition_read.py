"""The strict definition digest the macOS service manager rechecks.

The reader is the one ``macos_service`` uses to digest a LaunchAgent definition
before it is trusted. The retired native handoff had its own copy; these tests
exercise the surviving reader.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Callable

import pytest

from ciao import macos_service

#: ``(digest, bytes)``; the service manager keeps only the digest.
Reader = Callable[[Path], tuple[str, bytes | None]]


def _macos_reader(path: Path) -> tuple[str, bytes | None]:
    return macos_service._definition_digest(path), None


READERS = [
    pytest.param(_macos_reader, id="macos_service"),
]


@pytest.mark.parametrize("reader", READERS)
def test_a_utf16_definition_with_ctrl_z_and_crlf_is_read_as_exact_bytes(
    reader: Reader, tmp_path: Path
) -> None:
    """CRLF and a 0x1A byte are data, and come back exactly. A UTF-16 payload
    carries NULs and a Ctrl-Z as ``1a 00``; a reader must return the bytes
    unchanged."""
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
