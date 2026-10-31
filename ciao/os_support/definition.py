"""One strict, bounded read of a service definition or native state file.

Both the native installer handoff and the service manager's fresh-activation
path must hash *exactly* the bytes a launchd or Task Scheduler job will read.
The native handoff also reads its own result and the workspace ``.env`` that
proves a completed setup; each of those used to be a ``Path.read_text`` (or a
near-identical copy of this read), which is how one of them ended up unbounded
and able to block on a FIFO. Keeping the one implementation here means binary
mode, the byte count and the inode/mtime rechecks cannot drift between callers.

Windows is the reason ``O_BINARY`` is explicit even though the module also runs
on POSIX. A service definition is UTF-16 XML or an ASCII plist, and Windows'
text mode treats a ``Ctrl-Z`` byte as end-of-file and rewrites CRLF; either
would make the digest or the parsed bytes describe something other than the
file launchd or Task Scheduler sees. ``O_BINARY`` is absent on POSIX, where the
read is already binary.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ciao.os_support.private import is_private_descriptor, is_private_descriptor_strict

#: A service definition is a plist or task XML of a few kilobytes. Anything
#: past this is not the file setup wrote, and hashing it is not worth the read.
MAX_DEFINITION_BYTES = 256 * 1024


class DefinitionReadError(RuntimeError):
    """The file is missing, not a regular file, too large, or changed."""


@dataclass(frozen=True)
class DefinitionRead:
    """One bounded, safely read file: its digest and the very bytes."""

    sha256: str
    data: bytes


def _open_flags() -> int:
    """The binary, no-follow, non-blocking read flags this platform provides."""
    return (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_BINARY", 0)
    )


def read_bounded_file(
    path: Path,
    *,
    max_bytes: int,
    label: str,
    require_private: bool = False,
    require_private_owner: bool = False,
) -> DefinitionRead:
    """Read exactly one bounded regular file, or refuse.

    Strict by design: a missing file, a symlink, a FIFO or any other special
    file, an unreadable path, a file larger than ``max_bytes``, a replacement
    between ``lstat`` and ``open``, a descriptor that is not a regular file, a
    read that does not consume exactly the size the descriptor opened with, and
    metadata that changes between the start and the end of the read are all
    refusals. The type is checked on the path *and* on the opened descriptor,
    and the open is non-blocking where the platform has it, so a FIFO swapped in
    after the ``lstat`` can neither block the open nor be read as the file. The
    bytes returned are the bytes hashed, which is what a caller must parse.

    ``label`` names the file in every refusal. ``require_private`` additionally
    requires the *opened descriptor* to be owner-only, so the answer describes
    the inode whose bytes were read rather than a pathname that may have been
    swapped between a path check and the open. ``require_private_owner``
    strengthens that to the strict verifier: the inode must be owned by the
    effective user, and on macOS its extended ACL must not allow another
    principal. It implies ``require_private`` and exists so the legacy mode-only
    answer stays unchanged for its existing callers.
    """
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        raise DefinitionReadError(f"{label} is missing: {path}") from None
    except OSError as exc:
        raise DefinitionReadError(
            f"Could not inspect {label} {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(before.st_mode):
        raise DefinitionReadError(f"{label} is not a regular file: {path}")
    try:
        descriptor = os.open(path, _open_flags())
    except OSError as exc:
        raise DefinitionReadError(f"Could not open {label} {path}: {exc}") from exc
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise DefinitionReadError(f"{label} is not a regular file: {path}")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise DefinitionReadError(
                f"{label} was replaced while it was being read: {path}"
            )
        if require_private or require_private_owner:
            try:
                private = (
                    is_private_descriptor_strict(descriptor)
                    if require_private_owner
                    else is_private_descriptor(descriptor)
                )
            except OSError as exc:
                raise DefinitionReadError(
                    f"Could not inspect {label} {path}: {exc}"
                ) from exc
            if not private:
                raise DefinitionReadError(f"{label} is not owner-only: {path}")
        total = 0
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise DefinitionReadError(
                    f"{label} is larger than {max_bytes} bytes: {path}"
                )
            digest.update(chunk)
            chunks.append(chunk)
        settled = os.fstat(descriptor)
    except DefinitionReadError:
        raise
    except OSError as exc:
        raise DefinitionReadError(f"Could not read {label} {path}: {exc}") from exc
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
    # The byte count against the size the descriptor opened with, then the
    # descriptor against itself, then the pathname against the descriptor: a
    # rewrite in place changes size/mtime, a replacement changes the inode, and
    # an early EOF or an append is a read that did not cover the file it opened.
    if (
        total != opened.st_size
        or (settled.st_dev, settled.st_ino) != (opened.st_dev, opened.st_ino)
        or settled.st_size != opened.st_size
        or settled.st_mtime_ns != opened.st_mtime_ns
    ):
        raise DefinitionReadError(
            f"{label} changed while it was being read: {path}"
        )
    try:
        after = os.lstat(path)
    except OSError as exc:
        raise DefinitionReadError(f"Could not re-inspect {label} {path}: {exc}") from exc
    if (
        (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
        or after.st_size != settled.st_size
        or after.st_mtime_ns != settled.st_mtime_ns
    ):
        raise DefinitionReadError(
            f"{label} changed while it was being read: {path}"
        )
    return DefinitionRead(digest.hexdigest(), b"".join(chunks))


def read_definition(
    definition: Path, *, max_bytes: int = MAX_DEFINITION_BYTES
) -> DefinitionRead:
    """One bounded, strict read of a service definition; or a refusal."""
    return read_bounded_file(
        definition, max_bytes=max_bytes, label="the service definition"
    )
