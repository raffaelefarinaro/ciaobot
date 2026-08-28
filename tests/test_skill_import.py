"""Tests for validated skill zip import (``ciao/skill_import.py``)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from ciao.skill_import import (
    MAX_SKILL_ASSET_BYTES,
    MAX_SKILL_TOTAL_BYTES,
    extract_skill_zip,
    validate_skill_zip,
)


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _skill_zip(name: str = "demo", extra: dict[str, bytes] | None = None) -> bytes:
    entries: dict[str, bytes] = {
        f"{name}/SKILL.md": (
            f"---\nname: {name}\ndescription: Demo skill\n---\n\n# {name}\n"
        ).encode()
    }
    if extra:
        entries.update(extra)
    return _zip_bytes(entries)


def test_validate_accepts_valid_skill_zip() -> None:
    name, errors = validate_skill_zip(_skill_zip())
    assert errors == []
    assert name == "demo"


def test_validate_rejects_dot_folder_name() -> None:
    # A zip whose top-level folder is "." would extract SKILL.md into the
    # skills/ root, where inventory and sync never look.
    name, errors = validate_skill_zip(_skill_zip(name="."))
    assert name is None
    assert any("not a valid skill directory name" in e for e in errors)


def test_validate_rejects_dotdot_folder_name() -> None:
    # ".." is caught by the zip-slip check before the folder-name check.
    name, errors = validate_skill_zip(_skill_zip(name=".."))
    assert name is None
    assert errors


def test_validate_rejects_leading_dot_folder_name() -> None:
    name, errors = validate_skill_zip(_skill_zip(name=".hidden"))
    assert name is None
    assert any("not a valid skill directory name" in e for e in errors)


def test_validate_rejects_oversized_member() -> None:
    big = b"x" * (MAX_SKILL_ASSET_BYTES + 1)
    name, errors = validate_skill_zip(_skill_zip(extra={"demo/asset.bin": big}))
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)


def test_validate_rejects_oversized_total() -> None:
    # Five members each under the per-file cap but over the total cap.
    per = MAX_SKILL_ASSET_BYTES
    extra = {f"demo/{i}.bin": bytes([i]) * per for i in range(5)}
    name, errors = validate_skill_zip(_skill_zip(extra=extra))
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)


def test_extract_streams_through_caps(tmp_path: Path) -> None:
    # A zip that lies about its declared uncompressed size must still be
    # bounded during extraction.
    big = b"y" * (MAX_SKILL_ASSET_BYTES + 1)
    name, errors = extract_skill_zip(_skill_zip(extra={"demo/asset.bin": big}), tmp_path)
    assert name is None
    assert any("exceeds" in e and "uncompressed" in e for e in errors)
    # Nothing should have been left behind from the aborted extraction.
    assert not (tmp_path / "demo").exists()


def test_extract_removes_temp_dir_on_aborted_extraction(tmp_path: Path) -> None:
    # Cap violations and traversal rejects return from inside the extraction
    # loop; the unique temp dir must be cleaned up on those paths too, or
    # repeated failed imports litter skills/ with .name.tmp-* directories.
    big = b"y" * (MAX_SKILL_ASSET_BYTES + 1)
    name, errors = extract_skill_zip(_skill_zip(extra={"demo/asset.bin": big}), tmp_path)
    assert name is None
    assert errors
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".demo.tmp-")]
    assert leftovers == []


def test_import_rejects_an_unregistered_workspace(tmp_path: Path) -> None:
    """A stale client or typo must not scaffold an orphan agent root.

    ``config.agent_root`` accepts any single-segment name, so an unvalidated
    ``workspace`` form value would create ``<install>/<name>/skills`` and the
    following sync would scaffold an entire agent root for a workspace that
    is not registered.
    """
    import io
    import zipfile
    from types import SimpleNamespace

    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.web.routes_api import skill_import

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault"),
        },
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("demo/SKILL.md", "---\nname: demo\ndescription: Demo\n---\n# demo\n")

    async def _fake_form():
        class Upload:
            filename = "demo.zip"

            async def read(self, *_a, **_k):
                return b""

        return {
            "workspace": "typo-ws",
            "file": Upload(),
        }

    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
        headers={"content-length": "100"},
        form=_fake_form,
        query_params={},
    )

    import asyncio

    response = asyncio.run(skill_import(request))
    assert response.status_code == 400
    assert "Unknown workspace: typo-ws" in response.body.decode()
    # Nothing was written outside the registered roots.
    assert not (tmp_path / "typo-ws").exists()


def test_import_rejects_a_missing_or_malformed_content_length(tmp_path: Path) -> None:
    """No valid Content-Length, no multipart parse.

    The pre-parse guard is the only cheap bound on the request body: a
    chunked/HTTP2 client that omits (or mangles) the header must be rejected
    before `request.form()` spools the whole body to disk or proxy memory.
    """
    import io
    import zipfile
    from types import SimpleNamespace

    from ciao.config import CiaoConfig, WorkspaceConfig
    from ciao.web.routes_api import skill_import

    config = CiaoConfig(
        pwa_auth_token="t",
        workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json",
        media_root=tmp_path / ".runtime" / "media",
        workspaces={
            "personal": WorkspaceConfig(name="personal", vault_root="memory-vault"),
        },
    )

    form_calls: list[int] = []

    async def _fake_form():
        form_calls.append(1)
        return {}

    import asyncio

    # Missing header entirely.
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(config=config)),
        headers={},
        form=_fake_form,
        query_params={},
    )
    response = asyncio.run(skill_import(request))
    assert response.status_code == 400
    assert "Zip too large" in response.body.decode()

    # Malformed header.
    request.headers = {"content-length": "not-a-number"}
    response = asyncio.run(skill_import(request))
    assert response.status_code == 400

    # And the multipart parser was never invoked: the body was never read.
    assert form_calls == []


def test_extract_writes_valid_skill(tmp_path: Path) -> None:
    name, errors = extract_skill_zip(_skill_zip(), tmp_path)
    assert errors == []
    assert name == "demo"
    assert (tmp_path / "demo" / "SKILL.md").is_file()


def test_extract_is_transactional_on_corrupt_member(tmp_path: Path) -> None:
    # A zip with a valid central directory and SKILL.md but a later member
    # whose data is truncated must not leave a partially installed skill.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("demo/SKILL.md", "---\nname: demo\ndescription: Demo\n---\n")
        zf.writestr("demo/asset.bin", b"x" * 100)
    raw = buf.getvalue()
    # Corrupt the last member's data by truncating the archive body.
    truncated = raw[: len(raw) - 50]
    name, errors = extract_skill_zip(truncated, tmp_path)
    assert name is None
    assert errors
    # Nothing should be left behind from the aborted extraction.
    assert not (tmp_path / "demo").exists()
    assert not list(tmp_path.glob(".demo.tmp-*"))


def test_concurrent_non_force_imports_of_the_same_skill_do_not_clobber(
    tmp_path: Path,
) -> None:
    """Two same-name imports without force: exactly one wins.

    The route dispatches extraction via asyncio.to_thread, so two imports of
    the same previously-absent skill run in parallel; without per-name
    serialization both passed the existence check and the second deleted the
    first's just-installed directory despite overwrite=False.
    """
    import threading

    zip_a = _skill_zip("demo", extra={"demo/asset.bin": b"first"})
    zip_b = _skill_zip("demo", extra={"demo/asset.bin": b"second"})
    results: list[tuple[str | None, list[str]]] = []
    results_lock = threading.Lock()

    def do_import(data: bytes) -> None:
        outcome = extract_skill_zip(data, tmp_path)
        with results_lock:
            results.append(outcome)

    threads = [
        threading.Thread(target=do_import, args=(data,))
        for data in (zip_a, zip_b)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    succeeded = [(name, errs) for name, errs in results if name == "demo"]
    refused = [(name, errs) for name, errs in results if name is None]
    # Both requests complete, but only one archive ends up installed: the
    # loser is refused with "already exists", never a silent replacement.
    assert len(succeeded) == 1, results
    assert len(results) == 2
    refused_errors = [e for _n, errs in results if _n is None for e in errs]
    assert any("already exists" in e for e in refused_errors), refused_errors
    # And the winner's content is intact (not half-deleted).
    installed = (tmp_path / "demo" / "asset.bin").read_bytes()
    assert installed in (b"first", b"second")
