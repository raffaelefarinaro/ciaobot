"""Validation primitives for provider migration preflight."""

import hashlib
import json
import os
import platform
import dataclasses
import queue
import re
import shutil
import signal
import stat
import subprocess
import sys
import sysconfig
import threading
import time
import secrets
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ciao.release_manifest import _VERSION_RE

if TYPE_CHECKING:
    from ciao.engine_update import Operation


def valid_operation_id(operation_id: str, to_version: str | None = None) -> bool:
    """Accept only the staging transaction's ASCII timestamp-version spelling."""
    if not isinstance(operation_id, str) or not operation_id.isascii():
        return False
    match = re.fullmatch(r"[0-9]{8}T[0-9]{6}Z-(.+)", operation_id)
    if match is None:
        return False
    try:
        datetime.strptime(operation_id[:16], "%Y%m%dT%H%M%SZ")
    except ValueError:
        return False
    version = match.group(1)
    return bool(_VERSION_RE.fullmatch(version)) and (
        to_version is None or version == to_version
    )


def _version_is_newer(candidate: str, installed: str) -> bool:
    return tuple(map(int, candidate.split("."))) > tuple(map(int, installed.split(".")))


def _managed_major_eligible(installed: str) -> bool:
    return int(installed.split(".", 1)[0]) < 3


def _claude_version_supported(installed: str) -> bool:
    return tuple(map(int, installed.split("."))) >= (2, 1, 277)


_NATIVE_PACKAGES = frozenset({
    "@opencode/cli-darwin-arm64", "@opencode/cli-darwin-x64-baseline",
    "@opencode/cli-windows-arm64", "@opencode/cli-windows-x64-baseline",
    "@opencode/cli-linux-arm64", "@opencode/cli-linux-arm64-musl",
    "@opencode/cli-linux-x64-baseline", "@opencode/cli-linux-x64-baseline-musl",
})


def _ready_installer_claim(package: str, argv: list[str]) -> bool:
    if package not in _NATIVE_PACKAGES:
        return False
    if package.startswith("@opencode/cli-windows-"):
        return len(argv) == 2 and [Path(p).name.lower() for p in argv] == ["node.exe", "npm-cli.js"]
    return len(argv) == 1 and Path(argv[0]).name == "npm"


def validate_snapshot(snapshot: object) -> dict[str, object]:
    """Reject every deviation from the shared, approval-safe snapshot contract."""
    def fail() -> None:
        raise ValueError("invalid provider snapshot")

    def obj(value: Any, keys: str) -> Any:
        if not isinstance(value, dict) or set(value) != set(keys.split()):
            fail()
        return value

    def text(value: Any, *, nonempty: bool = False) -> None:
        if not isinstance(value, str) or len(value) > 512 or (nonempty and not value):
            fail()

    def path(value: Any) -> None:
        text(value, nonempty=True)
        if not Path(value).is_absolute():
            fail()

    def number(value: Any, *, positive: bool = False) -> None:
        if type(value) is not int or value < int(positive):
            fail()

    def digest(value: Any) -> None:
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
            fail()

    def version(value: Any) -> None:
        if value is not None and (not isinstance(value, str) or not value.isascii() or len(value) > 512 or not _VERSION_RE.fullmatch(value)):
            fail()

    def identity(value: Any) -> None:
        if value is None:
            return
        value = obj(value, "logical_path links target")
        path(value["logical_path"])
        if not isinstance(value["links"], list) or len(value["links"]) > 40:
            fail()
        for link in value["links"]:
            link = obj(link, "path device inode mtime_ns link_text")
            path(link["path"])
            text(link["link_text"], nonempty=True)
            for key in ("device", "inode", "mtime_ns"):
                number(link[key])
        target = obj(value["target"], "path device inode size mtime_ns sha256")
        path(target["path"])
        for key in ("device", "inode", "mtime_ns"):
            number(target[key])
        number(target["size"], positive=True)
        digest(target["sha256"])

    data = obj(snapshot, "schema operation default_providers installer capabilities providers can_apply reason")
    if type(data["schema"]) is not int or data["schema"] != 1:
        fail()
    operation = obj(data["operation"], "id phase to_version wheel_sha256 env_python")
    for key in ("id", "phase", "to_version"):
        text(operation[key], nonempty=True)
    if operation["phase"] != "staged" or not valid_operation_id(operation["id"], operation["to_version"]):
        fail()
    digest(operation["wheel_sha256"])
    path(operation["env_python"])
    defaults = data["default_providers"]
    if defaults is not None and (not isinstance(defaults, list) or any(x not in ("claude", "opencode") for x in defaults) or defaults != sorted(set(defaults))):
        fail()
    installer = obj(data["installer"], "state package npm_argv reason")
    text(installer["package"])
    text(installer["reason"])
    argv = installer["npm_argv"]
    if not isinstance(argv, list) or len(argv) > 2:
        fail()
    for item in argv:
        path(item)
    if installer["state"] not in ("ready", "unavailable", "unknown"):
        fail()
    if installer["state"] == "ready":
        if not _ready_installer_claim(installer["package"], argv):
            fail()
    elif installer["package"] or argv:
        fail()
    caps = obj(data["capabilities"], "managed_resolver operation_schema installed_target candidate_target candidate_running")
    for key in ("managed_resolver", "operation_schema"):
        number(caps[key])
    for key in ("installed_target", "candidate_target", "candidate_running"):
        if caps[key] not in ("accept", "reject", "unknown"):
            fail()
    rows = data["providers"]
    if not isinstance(rows, list) or len(rows) != 2:
        fail()
    for row, name in zip(rows, ("opencode", "claude")):
        row = obj(row, "provider selected state action installed candidate tested identity reason")
        if row["provider"] != name:
            fail()
        selected = None if defaults is None else name in defaults
        if row["selected"] is not selected:
            fail()
        for key in ("installed", "candidate", "tested"):
            version(row[key])
        text(row["reason"])
        identity(row["identity"])
        state, action = row["state"], row["action"]
        if selected is None:
            valid = (state, action) == ("unknown", "manual")
        elif not selected:
            valid = (state, action) == ("not_default", "none")
        elif state in ("compatible", "advisory"):
            valid = action == "none" and row["installed"] is not None and row["identity"] is not None and (state != "advisory" or row["tested"] is not None) and (name != "opencode" or (caps["installed_target"] == "accept" and _managed_major_eligible(row["installed"]))) and (name != "claude" or _claude_version_supported(row["installed"]))
        elif state == "absent":
            valid = action == "none" and row["installed"] is None and row["identity"] is None
        elif state in ("unknown", "blocked"):
            valid = action == "manual"
        elif state == "required":
            valid = (name == "opencode" and action == "stage_opencode" and all(row[k] is not None for k in ("installed", "candidate", "tested", "identity")) and row["candidate"] == row["tested"] and _managed_major_eligible(row["installed"]) and _version_is_newer(row["candidate"], row["installed"]) and installer["state"] == "ready" and caps["managed_resolver"] == 1 and caps["operation_schema"] == 1 and caps["installed_target"] == "reject" and caps["candidate_target"] == caps["candidate_running"] == "accept")
        else:
            valid = False
        if not valid:
            fail()
    if not (rows[0]["selected"] is True and caps["installed_target"] == "reject") and installer != {"state": "unavailable", "package": "", "npm_argv": [], "reason": "not needed"}:
        fail()
    if type(data["can_apply"]) is not bool or data["can_apply"] != (defaults is not None and not any(row["selected"] is True and row["state"] in ("unknown", "blocked") for row in rows)):
        fail()
    text(data["reason"])
    return cast(dict[str, object], data)


def snapshot_id(snapshot: Mapping[str, object]) -> str:
    data = validate_snapshot(snapshot)
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_flags() -> int:
    # Refuse special files without blocking before the descriptor's fstat.
    return os.O_RDONLY | (getattr(os, "O_NONBLOCK", 0) if os.name == "posix" else 0)


def _validate_operation(op: "Operation", state_dir: Path | None) -> dict[str, object]:
    from ciao.engine_update import default_state_dir
    from ciao.os_support.files import open_fd
    from ciao.update_host import current_update_host, default_update_host

    try:
        root = state_dir or default_state_dir()
        stage = Path(op.stage_dir)
        if (any(not isinstance(item, str) or len(item) > 512 for item in (op.id, op.phase, op.to_version, op.wheel_sha256, op.env_python, op.stage_dir, op.wheel))
                or not root.is_absolute() or not stage.is_absolute() or not valid_operation_id(op.id, op.to_version)
                or stage != root / op.to_version or op.phase != "staged"
                or re.fullmatch(r"[0-9a-f]{64}", op.wheel_sha256) is None):
            raise ValueError
        wheel = Path(op.wheel)
        if not wheel.is_absolute() or not wheel.resolve(strict=True).is_relative_to(stage.resolve(strict=True)):
            raise ValueError
        fd = open_fd(wheel, _read_flags(), 0o600, follow_symlinks=False)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            after = os.fstat(handle.fileno())
            if any(getattr(before, key) != getattr(after, key) for key in ("st_dev", "st_ino", "st_size", "st_mtime_ns")):
                raise ValueError
        if digest != op.wheel_sha256:
            raise ValueError
        python = Path(op.env_python)
        host = current_update_host() if sys.platform == "win32" else default_update_host(label="provider-preflight")
        if not python.is_absolute() or python != host.find_staged_python(stage / "tool") or not python.is_file():
            raise ValueError
        parent = stage
        for part in python.relative_to(stage).parts[:-1]:
            if parent.is_symlink() or parent.is_junction():
                raise ValueError
            parent /= part
        if parent.is_symlink() or parent.is_junction():
            raise ValueError
        return {"id": op.id, "phase": op.phase, "to_version": op.to_version, "wheel_sha256": op.wheel_sha256, "env_python": op.env_python}
    except Exception:
        raise ValueError("invalid staged operation") from None


def _walk_links(logical: Path) -> tuple[list[dict[str, object]], Path]:
    if not logical.is_absolute():
        raise ValueError("invalid identity")
    todo = list(logical.parts[1:])
    current = Path(logical.anchor)
    links: list[dict[str, object]] = []
    while todo:
        part = todo.pop(0)
        if part == "..":
            current = current.parent
            continue
        if part == ".":
            continue
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            if len(links) == 40:
                raise ValueError("invalid identity")
            target = os.readlink(current)
            links.append({"path": str(current), "device": info.st_dev, "inode": info.st_ino, "mtime_ns": info.st_mtime_ns, "link_text": target})
            target_path = Path(target)
            if target_path.is_absolute():
                current = Path(target_path.anchor)
                todo = list(target_path.parts[1:]) + todo
            else:
                current = current.parent
                todo = list(target_path.parts) + todo
    return links, current


def _file_identity(logical: Path) -> dict[str, object]:
    from ciao.os_support.files import open_fd

    links, target = _walk_links(logical)
    fd = open_fd(target, _read_flags(), 0o600, follow_symlinks=False)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise ValueError("invalid identity")
        sha = hashlib.file_digest(handle, "sha256").hexdigest()
        after = os.fstat(handle.fileno())
        keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, key) != getattr(after, key) for key in keys):
            raise ValueError("invalid identity")
        if _walk_links(logical) != (links, target):
            raise ValueError("invalid identity")
        current = target.stat()
        if any(getattr(before, key) != getattr(current, key) for key in keys):
            raise ValueError("invalid identity")
    return {"logical_path": str(logical), "links": links, "target": {"path": str(target), "device": before.st_dev, "inode": before.st_ino, "size": before.st_size, "mtime_ns": before.st_mtime_ns, "sha256": sha}}


_WINDOWS_PROBE_BOOTSTRAP = """\
import subprocess, sys
if sys.stdin.buffer.read(1) != b'G':
    raise SystemExit(125)
raise SystemExit(subprocess.call(sys.argv[1:], stdin=subprocess.DEVNULL, shell=False))
"""


def _use_windows_bootstrap() -> bool:
    return sys.platform == "win32"


def _run_bounded(argv: list[str], timeout: float, stdout_cap: int, stderr_cap: int, *, include_stderr: bool = False, cwd: Path | None = None, cwd_fd: int | None = None, env: Mapping[str, str] | None = None, combined_cap: int | None = None) -> bytes:
    from ciao.os_support.processes import ProcessTree, tree_spawn_options

    allowed_env = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    child_env = dict(env) if env is not None else {key: os.environ[key] for key in allowed_env if key in os.environ}
    deadline = time.monotonic() + timeout
    gated = _use_windows_bootstrap()
    command = [sys.executable, "-I", "-S", "-c", _WINDOWS_PROBE_BOOTSTRAP, *argv] if gated else argv
    options = tree_spawn_options()
    if cwd_fd is not None:
        if os.name != "posix" or cwd is not None or not stat.S_ISDIR(os.fstat(cwd_fd).st_mode):
            raise ValueError("invalid probe directory")
        bootstrap = "import os,sys; fd=int(sys.argv[1]); os.fchdir(fd); os.close(fd); os.execvpe(sys.argv[2],sys.argv[2:],os.environ)"
        command = [sys.executable, "-I", "-S", "-c", bootstrap, str(cwd_fd), *argv]
        options["pass_fds"] = (cwd_fd,)
    process = subprocess.Popen(command, stdin=subprocess.PIPE if gated else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env, cwd=cwd, **options)
    try:
        tree = ProcessTree(process.pid)
    except Exception:
        if process.stdin is not None:
            process.stdin.close()
        if sys.platform != "win32":
            try:
                getattr(os, "killpg")(process.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        raise ValueError("probe failed") from None
    messages: queue.Queue[tuple[int, bytes | None]] = queue.Queue(maxsize=4)
    stopped = threading.Event()

    def send(index: int, data: bytes | None) -> None:
        while not stopped.is_set():
            try:
                messages.put((index, data), timeout=0.1)
                return
            except queue.Full:
                pass

    def read_stream(index: int, stream: Any) -> None:
        try:
            while not stopped.is_set():
                data = stream.read1(4096)
                send(index, data)
                if not data:
                    return
        except OSError:
            send(index, None)
        finally:
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    for i, stream in ((0, process.stdout), (1, process.stderr)):
        threading.Thread(target=read_stream, args=(i, stream), daemon=True).start()
    outputs = [bytearray(), bytearray()]
    finished: set[int] = set()
    try:
        if gated:
            if time.monotonic() >= deadline or process.stdin is None:
                raise ValueError("probe failed")
            try:
                process.stdin.write(b"G")
                process.stdin.close()
            except OSError:
                raise ValueError("probe failed") from None
        while len(finished) < 2:
            index, data = messages.get(timeout=max(0, deadline - time.monotonic()))
            if data is None:
                raise ValueError("probe failed")
            if not data:
                finished.add(index)
                continue
            outputs[index].extend(data)
            if (len(outputs[index]) > (stdout_cap if index == 0 else stderr_cap)
                    or (combined_cap is not None and sum(map(len, outputs)) > combined_cap)):
                raise ValueError("probe failed")
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0 and not include_stderr:
            raise ValueError("probe failed")
        return bytes(outputs[0] + outputs[1] if include_stderr else outputs[0])
    except (queue.Empty, subprocess.TimeoutExpired):
        raise ValueError("probe failed") from None
    finally:
        stopped.set()
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
        try:
            tree.kill()
        except OSError:
            if process.poll() is None:
                process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
        tree.close()


def installer_capability(*, state_dir: Path | None = None) -> dict[str, object]:
    """Discover the platform package and npm command without modifying either."""
    from ciao.tool_path import resolve_command, resolve_tool

    def unavailable(state: str, reason: str) -> dict[str, object]:
        return {"state": state, "package": "", "npm_argv": [], "reason": reason}

    try:
        system = platform.system().lower()
        machine = sysconfig.get_platform().removeprefix("win-").lower() if system == "windows" else platform.machine().lower()
    except Exception:
        return unavailable("unknown", "unsupported platform")
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(machine)
    if arch is None or system not in ("darwin", "windows", "linux"):
        return unavailable("unknown", "unsupported platform")
    suffix = "-baseline" if arch == "x64" else ""
    if system == "linux":
        try:
            libc = platform.libc_ver()[0].lower()
        except Exception:
            return unavailable("unknown", "unknown libc")
        if not libc:
            try:
                gnu = getattr(os, "confstr")("CS_GNU_LIBC_VERSION")
            except (OSError, ValueError, AttributeError):
                gnu = None
            if gnu and gnu.startswith("glibc ") and gnu[6:].strip():
                libc = "glibc"
            else:
                try:
                    ldd = resolve_tool("ldd")
                    output = _run_bounded([ldd, "--version"], 3, 4096, 4096, include_stderr=True) if ldd and Path(ldd).is_absolute() else b""
                    if b"musl" in output.lower():
                        libc = "musl"
                except (OSError, ValueError):
                    pass
        if libc not in ("glibc", "musl"):
            return unavailable("unknown", "unknown libc")
        if libc == "musl":
            suffix += "-musl"
    package = f"@opencode/cli-{system}-{arch}{suffix}"
    try:
        argv = resolve_command("npm")
        if not argv:
            return unavailable("unavailable", "npm not found")
        if (any(not isinstance(p, str) or len(p) > 512 or not Path(p).is_absolute() or not Path(p).is_file() for p in argv)
                or not _ready_installer_claim(package, argv)):
            return unavailable("unknown", "npm command unavailable")
        if system != "windows":
            if len(argv) != 1 or not os.access(argv[0], os.X_OK):
                return unavailable("unknown", "npm command unavailable")
            with open(argv[0], "rb") as handle:
                first_line = handle.readline(512).lower()
            if first_line.startswith(b"#!") and re.search(rb"(?:^|[/\s])(ba|da|z|k)?sh(?:\s|$)", first_line):
                return unavailable("unknown", "npm command unavailable")
        return {"state": "ready", "package": package, "npm_argv": argv, "reason": ""}
    except Exception:
        return unavailable("unknown", "npm command unavailable")


_OPERATION_FIELDS = ("provider_error", "provider_snapshot_id", "provider_snapshot_json", "provider_state", "provider_version")
_TARGET_SCRIPT = r'''
import dataclasses, json, sys
import ciao, ciao.provider_versions as versions
import ciao.providers.opencode as opencode
import ciao.providers.claude as claude
import ciao.engine_update as update
record = versions.load_record()
candidate = record["providers"]["opencode"].get("version", "")
wanted = {"provider_error", "provider_snapshot_id", "provider_snapshot_json", "provider_state", "provider_version"}
fields = sorted(f.name for f in dataclasses.fields(update.Operation) if f.name in wanted)
print(json.dumps({"engine_version": ciao.__version__, "record": record,
 "installed_error": opencode._server_version_error({"version": sys.argv[1]}),
 "candidate_error": opencode._server_version_error({"version": candidate}),
 "bundled_claude": claude.get_bundled_claude_path(),
 "managed_resolver_version": getattr(opencode, "MANAGED_OPENCODE_RESOLVER_VERSION", 0),
 "operation_schema_version": getattr(update, "PROVIDER_OPERATION_SCHEMA_VERSION", 0),
 "operation_schema_fields": fields}))
'''


def _target_probe(python: str, version: str, installed: str) -> dict[str, Any]:
    from ciao.provider_versions import validate_record

    try:
        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result or len(key) > 512:
                    raise ValueError
                result[key] = value
            return result

        def strings_bounded(value: Any) -> bool:
            if isinstance(value, str):
                return len(value) <= 512
            if isinstance(value, dict):
                return all(strings_bounded(v) for v in value.values())
            if isinstance(value, list):
                return all(strings_bounded(v) for v in value)
            return True

        data = json.loads(_run_bounded([python, "-I", "-c", _TARGET_SCRIPT, installed], 15, 16384, 4096), object_pairs_hook=pairs)
        keys = {"engine_version", "record", "installed_error", "candidate_error", "bundled_claude", "managed_resolver_version", "operation_schema_version", "operation_schema_fields"}
        if not isinstance(data, dict) or set(data) != keys or data["engine_version"] != version or not strings_bounded(data):
            raise ValueError
        record = validate_record(data["record"])
        entry = record["providers"]["opencode"]
        if record["ciaobot"] != version or entry.get("status") != "tested" or entry.get("package") != "@opencode/cli" or not entry["version"].isascii():
            raise ValueError
        for key in ("installed_error", "candidate_error", "bundled_claude"):
            item = data[key]
            if item is not None and (not isinstance(item, str) or len(item) > 512):
                raise ValueError
        if data["bundled_claude"] is not None and not Path(data["bundled_claude"]).is_absolute():
            raise ValueError
        for key in ("managed_resolver_version", "operation_schema_version"):
            if type(data[key]) is not int or data[key] < 0:
                raise ValueError
        fields = data["operation_schema_fields"]
        if not isinstance(fields, list) or any(not isinstance(x, str) or x not in _OPERATION_FIELDS for x in fields) or fields != sorted(set(fields)):
            raise ValueError
        return data
    except Exception:
        raise ValueError("target probe failed") from None


def _cli_version(path: str) -> tuple[str, dict[str, object]]:
    from ciao.provider_versions import parse_cli_version

    logical = Path(path)
    identity = _file_identity(logical)
    if _file_identity(logical) != identity:
        raise ValueError("provider changed")
    output = _run_bounded([path, "--version"], 3, 4096, 4096).decode("utf-8")
    version = parse_cli_version(output)
    if version is None or not version.isascii() or not _VERSION_RE.fullmatch(version):
        raise ValueError("provider version unavailable")
    return version, identity


def provider_snapshot(op: "Operation", default_providers: frozenset[str] | None, *, state_dir: Path | None = None) -> dict[str, object]:
    """Observe provider compatibility for a staged engine without changing it."""
    from ciao.providers.opencode import _server_version_error
    from ciao.tool_path import resolve_tool

    operation = _validate_operation(op, state_dir)
    if default_providers is not None and (not isinstance(default_providers, frozenset) or not default_providers <= {"opencode", "claude"}):
        raise ValueError("invalid provider selection")
    defaults = sorted(default_providers) if default_providers is not None else None
    rows: list[dict[str, Any]] = []
    for name in ("opencode", "claude"):
        selected = None if defaults is None else name in defaults
        rows.append({"provider": name, "selected": selected, "state": "unknown" if selected is None or selected else "not_default", "action": "manual" if selected is None or selected else "none", "installed": None, "candidate": None, "tested": None, "identity": None, "reason": "Existing chats using a non-default provider may still need attention." if selected is False else "Provider compatibility is unknown."})
    installer: dict[str, object] = {"state": "unavailable", "package": "", "npm_argv": [], "reason": "not needed"}
    caps: dict[str, object] = {"managed_resolver": 0, "operation_schema": 0, "installed_target": "unknown", "candidate_target": "unknown", "candidate_running": "unknown"}
    explicit = os.environ.get("CIAO_OPENCODE_BIN", "").strip() if rows[0]["selected"] else ""
    if rows[0]["selected"]:
        try:
            binary = os.path.abspath(os.path.expanduser(explicit)) if explicit and Path(explicit).expanduser().is_file() else None if explicit else resolve_tool("opencode")
            if not binary:
                rows[0].update(state="blocked" if explicit else "absent", action="manual" if explicit else "none", reason="Configured OpenCode is missing." if explicit else "OpenCode is not installed.")
            else:
                installed, identity = _cli_version(binary)
                rows[0].update(installed=installed, identity=identity)
        except Exception:
            pass
    target: dict[str, Any] | None = None
    if defaults:
        try:
            target = _target_probe(op.env_python, op.to_version, rows[0]["installed"] or "")
        except ValueError:
            pass
    if _validate_operation(op, state_dir) != operation:
        raise ValueError("invalid staged operation")
    if defaults and target is None and rows[0]["state"] == "absent":
        rows[0].update(state="unknown", action="manual", reason="Target compatibility is unknown.")
    if target is not None:
        entry = target["record"]["providers"]["opencode"]
        candidate = entry["version"]
        caps.update(managed_resolver=target["managed_resolver_version"], operation_schema=1 if target["operation_schema_version"] == 1 and target["operation_schema_fields"] == list(_OPERATION_FIELDS) else 0, installed_target="accept" if target["installed_error"] is None else "reject", candidate_target="accept" if target["candidate_error"] is None else "reject")
        try:
            check = _server_version_error({"version": candidate})
            caps["candidate_running"] = "accept" if check is None else "reject" if isinstance(check, str) else "unknown"
        except Exception:
            pass
        op_row = rows[0]
        if op_row["selected"] and op_row["installed"] is not None:
            op_row.update(candidate=candidate, tested=candidate)
            if not _managed_major_eligible(op_row["installed"]):
                op_row.update(state="blocked", action="manual", reason="OpenCode requires a manual major-version transition.")
            elif caps["installed_target"] == "accept":
                older = tuple(map(int, op_row["installed"].split("."))) < tuple(map(int, candidate.split(".")))
                op_row.update(state="advisory" if older else "compatible", action="none", reason="An update is available." if older else "OpenCode is compatible.")
            else:
                eligible_major = _managed_major_eligible(op_row["installed"])
                upgrade = _version_is_newer(candidate, op_row["installed"])
                if not explicit and eligible_major and upgrade:
                    installer = installer_capability(state_dir=state_dir)
                can_stage = (not explicit and eligible_major and upgrade and caps["managed_resolver"] == 1 and caps["operation_schema"] == 1 and caps["candidate_target"] == caps["candidate_running"] == "accept" and installer["state"] == "ready")
                op_row.update(state="required" if can_stage else "blocked", action="stage_opencode" if can_stage else "manual", reason="OpenCode requires a compatible version.")
        claude = rows[1]
        if claude["selected"]:
            try:
                binary = target["bundled_claude"] or resolve_tool("claude")
                if not binary:
                    claude.update(state="absent", action="none", reason="Claude is not installed.")
                else:
                    installed, identity = _cli_version(binary)
                    entry = target["record"]["providers"]["claude"]
                    tested = entry.get("version") if entry.get("status") == "tested" else None
                    claude.update(installed=installed, identity=identity, tested=tested)
                    if not _claude_version_supported(installed):
                        claude.update(state="blocked", action="manual", reason="Claude is too old.")
                    elif tested and tuple(map(int, installed.split("."))) < tuple(map(int, tested.split("."))):
                        claude.update(state="advisory", action="none", reason="An update is available.")
                    else:
                        claude.update(state="compatible", action="none", reason="Claude is compatible.")
            except Exception:
                pass
    if _validate_operation(op, state_dir) != operation:
        raise ValueError("invalid staged operation")
    can_apply = defaults is not None and not any(row["selected"] and row["state"] in ("unknown", "blocked") for row in rows)
    snapshot: dict[str, object] = {"schema": 1, "operation": operation, "default_providers": defaults, "installer": installer, "capabilities": caps, "providers": rows, "can_apply": can_apply, "reason": "" if can_apply else "Provider compatibility needs attention."}
    return validate_snapshot(snapshot)


def _candidate_paths(op: "Operation", state_dir: Path | None, *, require_candidate: bool = True) -> tuple[Path, Path]:
    from ciao.engine_update import default_state_dir
    from ciao.os_support.private import is_private_strict

    root = state_dir if state_dir is not None else default_state_dir()
    stage = Path(op.stage_dir)
    if (not root.is_absolute() or not stage.is_absolute()
            or ".." in root.parts or ".." in stage.parts
            or not valid_operation_id(op.id, op.to_version)
            or stage != root / op.to_version):
        raise ValueError("invalid candidate operation")
    # Ancestors outside the private state root may be shared, but must not
    # redirect traversal. The state root and its descendants must be private.
    for path in reversed(root.parents):
        if path.is_symlink() or path.is_junction():
            raise ValueError("invalid candidate path")
    for path in (root, stage, *((stage / "provider-candidate",) if require_candidate else ())):
        if (path.is_symlink() or path.is_junction() or not path.is_dir()
                or not is_private_strict(path)):
            raise ValueError("invalid candidate path")
    return root, stage / "provider-candidate"


def _candidate_directory_fd(path: Path) -> int:
    """Open every POSIX path component without following links."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("unsafe candidate path")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_candidate_file(path: Path, flags: int, mode: int = 0o600) -> int:
    from ciao.os_support.files import open_fd
    from ciao.os_support.private import open_private

    if sys.platform == "win32":
        if any(p.is_symlink() or p.is_junction() for p in path.parents):
            raise ValueError("unsafe candidate path")
        if flags & os.O_CREAT:
            return open_private(path, flags, mode, follow_symlinks=False)
        return open_fd(path, flags, follow_symlinks=False)
    parent = _candidate_directory_fd(path.parent)
    try:
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=parent)
    finally:
        os.close(parent)


def _make_candidate_directory(path: Path) -> os.stat_result:
    from ciao.os_support.private import make_private_dir, is_private_strict

    if sys.platform == "win32":
        path.mkdir(mode=0o700)
        make_private_dir(path)
    else:
        parent = _candidate_directory_fd(path.parent)
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent)
        finally:
            os.close(parent)
    if not is_private_strict(path) or path.is_symlink() or path.is_junction():
        raise ValueError("invalid candidate directory")
    return path.lstat()


def _secure_windows_work(work: Path, expected: os.stat_result, validate_only: bool = False) -> None:
    """Protect this run's inherited owner-only output, after validating it all."""
    from ciao.os_support.private import (
        is_owned_path, is_owned_descriptor, is_private, is_private_descriptor,
        is_private_strict, make_private, make_private_dir,
    )

    entries: list[tuple[Path, os.stat_result]] = []

    def matches(path: Path, original: os.stat_result) -> bool:
        info = path.lstat()
        return (not path.is_symlink() and not path.is_junction()
                and (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
                == (original.st_dev, original.st_ino, stat.S_IFMT(original.st_mode)))

    def validate(path: Path) -> None:
        info = path.lstat()
        if path.is_symlink() or path.is_junction():
            raise ValueError("candidate work requires manual recovery")
        if stat.S_ISDIR(info.st_mode):
            if not is_owned_path(path) or not is_private(path):
                raise ValueError("candidate work requires manual recovery")
            entries.append((path, info))
            for child in path.iterdir():
                validate(child)
        elif stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ValueError("candidate work requires manual recovery")
            fd = _open_candidate_file(path, _read_flags())
            try:
                opened = os.fstat(fd)
                if ((opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                        or opened.st_nlink != 1
                        or not is_owned_descriptor(fd) or not is_private_descriptor(fd)):
                    raise ValueError("candidate work requires manual recovery")
            finally:
                os.close(fd)
            entries.append((path, info))
        else:
            raise ValueError("candidate work requires manual recovery")
        if not matches(path, info):
            raise ValueError("candidate work requires manual recovery")

    if not matches(work, expected) or not is_private_strict(work):
        raise ValueError("candidate work requires manual recovery")
    for child in work.iterdir():
        validate(child)
    if validate_only:
        return
    identities = dict(entries)
    for path, info in entries:
        if (not matches(work, expected) or not matches(path, info)
                or any(not matches(parent, identities[parent]) for parent in path.parents
                       if parent in identities)):
            raise ValueError("candidate work requires manual recovery")
        (make_private_dir if stat.S_ISDIR(info.st_mode) else make_private)(path)


def _remove_candidate_work(path: Path, expected: os.stat_result, validate_only: bool = False) -> None:
    """Remove only this invocation's tree, never traverse a substituted link."""
    from ciao.os_support.private import (
        is_owned_descriptor, descriptor_acl_grants_another_account, is_private_strict,
        is_private_descriptor_strict,
    )

    def matches(info: os.stat_result, other: os.stat_result) -> bool:
        return (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)

    if sys.platform == "win32":
        if path.is_symlink() or path.is_junction() or not matches(path.lstat(), expected):
            raise ValueError("candidate work requires manual recovery")
        def validate_windows(entry: Path) -> None:
            info = entry.lstat()
            if entry.is_symlink() or entry.is_junction():
                raise ValueError("candidate work requires manual recovery")
            if stat.S_ISDIR(info.st_mode):
                if not is_private_strict(entry):
                    raise ValueError("candidate work requires manual recovery")
                for child in entry.iterdir():
                    validate_windows(child)
            elif stat.S_ISREG(info.st_mode):
                fd = _open_candidate_file(entry, _read_flags())
                try:
                    opened = os.fstat(fd)
                    if (not is_private_descriptor_strict(fd)
                            or not stat.S_ISREG(opened.st_mode)
                            or not matches(opened, info) or opened.st_nlink != 1):
                        raise ValueError("candidate work requires manual recovery")
                finally:
                    os.close(fd)
            else:
                raise ValueError("candidate work requires manual recovery")
        validate_windows(path)
        # All entries were validated before the first deletion.
        if not validate_only:
            shutil.rmtree(path)
        return
    parent = _candidate_directory_fd(path.parent)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW

    def validate(fd: int) -> None:
        if (os.fstat(fd).st_mode & 0o022 or not is_owned_descriptor(fd)
                or descriptor_acl_grants_another_account(fd)):
            raise ValueError("candidate work requires manual recovery")
        for entry in os.listdir(fd):
            info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
                raise ValueError("candidate work requires manual recovery")
            if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                raise ValueError("candidate work requires manual recovery")
            child = os.open(entry, flags if stat.S_ISDIR(info.st_mode) else _read_flags() | os.O_NOFOLLOW, dir_fd=fd)
            try:
                opened = os.fstat(child)
                if (not matches(opened, info)
                        or (stat.S_ISREG(opened.st_mode) and opened.st_nlink != 1)
                        or opened.st_mode & 0o022
                        or not is_owned_descriptor(child)
                        or descriptor_acl_grants_another_account(child)):
                    raise ValueError("candidate work requires manual recovery")
                if stat.S_ISDIR(info.st_mode):
                    validate(child)
            finally:
                os.close(child)

    def remove(directory: int, name: str, identity: os.stat_result, *, prevalidate: bool = False) -> None:
        fd = os.open(name, flags, dir_fd=directory)
        try:
            if (not matches(os.fstat(fd), identity) or not is_owned_descriptor(fd)
                    or descriptor_acl_grants_another_account(fd)):
                raise ValueError("candidate work requires manual recovery")
            if prevalidate:
                validate(fd)
                if validate_only:
                    return
            for entry in os.listdir(fd):
                info = os.stat(entry, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    remove(fd, entry, info)
                else:
                    os.unlink(entry, dir_fd=fd)
            if not matches(os.stat(name, dir_fd=directory, follow_symlinks=False), os.fstat(fd)):
                raise ValueError("candidate work requires manual recovery")
            os.rmdir(name, dir_fd=directory)
        finally:
            os.close(fd)
    try:
        remove(parent, path.name, expected, prevalidate=True)
    finally:
        os.close(parent)


def _validate_package_directory(path: Path) -> None:
    from ciao.os_support.private import (
        descriptor_acl_grants_another_account, is_owned_descriptor, is_private_strict,
    )
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise ValueError("unsafe package path")
    if sys.platform == "win32":
        if not is_private_strict(path):
            raise ValueError("unsafe package directory")
        return
    fd = _candidate_directory_fd(path)
    try:
        if (not is_owned_descriptor(fd) or descriptor_acl_grants_another_account(fd)
                or os.fstat(fd).st_mode & 0o022):
            raise ValueError("unsafe package directory")
    finally:
        os.close(fd)


def _publish_candidate(source: Path, destination: Path) -> None:
    if sys.platform == "win32":
        os.rename(source, destination)
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if source.parent != destination.parent:
        raise ValueError("invalid candidate publication")
    parent = _candidate_directory_fd(source.parent)
    src, dst = os.fsencode(source.name), os.fsencode(destination.name)
    result: int
    try:
        if sys.platform == "darwin":
            result = libc.renameatx_np(parent, src, parent, dst, 4)  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            result = libc.renameat2(parent, src, parent, dst, 1)  # RENAME_NOREPLACE
        else:
            raise ValueError("atomic candidate publication unavailable")
    finally:
        os.close(parent)
    if result:
        raise OSError(ctypes.get_errno(), "candidate publication failed")


def stage_opencode_candidate(op: "Operation", approved_snapshot_id: str,
                             default_providers: frozenset[str] | None, *,
                             state_dir: Path | None = None) -> dict[str, str]:
    """Stage the approved native provider without changing the active provider."""
    from ciao.engine_update import default_state_dir
    from ciao.os_support.private import (descriptor_acl_grants_another_account,
                                         is_owned_descriptor, is_private_strict,
                                         is_private_descriptor_strict)

    root = state_dir if state_dir is not None else default_state_dir()
    stage = Path(op.stage_dir)
    if (not root.is_absolute() or not stage.is_absolute() or ".." in root.parts
            or ".." in stage.parts or not valid_operation_id(op.id, op.to_version)
            or stage != root / op.to_version):
        raise ValueError("invalid candidate operation")
    staged = dataclasses.replace(op, phase="staged")
    snapshot = provider_snapshot(staged, default_providers, state_dir=root)
    if snapshot_id(snapshot) != approved_snapshot_id or dataclasses.replace(op, phase="staged") != staged:
        raise ValueError("stale provider approval")
    rows = cast(list[dict[str, Any]], snapshot["providers"])
    selected = [row for row in rows if row["state"] == "required" and row["action"] == "stage_opencode" and row["selected"] is True]
    if not snapshot["can_apply"] or len(selected) != 1:
        raise ValueError("provider staging not approved")
    installer = cast(dict[str, Any], snapshot["installer"])
    if installer_capability(state_dir=root) != installer:
        raise ValueError("installer changed")
    version = cast(str, selected[0]["candidate"])
    package = cast(str, installer["package"])
    argv = cast(list[str], installer["npm_argv"])
    _, candidate = _candidate_paths(op, root, require_candidate=False)
    work = stage / "provider-candidate-work"
    if work.exists() or work.is_symlink() or any(stage.glob("provider-candidate-tmp-*")):
        raise ValueError("candidate work requires manual recovery")
    if candidate.exists() or candidate.is_symlink():
        fd = _candidate_open(candidate / "candidate.json", 0o600)
        with os.fdopen(fd, "rb") as handle:
            data = json.loads(handle.read(65537))
        sha = data["sha256"]
        verify_candidate(op, sha, state_dir=root)
        if data["version"] != version or data["package"] != package:
            raise ValueError("candidate does not match approval")
        return {"version": version, "sha256": sha, "relative_binary": data["relative_binary"]}
    work_identity = _make_candidate_directory(work)
    temp: Path | None = None
    temp_identity: os.stat_result | None = None
    cleanup_attempted = False
    def same_directory(path: Path, original: os.stat_result) -> bool:
        try:
            current = path.lstat()
            return stat.S_ISDIR(current.st_mode) and (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)
        except OSError:
            return False
    try:
        if not is_private_strict(work):
            raise ValueError("invalid work directory")
        prefix, cache = work / "prefix", work / "cache"
        config = work / "user.npmrc"
        global_config = work / "global.npmrc"
        for path in (config, global_config):
            fd = _open_candidate_file(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            os.close(fd)
        env = {"PATH": str(Path(argv[0]).parent) + os.pathsep + os.defpath,
               "HOME": str(work), "USERPROFILE": str(work),
               "TEMP": str(work), "TMP": str(work),
               "NPM_CONFIG_USERCONFIG": str(config), "NPM_CONFIG_GLOBALCONFIG": str(global_config),
               "NPM_CONFIG_UPDATE_NOTIFIER": "false"}
        for key in ("SYSTEMROOT", "WINDIR"):
            if key in os.environ:
                env[key] = os.environ[key]
        command = [*argv, "install", "--prefix", str(prefix), "--cache", str(cache),
                   "--ignore-scripts", "--no-save", "--no-audit", "--no-fund",
                   "--registry=https://registry.npmjs.org/", f"{package}@{version}"]
        _run_bounded(command, 600, 16384, 16384, cwd=work, env=env, combined_cap=16384)
        if not same_directory(work, work_identity):
            raise ValueError("candidate work changed")
        if sys.platform == "win32":
            _secure_windows_work(work, work_identity)
        else:
            _remove_candidate_work(work, work_identity, True)
        base = prefix / "node_modules" / "@opencode" / package.split("/", 1)[1]
        package_dirs = (prefix, prefix / "node_modules", prefix / "node_modules" / "@opencode", base, base / "bin")
        dir_identities = []
        for path in package_dirs:
            _validate_package_directory(path)
            dir_identities.append(path.lstat())
        fd = _open_candidate_file(base / "package.json", _read_flags())
        with os.fdopen(fd, "rb") as handle:
            if (not stat.S_ISREG(os.fstat(handle.fileno()).st_mode)
                    or not is_owned_descriptor(handle.fileno())
                    or (sys.platform == "win32" and not is_private_descriptor_strict(handle.fileno()))
                    or descriptor_acl_grants_another_account(handle.fileno())):
                raise ValueError("invalid package metadata")
            raw = handle.read(65537)
        if len(raw) > 65536:
            raise ValueError("invalid package metadata")
        def unique_package_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate package metadata key")
                result[key] = value
            return result
        metadata = json.loads(raw, object_pairs_hook=unique_package_keys)
        parts = package.split("/")[1].split("-")
        package_os = "win32" if parts[1] == "windows" else parts[1]
        if (not isinstance(metadata, dict) or metadata.get("name") != package
                or metadata.get("version") != version or metadata.get("os") != [package_os]
                or metadata.get("cpu") != [parts[2]] or metadata.get("scripts", {}) != {}):
            raise ValueError("invalid package metadata")
        name = "opencode.exe" if sys.platform == "win32" else "opencode"
        fd = _open_candidate_file(base / "bin" / name, _read_flags())
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            magic = source.read(4)
            expected_magic = {"darwin": (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe"), "linux": (b"\x7fELF",), "windows": ()}[parts[1]]
            valid_magic = magic in expected_magic or (parts[1] == "windows" and magic.startswith(b"MZ"))
            if (not stat.S_ISREG(info.st_mode) or not is_owned_descriptor(source.fileno())
                    or (sys.platform == "win32" and not is_private_descriptor_strict(source.fileno()))
                    or descriptor_acl_grants_another_account(source.fileno())
                    or (sys.platform != "win32" and not info.st_mode & stat.S_IXUSR)
                    or info.st_size < 256 or not valid_magic):
                raise ValueError("invalid native executable")
            source.seek(0)
            temp = stage / ("provider-candidate-tmp-" + secrets.token_hex(16))
            temp_identity = _make_candidate_directory(temp)
            out = _open_candidate_file(temp / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(out, "wb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            after = os.fstat(source.fileno())
            if (any(getattr(info, k) != getattr(after, k) for k in ("st_dev", "st_ino", "st_size", "st_mtime_ns"))
                    or any(not same_directory(path, identity) for path, identity in zip(package_dirs, dir_identities))):
                raise ValueError("package changed while copying")
        with os.fdopen(_open_candidate_file(temp / name, _read_flags()), "rb") as handle:
            sha = hashlib.file_digest(handle, "sha256").hexdigest()
        descriptor = {"schema": 1, "operation_id": op.id, "package": package, "version": version, "sha256": sha, "relative_binary": name}
        fd = _open_candidate_file(temp / "candidate.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "wb") as descriptor_file:
            descriptor_file.write(json.dumps(descriptor).encode())
            descriptor_file.flush()
            os.fsync(descriptor_file.fileno())
        if sys.platform != "win32":
            dfd = _candidate_directory_fd(temp)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        # Verification must precede publication; the verifier accepts the private temp path.
        _verify_candidate_at(op, sha, root, temp)
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("candidate already exists")
        _publish_candidate(temp, candidate)
        temp = None
        temp_identity = None
        if sys.platform != "win32":
            dfd = _candidate_directory_fd(stage)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        try:
            cleanup_attempted = True
            _remove_candidate_work(work, work_identity)
        except (OSError, ValueError):
            raise ValueError("candidate work requires manual recovery") from None
        return {"version": version, "sha256": sha, "relative_binary": name}
    except Exception:
        if not cleanup_attempted:
            try:
                if temp is not None and temp_identity is None:
                    raise ValueError("candidate work requires manual recovery")
                if sys.platform == "win32":
                    _secure_windows_work(work, work_identity, True)
                    if temp is not None and temp_identity is not None:
                        _remove_candidate_work(temp, temp_identity, True)
                    _secure_windows_work(work, work_identity)
                trees = [(work, work_identity)]
                if temp is not None and temp_identity is not None:
                    trees.append((temp, temp_identity))
                for path, identity in trees:
                    _remove_candidate_work(path, identity, True)
                for path, identity in trees:
                    _remove_candidate_work(path, identity)
            except (OSError, ValueError):
                raise ValueError("candidate work requires manual recovery") from None
        raise


def _candidate_open(path: Path, mode: int) -> int:
    from ciao.os_support.private import (
        descriptor_acl_grants_another_account, is_owned_descriptor,
        is_private_descriptor_strict,
    )

    fd = _open_candidate_file(path, _read_flags())
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or not is_owned_descriptor(fd)
                or (sys.platform == "win32" and not is_private_descriptor_strict(fd))
                or (sys.platform != "win32" and stat.S_IMODE(info.st_mode) != mode)
                or descriptor_acl_grants_another_account(fd)):
            raise ValueError("invalid candidate file")
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_candidate(op: "Operation", expected_sha256: str, *, state_dir: Path | None = None) -> Path:
    """Verify a published private candidate without modifying state."""
    return _verify_candidate_at(op, expected_sha256, state_dir, None)


def _verify_candidate_at(op: "Operation", expected_sha256: str, state_dir: Path | None, temporary: Path | None) -> Path:
    from ciao.providers.opencode import _server_version_error
    from ciao.os_support.private import is_private_strict

    try:
        root, published = _candidate_paths(op, state_dir, require_candidate=temporary is None)
        candidate = temporary if temporary is not None else published
        if temporary is not None and (temporary.parent != published.parent or temporary.is_symlink() or temporary.is_junction() or not is_private_strict(temporary)):
            raise ValueError
        staged = dataclasses.replace(op, phase="staged")
        _validate_operation(staged, root)
        target_identity = _file_identity(Path(staged.env_python))
        if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise ValueError
        fd = _candidate_open(candidate / "candidate.json", 0o600)
        with os.fdopen(fd, "rb") as handle:
            metadata_info = os.fstat(handle.fileno())
            raw = handle.read(65537)
            metadata_after = os.fstat(handle.fileno())
        metadata_keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(metadata_info, k) != getattr(metadata_after, k) for k in metadata_keys):
            raise ValueError
        if len(raw) > 65536:
            raise ValueError
        def unique(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result
        data = json.loads(raw, object_pairs_hook=unique)
        binary_name = "opencode.exe" if sys.platform == "win32" else "opencode"
        if (not isinstance(data, dict) or set(data) != {"schema", "operation_id", "package", "version", "sha256", "relative_binary"}
                or type(data["schema"]) is not int or data["schema"] != 1
                or data["operation_id"] != op.id or data["relative_binary"] != binary_name
                or data["sha256"] != expected_sha256
                or not isinstance(data["version"], str) or not _VERSION_RE.fullmatch(data["version"])
                or not _managed_major_eligible(data["version"])):
            raise ValueError
        capability = installer_capability(state_dir=root)
        if capability["state"] != "ready" or data["package"] != capability["package"]:
            raise ValueError
        target = _target_probe(op.env_python, op.to_version, data["version"])
        if (target["record"]["providers"]["opencode"]["version"] != data["version"]
                or target["managed_resolver_version"] != 1
                or target["installed_error"] is not None or target["candidate_error"] is not None
                or _server_version_error({"version": data["version"]}) is not None):
            raise ValueError
        binary = candidate / binary_name
        fd = _candidate_open(binary, 0o700)
        with os.fdopen(fd, "rb") as handle:
            before = os.fstat(handle.fileno())
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
            after = os.fstat(handle.fileno())
            keys = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
            if digest != expected_sha256 or any(getattr(before, k) != getattr(after, k) for k in keys):
                raise ValueError
            output = _run_bounded([str(binary), "--version"], 3, 4096, 4096)
            current = binary.lstat()
            if (output.strip() != data["version"].encode("ascii")
                    or any(getattr(before, k) != getattr(os.fstat(handle.fileno()), k) or getattr(before, k) != getattr(current, k) for k in keys)):
                raise ValueError
        _candidate_paths(op, root, require_candidate=temporary is None)
        if dataclasses.replace(op, phase="staged") != staged or _file_identity(Path(staged.env_python)) != target_identity:
            raise ValueError
        _validate_operation(staged, root)
        fd = _candidate_open(candidate / "candidate.json", 0o600)
        with os.fdopen(fd, "rb") as handle:
            current_metadata = os.fstat(handle.fileno())
            if (any(getattr(metadata_info, k) != getattr(current_metadata, k) for k in metadata_keys)
                    or handle.read(65537) != raw):
                raise ValueError
        return binary
    except Exception:
        raise ValueError("candidate verification failed") from None
