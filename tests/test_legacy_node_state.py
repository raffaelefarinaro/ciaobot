"""Tests for the fail-closed legacy node-state detector (#636).

The detector exists so a Mac that was a client of another host cannot come up
as a local writer once node mode is removed, so the tests are about which
answer it gives and, just as importantly, that it never writes: the trap it was
written to avoid is `NodeStateManager` *creating* a host state when the file is
absent, and a detector that created one would turn every unreadable state into
a host.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ciao import legacy_node_state
from ciao.legacy_node_state import (
    LegacyNodeState,
    detect,
    writers_armed,
)


def _write_state(runtime_root: Path, payload: object) -> Path:
    state = runtime_root / "node_state.json"
    state.write_text(json.dumps(payload), encoding="utf-8")
    return state


def test_none_without_state_file(tmp_path: Path) -> None:
    # A fresh install, or one whose node mode is already gone: nothing to guard.
    assert detect(tmp_path) == LegacyNodeState(kind="none", role="", host_url="")
    assert writers_armed(detect(tmp_path)) is True


def test_none_when_runtime_root_does_not_exist(tmp_path: Path) -> None:
    # A runtime root that was never created is still `none`, not `invalid`:
    # bootstrap mode starts before the wizard has made one.
    assert detect(tmp_path / "never-created") == LegacyNodeState(kind="none")


@pytest.mark.parametrize("role", ["host", "active", "HOST", " active "])
def test_host_for_host_and_active(tmp_path: Path, role: str) -> None:
    _write_state(tmp_path, {"node_id": "mac", "role": role, "host_url": None})

    result = detect(tmp_path)

    assert result.kind == "host"
    assert result.role == "host"
    assert result.host_url == ""
    assert writers_armed(result) is True


@pytest.mark.parametrize("role", ["client", "standby", "Client", " standby "])
def test_client_for_client_and_standby_with_url(tmp_path: Path, role: str) -> None:
    _write_state(
        tmp_path,
        {
            "node_id": "mac",
            "role": role,
            "host_url": "https://studio.example:8443/",
            "peers": [],
        },
    )

    result = detect(tmp_path)

    assert result.kind == "client"
    assert result.role == "client"
    # The address round-trips verbatim: it is the host the operator is told to
    # point this Mac's PWA at, so rewriting it here would name a different one.
    assert result.host_url == "https://studio.example:8443/"
    assert writers_armed(result) is False


def test_host_url_may_be_null_for_a_client_only_through_peers(tmp_path: Path) -> None:
    # A `host_url` of `null` is what a demoted Mac persists, and it leaves the
    # client with no addressable host, so it is `invalid` — never a client.
    _write_state(tmp_path, {"role": "client", "host_url": None})

    assert detect(tmp_path).kind == "invalid"


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "",
        "null",
        "[]",
        '"host"',
        "42",
    ],
)
def test_invalid_when_the_file_is_not_a_usable_object(
    tmp_path: Path, raw: str
) -> None:
    (tmp_path / "node_state.json").write_text(raw, encoding="utf-8")

    result = detect(tmp_path)

    assert result.kind == "invalid"
    assert result.role == "invalid"
    assert result.host_url == ""
    assert writers_armed(result) is False


@pytest.mark.parametrize("role", ["weird", "", "hostess", "replica", "HOSTS"])
def test_invalid_for_an_unknown_role(tmp_path: Path, role: str) -> None:
    _write_state(tmp_path, {"role": role})

    assert detect(tmp_path).kind == "invalid"


def test_invalid_for_a_missing_role(tmp_path: Path) -> None:
    _write_state(tmp_path, {"node_id": "mac", "host_url": None})

    assert detect(tmp_path).kind == "invalid"


@pytest.mark.parametrize(
    "host_url",
    [
        "https://",
        "http://",
        "https://:8443",
        "https:///app",
        "studio.example:8443",  # no scheme
        "ftp://studio.example",
        "https://user:secret@studio.example",
        "https://studio example",
        "not a url at all",
        42,
        ["https://studio.example"],
    ],
)
def test_invalid_for_a_client_without_an_addressable_host(
    tmp_path: Path, host_url: object
) -> None:
    _write_state(tmp_path, {"role": "client", "host_url": host_url})

    result = detect(tmp_path)

    assert result.kind == "invalid"
    # The address is not echoed back for an invalid state: it is the one value
    # here a user must not be shown as somewhere to go.
    assert result.host_url == ""


def test_invalid_when_the_file_cannot_be_read(tmp_path: Path) -> None:
    # A state file that is a directory is a runtime root nobody can account
    # for. `read_text` on it raises `IsADirectoryError`, an `OSError`.
    (tmp_path / "node_state.json").mkdir()

    result = detect(tmp_path)

    assert result.kind == "invalid"
    assert result.role == "invalid"
    assert writers_armed(result) is False


@pytest.mark.parametrize(
    "raw",
    [
        # A stray byte where a string used to be — the shape an aborted write
        # or a botched edit leaves. `read_text` raises `UnicodeDecodeError`
        # here, which is a `ValueError` and *not* an `OSError`, so an
        # `OSError`-only guard let it out of a function whose contract is
        # "never raises", straight into the boot.
        b'{"role": "host"}\xff',
        # A latin-1 `é` written as one byte: still a valid JSON document to
        # anything that decodes it as latin-1, and undecodable as UTF-8.
        b'{"role": "host", "node_id": "mac\xe9"}',
        # UTF-16: the BOM plus the NUL bytes every ASCII character costs there.
        b'\xff\xfe{\x00"\x00r\x00o\x00l\x00e\x00"\x00:\x00 \x00h\x00o\x00s\x00t\x00"\x00',
    ],
)
def test_invalid_when_the_file_is_not_utf8(tmp_path: Path, raw: bytes) -> None:
    # "Unreadable" is not only an unreadable file: a state the process cannot
    # decode is exactly as unaccountable, and it is the case a fail-closed gate
    # has to survive. Every byte string here starts with a role that would arm
    # the writers if it were read, so a guess here is not a harmless default.
    (tmp_path / "node_state.json").write_bytes(raw)

    result = detect(tmp_path)

    assert result.kind == "invalid"
    assert result.role == "invalid"
    assert result.host_url == ""
    assert writers_armed(result) is False


def test_detect_never_writes(tmp_path: Path) -> None:
    """The whole point: reading a state must never produce one.

    `NodeStateManager` writes a fresh host state when the file is absent, so a
    detector that reached for it would answer `host` for a runtime root nobody
    had ever claimed. Every kind is exercised here, and the directory listing
    and file bytes are compared before and after each call.
    """
    cases: list[tuple[str, dict[str, object] | None]] = [
        ("none", None),
        ("host", {"role": "host", "host_url": None}),
        ("client", {"role": "client", "host_url": "https://studio.example:8443"}),
        ("invalid", {"role": "weird"}),
    ]

    for label, payload in cases:
        root = tmp_path / label
        root.mkdir()
        if payload is not None:
            _write_state(root, payload)
        before_listing = sorted(p.name for p in root.iterdir())
        before_bytes = {
            p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()
        }

        result = detect(root)

        assert sorted(p.name for p in root.iterdir()) == before_listing, (
            f"detect({label!r}) created or removed a file"
        )
        assert {
            p.name: p.read_bytes() for p in sorted(root.iterdir()) if p.is_file()
        } == before_bytes, f"detect({label!r}) rewrote the state file"
        assert result.kind == label


def test_the_detector_imports_nothing_from_ciao() -> None:
    """The property that lets it outlive `node_state.py` (#577 deletes it).

    A detector that imported `NodeStateManager` for the role table or the URL
    rule would be deleted along with it, taking the startup gate with it, and a
    failing import at the earliest startup path is a worse failure than a
    double-writer. The duplicated `_ROLE_ALIASES` and `_client_host_url` are the
    price of that, and this is what holds the price paid.

    Parsed rather than grepped, so a docstring that merely *mentions* the
    modules this one must not reach for cannot fail the check.
    """
    source = Path(legacy_node_state.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")

    offenders = sorted(
        name for name in imported if name == "ciao" or name.startswith("ciao.")
    )

    assert offenders == [], f"legacy_node_state must stay a leaf module: {offenders}"
