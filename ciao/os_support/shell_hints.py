"""Copyable shell lines that put a directory on the user's ``PATH``.

Two callers want this text and they want different shapes: ``ciao setup``
prints a session-only line, the setup wizard hands out a line that survives a
new terminal. Rendering either one per OS is easy to get wrong -- a POSIX
``export`` line does nothing in PowerShell or cmd (#696) -- so the wording and
the quoting live here once and every call site asks this module.
"""

from __future__ import annotations

import os
import hashlib
import ctypes
import json
import ntpath
import re
import selectors
import shlex
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_path_change_lock = threading.Lock()
_attempted_path_revisions: set[str] = set()
_WindowsResolver = Callable[[str, str, str], str | None]


@dataclass(frozen=True)
class _PosixAccount:
    uid: int
    home: str
    shell: str
    name: str


def _posix_account() -> _PosixAccount:
    if sys.platform == "win32":
        raise OSError("POSIX accounts unavailable")
    import pwd
    account = pwd.getpwuid(os.geteuid())
    return _PosixAccount(account.pw_uid, account.pw_dir, account.pw_shell, account.pw_name)


def _run_posix_probe(account: _PosixAccount, interactive: bool, environment: dict[str, str]) -> bytes:
    from ciao.os_support.processes import ProcessTree, tree_spawn_options

    shell = os.path.basename(account.shell)
    if shell == "fish":
        values = '"$PATH" "$HOME" "$ZDOTDIR" "$XDG_CONFIG_HOME"'
    else:
        values = '"$PATH" "$HOME" "${ZDOTDIR-}" "${XDG_CONFIG_HOME-}"'
    snippet = ("builtin printf '__ciaobot_path_probe_PATH_START__%s__ciaobot_path_probe_PATH_END__"
               "__ciaobot_path_probe_HOME_START__%s__ciaobot_path_probe_HOME_END__"
               "__ciaobot_path_probe_ZDOTDIR_START__%s__ciaobot_path_probe_ZDOTDIR_END__"
               "__ciaobot_path_probe_XDG_START__%s__ciaobot_path_probe_XDG_END__' " + values)
    process = subprocess.Popen([account.shell, "-lic" if interactive else "-lc", snippet], cwd=account.home,
                               env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, **tree_spawn_options())
    tree = ProcessTree(process.pid)
    deadline = time.monotonic() + 5
    output = {"stdout": bytearray(), "stderr": bytearray()}
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Shell probe timed out")
                for key, _ in selector.select(remaining):
                    data = os.read(key.fd, 8192)
                    if not data:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(data)
                    if len(output[key.data]) > (65536 if key.data == "stdout" else 8192):
                        raise ValueError("Shell probe output exceeded limit")
            if process.wait(timeout=max(0, deadline - time.monotonic())) != 0:
                raise ValueError("Shell probe failed")
        return bytes(output["stdout"])
    except Exception:
        try:
            tree.kill()
        except OSError:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise
    finally:
        tree.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()


def _probe_posix_account(account: _PosixAccount,
                         runner: Callable[[_PosixAccount, bool, dict[str, str]], bytes] = _run_posix_probe) -> dict[str, str]:
    if not os.path.isabs(account.home) or not os.path.isabs(account.shell):
        raise ValueError("Account paths must be absolute")
    if os.path.basename(account.shell) not in ("bash", "zsh", "fish"):
        raise ValueError("Unsupported shell")
    shell_info = os.stat(account.shell)
    if not stat.S_ISREG(shell_info.st_mode) or not os.access(account.shell, os.X_OK):
        raise ValueError("Shell is not executable")
    environment = {"HOME": account.home, "USER": account.name, "LOGNAME": account.name,
                   "SHELL": account.shell, "LANG": "C.UTF-8"}
    frames: list[dict[str, str]] = []
    for interactive in (True, False):
        text = runner(account, interactive, environment).decode("utf-8", errors="strict")
        values: dict[str, str] = {}
        previous_end = 0
        for name, marker in (("PATH", "PATH"), ("HOME", "HOME"), ("ZDOTDIR", "ZDOTDIR"), ("XDG_CONFIG_HOME", "XDG")):
            start, end = f"__ciaobot_path_probe_{marker}_START__", f"__ciaobot_path_probe_{marker}_END__"
            if text.count(start) != 1 or text.count(end) != 1:
                raise ValueError("Malformed shell probe")
            left, right = text.index(start) + len(start), text.index(end)
            if right < left or left - len(start) < previous_end:
                raise ValueError("Malformed shell probe")
            values[name] = text[left:right]
            previous_end = right + len(end)
        if (not values["PATH"] or values["HOME"] != account.home
                or any(not os.path.isabs(part) for part in values["PATH"].split(":"))):
            raise ValueError("Unsafe shell environment")
        for key in ("ZDOTDIR", "XDG_CONFIG_HOME"):
            root = values[key]
            if root and (not os.path.isabs(root) or os.path.commonpath((account.home, os.path.normpath(root))) != account.home):
                raise ValueError("Unsafe shell configuration root")
        frames.append(values)
    if any(frames[0][key] != frames[1][key] for key in ("HOME", "ZDOTDIR", "XDG_CONFIG_HOME")):
        raise ValueError("Ambiguous shell configuration")
    return frames[0]


def _posix_state(command: str, executable: str, account: _PosixAccount, environment: Mapping[str, str],
                 account_key: str) -> tuple[dict[str, Any], str, str]:
    from ciao.os_support.tool_path import resolve_executable

    if sys.platform == "win32":
        raise OSError("POSIX profile inspection unavailable")
    if not command or os.path.basename(command) != command or any(c in command for c in "/\x00"):
        raise ValueError("Command must be a bare name")
    target, links = _target_identity(executable)
    directory = os.path.dirname(executable)
    if any(c in directory for c in ":\x00\r\n"):
        raise ValueError("Directory cannot be represented in PATH")
    home = Path(account.home)
    shell = os.path.basename(account.shell)
    if not home.is_absolute() or environment.get("HOME") != account.home or not environment.get("PATH"):
        raise ValueError("Unsafe account environment")
    if any(not os.path.isabs(part) for part in environment["PATH"].split(":")):
        raise ValueError("Unsafe PATH entry")
    for name in ("ZDOTDIR", "XDG_CONFIG_HOME"):
        root = environment.get(name, "")
        if root and (not os.path.isabs(root) or os.path.commonpath((account.home, os.path.normpath(root))) != account.home):
            raise ValueError("Unsafe shell configuration root")
    if shell == "bash":
        candidates = [home / name for name in (".bash_profile", ".bash_login", ".profile")]
        destination = next((path for path in candidates if os.path.lexists(path)), candidates[0])
    elif shell == "zsh":
        destination = Path(environment.get("ZDOTDIR") or account.home) / ".zshrc"
    elif shell == "fish":
        destination = Path(environment.get("XDG_CONFIG_HOME") or str(home / ".config")) / "fish" / "config.fish"
    else:
        raise ValueError("Unsupported shell")
    destination = Path(os.path.normpath(destination))
    if os.path.commonpath((account.home, str(destination))) != account.home or destination == home:
        raise ValueError("Unsafe profile destination")
    ancestors: list[dict[str, Any]] = []
    for parent in [home, *(home / Path(*destination.relative_to(home).parts[:i])
                            for i in range(1, len(destination.relative_to(home).parts)) )]:
        try:
            info = os.lstat(parent)
        except FileNotFoundError:
            ancestors.append({"path": str(parent), "absent": True})
            continue
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != account.uid or info.st_mode & 0o022:
            raise ValueError("Unsafe profile parent")
        ancestors.append({"path": str(parent), "device": info.st_dev, "inode": info.st_ino,
                          "uid": info.st_uid, "mode": info.st_mode})
    if ancestors[0].get("absent"):
        raise ValueError("Missing account home")
    old = b""
    profile: dict[str, Any] = {"absent": True}
    try:
        info = os.lstat(destination)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_uid != account.uid or info.st_mode & 0o022 or info.st_nlink != 1:
            raise ValueError("Unsafe profile")
        fd = os.open(destination, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(fd, "rb") as source:
                opened = os.fstat(source.fileno())
                if _identity(opened) != _identity(info) or opened.st_nlink != 1:
                    raise ValueError("Profile changed")
                old = source.read()
                after_read = os.fstat(source.fileno())
                if _identity(after_read) != _identity(info) or after_read.st_nlink != 1:
                    raise ValueError("Profile changed")
        except BaseException:
            raise
        profile = {"identity": _identity(info), "uid": info.st_uid, "mode": info.st_mode}
    block_id = hashlib.sha256(directory.encode("utf-8")).hexdigest()[:16]
    opening = f"# >>> Ciaobot PATH {block_id} >>>".encode()
    closing = f"# <<< Ciaobot PATH {block_id} <<<".encode()
    if shell == "fish":
        quoted = "'" + directory.replace("\\", "\\\\").replace("'", "\\'") + "'"
        line = f"fish_add_path --append --path {quoted}".encode()
    else:
        quoted = shlex.quote(directory)
        line = f'case ":$PATH:" in *:{quoted}:*) ;; *) export PATH="$PATH":{quoted} ;; esac'.encode()
    lines = old.splitlines()
    marker_re = re.compile(rb"# (>>>|<<<) Ciaobot PATH ([0-9a-f]{16}) (>>>|<<<)$")
    active: bytes | None = None
    own_count = 0
    for index, existing in enumerate(lines):
        if existing.startswith((b"# >>> Ciaobot PATH", b"# <<< Ciaobot PATH")):
            match = marker_re.fullmatch(existing)
            if not match:
                raise ValueError("Malformed PATH marker")
            if match.group(1) == b">>>" and match.group(3) == b">>>" and active is None:
                active = match.group(2)
                if existing == opening:
                    own_count += 1
                    if index + 2 >= len(lines) or lines[index + 1] != line or lines[index + 2] != closing:
                        raise ValueError("Modified PATH block")
            elif match.group(1) == b"<<<" and match.group(3) == b"<<<" and active == match.group(2):
                active = None
            else:
                raise ValueError("Malformed PATH marker")
    if active is not None or own_count > 1:
        raise ValueError("Malformed PATH block")
    if old and not old.endswith(b"\n") and lines and lines[-1].startswith(b"#") and opening.startswith(lines[-1]):
        raise ValueError("Partial PATH marker")
    newline = b"\n"
    append = (newline if old and not old.endswith(b"\n") else b"") + newline.join((opening, line, closing, b""))
    shell_stat = os.stat(account.shell)
    state: dict[str, Any] = {"schema": 1, "platform": "posix", "command": command,
        "executable": executable, "target_identity": target, "symlink_identities": links,
        "account_key": account_key, "windows": None, "posix": {
            "uid": account.uid, "home": account.home, "shell": account.shell,
            "shell_identity": _identity(shell_stat), "environment": dict(environment),
            "path": environment["PATH"], "destination": str(destination), "profile": profile,
            "profile_bytes_hex": old.hex(), "ancestors": ancestors, "append_hex": append.hex()}}
    found = resolve_executable(command, path=environment["PATH"])
    if found:
        return state, "already_configured" if os.path.samefile(found, executable) else "refused", str(destination)
    if own_count:
        return state, "refused", str(destination)
    found = resolve_executable(command, path=environment["PATH"] + ":" + directory)
    return state, "ready" if found and os.path.samefile(found, executable) else "refused", str(destination)


def _preview_posix_path_change(command: str, executable: str, account: _PosixAccount,
                               probe: Callable[[_PosixAccount], Mapping[str, str]], account_key: str) -> dict[str, str]:
    result = {"status": "refused", "detail": "PATH could not be safely updated.", "executable": executable,
              "directory": os.path.dirname(executable), "destination": "", "revision": ""}
    with _path_change_lock:
        try:
            state, status, destination = _posix_state(command, executable, account, probe(account), account_key)
            result["status"], result["destination"] = status, destination
            if status == "ready":
                result["revision"] = _path_revision(state)
                _attempted_path_revisions.discard(result["revision"])
        except Exception:
            pass
    return result


def _apply_posix_path_change(command: str, executable: str, revision: str) -> dict[str, str]:
    from ciao.os_support.tool_path import clear_terminal_path_cache
    from ciao.os_support.users import user_key

    def result(status: str, detail: str, mutation: str = "none") -> dict[str, str]:
        return {"status": status, "detail": detail, "mutation": mutation, "warning": ""}

    if sys.platform == "win32":
        return result("refused", "POSIX PATH changes are unavailable.")
    with _path_change_lock:
        try:
            account = _posix_account()
            state, status, _ = _posix_state(command, executable, account, _probe_posix_account(account), user_key())
            if not revision or revision in _attempted_path_revisions or revision != _path_revision(state):
                return result("stale", "PATH approval is stale.")
            if status != "ready":
                return result(status, "PATH cannot be updated.")
        except Exception:
            return result("stale", "PATH approval is stale.")
        posix = state["posix"]
        ancestors = posix["ancestors"]
        descriptors: list[int] = []
        changed = False
        attempted_write = False
        write_returned = False
        written = False
        cache_cleared = False
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            root = os.open(account.home, flags)
            descriptors.append(root)
            root_info = os.fstat(root)
            first = ancestors[0]
            if (root_info.st_dev, root_info.st_ino, root_info.st_uid, root_info.st_mode) != (
                first["device"], first["inode"], first["uid"], first["mode"]
            ):
                raise OSError("Home changed")
            parent = root
            for ancestor in ancestors[1:]:
                name = os.path.basename(ancestor["path"])
                if ancestor.get("absent"):
                    os.mkdir(name, 0o700, dir_fd=parent)
                    changed = True
                    os.fsync(parent)
                child = os.open(name, flags, dir_fd=parent)
                descriptors.append(child)
                info = os.fstat(child)
                if ancestor.get("absent"):
                    if info.st_uid != account.uid or stat.S_IMODE(info.st_mode) != 0o700:
                        raise OSError("Created directory is unsafe")
                elif (info.st_dev, info.st_ino, info.st_uid, info.st_mode) != (
                    ancestor["device"], ancestor["inode"], ancestor["uid"], ancestor["mode"]
                ):
                    raise OSError("Directory changed")
                parent = child
            filename = os.path.basename(posix["destination"])
            profile = posix["profile"]
            old = bytes.fromhex(posix["profile_bytes_hex"])
            if profile.get("absent"):
                fd = os.open(filename, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW | os.O_CREAT | os.O_EXCL,
                             0o600, dir_fd=parent)
                changed = True
            else:
                fd = os.open(filename, os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, dir_fd=parent)
            descriptors.append(fd)
            info = os.fstat(fd)
            if info.st_nlink != 1:
                raise OSError("Profile has multiple links")
            if profile.get("absent"):
                if not stat.S_ISREG(info.st_mode) or info.st_uid != account.uid or stat.S_IMODE(info.st_mode) != 0o600:
                    raise OSError("Created profile is unsafe")
            elif (_identity(info) != profile["identity"] or info.st_uid != profile["uid"]
                  or info.st_mode != profile["mode"]):
                raise OSError("Profile changed")
            current = bytearray()
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                current.extend(chunk)
            checked = os.fstat(fd)
            if bytes(current) != old or _identity(checked) != _identity(info) or checked.st_nlink != 1:
                raise OSError("Profile changed")
            payload = bytes.fromhex(posix["append_hex"])
            attempted_write = True
            _attempted_path_revisions.add(revision)
            count = os.write(fd, payload)
            write_returned = True
            if count != len(payload):
                return result("write_failed", "PATH update was incomplete.", "partial" if changed or count else "none")
            changed = True
            written = True
            os.fsync(fd)
            os.fsync(parent)
        except Exception:
            if written:
                return result("verification_failed", "PATH was written but could not be verified.", "written")
            mutation = "unknown" if attempted_write and not write_returned else ("partial" if changed else "none")
            return result("write_failed", "PATH could not be written completely.", mutation)
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if written:
                try:
                    clear_terminal_path_cache()
                    cache_cleared = True
                except Exception:
                    pass
        if written and cache_cleared:
            try:
                fresh = _posix_account()
                if fresh != account:
                    raise ValueError("Account changed")
                _, verified, _ = _posix_state(command, executable, fresh, _probe_posix_account(fresh), user_key())
                if verified == "already_configured":
                    return result("applied", "PATH updated; reopen terminals.", "written")
            except Exception:
                pass
        return result("verification_failed", "PATH was written but could not be verified.", "written")


def preview_path_change(command: str, executable: str) -> dict[str, str]:
    """Preview a PATH append without writing configuration."""
    from ciao.os_support.users import user_key

    try:
        if sys.platform == "win32":
            import winreg
            return _preview_windows_path_change(command, executable, winreg, os.environ,
                                                _resolve_windows_path_executable, user_key())
        account = _posix_account()
        return _preview_posix_path_change(command, executable, account, _probe_posix_account, user_key())
    except Exception:
        return {"status": "refused", "detail": "PATH could not be safely inspected.",
                "executable": executable, "directory": os.path.dirname(executable), "destination": "", "revision": ""}


def apply_path_change(command: str, executable: str, revision: str) -> dict[str, str]:
    """Apply a previously previewed PATH append and verify its effect."""
    try:
        if sys.platform == "win32":
            import winreg
            from ciao.os_support.users import user_key
            return _apply_windows_path_change(command, executable, revision, winreg, os.environ,
                                             _resolve_windows_path_executable, user_key(), _broadcast_windows_path_change)
        return _apply_posix_path_change(command, executable, revision)
    except Exception:
        return {"status": "write_failed", "detail": "PATH could not be updated.", "mutation": "unknown", "warning": ""}


def _identity(st: os.stat_result) -> list[int]:
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def _target_identity(executable: str) -> tuple[list[int], list[dict[str, Any]]]:
    if not os.path.isabs(executable):
        raise ValueError("Executable must be absolute")
    # Record each link, including links in parent components.
    pending = Path(executable)
    links: list[dict[str, Any]] = []
    for _ in range(40):
        parts = pending.parts
        prefix = Path(parts[0])
        for index, part in enumerate(parts[1:], 1):
            prefix /= part
            info = os.lstat(prefix)
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(prefix)
                links.append({"path": str(prefix), "identity": _identity(info), "link": os.fsencode(link).hex()})
                pending = Path(os.path.abspath(os.path.join(str(prefix.parent), link, *parts[index + 1:])))
                break
        else:
            info = os.stat(pending)
            if not stat.S_ISREG(info.st_mode) or not os.access(pending, os.X_OK):
                raise ValueError("Executable is not a regular executable")
            return _identity(info), links
    raise ValueError("Too many executable links")


def _validate_windows_path(path: str) -> None:
    if not path:
        return
    for entry in path.split(";"):
        entry = entry.strip()
        if entry.startswith('"') and entry.endswith('"'):
            entry = entry[1:-1]
        if not entry or '"' in entry:
            raise ValueError("Unsafe PATH entry")
        if sys.platform == "win32":
            drive, tail = ntpath.splitdrive(entry)
            unc_root = drive.startswith(("\\\\", "//")) and len(drive[2:].replace("/", "\\").split("\\")) >= 2
            if not drive or (not tail.startswith(("\\", "/")) and not (unc_root and not tail)):
                raise ValueError("Unsafe PATH entry")
        elif not os.path.isabs(entry):
            raise ValueError("Unsafe PATH entry")


def _windows_state(command: str, executable: str, registry: Any, environment: Mapping[str, str],
                   resolve: _WindowsResolver, account_key: str) -> tuple[dict[str, Any], str, str]:
    if not command or os.path.basename(command) != command or any(c in command for c in "/\\\x00"):
        raise ValueError("Command must be a bare name")
    target, links = _target_identity(executable)
    directory = os.path.dirname(executable)
    if any(c in directory for c in ";\x00\r\n"):
        raise ValueError("Directory cannot be represented in PATH")
    machine, user = _read_windows_path_sources(registry)
    env = {key.upper(): value for key, value in environment.items()}
    inputs: dict[str, str | None] = {}

    def expand(value: str) -> str:
        def replace(match: re.Match[str]) -> str:
            name = match.group(1).upper()
            inputs[name] = env.get(name)
            if inputs[name] is None:
                raise ValueError("Unresolved PATH variable")
            return str(inputs[name])
        return re.sub(r"%([^%]+)%", replace, value)

    machine_expanded = expand(machine[1]) if machine[2] == registry.REG_EXPAND_SZ else machine[1]
    user_expanded = expand(user[1]) if user[2] == registry.REG_EXPAND_SZ else user[1]
    _validate_windows_path(machine_expanded)
    _validate_windows_path(user_expanded)
    effective = ";".join(value for value in (machine_expanded, user_expanded) if value)
    pathext = env.get("PATHEXT", "") or ".COM;.EXE;.BAT;.CMD"
    proposed = user[1] + (";" if user[1] and not user[1].endswith(";") else "") + directory
    proposed_kind = user[2] if user[0] else registry.REG_EXPAND_SZ
    proposed_expanded = expand(proposed) if proposed_kind == registry.REG_EXPAND_SZ else proposed
    _validate_windows_path(proposed_expanded)
    state: dict[str, Any] = {
        "schema": 1, "platform": "windows", "command": command, "executable": executable,
        "target_identity": target, "symlink_identities": links, "account_key": account_key,
        "posix": None, "windows": {"machine": machine, "user": user,
            "expansion_inputs": dict(sorted(inputs.items())), "pathext": pathext, "proposed_user_path": proposed},
    }
    found = resolve(command, effective, pathext)
    if found:
        return state, "already_configured" if os.path.samefile(found, executable) else "refused", effective
    entries = [entry.strip().strip('"').rstrip("\\/").casefold() for entry in effective.split(";")]
    if directory.rstrip("\\/").casefold() in entries:
        return state, "refused", effective
    hypothetical = ";".join(value for value in (machine_expanded, proposed_expanded) if value)
    found = resolve(command, hypothetical, pathext)
    if not found or not os.path.samefile(found, executable):
        return state, "refused", effective
    return state, "ready", effective


def _path_revision(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def _preview_windows_path_change(command: str, executable: str, registry: Any, environment: Mapping[str, str],
                                 resolve: _WindowsResolver, account_key: str) -> dict[str, str]:
    result = {"status": "refused", "detail": "PATH could not be safely updated.", "executable": executable,
              "directory": os.path.dirname(executable), "destination": r"HKCU\Environment\Path", "revision": ""}
    with _path_change_lock:
        try:
            state, status, _ = _windows_state(command, executable, registry, environment, resolve, account_key)
            result["status"] = status
            result["detail"] = {"ready": "Ready to append PATH.", "already_configured": "Already configured.", "refused": "Command would not resolve safely."}[status]
            if status == "ready":
                result["revision"] = _path_revision(state)
                _attempted_path_revisions.discard(result["revision"])
        except Exception:
            pass
    return result


def _resolve_windows_path_executable(command: str, path: str, pathext: str) -> str | None:
    from ciao.os_support.tool_path import resolve_executable

    def current_pathext() -> str:
        return os.environ.get("PATHEXT", "") or ".COM;.EXE;.BAT;.CMD"

    if current_pathext() != pathext:
        raise ValueError("PATHEXT changed")
    clean_path = ";".join(entry.strip().strip('"') for entry in path.split(";"))
    found = resolve_executable(command, path=clean_path)
    if current_pathext() != pathext:
        raise ValueError("PATHEXT changed")
    return found


def _broadcast_windows_path_change(send: Callable[..., int] | None = None) -> None:
    if send is None:
        if sys.platform != "win32":
            raise OSError("Windows notification unavailable")
        from ctypes import wintypes

        native = ctypes.WinDLL("user32", use_last_error=True).SendMessageTimeoutW
        native.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPCWSTR,
                           wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        native.restype = ctypes.c_size_t
        send = native
    result = ctypes.c_size_t()
    if not send(0xFFFF, 0x001A, 0, "Environment", 2, 5000, ctypes.byref(result)):
        raise OSError("Windows PATH notification failed")


def _apply_windows_path_change(command: str, executable: str, revision: str, registry: Any,
                               environment: Mapping[str, str], resolve: _WindowsResolver,
                               account_key: str, notify: Callable[[], None]) -> dict[str, str]:
    def result(status: str, detail: str, mutation: str = "none", warning: str = "") -> dict[str, str]:
        return {"status": status, "detail": detail, "mutation": mutation, "warning": warning}

    with _path_change_lock:
        try:
            state, status, _ = _windows_state(command, executable, registry, environment, resolve, account_key)
            if not revision or revision in _attempted_path_revisions or revision != _path_revision(state):
                return result("stale", "PATH approval is stale.")
            if status != "ready":
                return result(status, "PATH cannot be updated.")
        except Exception:
            return result("stale", "PATH approval is stale.")
        written = False
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, "Environment", 0, registry.KEY_SET_VALUE) as key:
                try:
                    user = state["windows"]["user"]
                    _attempted_path_revisions.add(revision)
                    registry.SetValueEx(key, "Path", 0, user[2] if user[0] else registry.REG_EXPAND_SZ,
                                        state["windows"]["proposed_user_path"])
                    written = True
                except Exception:
                    return result("write_failed", "PATH write may have failed.", "unknown")
        except Exception:
            if written:
                return result("verification_failed", "PATH was written but could not be verified.", "written")
            return result("write_failed", "PATH could not be written.")
        warning = ""
        try:
            notify()
        except Exception:
            warning = "Windows was not notified; reopen terminals."
        try:
            _, status, _ = _windows_state(command, executable, registry, environment, resolve, account_key)
            if status == "already_configured":
                return result("applied", "PATH updated; reopen terminals.", "written", warning)
        except Exception:
            pass
        return result("verification_failed", "PATH was written but could not be verified.", "written")


def _read_windows_path_sources(
    registry: Any,
) -> tuple[tuple[bool, str, int | None], tuple[bool, str, int | None]]:
    """Read both raw registry PATHs, failing closed if either is unknown."""
    def read(hive: Any, subkey: str) -> tuple[bool, str, int | None]:
        with registry.OpenKey(hive, subkey) as key:
            try:
                value, kind = registry.QueryValueEx(key, "Path")
            except FileNotFoundError:
                return False, "", None
        if not isinstance(value, str) or kind not in (registry.REG_SZ, registry.REG_EXPAND_SZ):
            raise ValueError("Invalid registry PATH value")
        return True, value, int(kind)

    machine = read(
        registry.HKEY_LOCAL_MACHINE,
        r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
    )
    user = read(registry.HKEY_CURRENT_USER, r"Environment")
    return machine, user


def path_hint(directory: str, *, persist: bool) -> str:
    """A copyable line that puts ``directory`` on ``PATH`` in the user's shell.

    ``directory`` is an absolute directory with no trailing separator. POSIX
    keeps the two shapes it has always printed: the short session-only
    ``export`` line, and, when ``persist`` is set, an append to the rc file a
    login shell reads. Windows has no session-only equivalent worth typing --
    PowerShell cannot make ``ciao`` findable for the *next* terminal without
    writing the user PATH -- so both shapes are the same persistent update
    there.

    The Windows line goes through the registry rather than
    ``[Environment]::SetEnvironmentVariable`` on purpose (#854). That setter
    reads the old value through ``GetEnvironmentVariable``, which hands back
    the *expanded* string, and writes the result back as ``REG_SZ``. A user
    PATH stored as ``REG_EXPAND_SZ`` with ``%USERPROFILE%\\...`` entries
    would then be frozen to today's expanded paths and change type under every
    other tool that reads it. ``GetValue(..., 'DoNotExpandEnvironmentNames')``
    plus an explicit ``RegistryValueKind`` keeps both the entries and the kind.
    """
    if sys.platform == "win32":
        # Single-quoted so a ``$`` or a backtick in the path cannot expand;
        # doubling ``'`` is how PowerShell escapes one inside a literal.
        quoted = directory.replace("'", "''")
        return (
            "$k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey("
            f"'Environment', $true); $v = $k.GetValue('Path', '', "
            "'DoNotExpandEnvironmentNames'); "
            f"$k.SetValue('Path', '{quoted}' + "
            "$(if ($v) { ';' + $v }), "
            "[Microsoft.Win32.RegistryValueKind]::ExpandString)"
        )
    if not persist:
        return f'export PATH="{directory}:$PATH"'
    home = str(Path.home())
    target = directory
    if target == home or target.startswith(home + os.sep):
        # Written into an rc file, so keep it portable across machines and
        # readable to whoever opens that file later.
        target = "$HOME" + target[len(home) :]
    # The engine usually runs under launchd, where SHELL is unset; zsh is the
    # macOS default and the shell the documented installer assumes.
    shell = os.path.basename(os.environ.get("SHELL", "") or "zsh")
    if shell == "fish":
        return f"fish_add_path {target}"
    # macOS terminals start login shells, which read ~/.bash_profile — not
    # ~/.bashrc, which would fix only the shell the user is sitting in.
    rc = "~/.bash_profile" if shell == "bash" else "~/.zshrc"
    return f"""echo 'export PATH="{target}:$PATH"' >> {rc} && source {rc}"""


def path_hint_note() -> str:
    """Extra line that belongs under ``path_hint``, or "" when there is none.

    Only Windows needs one: a persistent user-PATH write is invisible to the
    terminal that ran it, so without this the command looks like it did
    nothing.
    """
    if sys.platform == "win32":
        return "Open a new terminal for the change to take effect."
    return ""
