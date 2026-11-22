"""Two-process contention on the packaged curation logs (#3101, A1).

The waiter is a real child process. Its ``curation_run.lock_exclusive`` is
wrapped to print ``entering-lock`` from *inside* the acquisition, before the
real ``flock`` call, and ``publish_log`` prints ``publishing`` from the
publication seam. The parent holds the canonical state lock, waits until the
child reports it is at the lock, checks that nothing was published, then
releases; only then may the child publish.

There are no sleeps. Each ordering claim is a blocking read on the child's
stdout. The only timed wait is a failure bound: if a child never reaches the
lock (for example because the writer stopped taking it) the read fails the
test instead of hanging. The non-blocking ``select`` check can only fail when
the child got past the lock too early.

macOS and Linux only; skipped elsewhere rather than passing through a weaker
path.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from ciao import curation_run as cr

pytestmark = pytest.mark.skipif(
    os.name != "posix" or sys.platform not in {"darwin", "linux"},
    reason="the curation log contract is verified on macOS and Linux only",
)

_LOCK_BOUND_SECONDS = 30

_CHILD = r"""
import sys
from datetime import date
from pathlib import Path

from ciao import curation_logs, curation_run as cr

operation, vault, holder = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
real_lock = cr.lock_exclusive
real_publish = curation_logs.publish_log


def lock_then_announce(fd, *, blocking=True):
    sys.stdout.write("entering-lock\n")
    sys.stdout.flush()
    real_lock(fd, blocking=blocking)


def announcing_publish(path, data, *, snapshot):
    sys.stdout.write("publishing\n")
    sys.stdout.flush()
    real_publish(path, data, snapshot=snapshot)


cr.lock_exclusive = lock_then_announce
curation_logs.publish_log = announcing_publish
if operation == "append":
    cr.append_log(vault, holder=holder, log="curation", payload=b"technical report")
elif operation == "marker":
    with cr._state_lock(vault):
        cr._write_full_pass_marker(vault, today=date(2026, 9, 19))
else:
    raise SystemExit(f"unknown operation {operation}")
sys.stdout.write("done\n")
sys.stdout.flush()
"""


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Parent and children must lock the same file, so the override is set before either spawns.

    Set only in the children's environment, the parent would lock the shared
    default file while the children waited on another, and the exclusion
    under test would not hold.
    """
    monkeypatch.setenv("CIAO_QUEUE_LOCK_DIR", str(tmp_path / "locks"))


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "memory-vault"
    (vault / "Workspace").mkdir(parents=True)
    (vault / cr.CURATION_LOG_RELATIVE).write_text(
        "---\nlast_full_pass: 2026-09-01\n---\n\n# Curation log\n", encoding="utf-8", newline=""
    )
    return vault


def _spawn(operation: str, vault: Path, holder: str) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, operation, str(vault), holder],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _line(proc: subprocess.Popen[str]) -> str:
    """The child's next line. Fails, rather than hangs, if the child never writes one."""
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], _LOCK_BOUND_SECONDS)
    if not ready:
        proc.kill()
        pytest.fail("the child never reached the state lock")
    return proc.stdout.readline().strip()


def _nothing_pending(proc: subprocess.Popen[str]) -> bool:
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], 0)
    return not ready


def _finish(proc: subprocess.Popen[str]) -> None:
    """Read to the child's ``done`` line, then require a clean exit."""
    line = _line(proc)
    while line == "publishing":
        line = _line(proc)
    assert line == "done", proc.stderr.read() if proc.stderr else ""
    assert proc.wait(timeout=30) == 0


def test_a_child_append_signals_from_inside_the_lock_and_waits_for_release(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="nightly")
    body = (vault / cr.CURATION_LOG_RELATIVE).read_bytes()

    with cr._state_lock(vault):
        child = _spawn("append", vault, lease.holder)
        assert _line(child) == "entering-lock"
        assert _nothing_pending(child), "the child published while the parent held the lock"
        assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == body

    assert _line(child) == "publishing"
    _finish(child)
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes() == body + b"\n\n" + b"technical report"


def test_a_child_marker_signals_from_inside_the_lock_and_waits_for_release(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS))

    with cr._state_lock(vault):
        child = _spawn("marker", vault, "")
        assert _line(child) == "entering-lock"
        assert _nothing_pending(child), "the marker was written while the parent held the lock"

    assert _line(child) == "publishing"
    _finish(child)
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-19"


def test_an_alias_of_the_vault_contends_on_the_same_lock(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    alias = tmp_path / "alias-vault"
    alias.symlink_to(vault, target_is_directory=True)
    lease = cr.begin_run(vault, holder="nightly")

    with cr._state_lock(vault):
        child = _spawn("append", alias, lease.holder)
        assert _line(child) == "entering-lock"
        assert _nothing_pending(child), "a root alias bypassed the canonical lock"

    assert _line(child) == "publishing"
    _finish(child)
    assert (vault / cr.CURATION_LOG_RELATIVE).read_bytes().endswith(b"\n\ntechnical report")


def test_append_and_marker_both_survive_contention_in_either_order(tmp_path: Path) -> None:
    vault = _vault(tmp_path)
    lease = cr.begin_run(vault, holder="nightly")
    cr.record_done(vault, sorted(cr.REQUIRED_HYGIENE_KEYS), holder=lease.holder)

    with cr._state_lock(vault):
        appender = _spawn("append", vault, lease.holder)
        marker = _spawn("marker", vault, "")
        assert _line(appender) == "entering-lock"
        assert _line(marker) == "entering-lock"
        assert _nothing_pending(appender) and _nothing_pending(marker)

    # The lock admits the two children in either order. Both publications must
    # land, and neither may overwrite the other's committed bytes.
    _finish(appender)
    _finish(marker)
    text = (vault / cr.CURATION_LOG_RELATIVE).read_text(encoding="utf-8")
    assert text.count("technical report") == 1
    assert text.count("last_full_pass") == 1
    assert cr.read_last_full_pass(vault / cr.CURATION_LOG_RELATIVE) == "2026-09-19"
