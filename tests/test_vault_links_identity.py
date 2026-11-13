"""Link-migration receipts identify the vault they were written for (#2837).

A receipt is machine-local evidence bound to the vault directory's inode; the
portable `.ciao-vault-links.json` marker carries the link state between runtimes
and clones. Replacement tests rename the original away before re-creating the
path, so the inodes are necessarily distinct whatever the filesystem recycles.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ciao
import ciao.vault_migrate_links as m
from ciao import migration_notices
from ciao.vault_migrate_links import (
    ReceiptUnavailable,
    migrate_links,
    peek_receipt,
    read_receipt,
    receipt_path,
    unmigrate_links,
)

_REAL_GIT_STATE = m.vault_git_state
_REAL_PUBLISH = m._publish_receipt
_REAL_ARCHIVE = m._archive_legacy
_PYTHONPATH = str(Path(ciao.__file__).resolve().parents[1])
_ROOT_USER = hasattr(os, "geteuid") and os.geteuid() == 0
_NO_CHMOD = pytest.mark.skipif(sys.platform == "win32" or _ROOT_USER, reason="needs POSIX chmod as a non-root user")


@pytest.fixture(autouse=True)
def _clean_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "vault_git_state", lambda root: {"head": "", "dirty": False})


def _note(vault: Path, name: str, text: str) -> Path:
    path = vault / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def _seed(vault: Path) -> None:
    _note(vault, "A.md", "See [[B]].\n")
    _note(vault, "B.md", "# B\n")


def _marker(vault: Path) -> dict[str, Any] | None:
    path = vault / m.MARKER_NAME
    return json.loads(path.read_bytes()) if path.exists() else None


def _archives(runtime: Path) -> list[Path]:
    return sorted((runtime / "migration").glob("vault-links-legacy-*.json"))


def _listing(vault: Path) -> dict[str, bytes]:
    return {p.relative_to(vault).as_posix(): p.read_bytes() for p in sorted(vault.rglob("*")) if p.is_file()}


def _notes(vault: Path) -> dict[str, bytes]:
    return {name: data for name, data in _listing(vault).items() if name.endswith(".md")}


def _migrated(tmp_path: Path, name: str = "vault", runtime: str = "runtime") -> tuple[Path, Path]:
    vault, rt = tmp_path / name, tmp_path / runtime
    _seed(vault)
    assert migrate_links(vault, rt, apply=True)["complete"]
    return vault, rt


def _downgrade(vault: Path, runtime: Path, version: int, *, status: str | None = "migrated", operation: str = "migrate") -> bytes:
    """Rewrite the receipt as schema 1 or 2, drop the marker, return the raw bytes."""
    path = receipt_path(runtime)
    data = json.loads(path.read_bytes())
    for key in ("vault_inode", "operation_id", "base_marker"):
        data.pop(key, None)
    data["schema_version"] = version
    if version == 1:
        for key in ("operation", "files", "legacy_rewrites"):
            data.pop(key, None)
        if status is None:
            data.pop("status", None)
        else:
            data["status"] = status
    else:
        data.update(operation=operation, status=status)
    raw = json.dumps(data, indent=2).encode("utf-8")
    path.write_bytes(raw)
    (vault / m.MARKER_NAME).unlink(missing_ok=True)
    return raw


def _replace_vault(vault: Path, tmp_path: Path) -> Path:
    original = tmp_path / "original-vault"
    vault.rename(original)
    vault.mkdir()
    shutil.copytree(original, vault, dirs_exist_ok=True)
    assert vault.stat().st_ino != original.stat().st_ino
    return original


def _record_calls(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[str]:
    calls: list[str] = []
    for name in names:
        monkeypatch.setattr(m, name, lambda *a, _n=name, **k: calls.append(_n))
    return calls


# ---- slice 1: inode binding and one-time adoption --------------------------


@pytest.mark.parametrize("kind", ["fresh", "adopted"])
def test_replacement_cannot_inherit_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    vault, runtime = _migrated(tmp_path)
    if kind == "adopted":
        _downgrade(vault, runtime, 1)
        assert read_receipt(runtime, vault_root=vault) is not None
    _replace_vault(vault, tmp_path)
    notes, evidence = _listing(vault), receipt_path(runtime).read_bytes()
    calls = _record_calls(monkeypatch, "_replace_image")

    with pytest.raises(ReceiptUnavailable, match="replaced"):
        read_receipt(runtime, vault_root=vault)
    with pytest.raises(ReceiptUnavailable, match="replaced"):
        peek_receipt(runtime, vault_root=vault)
    undone = unmigrate_links(vault, runtime, apply=True)
    forced = migrate_links(vault, runtime, apply=True, force=True)

    assert undone["failed"] and undone["files_restored"] == 0
    assert forced["failed"] and forced["files_rewritten"] == 0
    assert not calls
    assert _listing(vault) == notes and receipt_path(runtime).read_bytes() == evidence


def test_inode_mismatch_refuses_before_note_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _migrated(tmp_path)
    monkeypatch.setattr(m, "_vault_inode", lambda root: -1)
    calls = _record_calls(monkeypatch, "migrate_vault_links", "_legacy_image", "_replace_image", "_preflight")

    results = [
        migrate_links(vault, runtime),
        migrate_links(vault, runtime, apply=True),
        migrate_links(vault, runtime, apply=True, force=True),
        unmigrate_links(vault, runtime),
        unmigrate_links(vault, runtime, apply=True),
    ]

    assert all(result["failed"] and "replaced" in result["failed"][0]["error"] for result in results)
    assert not calls


def test_unavailable_inode_is_null_and_compared_by_equality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = tmp_path / "vault", tmp_path / "runtime"
    _seed(vault)
    monkeypatch.setattr(m, "_vault_inode", lambda root: None)
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert json.loads(receipt_path(runtime).read_bytes())["vault_inode"] is None
    assert read_receipt(runtime, vault_root=vault) is not None

    monkeypatch.setattr(m, "_vault_inode", lambda root: 123)
    with pytest.raises(ReceiptUnavailable, match="replaced"):
        peek_receipt(runtime, vault_root=vault)

    data = json.loads(receipt_path(runtime).read_bytes())
    data["vault_inode"] = 5
    receipt_path(runtime).write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(m, "_vault_inode", lambda root: None)
    with pytest.raises(ReceiptUnavailable, match="replaced"):
        peek_receipt(runtime, vault_root=vault)


_LEGACY_CASES = [
    (1, None, "migrate"), (1, "migrated", "migrate"), (1, "partial", "migrate"),
    (2, "prepared", "migrate"), (2, "partial", "migrate"), (2, "migrated", "migrate"),
    (2, "prepared", "undo"), (2, "partial", "undo"),
]


@pytest.mark.parametrize(("version", "status", "operation"), _LEGACY_CASES)
def test_legacy_first_read_adopts_once(tmp_path: Path, version: int, status: str | None, operation: str) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, version, status=status, operation=operation)
    source = json.loads(raw)
    before = _listing(vault)

    completed = read_receipt(runtime, vault_root=vault)

    assert (completed is not None) == (status in (None, "migrated") and operation == "migrate")
    adopted = receipt_path(runtime).read_bytes()
    data = json.loads(adopted)
    assert data["schema_version"] == 3 and data["operation_id"] is None and data["base_marker"] is None
    assert data["vault_inode"] == os.stat(vault).st_ino
    for key, value in source.items():
        if key != "schema_version":
            assert data[key] == value
    assert data["status"] == (source.get("status") or "migrated")
    archives = _archives(runtime)
    assert len(archives) == 1 and archives[0].read_bytes() == raw
    assert hashlib.sha256(raw).hexdigest() in archives[0].name
    read_receipt(runtime, vault_root=vault)
    assert receipt_path(runtime).read_bytes() == adopted and len(_archives(runtime)) == 1
    assert _marker(vault) is None and _listing(vault) == before


def test_legacy_archive_preserves_exact_bytes(tmp_path: Path) -> None:
    vault, runtime = tmp_path / "vault", tmp_path / "runtime"
    vault.mkdir()
    raw = (b'{\r\n "vault_root": ' + json.dumps(str(vault)).encode() + b',\r\n  "schema_version": 1, '
           b'"rewrites": [], "note": "caf\\u00e9 \xc3\xa9"\r\n}')
    receipt_path(runtime).parent.mkdir(parents=True)
    receipt_path(runtime).write_bytes(raw)

    assert read_receipt(runtime, vault_root=vault) is not None

    assert _archives(runtime)[0].read_bytes() == raw
    assert json.loads(receipt_path(runtime).read_bytes())["note"] == "café é"


def _hold_lock(runtime: Path) -> subprocess.Popen[str]:
    script = ("import sys; from pathlib import Path; "
              "from ciao.vault_migrate_links import _transaction_lock; "
              "lock = _transaction_lock(Path(sys.argv[1])); lock.__enter__(); "
              "print('locked', flush=True); sys.stdin.readline(); lock.__exit__(None, None, None)")
    process = subprocess.Popen([sys.executable, "-c", script, str(runtime)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": _PYTHONPATH})
    assert process.stdout is not None and process.stdout.readline().strip() == "locked"
    return process


@pytest.mark.parametrize("obstacle", ["publish", "archive", "busy", pytest.param("readonly", marks=_NO_CHMOD)])
def test_failed_adoption_keeps_legacy_semantics(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, obstacle: str) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, 1)
    process: subprocess.Popen[str] | None = None
    directory = runtime / "migration"

    def raiser(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    if obstacle == "publish":
        monkeypatch.setattr(m, "_publish_receipt", raiser)
    elif obstacle == "archive":
        monkeypatch.setattr(m, "_archive_legacy", raiser)
    elif obstacle == "busy":
        process = _hold_lock(runtime)
    else:
        directory.chmod(0o500)
    try:
        assert read_receipt(runtime, vault_root=vault) is not None
        assert receipt_path(runtime).read_bytes() == raw
        assert migration_notices._links_scope(SimpleNamespace(vault_root=vault), runtime).vault is None
    finally:
        directory.chmod(0o700)
        if process is not None:
            assert process.stdin is not None
            process.stdin.write("release\n")
            process.stdin.flush()
            assert process.wait(timeout=10) == 0
    if obstacle in ("publish", "archive"):
        monkeypatch.setattr(m, "_publish_receipt", _REAL_PUBLISH)
        monkeypatch.setattr(m, "_archive_legacy", _REAL_ARCHIVE)

    assert read_receipt(runtime, vault_root=vault) is not None
    assert len(_archives(runtime)) == 1 and json.loads(receipt_path(runtime).read_bytes())["schema_version"] == 3



@_NO_CHMOD
def test_adoption_writes_nothing_under_the_vault(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    _downgrade(vault, runtime, 1)
    before, mtime = _listing(vault), vault.stat().st_mtime_ns
    vault.chmod(0o555)
    try:
        assert read_receipt(runtime, vault_root=vault) is not None
    finally:
        vault.chmod(0o755)
    assert _listing(vault) == before and vault.stat().st_mtime_ns == mtime
    assert len(_archives(runtime)) == 1


def test_mismatched_existing_archive_blocks_adoption(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, 1)
    archive = runtime / "migration" / f"vault-links-legacy-{hashlib.sha256(raw).hexdigest()}.json"
    archive.write_bytes(b"other")

    assert read_receipt(runtime, vault_root=vault) is not None

    assert receipt_path(runtime).read_bytes() == raw and archive.read_bytes() == b"other"


def test_concurrent_reads_adopt_once(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, 1)
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    script = (
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "from ciao.vault_migrate_links import read_receipt\n"
        "runtime, vault, barrier, name = map(Path, sys.argv[1:5])\n"
        "(barrier / f'ready-{name}').touch()\n"
        "deadline = time.monotonic() + 10\n"
        "while not (barrier / 'go').exists():\n"
        "    time.sleep(0.005)\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit(3)\n"
        "print(json.dumps({'completed': read_receipt(runtime, vault_root=vault) is not None}))\n"
    )
    env = {**os.environ, "PYTHONPATH": _PYTHONPATH}
    processes = [subprocess.Popen([sys.executable, "-c", script, str(runtime), str(vault), str(barrier), name],
                                  stdout=subprocess.PIPE, text=True, env=env) for name in ("one", "two")]
    try:
        deadline = time.monotonic() + 10
        while not all((barrier / f"ready-{n}").exists() for n in ("one", "two")):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        (barrier / "go").touch()
        outputs = [process.communicate(timeout=30)[0] for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
    assert [process.returncode for process in processes] == [0, 0]
    assert all(json.loads(out)["completed"] is True for out in outputs)
    assert len(_archives(runtime)) == 1 and _archives(runtime)[0].read_bytes() == raw
    assert json.loads(receipt_path(runtime).read_bytes())["schema_version"] == 3


def test_refusing_operation_never_adopts(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, 1)
    data = json.loads(raw)
    data["rewrites"][0]["offset"] = 1000
    raw = json.dumps(data).encode()
    receipt_path(runtime).write_bytes(raw)

    assert unmigrate_links(vault, runtime, apply=True)["failed"]

    assert receipt_path(runtime).read_bytes() == raw
    assert not _archives(runtime) and _marker(vault) is None


def test_publishing_operation_archives_legacy_bytes(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = _downgrade(vault, runtime, 1)

    assert unmigrate_links(vault, runtime, apply=True)["files_restored"] == 1

    assert (vault / "A.md").read_bytes() == b"See [[B]].\n"
    archives = [p for p in (runtime / "migration").glob("vault-links.*.json")]
    assert [p.read_bytes() for p in archives] == [raw]


# ---- slice 2: portable marker, binding, observer, stale refusal ------------

_FOREIGN = "f" * 32


def _write_marker(vault: Path, operation_id: str, state: str) -> None:
    (vault / m.MARKER_NAME).write_text(
        json.dumps({"format": 1, "operation_id": operation_id, "state": state}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _receipt(runtime: Path) -> dict[str, Any]:
    return json.loads(receipt_path(runtime).read_bytes())


def _two(tmp_path: Path) -> tuple[Path, Path, Path]:
    vault = tmp_path / "vault"
    _seed(vault)
    return vault, tmp_path / "a", tmp_path / "b"


def test_serial_runtimes_cannot_retain_conflicting_completion(tmp_path: Path) -> None:
    vault, a, b = _two(tmp_path)
    assert migrate_links(vault, a, apply=True)["complete"]
    first_op = _marker(vault)["operation_id"]  # type: ignore[index]

    seen = migrate_links(vault, b, apply=True)
    assert seen["complete"] and seen["observed_only"] is True and seen["files_rewritten"] == 0
    assert not receipt_path(b).exists()
    assert read_receipt(b, vault_root=vault)["observed_only"] is True  # type: ignore[index]

    assert unmigrate_links(vault, a, apply=True)["files_restored"] == 1
    assert (vault / "A.md").read_bytes() == b"See [[B]].\n"
    assert read_receipt(b, vault_root=vault) is None

    again = migrate_links(vault, b, apply=True)
    assert "skipped" not in again and again["files_rewritten"] == 1
    owned = read_receipt(b, vault_root=vault)
    assert owned is not None and _marker(vault)["operation_id"] == owned["operation_id"] != first_op  # type: ignore[index]


def test_marker_holds_only_portable_state(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    raw = (vault / m.MARKER_NAME).read_bytes()
    data = json.loads(raw)

    assert set(data) == {"format", "operation_id", "state"} and data["format"] == 1 and data["state"] == "migrated"
    assert len(data["operation_id"]) == 32 and set(data["operation_id"]) <= set("0123456789abcdef")
    assert data["operation_id"] == _receipt(runtime)["operation_id"]
    assert raw == (json.dumps(data, indent=2, sort_keys=True) + "\n").encode()
    mtime = (vault / m.MARKER_NAME).stat().st_mtime_ns
    assert migrate_links(vault, runtime, apply=True)["skipped"] == "already migrated"
    assert (vault / m.MARKER_NAME).read_bytes() == raw and (vault / m.MARKER_NAME).stat().st_mtime_ns == mtime


def test_undo_invalidates_other_runtimes_before_first_note_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, a, b = _two(tmp_path)
    migrate_links(vault, a, apply=True)
    original, seen = m._replace_image, []

    def watch(root: Path, image: dict[str, Any], *, undo: bool) -> bool:
        if not seen:
            seen.append((_marker(vault)["state"], read_receipt(b, vault_root=vault)))  # type: ignore[index]
        return original(root, image, undo=undo)

    monkeypatch.setattr(m, "_replace_image", watch)
    assert unmigrate_links(vault, a, apply=True)["files_restored"] == 1
    assert seen == [("undoing", None)]


def test_stale_undo_evidence_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, a, b = _two(tmp_path)
    migrate_links(vault, a, apply=True)
    stale = receipt_path(a).read_bytes()
    unmigrate_links(vault, a, apply=True)
    assert migrate_links(vault, b, apply=True)["complete"]
    receipt_path(a).write_bytes(stale)
    notes = _listing(vault)
    calls = _record_calls(monkeypatch, "_replace_image", "_legacy_image")

    for apply in (False, True):
        result = unmigrate_links(vault, a, apply=apply)
        assert result["failed"] and "stale" in result["failed"][0]["error"]

    assert not calls and _listing(vault) == notes and receipt_path(a).read_bytes() == stale
    assert read_receipt(a, vault_root=vault)["observed_only"] is True  # type: ignore[index]
    assert migrate_links(vault, a, apply=True)["observed_only"] is True
    assert receipt_path(a).read_bytes() == stale


def test_marker_from_another_copy_makes_receipt_stale(tmp_path: Path) -> None:
    vault, runtime = _migrated(tmp_path)
    config = SimpleNamespace(vault_root=vault)
    _write_marker(vault, _FOREIGN, "unmigrated")  # a pull from another machine
    assert read_receipt(runtime, vault_root=vault) is None
    assert "stale" in unmigrate_links(vault, runtime, apply=True)["failed"][0]["error"]
    assert migration_notices._links_scope(config, runtime).readable is True

    (vault / m.MARKER_NAME).unlink()  # a checkout from before the migration
    assert read_receipt(runtime, vault_root=vault) is None
    assert "stale" in unmigrate_links(vault, runtime, apply=True)["failed"][0]["error"]

    old = receipt_path(runtime).read_bytes()
    _note(vault, "A.md", "See [[B]].\n")
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert _receipt(runtime)["operation_id"] == _marker(vault)["operation_id"]  # type: ignore[index]
    assert old in [p.read_bytes() for p in (runtime / "migration").glob("vault-links.*.json")]


def test_fresh_clone_observes_completion_without_a_receipt(tmp_path: Path) -> None:
    vault, _ = _migrated(tmp_path)
    clone, c = tmp_path / "clone", tmp_path / "c"
    shutil.copytree(vault, clone)

    assert read_receipt(c, vault_root=clone)["observed_only"] is True  # type: ignore[index]
    assert migrate_links(clone, c, apply=True)["observed_only"] is True
    assert not receipt_path(c).exists()
    assert unmigrate_links(clone, c, apply=True)["skipped"] == "no migration receipt to reverse"


@pytest.mark.parametrize("operation", ["migrate", "undo"])
def test_interrupted_marker_publication_is_repaired_by_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    vault, a, b = _two(tmp_path)
    if operation == "undo":
        migrate_links(vault, a, apply=True)
    notes = _listing(vault)
    answer = read_receipt(b, vault_root=vault)
    real = m._publish_marker

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_publish_marker", interrupt)
    run = migrate_links if operation == "migrate" else unmigrate_links
    with pytest.raises(KeyboardInterrupt):
        run(vault, a, apply=True)
    monkeypatch.setattr(m, "_publish_marker", real)

    prepared = _receipt(a)
    assert prepared["status"] == "prepared" and prepared["base_marker"] == _marker(vault)
    assert _listing(vault) == notes and read_receipt(b, vault_root=vault) == answer
    assert not run(vault, a, apply=True)["failed"]
    assert _marker(vault)["operation_id"] == prepared["operation_id"]  # type: ignore[index]
    if operation == "migrate":
        assert _receipt(a)["operation_id"] == prepared["operation_id"] and _marker(vault)["state"] == "migrated"  # type: ignore[index]
    else:
        assert _marker(vault)["state"] == "unmigrated"  # type: ignore[index]


@pytest.mark.parametrize("operation", ["migrate", "undo"])
def test_marker_publication_failure_attempts_no_note_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    vault, a, _ = _two(tmp_path)
    if operation == "undo":
        migrate_links(vault, a, apply=True)

    def deny(*args: object, **kwargs: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(m, "_publish_marker", deny)
    calls = _record_calls(monkeypatch, "_replace_image")
    run = migrate_links if operation == "migrate" else unmigrate_links
    result = run(vault, a, apply=True)

    assert result["failed"] and "cannot publish vault link state" in result["failed"][0]["error"]
    assert not calls and _receipt(a)["status"] == "prepared"


def test_concurrent_marker_change_stops_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, a, _ = _two(tmp_path)
    original = m._replace_image

    def foreign(root: Path, image: dict[str, Any], *, undo: bool) -> bool:
        changed = original(root, image, undo=undo)
        _write_marker(vault, _FOREIGN, "migrated")
        return changed

    monkeypatch.setattr(m, "_replace_image", foreign)
    result = migrate_links(vault, a, apply=True)

    assert result["complete"] is False
    entry = next(f for f in result["failed"] if f["path"] == str(vault.resolve() / m.MARKER_NAME))
    assert "another Ciaobot runtime" in entry["error"]
    assert _marker(vault)["operation_id"] == _FOREIGN  # type: ignore[index]
    assert peek_receipt(a, vault_root=vault)["status"] == "migrated"  # type: ignore[index]
    assert read_receipt(a, vault_root=vault)["operation_id"] == _FOREIGN  # type: ignore[index]


def test_zero_image_runs_never_touch_the_marker(tmp_path: Path) -> None:
    quiet, runtime = tmp_path / "quiet", tmp_path / "rt"
    _note(quiet, "A.md", "# A\n")
    assert migrate_links(quiet, runtime, apply=True)["complete"]
    assert _receipt(runtime)["operation_id"] is None and _marker(quiet) is None
    assert read_receipt(runtime, vault_root=quiet) is not None

    vault, a = _migrated(tmp_path)
    b = tmp_path / "b"
    _downgrade(vault, a, 2)
    assert migrate_links(vault, b, apply=True)["complete"]
    assert _receipt(b)["operation_id"] is None and _marker(vault) is None
    assert unmigrate_links(vault, a, apply=True)["files_restored"] == 1
    assert read_receipt(b, vault_root=vault) is None


def test_legacy_receipts_in_two_runtimes(tmp_path: Path) -> None:
    vault, a = _migrated(tmp_path)
    b = tmp_path / "b"
    _downgrade(vault, a, 2)
    receipt_path(b).parent.mkdir(parents=True)
    receipt_path(b).write_text(json.dumps({"schema_version": 1, "vault_root": str(vault), "rewrites": []}), encoding="utf-8")
    assert read_receipt(a, vault_root=vault) is not None and read_receipt(b, vault_root=vault) is not None

    assert unmigrate_links(vault, a, apply=True)["files_restored"] == 1
    assert read_receipt(b, vault_root=vault) is None
    assert migrate_links(vault, b, apply=True)["files_rewritten"] == 1
    assert _marker(vault)["operation_id"] == _receipt(b)["operation_id"]  # type: ignore[index]
    assert read_receipt(a, vault_root=vault)["observed_only"] is True  # type: ignore[index]


def _watch_writes(monkeypatch: pytest.MonkeyPatch, vault: Path, runtime: Path, *, operation: str, statuses: set[str], state: str) -> list[bool]:
    original, ok = m._replace_image, []

    def watch(root: Path, image: dict[str, Any], *, undo: bool) -> bool:
        receipt, marker = _receipt(runtime), _marker(vault)
        ok.append(receipt["operation"] == operation and receipt["status"] in statuses
                  and receipt["operation_id"] is not None and marker is not None
                  and marker["operation_id"] == receipt["operation_id"] and marker["state"] == state)
        return original(root, image, undo=undo)

    monkeypatch.setattr(m, "_replace_image", watch)
    return ok


def _three(tmp_path: Path) -> tuple[Path, Path]:
    vault, runtime = tmp_path / "vault", tmp_path / "rt"
    _seed(vault)
    _note(vault, "C.md", "See [[B]].\n")
    return vault, runtime


def test_prepared_publication_precedes_every_note_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # fresh migrate
    vault, runtime = _three(tmp_path)
    ok = _watch_writes(monkeypatch, vault, runtime, operation="migrate", statuses={"prepared"}, state="migrating")
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert ok == [True, True]

    # undo
    ok = _watch_writes(monkeypatch, vault, runtime, operation="undo", statuses={"prepared"}, state="undoing")
    assert unmigrate_links(vault, runtime, apply=True)["files_restored"] == 2
    assert ok == [True, True]


def test_resume_and_force_publish_before_note_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = m._replace_image
    # resume after an interrupted first run
    vault, runtime = _three(tmp_path)
    calls = 0

    def interrupt(root: Path, image: dict[str, Any], *, undo: bool) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original(root, image, undo=undo)

    monkeypatch.setattr(m, "_replace_image", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(m, "_replace_image", original)
    ok = _watch_writes(monkeypatch, vault, runtime, operation="migrate", statuses={"prepared", "partial"}, state="migrating")
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert ok and all(ok)

    # forced re-run with a late note
    monkeypatch.setattr(m, "_replace_image", original)
    _note(vault, "D.md", "See [[B]].\n")
    ok = _watch_writes(monkeypatch, vault, runtime, operation="migrate", statuses={"prepared"}, state="migrating")
    assert migrate_links(vault, runtime, apply=True, force=True)["files_rewritten"] == 1
    assert ok and all(ok)


def test_legacy_partial_resume_claims_an_operation_before_note_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _three(tmp_path)
    assert migrate_links(vault, runtime, apply=True)["complete"]
    _downgrade(vault, runtime, 2, status="partial")
    _note(vault, "C.md", "See [[B]].\n")
    ok = _watch_writes(monkeypatch, vault, runtime, operation="migrate", statuses={"prepared", "partial"}, state="migrating")

    assert migrate_links(vault, runtime, apply=True)["complete"]

    assert len(ok) == 2 and all(ok) and (vault / "C.md").read_bytes() != b"See [[B]].\n"
    assert _marker(vault)["state"] == "migrated"  # type: ignore[index]


def test_undo_cleanup_retry_keeps_completion_cleared(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, a, b = _two(tmp_path)
    migrate_links(vault, a, apply=True)
    original = Path.unlink

    def deny(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == receipt_path(a):
            raise PermissionError("denied")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny)
    assert unmigrate_links(vault, a, apply=True)["failed"]
    assert _marker(vault)["state"] == "unmigrated" and read_receipt(b, vault_root=vault) is None  # type: ignore[index]
    assert receipt_path(a).exists()

    monkeypatch.setattr(Path, "unlink", original)
    retry = unmigrate_links(vault, a, apply=True)
    assert not retry["failed"] and retry["files_restored"] == 0
    assert not receipt_path(a).exists() and _marker(vault)["state"] == "unmigrated"  # type: ignore[index]


def test_interrupt_between_claim_and_marker_is_repaired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _three(tmp_path)
    assert migrate_links(vault, runtime, apply=True)["complete"]
    migrated = _notes(vault)
    _downgrade(vault, runtime, 2, status="partial")
    _note(vault, "C.md", "See [[B]].\n")
    before = _listing(vault)
    real = m._publish_marker

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(m, "_publish_marker", interrupt)
    with pytest.raises(KeyboardInterrupt):
        migrate_links(vault, runtime, apply=True)
    monkeypatch.setattr(m, "_publish_marker", real)

    claimed = _receipt(runtime)
    assert len(claimed["operation_id"]) == 32 and claimed["status"] == "partial" and claimed["base_marker"] is None
    assert _marker(vault) is None and _listing(vault) == before
    assert m._binding(claimed, None) == "staged"
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert _marker(vault) == {"format": 1, "operation_id": claimed["operation_id"], "state": "migrated"}
    assert _notes(vault) == migrated


def test_plain_rerun_repairs_marker_after_failed_settlement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = tmp_path / "vault", tmp_path / "rt"
    _seed(vault)
    real = m._publish_marker

    def deny_settle(root: Path, marker: dict[str, Any], *, expected: dict[str, Any] | None) -> None:
        if marker["state"] == "migrated":
            raise PermissionError("denied")
        real(root, marker, expected=expected)

    monkeypatch.setattr(m, "_publish_marker", deny_settle)
    result = migrate_links(vault, runtime, apply=True)
    assert result["complete"] is False
    assert any(f["path"] == str(vault.resolve() / m.MARKER_NAME) and "settlement failed" in f["error"] for f in result["failed"])
    operation = _receipt(runtime)["operation_id"]
    assert _receipt(runtime)["status"] == "migrated" and _marker(vault) == {"format": 1, "operation_id": operation, "state": "migrating"}
    assert read_receipt(runtime, vault_root=vault) is None

    monkeypatch.setattr(m, "_publish_marker", real)
    calls = _record_calls(monkeypatch, "_replace_image")
    assert migrate_links(vault, runtime, apply=True)["complete"]
    assert _marker(vault) == {"format": 1, "operation_id": operation, "state": "migrated"}
    assert read_receipt(runtime, vault_root=vault) is not None and not calls


def test_undo_marker_settlement_failure_keeps_receipt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault, runtime = _migrated(tmp_path)
    operation = _marker(vault)["operation_id"]  # type: ignore[index]
    real = m._publish_marker

    def deny_settle(root: Path, marker: dict[str, Any], *, expected: dict[str, Any] | None) -> None:
        if marker["state"] == "unmigrated":
            raise PermissionError("denied")
        real(root, marker, expected=expected)

    monkeypatch.setattr(m, "_publish_marker", deny_settle)
    result = unmigrate_links(vault, runtime, apply=True)
    assert result["files_restored"] == 1 and result["receipt_removed"] is False
    assert receipt_path(runtime).exists() and _marker(vault) == {"format": 1, "operation_id": operation, "state": "undoing"}
    assert any(f["path"] == str(vault.resolve() / m.MARKER_NAME) and "retry undo" in f["error"] for f in result["failed"])

    monkeypatch.setattr(m, "_publish_marker", real)
    assert not unmigrate_links(vault, runtime, apply=True)["failed"]
    assert _marker(vault)["state"] == "unmigrated" and not receipt_path(runtime).exists()  # type: ignore[index]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_observer_in_a_dirty_git_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(m, "vault_git_state", _REAL_GIT_STATE)

    def init(vault: Path) -> None:
        _seed(vault)
        for args in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "commit", "-qm", "seed"]):
            subprocess.run(["git", "-C", str(vault), *args], check=True, capture_output=True)

    vault, a, b = _two(tmp_path)
    shutil.rmtree(vault)
    init(vault)
    assert migrate_links(vault, a, apply=True)["complete"]
    assert m.vault_git_state(vault)["dirty"] is True

    seen = migrate_links(vault, b, apply=True)
    assert seen["observed_only"] is True and "skipped" not in seen

    _note(vault, "C.md", "See [[B]].\n")
    marker_before = (vault / m.MARKER_NAME).read_bytes()
    publishes = _spy(monkeypatch, "_publish_receipt", "_publish_marker")
    skipped = migrate_links(vault, b, apply=True)
    assert skipped["skipped"] == "vault has uncommitted changes"
    assert publishes == []
    assert (vault / "C.md").read_bytes() == b"See [[B]].\n"
    assert (vault / m.MARKER_NAME).read_bytes() == marker_before
    assert not receipt_path(b).exists()

    plain = tmp_path / "plain"
    init(plain)
    _note(plain, "B.md", "# B edited\n")
    planned = _spy(monkeypatch, "migrate_vault_links")
    assert migrate_links(plain, tmp_path / "c", apply=True)["skipped"] == "vault has uncommitted changes"
    assert planned == [] and publishes == []
    assert not (plain / m.MARKER_NAME).exists() and not receipt_path(tmp_path / "c").exists()


def _spy(monkeypatch: pytest.MonkeyPatch, *names: str) -> list[str]:
    calls: list[str] = []
    for name in names:
        real = getattr(m, name)
        monkeypatch.setattr(m, name, lambda *a, _n=name, _real=real, **k: (calls.append(_n), _real(*a, **k))[1])
    return calls


def _plant_marker(vault: Path, kind: str, tmp_path: Path) -> str:
    """Put an untrustworthy marker in the vault; return the refusal it must produce."""
    path = vault / m.MARKER_NAME
    path.unlink(missing_ok=True)
    payloads = {"not-json": b"{", "not-utf8": b"\xff\xfe", "deep": b"[" * 100_000,
                "extra-key": json.dumps({"format": 1, "operation_id": "0" * 32, "state": "migrated", "x": 1}).encode()}
    if kind in payloads:
        path.write_bytes(payloads[kind])
        return m._MARKER_MALFORMED_MESSAGE
    if kind == "directory":
        path.mkdir()
    else:
        outside = tmp_path / "outside-marker.json"
        outside.write_text(json.dumps({"format": 1, "operation_id": "0" * 32, "state": "migrated"}))
        try:
            path.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
    return m._MARKER_UNSAFE_MESSAGE


def _marker_entry(vault: Path) -> tuple[str, Any]:
    path = vault / m.MARKER_NAME
    if path.is_symlink():
        return "link", os.readlink(path)
    if path.is_dir():
        return "dir", None
    return "file", path.read_bytes()


_BAD_MARKERS = pytest.mark.parametrize("kind", ["not-json", "not-utf8", "deep", "extra-key", "symlink", "directory"])


@_BAD_MARKERS
def test_untrustworthy_marker_refuses_migration(tmp_path: Path, kind: str) -> None:
    vault, runtime = tmp_path / "vault", tmp_path / "runtime"
    _seed(vault)
    notes = _notes(vault)
    refusal = _plant_marker(vault, kind, tmp_path)
    entry = _marker_entry(vault)

    result = migrate_links(vault, runtime, apply=True)

    assert result["complete"] is False and refusal in result["failed"][0]["error"]
    assert _notes(vault) == notes and _marker_entry(vault) == entry
    assert not receipt_path(runtime).exists()
    with pytest.raises(ReceiptUnavailable, match=refusal):
        read_receipt(runtime, vault_root=vault)


@_BAD_MARKERS
def test_untrustworthy_marker_refuses_undo(tmp_path: Path, kind: str) -> None:
    vault, runtime = _migrated(tmp_path)
    notes, receipt = _notes(vault), receipt_path(runtime).read_bytes()
    refusal = _plant_marker(vault, kind, tmp_path)
    entry = _marker_entry(vault)

    result = unmigrate_links(vault, runtime, apply=True)

    assert result["files_restored"] == 0 and refusal in result["failed"][0]["error"]
    assert _notes(vault) == notes and _marker_entry(vault) == entry
    assert receipt_path(runtime).read_bytes() == receipt


def test_unmigrated_marker_with_nothing_to_convert_writes_nothing(tmp_path: Path) -> None:
    vault, a, b = _two(tmp_path)
    migrate_links(vault, a, apply=True)
    unmigrate_links(vault, a, apply=True)
    _note(vault, "A.md", "See B.\n")
    raw = (vault / m.MARKER_NAME).read_bytes()

    result = migrate_links(vault, b, apply=True)

    assert result["complete"] is True and "observed_only" not in result and "receipt_path" not in result
    assert not receipt_path(b).exists() and (vault / m.MARKER_NAME).read_bytes() == raw
