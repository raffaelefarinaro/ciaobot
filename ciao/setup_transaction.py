"""Private authority for resuming a separate-notes first setup."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

from ciao.memory_tool import default_memory_dir
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import make_private_dir, mkstemp_private, open_private


def _identity(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_dev, stat.st_ino]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path: Path, data: bytes) -> None:
    fd, name = mkstemp_private(dir=path.parent, prefix=".setup-", suffix=".tmp")
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _sync_dir(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def pending_matches(root: Path, notes: Path) -> bool:
    state = default_memory_dir().expanduser().resolve() / "setup-transactions"
    journal = state / (hashlib.sha256(os.fsencode(str(root))).hexdigest() + ".json")
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        tx = SetupTransaction.__new__(SetupTransaction)
        tx.root, tx.notes, tx.data = root, notes, data
        if (data["root"] != str(root) or data["notes"] != str(notes)
            or data["root_id"] != _identity(root) or data["notes_id"] != _identity(notes)
            or data["complete"]):
            return False
        tx.verify_binding("env")
        tx.verify_binding("registry")
        return True
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False


class SetupTransaction:
    def __init__(self, root: Path, notes: Path):
        self.root, self.notes = root, notes
        state = default_memory_dir().expanduser().resolve() / "setup-transactions"
        if state == root or state == notes or root in state.parents or notes in state.parents:
            raise ValueError("setup transaction state must be separate from both folders")
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        make_private_dir(state)
        key = hashlib.sha256(os.fsencode(str(root))).hexdigest()
        self.journal = state / f"{key}.json"
        self.lock_fd = open_private(state / f"{key}.lock", os.O_RDWR | os.O_CREAT)
        try:
            lock_exclusive(self.lock_fd, blocking=False)
        except BaseException:
            os.close(self.lock_fd)
            raise
        self.data: dict[str, Any] = {}

    def close(self) -> None:
        unlock(self.lock_fd)
        os.close(self.lock_fd)

    def save(self) -> None:
        _atomic(self.journal, json.dumps(self.data, sort_keys=True).encode())

    def begin(self, password: str | None, expected_notes: tuple[int, int] | None = None) -> str:
        from ciao.cli import select_setup_vault
        from ciao.setup_marker import is_set_up_workspace

        if expected_notes is not None and _identity(self.notes) != list(expected_notes):
            raise ValueError("notes folder changed since inspection; inspect it again")
        self._discard_unpublished()
        if self.journal.exists():
            try:
                data = json.loads(self.journal.read_text(encoding="utf-8"))
                if not self._folders_match(data) or data["complete"]:
                    raise ValueError("pending setup does not match these folders")
                self.data = data
                for name in ("env", "registry"):
                    self.verify_binding(name)
                token = password if password is not None else data.get("generated_token")
                if not isinstance(token, str) or not hmac.compare_digest(
                    hashlib.scrypt(token.encode(), salt=bytes.fromhex(data["salt"]), n=16384, r=8, p=1).hex(),
                    data["verifier"],
                ):
                    raise ValueError("Use the password chosen when setup began.")
                return token
            except (KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
                raise ValueError("pending setup is invalid or unavailable") from exc
        selection = select_setup_vault(self.root, vault_mode="scratch")
        if is_set_up_workspace(self.root) or selection.source in {"env", "registry", "legacy"}:
            raise ValueError("an existing installation cannot be rebound during setup")
        if "PWA_AUTH_TOKEN" in selection.existing_env:
            raise ValueError("existing password configuration cannot be used for separate notes setup")
        token = password or secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        env = self.root / ".env"
        if env.is_symlink():
            raise ValueError("setup cannot change a symlinked .env")
        self.data = {
            "root": str(self.root), "notes": str(self.notes), "root_id": _identity(self.root),
            "notes_id": _identity(self.notes), "salt": salt.hex(),
            "verifier": hashlib.scrypt(token.encode(), salt=salt, n=16384, r=8, p=1).hex(),
            "generated_token": token if password is None else None, "complete": False,
            "env": {"original": _digest(env.read_bytes()) if env.exists() else None,
                    "original_identity": _identity(env) if env.exists() else None},
            "registry": {"original": None, "original_identity": None},
        }
        self.save()
        return token

    def _folders_match(self, data: dict[str, Any]) -> bool:
        same: bool = (data["root"] == str(self.root) and data["notes"] == str(self.notes)
                      and data["root_id"] == _identity(self.root)
                      and data["notes_id"] == _identity(self.notes))
        return same

    def _discard_unpublished(self) -> None:
        """Delete a journal that no longer fits and never published a binding.

        Such a journal only records intent (the password verifier); nothing it
        authorises exists on disk, so a different notes folder, a replaced
        folder inode or an edited `.env` starts a fresh setup instead of
        failing for good. Once either binding was published the journal is the
        only proof of ownership and is never discarded here.
        """
        try:
            data = json.loads(self.journal.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return  # absent or unreadable: begin() reports it
        try:
            if any(data[name].get("published") for name in ("env", "registry")):
                return
            self.data = data
            current = self._folders_match(data) and not data["complete"]
            if current:
                for name in ("env", "registry"):
                    self.verify_binding(name)
        except (KeyError, TypeError, AttributeError, OSError):
            return
        except ValueError:
            current = False
        if not current:
            self.journal.unlink()
            _sync_dir(self.journal.parent)
            self.data = {}

    def pin(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        """The values a binding's text is built from.

        Until the binding is published the caller's values are recorded; after
        that the journaled ones win, so a retry rebuilds the identical text
        even when it is given a different port.
        """
        item = self.data[name]
        if item.get("published"):
            if "values" not in item:
                raise ValueError("pending setup predates this version; finish or remove it with that version")
            pinned: dict[str, Any] = item["values"]
            return pinned
        if item.get("values") != values:
            item["values"] = values
            self.save()
        return values

    def verify_binding(self, name: str) -> None:
        item = self.data[name]
        path = self.root / (".env" if name == "env" else ".runtime/workspaces.json")
        if not path.exists():
            if item["original"] is not None:
                raise ValueError("setup binding changed; refusing to resume")
            return
        content = _digest(path.read_bytes())
        if content == item["original"] and _identity(path) == item["original_identity"]:
            return
        if content != item.get("published") or _identity(path) != item.get("identity"):
            raise ValueError("setup binding changed; refusing to resume")

    def _remove_stage(self, name: str) -> None:
        item = self.data[name]
        raw = item.get("stage")
        if not raw:
            return
        path = Path(raw)
        parent = self.root if name == "env" else self.root / ".runtime"
        if path.parent != parent or not path.name.startswith(".setup-binding-"):
            raise ValueError("pending setup stage is invalid")
        if path.exists():
            if _identity(path) != item.get("identity"):
                raise ValueError("pending setup stage changed")
            path.unlink()
            _sync_dir(parent)

    def publish(self, name: str, text: str, *, owner: Any = None) -> None:
        """Publish a binding file.

        The registry is created with `os.link`, which fails when the file
        exists: a compare-and-swap on "absent" that also keeps the staged
        inode as the journaled identity (`owner.replace` renames a temp file it
        creates itself, so the identity could not be journaled first). Given
        the registry `owner`, whose lock this must run under, the swap's
        expectation is checked first and the owner's view is advanced after, so
        a later `owner.replace(expected=...)` is not a stale-view conflict.
        """
        path = self.root / (".env" if name == "env" else ".runtime/workspaces.json")
        self.verify_binding(name)
        item = self.data[name]
        raw = text.encode()
        if item.get("published"):
            if item["published"] != _digest(raw):
                raise ValueError("setup binding differs from pending setup")
            if path.exists() and _identity(path) == item["identity"]:
                self._remove_stage(name)
                if owner is not None:
                    owner.current = raw
                return
        if owner is not None and (name != "registry" or owner.current is not None):
            from ciao.workspaces import WorkspaceRegistryConflict

            raise WorkspaceRegistryConflict("workspace registry changed since it was loaded")
        self._remove_stage(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, stage_name = mkstemp_private(dir=path.parent, prefix=".setup-binding-", suffix=".tmp")
        stage = Path(stage_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                item.update(published=_digest(raw), identity=_identity(stage), stage=str(stage))
                self.save()
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if item["original"] is None:
                os.link(stage, path)
                stage.unlink()
            else:
                os.replace(stage, path)
            _sync_dir(path.parent)
            if owner is not None:
                owner.current = raw
        finally:
            stage.unlink(missing_ok=True)

    def complete(self) -> None:
        if _identity(self.root) != self.data["root_id"] or _identity(self.notes) != self.data["notes_id"]:
            raise ValueError("notes or Ciaobot folder changed during setup")
        self.verify_binding("env")
        self.verify_binding("registry")
        if not all(self.data[name].get("published") for name in ("env", "registry")):
            raise ValueError("setup bindings were not published")
        self._remove_stage("env")
        self._remove_stage("registry")
        self.data["complete"] = True
        self.save()
        self.journal.unlink()
        _sync_dir(self.journal.parent)
