"""Design-only #3061 baseline; run only after review. No engine/CLI boot."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import queue
import stat
import subprocess
import sys
import threading

BASE = "448ca028e7c9ba836980343e7a86c7eaddd8828a"
HELPERS = (
    "ciao/curation_run.py", "ciao/memory_receipts.py",
    "ciao/os_support/files.py", "ciao/os_support/private.py",
    "ciao/os_support/links.py", "ciao/os_support/locks.py",
)
TIMEOUT = 30


class Blocked(RuntimeError):
    """Only fixed harness-owned reason labels, never external error text."""


def require(condition, label):
    if not condition:
        raise Blocked(label)


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}, sort_keys=True), flush=True)


def isolate(root: Path, repo: Path):
    # Defense in depth; launch.ps1 must already sanitize BEFORE Python startup.
    require(sys.flags.isolated and sys.flags.no_site and sys.dont_write_bytecode,
            "requires-isolated-no-site-no-bytecode-startup")
    keep = {
        k: v for k, v in os.environ.items()
        if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
            "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE",
        }
    }
    os.environ.clear()
    os.environ.update(keep)
    for key, name in {
        "HOME": "home", "USERPROFILE": "home",
        "LOCALAPPDATA": "localappdata", "APPDATA": "appdata",
        "XDG_CONFIG_HOME": "config", "XDG_STATE_HOME": "state",
        "XDG_CACHE_HOME": "cache", "CIAO_MEMORY_DIR": "memory",
        "CIAO_LAUNCH_AGENTS_DIR": "launchagents",
        "CIAO_QUEUE_LOCK_DIR": "locks", "TMPDIR": "tmp",
        "TMP": "tmp", "TEMP": "tmp",
    }.items():
        folder = root / name
        folder.mkdir(exist_ok=True)
        os.environ[key] = str(folder)


def verify_imports(repo, sandbox, manifest_path):
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    require(data["base"] == BASE, "wrong-materialized-base")
    require(repo == sandbox / "source", "unexpected-materialized-source")
    for folder, expected in ((repo, data["source"]), (sandbox / "deps", data["deps"])):
        actual = {}
        for path in folder.rglob("*"):
            require(not path.is_symlink() and not (sys.platform == "win32" and path.is_junction()),
                    "import-tree-link")
            require("__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo", ".pth"},
                    "import-tree-cache-or-site-hook")
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                require(info.st_nlink == 1, "import-tree-hardlink")
                actual[path.relative_to(folder).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                require(stat.S_ISDIR(info.st_mode), "import-tree-special-file")
        require(actual == expected, "import-tree-manifest-mismatch")
    # -I -S excluded CWD, script directory, site and PYTHONPATH from startup.
    # Only freshly materialized, completely inventoried roots are added.
    sys.path[:0] = [str(repo), str(sandbox / "deps")]
    return data


def command(*args, cwd=None):
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        timeout=TIMEOUT, check=False,
    )
    require(result.returncode == 0, "metadata-command-failed")
    return result.stdout.strip()


def filesystem(root, provenance):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        volume_path = kernel.GetVolumePathNameW
        volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        volume_path.restype = wintypes.BOOL
        volume_info = kernel.GetVolumeInformationW
        volume_info.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD,
        ]
        volume_info.restype = wintypes.BOOL
        mount, kind = ctypes.create_unicode_buffer(1024), ctypes.create_unicode_buffer(64)
        require(volume_path(str(root), mount, len(mount)), "volume-path")
        require(volume_info(mount.value, None, 0, None, None, None, kind, len(kind)), "volume-info")
        require(kind.value.upper() == "NTFS", "requires-ntfs")
        return kind.value
    if sys.platform == "darwin":
        data = plistlib.loads(command("diskutil", "info", "-plist", str(root)).encode("utf-8"))
        kind = data.get("FilesystemType", "").lower()
        require(kind == "apfs", "requires-apfs")
        return kind
    require(sys.platform == "linux", "unsupported-platform")
    require(provenance["hosted_runner"] == "github-hosted", "requires-verified-hosted-linux-run")
    mount = json.loads(command(
        "/usr/bin/findmnt", "--json", "--output", "FSTYPE,MAJ:MIN", "--target", str(root),
    ))["filesystems"]
    require(len(mount) == 1 and mount[0]["fstype"] in {"ext4", "xfs"},
            "requires-single-reviewed-linux-mount")
    device = root.stat().st_dev
    number = f"{os.major(device)}:{os.minor(device)}"
    require(mount[0]["maj:min"] == number, "mount-device-mismatch")
    leaf = (Path("/sys/dev/block") / number).resolve(strict=True)
    disk = leaf.parent if (leaf / "partition").exists() else leaf
    # This minimal harness SUPPORTS ONLY direct disks/one partition. It refuses
    # loop/dm/md/multipath rather than pretending their backing chain is local.
    for node in {leaf, disk}:
        require((node / "dev").is_file(), "missing-native-block-identity")
        require(not any((node / name).exists() for name in ("loop", "dm", "md")),
                "unsupported-indirect-block-chain")
        slaves = node / "slaves"
        require(not slaves.exists() or not list(slaves.iterdir()), "unsupported-block-slaves")
    require((disk / "slaves").is_dir(), "missing-disk-backing-information")
    require((leaf / "dev").read_text(encoding="ascii").strip() == number,
            "sysfs-device-mismatch")
    location = str(disk).lower()
    require(location.startswith("/sys/devices/") and "/virtual/" not in location,
            "not-direct-guest-block-device")
    require(not any(token in location for token in ("iscsi", "/session", "fcoe", "rbd", "nbd", "aoe")),
            "network-block-chain")
    if "vmbus" in location:
        transport = "hyperv-vmbus"
    elif "/virtio" in location:
        transport = "virtio"
    else:
        controllers = [p for p in disk.parents if (p / "transport").is_file()]
        require(bool(controllers), "unverified-disk-transport")
        require(controllers[0].joinpath("transport").read_text(encoding="ascii").strip() == "pcie",
                "non-pcie-nvme-transport")
        require(disk.name.startswith("nvme"), "unverified-pcie-disk")
        transport = "nvme-pcie"
    emit("linux-backing", filesystem=mount[0]["fstype"], device=number,
         chain=["partition", "disk"] if leaf != disk else ["disk"],
         transport=transport, scope="guest-local-block-not-physical-hypervisor-backing")
    return mount[0]["fstype"]


def child(role, repo, sandbox, vault, manifest):
    def refuse_descendants(event, args):
        if event in {"subprocess.Popen", "os.system", "os.fork", "os.forkpty",
                     "os.posix_spawn", "os.exec", "os.spawn"}:
            raise Blocked("peer-descendant-spawn-forbidden")
    # Peer roles never need grandchildren. Catch Python-mediated spawn attempts;
    # reviewed native helper code is trusted, not an adversarial-code sandbox.
    sys.addaudithook(refuse_descendants)
    isolate(sandbox, repo)
    verify_imports(repo, sandbox, manifest)
    from ciao import curation_run
    from ciao.memory_receipts import lock_path_for
    from ciao.os_support.locks import lock_exclusive
    key = lock_path_for(str(curation_run.state_path(vault).resolve()))
    require(key.parent == sandbox / "locks", "lock-outside-sandbox")
    if role == "B":
        # Observe the real acquisition seam, never replace OS lock semantics.
        def observed(fd, *, blocking=True):
            require(blocking, "unexpected-lock-mode")
            try:
                lock_exclusive(fd, blocking=False)
            except BlockingIOError:
                emit("EXCLUDED_NONBLOCKING")
            else:
                # A is still holding: success here is a broken baseline.
                raise Blocked("missing-real-lock-contention")
            # Parent sends ACQUIRE only AFTER A's RELEASED acknowledgement.
            # No claim that a blocking syscall was ever waiting.
            require(sys.stdin.readline().strip() == "ACQUIRE", "missing-acquire-command")
            lock_exclusive(fd, blocking=False)
        curation_run.lock_exclusive = observed
    with curation_run._state_lock(vault):
        emit("HELD" if role == "A" else "ENTERED")
        require(sys.stdin.readline().strip() == "RELEASE", "missing-release")
    emit("RELEASED")


class Peer:
    def __init__(self, role, repo, sandbox, vault, manifest):
        self.process = None
        self.reader = None
        self.cleaned = False
        self.argv = (
            [sys.executable, "-I", "-S", "-B", "-u", str(Path(__file__).resolve()), "--child",
             role, str(repo), str(sandbox), str(vault), str(manifest)]
        )
        self.sandbox = sandbox
        self.events = queue.Queue()

    def start(self):
        # Caller registers this owner BEFORE starting, including partial-start failures.
        require(self.process is None, "peer-already-started")
        self.process = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", cwd=self.sandbox,
        )
        def read():
            try:
                for line in self.process.stdout:
                    self.events.put(line)
            except BaseException:
                # Never let Thread's default excepthook print paths/tracebacks.
                # Fixed event only; expected-event/EOF checks reject it.
                self.events.put('{"event":"READER_FAILED"}')
            finally:
                self.events.put("")
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()

    def expect(self, event):
        line = self.events.get(timeout=TIMEOUT)
        require(bool(line), "child-closed-pipe")
        require(line.strip() != '{"event":"READER_FAILED"}', "peer-reader-failed-redacted")
        require(json.loads(line) == {"event": event}, "unexpected-child-event")
        emit("handshake", child_event=event)

    def release(self):
        self.process.stdin.write("RELEASE\n")
        self.process.stdin.flush()

    def acquire(self):
        self.process.stdin.write("ACQUIRE\n")
        self.process.stdin.flush()

    def finish(self):
        require(self.process.wait(timeout=TIMEOUT) == 0, "child-exit")
        self.reader.join(timeout=TIMEOUT)
        require(not self.reader.is_alive(), "child-output-reader-not-closed")
        require(self.events.get(timeout=TIMEOUT) == "", "unexpected-extra-child-event")

    def cleanup(self):
        if self.process is None:
            return
        # Use only the owned Popen handle, never a PID scan or a guessed tree.
        # Reviewed peer roles/imports spawn no grandchildren.
        failed = False
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=TIMEOUT)
        except Exception:
            failed = True
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                if pipe is not None:
                    # Closing stdout while its reader is blocked can itself hang.
                    if pipe is self.process.stdout and self.reader is not None:
                        self.reader.join(timeout=TIMEOUT)
                        require(not self.reader.is_alive(), "cleanup-output-reader-live")
                    pipe.close()
            except Exception:
                failed = True
        require(not failed and self.process.poll() is not None, "peer-cleanup-unconfirmed")
        self.cleaned = True


def inspect_fd(fd):
    from ciao.os_support.private import is_private_descriptor_strict
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode), "not-regular")
    require(info.st_ino != 0 and info.st_nlink == 1, "unreliable-or-aliased-file-id")
    require(is_private_descriptor_strict(fd), "native-private-owner-check")
    return info


def refusal(path):
    from ciao.os_support.files import open_fd
    try:
        fd = open_fd(path, os.O_WRONLY | os.O_TRUNC, follow_symlinks=False)
    except OSError as exc:
        require(exc.errno == errno.ELOOP, "nofollow-wrong-error")
    else:
        os.close(fd)
        raise Blocked("nofollow-followed-link")


def probe(repo, root, manifest):
    isolate(root, repo)
    provenance = verify_imports(repo, root, manifest)
    emit("metadata", os=platform.system(), release=platform.release(),
         machine=platform.machine(), python=platform.python_version(),
         filesystem=filesystem(root, provenance), base=BASE,
         source_archive_sha256=provenance["archive_sha256"],
         dependency_wheel_sha256=provenance["wheel_sha256"],
         harness_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
         helper_sha256={p: hashlib.sha256((repo / p).read_bytes()).hexdigest() for p in HELPERS})
    from ciao import curation_run
    from ciao.memory_receipts import lock_path_for
    from ciao.os_support.files import open_fd, replace_file
    from ciao.os_support.links import link_dir, remove_link, is_link
    from ciao.os_support.private import (
        open_private, mkstemp_private, make_private_dir, is_private_strict,
        carry_mode,
    )
    # All tightened paths are newly created disposable fixtures, never adopted live paths.
    make_private_dir(root)
    folder = root / "vault"
    folder.mkdir(mode=0o700)
    make_private_dir(folder)
    require(is_private_strict(folder), "native-directory-private")
    target = folder / "binary.md"
    old = b"# synthetic\r\n\x00\ncaf\xc3\xa9\r\nlast"
    new = b"---\r\nmarker: synthetic\r\n---\r\n" + old + b"\n\nreport\n"
    fd = open_private(target, os.O_RDWR | os.O_EXCL, follow_symlinks=False)
    try:
        original = inspect_fd(fd)  # Assert privacy BEFORE writing.
        require(os.write(fd, old) == len(old), "short-write")
        os.fsync(fd)
        os.lseek(fd, 0, os.SEEK_SET)
        require(os.read(fd, len(old) + 1) == old, "binary-roundtrip")
    finally:
        os.close(fd)
    fd, staged = mkstemp_private(dir=folder, prefix=".baseline-")
    try:
        staged_info = inspect_fd(fd)  # Private from creation, no chmod repair.
        require(os.write(fd, new) == len(new), "short-stage-write")
        os.fsync(fd)
        carry_mode(fd, stat.S_IMODE(original.st_mode), temp=staged, original=target)
        inspect_fd(fd)
        require(target.read_bytes() == old, "target-changed-before-publication")
    finally:
        os.close(fd)
    require(Path(staged).parent == target.parent, "not-sibling-stage")
    # No verification descriptor remains open across Windows replacement.
    replace_file(staged, target)
    require(not Path(staged).exists(), "staging-not-consumed")
    fd = open_fd(target, os.O_RDONLY, follow_symlinks=False)
    try:
        replaced = inspect_fd(fd)
        require((replaced.st_dev, replaced.st_ino) == (staged_info.st_dev, staged_info.st_ino),
                "replacement-not-staged-identity")
        require((replaced.st_dev, replaced.st_ino) != (original.st_dev, original.st_ino),
                "replacement-retained-old-identity")
        require(os.read(fd, len(new) + 1) == new, "replacement-binary-roundtrip")
        require(os.path.samestat(replaced, os.stat(target)), "path-descriptor-identity")
        if sys.platform != "win32":
            require((replaced.st_uid, replaced.st_gid, stat.S_IMODE(replaced.st_mode)) ==
                    (original.st_uid, original.st_gid, stat.S_IMODE(original.st_mode)),
                    "posix-security-metadata-changed")
    finally:
        os.close(fd)
    emit("PASS", case="binary-private-sibling-replacement-native-identity")
    alias = root / "alias"
    link_dir(folder, alias)
    try:
        require(is_link(alias) and alias.resolve() == folder.resolve(), "root-alias")
        require(os.path.samefile(alias / target.name, target), "alias-file-identity")
        akey = lock_path_for(str(curation_run.state_path(folder).resolve()))
        bkey = lock_path_for(str(curation_run.state_path(alias).resolve()))
        require(akey == bkey and akey.parent == root / "locks", "canonical-lock-key")
        # Junction/symlink as final component must be refused by the existing helper.
        refusal(alias)
        emit("PASS", case="root-alias-canonical-key-and-final-link-nofollow")
        leaf = folder / "leaf-link"
        try:
            leaf.symlink_to(target)
        except OSError as exc:
            if sys.platform != "win32" or getattr(exc, "winerror", None) != 1314:
                raise
            emit("BLOCKED", case="windows-leaf-symlink-privilege")
            raise Blocked("required-leaf-nofollow-not-proved")
        try:
            refusal(leaf)
            require(target.read_bytes() == new, "link-refusal-truncated-target")
        finally:
            leaf.unlink()
        dangling = folder / "dangling"
        dangling.symlink_to(folder / "absent")
        try:
            refusal(dangling)
            require(not (folder / "absent").exists(), "dangling-created-target")
        finally:
            dangling.unlink()
        emit("PASS", case="leaf-and-dangling-nofollow-without-truncation")
        peers = []
        try:
            a = Peer("A", repo, root, folder, manifest)
            peers.append(a)
            a.start()
            a.expect("HELD")
            b = Peer("B", repo, root, alias, manifest)
            peers.append(b)
            b.start()
            b.expect("EXCLUDED_NONBLOCKING")  # Actual OS refusal, not a waiting-syscall claim.
            a.release()
            a.expect("RELEASED")
            b.acquire()
            b.expect("ENTERED")  # No negative timeout or sleep as exclusion evidence.
            b.release()
            b.expect("RELEASED")
            for peer in peers:
                peer.finish()
        finally:
            cleanup_failed = False
            for peer in peers:
                try:
                    peer.cleanup()
                except Exception:
                    cleanup_failed = True
            require(not cleanup_failed and all(peer.cleaned for peer in peers),
                    "protocol-cleanup-unconfirmed")
        emit("PASS", case="two-process-nonblocking-exclusion-then-post-release-acquisition")
    finally:
        remove_link(alias)  # Remove junction itself before temporary-tree cleanup.
    require(len(peers) == 2 and all(peer.cleaned for peer in peers), "peer-ownership-unconfirmed")
    root.joinpath("protocol-cleaned.json").write_text(json.dumps({
        "parent_pid": os.getpid(), "protocol_token": provenance["protocol_token"],
        "peers_reaped": 2, "pipes_closed": True,
    }, sort_keys=True), encoding="utf-8")
    emit("PASS", case="predispatch-scope-only-not-real-blocking-waiter-acceptance")


if __name__ == "__main__":
    try:
        if len(sys.argv) == 7 and sys.argv[1] == "--child":
            child(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]), Path(sys.argv[6]))
        else:
            require(len(sys.argv) == 4, "requires-external-reviewed-launcher")
            repo = Path(sys.argv[1]).resolve(strict=True)
            root = Path(sys.argv[2]).resolve(strict=True)
            probe(repo, root, Path(sys.argv[3]).resolve(strict=True))
    except Exception as exc:
        # Never print exception messages, environment, payloads, SID or host paths.
        emit("BLOCKED", error_type=type(exc).__name__,
             reason=str(exc) if isinstance(exc, Blocked) else "external-error-text-redacted")
        sys.exit(1)
