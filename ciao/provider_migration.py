"""Validation primitives for provider migration preflight."""

import hashlib
import json
import os
import platform
import queue
import re
import signal
import stat
import subprocess
import sys
import sysconfig
import threading
import time
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


def _run_bounded(argv: list[str], timeout: float, stdout_cap: int, stderr_cap: int, *, include_stderr: bool = False) -> bytes:
    from ciao.os_support.processes import ProcessTree, tree_spawn_options

    allowed_env = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in allowed_env if key in os.environ}
    deadline = time.monotonic() + timeout
    gated = _use_windows_bootstrap()
    command = [sys.executable, "-I", "-S", "-c", _WINDOWS_PROBE_BOOTSTRAP, *argv] if gated else argv
    process = subprocess.Popen(command, stdin=subprocess.PIPE if gated else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, **tree_spawn_options())
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

    def read_stream(index: int, stream: Any, cap: int) -> None:
        try:
            data = stream.read(cap + 1)
            messages.put((index, data))
        except OSError:
            messages.put((index, None))
        finally:
            stream.close()

    assert process.stdout is not None and process.stderr is not None
    for i, stream, cap in ((0, process.stdout, stdout_cap), (1, process.stderr, stderr_cap)):
        threading.Thread(target=read_stream, args=(i, stream, cap), daemon=True).start()
    outputs: dict[int, bytes] = {}
    try:
        if gated:
            if time.monotonic() >= deadline or process.stdin is None:
                raise ValueError("probe failed")
            try:
                process.stdin.write(b"G")
                process.stdin.close()
            except OSError:
                raise ValueError("probe failed") from None
        while len(outputs) < 2:
            index, data = messages.get(timeout=max(0, deadline - time.monotonic()))
            if data is None or len(data) > (stdout_cap if index == 0 else stderr_cap):
                raise ValueError("probe failed")
            outputs[index] = data
        if process.wait(timeout=max(0, deadline - time.monotonic())) != 0 and not include_stderr:
            raise ValueError("probe failed")
        return outputs[0] + outputs[1] if include_stderr else outputs[0]
    except (queue.Empty, subprocess.TimeoutExpired):
        raise ValueError("probe failed") from None
    finally:
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
