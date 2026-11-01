#!/bin/sh
set -eu

# One-line installer for the Ciaobot engine alone (no Ciaobot.app), #562.
# The engine manifest signature and the wheel digest are checked separately,
# after the bootstrap below. A missing uv is bootstrapped from the pinned
# release archive, which has to match the sha256 embedded in this file and is
# fetched over HTTPS; uv publishes no minisign or Apple signature for it.
# Python and the packages uv fetches afterwards have no digest pinned here.
#
# The trust boundary of that bootstrap is this user, not this process: the
# archive and the staged executable sit in a mode-0700 scratch directory, which
# is not per-user isolation. It keeps other users out, and it does not stop a
# process running as the same user, which can still replace the archive between
# the /usr/bin/shasum check and the tar read, or swap the staged copy before it
# runs.
# The legacy publish writes the verified uv, and sets its mode, through the
# descriptor its exclusive create returned, so a link substituted at
# ~/.local/bin/uv is refused rather than followed; a same-user writer can still
# replace that path after the publish returns. The reviewed digest table plus
# TLS bound what the network can hand this script; code already running as this
# user is outside that boundary.

repo=raffaelefarinaro/ciaobot
release_base=${CIAO_RELEASE_BASE_URL:-https://github.com/$repo/releases/download}
release_base=${release_base%/}
UV_VERSION=0.12.17
PYTHON_VERSION=3.13
CRYPTOGRAPHY_PIN="cryptography==50.0.0"
# Same key as ciao/release_manifest.py RELEASE_PUBLIC_KEY (a test keeps them equal).
RELEASE_PUBLIC_KEY="RWSDUnIeQDnpmnNJiTjLmN6XOVFqgn1A0EXvTVG7AJIZXJxhyFN9osxm"
SERVER_LABEL=com.ciao.server
DESKTOP_LABEL=Ciaobot
SHIM_MARKER="# Ciaobot shim (managed by the Ciaobot installer)"
PLISTBUDDY=/usr/libexec/PlistBuddy
# Script variables, not environment variables: the test harness rewrites these
# lines in its copy of the script, so a test run never talks to the real
# launchd, the running Ciaobot.app, the operator's `pgrep`, a real `sleep`, or a
# real engine answering the health check on some port of this Mac.
LAUNCHCTL=launchctl
OSASCRIPT=osascript
PGREP=pgrep
LSOF=lsof
PS=ps
CURL=curl
SLEEP=sleep
# The system tar, never whatever PATH resolves: a user-writable tar earlier on
# PATH must not decide what the uv allowlist inspection sees.
TAR=/usr/bin/tar
# The macOS system stat and shasum, never whatever PATH resolves first. `shasum`
# is the command that authenticates the uv archive and the release artifacts, so
# a user-writable directory earlier on PATH must not be able to answer the
# digest check; `stat` is the identity check on the published uv, so the same
# applies. The test harness rewrites these lines in its copy of the script.
STAT=/usr/bin/stat
SHASUM=/usr/bin/shasum
# The port `ciao run --installer-bootstrap` and a fresh install's first engine
# serve on: ciao.config's `pwa_port` default.
FRESH_PORT=8443
# Set only by bootstrap_verified_uv, once the pinned uv archive has matched its
# reviewed sha256 and passed the member allowlist: the private staged copy this
# run extracted. A native-mode install must take this rather than a PATH uv.
verified_uv=
# Set inside the legacy publish's subshell once its exclusive create has
# returned success, together with the inode read from that descriptor, so a
# failure while the descriptor is still open takes back only that file.
uv_created=0
uv_inode=
version=
workspace=
no_start=0
no_modify_path=0
migrate=0
as_host=0
as_client=
desktop_live=0
# Filled in by classify_install, empty until then. `migrate_path` is the
# decision: host, client, or skip for an install nothing has to be migrated for.
migrate_kind=
migrate_workspace=
migrate_host_url=
# The `.app` the engine being migrated runs out of, empty when the program this
# Mac's plist names lives outside a bundle. Only the completed hand-over deletes
# it, and only after the app's own agent is gone.
migrate_app_bundle=
migrate_path=skip
migration_dir="$HOME/.local/state/ciaobot/migration"
install_receipt="$HOME/.local/state/ciaobot/install-receipt.json"
migration_started_at=
# Set once this run has started a migration transaction, so a signal handler can
# tell "nothing has been touched yet" from "this stopped halfway".
migration_active=0
# The native server-host upgrade (#2330) is a *separate* transaction from the
# desktop-engine migration above: it is the host bundle and its owner-only
# ownership record, it is bound to this installer's pid, and only its own commit
# releases the before-images. `native_host_pending` means this run started one and
# it is still uncommitted. The rollback attempt is guarded, because `fail` may be
# reached from inside the rollback itself.
native_host_pending=0
native_host_operation=
native_host_committed=0
native_host_rollback_tried=0
# Set immediately before the install command runs, so the EXIT handler knows this
# shell may have created a durable operation even if bookkeeping never recorded it.
native_host_install_started=0
# Set once a launchd job may be running out of the candidate host. A rollback has
# to boot that job out and wait for the process to exit before it reverts the
# identity on disk: a still-running candidate is never reported as rolled back.
native_host_service_started=0
# The last `transaction-status` read: pending, nonce, owner pid and liveness.
native_status_pending=0
native_status_operation=
native_status_owner_pid=
native_status_owner_alive=0
# Set when this transaction has taken the app's own LaunchAgent out of launchd,
# which is not the same fact as having removed its plist: an agent that was
# booted out and never loaded again is an unloaded one, and an enabled label with
# no job behind it never starts. A rollback reloads and starts that agent
# whenever this is 1, and re-enables it alone when it is 0.
desktop_bootout_done=0
# Recorded in the migration receipt once the transaction has begun retiring the
# app's own agent, so a run that stops in the middle of that cannot be resumed
# as though launchd still had the job.
retiring_desktop=0
# The before-images, as paths in the migration receipt. An empty one is a fact
# about this Mac ("there was nothing here"), not a missing variable: a rollback
# acts on the difference instead of inventing a file.
before_server_plist=
before_desktop_plist=
before_shim=
before_install_receipt=
before_tool_env=
# The last validated read of the migration receipt, and why it was rejected.
receipt_valid=0
receipt_phase=
receipt_reason=none
receipt_kind=
receipt_installed_version=
receipt_started_at=
receipt_workspace=
receipt_host_url=
receipt_retiring_desktop=0
receipt_before_server_plist=
receipt_before_desktop_plist=
receipt_before_shim=
receipt_before_install_receipt=
receipt_before_tool_env=
# Set when the receipt says a host migration stopped while it was taking the
# app's own agent out of launchd. `ciao setup` has already repointed the engine
# plist at the new engine by then, so the classifier reads that Mac as an
# ordinary installer-managed install; it is not one, and this is the flag that
# says so.
resume_retiring=0
# Set when the receipt describes a host hand-over that never finished: a
# transaction in `started`, `installed_no_start` or `interrupted`. Every one of
# those phases can be reached with `ciao setup` already done - the engine plist
# then points at the new tool, which the classifier reports as an ordinary
# installer-managed engine - so the same window as `resume_retiring` opens for
# all of them, and the receipt is what says what the run was doing.
resume_unfinished_host=0
# Why a settled receipt is not believed, when it names a phase this script
# settles but the install on this Mac does not match it.
settled_mismatch=

usage() {
    cat >&2 <<'USAGE'
Usage: install-engine.sh [--version VERSION] [--workspace DIRECTORY] [--no-start] [--no-modify-path]
                         [--migrate [--as-host | --as-client URL]]

Installs the Ciaobot engine for the current user with uv; the PWA is its UI.

  --migrate            Move an engine Ciaobot.app currently manages to this
                       installer. The workspace, its .env and the runtime root
                       are left untouched: only the LaunchAgents, the shim, the
                       uv tool environment, ~/.local/bin/uv when this Mac had no
                       uv, and ~/.local/state/ciaobot/ change.
                       Before-images of the LaunchAgents, the shim, the install
                       receipt and any existing uv tool environment are kept in
                       ~/.local/state/ciaobot/migration/before/; a uv this run
                       had to publish is not one of them and stays after a
                       rollback. Any failure after the install puts the old
                       engine back. If Ciaobot.app
                       is still running 20s after it is asked to quit, the run
                       stops with that engine and its service unchanged. A
                       hand-over keeps the workspace that engine runs in:
                       --workspace may only name that same existing workspace,
                       and no workspace is ever created.
  --no-modify-path     Do not add ciao to a shell startup file.
  --as-host            With --migrate: treat this Mac as the host, even when its
                       node state cannot be read or trusted.
  --as-client URL      With --migrate: treat this Mac as a client of URL, which
                       has to be an http:// or https:// address with a host name.
                       Its local engine is disabled and never replaced, so the
                       Mac does not become a second writer.
USAGE
}

# --- the native server-host upgrade transaction ----------------------------
#
# Nothing in this section talks to launchd or starts a host. The Python installer
# is inert: it moves the app, writes its owner-only record and keeps a durable
# journal. What lives here is the shell's side of that transaction - when the
# candidate's service may already be running, how a failure gives the old host
# back before the old engine definition is reloaded, and how a previous run's
# abandoned operation is recovered. All of it runs through the verified wheel's
# own module, so there is no second copy of the policy in shell.
#
# Defined before `fail` because `fail` is reached from the argument parser too,
# where nothing is pending yet and every one of these returns immediately.

native_host_run() {
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
        python -I -m ciao.server_host_install "$@"
}

native_host_status() {
    # The module's strict JSON in, one validated field per line out. A status this
    # script cannot parse is a refusal, never "nothing pending": a corrupt journal
    # read as "settled" is how a second installer would clobber a live operation.
    native_status_json=$(native_host_run transaction-status) || return 1
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
        python -I -c '
import json
import sys

PHASES = ("prepared", "old_moved", "installed")

try:
    status = json.loads(sys.argv[1])
except ValueError:
    raise SystemExit(1)
if not isinstance(status, dict) or not isinstance(status.get("pending"), bool):
    raise SystemExit(1)
if not status["pending"]:
    print("pending=0")
    raise SystemExit(0)
operation = status.get("operation")
phase = status.get("phase")
owner = status.get("owner_pid")
alive = status.get("owner_alive")
if (
    not isinstance(operation, str)
    or len(operation) != 32
    or any(character not in "0123456789abcdef" for character in operation)
):
    raise SystemExit(1)
if phase not in PHASES:
    raise SystemExit(1)
if type(owner) is not int or owner <= 0 or not isinstance(alive, bool):
    raise SystemExit(1)
print("pending=1")
print("operation=" + operation)
print("owner_pid=%d" % owner)
print("owner_alive=1" if alive else "owner_alive=0")
' "$native_status_json"
}

read_native_host_status() {
    # <status-file>: sets the four native_status_* globals, or fails on anything
    # this script did not write itself.
    native_status_pending=0
    native_status_operation=
    native_status_owner_pid=
    native_status_owner_alive=0
    [ -f "$1" ] || return 1
    while IFS= read -r native_status_line; do
        case "$native_status_line" in
            pending=0) native_status_pending=0 ;;
            pending=1) native_status_pending=1 ;;
            operation=*) native_status_operation=${native_status_line#operation=} ;;
            owner_pid=*) native_status_owner_pid=${native_status_line#owner_pid=} ;;
            owner_alive=0) native_status_owner_alive=0 ;;
            owner_alive=1) native_status_owner_alive=1 ;;
            *) return 1 ;;
        esac
    done < "$1"
    if [ "$native_status_pending" -eq 1 ] && [ -z "$native_status_operation" ]; then
        return 1
    fi
    return 0
}

native_host_quiesce() {
    # Only when a job may have been (re)started against the candidate host. This
    # fails *closed*: a job launchd still holds, a process still present, or a
    # `pgrep` that could not answer all mean the identity on disk must not be
    # reverted. Replacing the bytes under a running
    # signed process is the one outcome this transaction exists to prevent, and
    # reporting it as rolled back would be a lie.
    if [ "$native_host_service_started" -eq 0 ]; then
        return 0
    fi
    native_host_executable="$HOME/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"
    # `bootout` reports a missing job as an error too, so its own status is not the
    # answer; what launchd holds afterwards is. Only launchd *confirming* it has no
    # such job counts as unloaded: an operational or permission failure of the query
    # is uncertainty and fails closed, exactly like a job it still holds.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
    native_host_job_rc=0
    native_host_job_state || native_host_job_rc=$?
    if [ "$native_host_job_rc" -ne 1 ]; then
        return 1
    fi
    native_host_waited=0
    while [ "$native_host_waited" -lt 30 ]; do
        native_host_grep_rc=0
        "$PGREP" -f "$native_host_executable" >/dev/null 2>&1 || native_host_grep_rc=$?
        case "$native_host_grep_rc" in
            0) ;;
            1) break ;;
            # A `pgrep` that could not run at all (bad option, no permission, a
            # missing binary) is not "no process": it is an unanswered question.
            *) return 1 ;;
        esac
        native_host_waited=$((native_host_waited + 1))
        "$SLEEP" 1
    done
    if [ "$native_host_grep_rc" -ne 1 ]; then
        return 1
    fi
    native_host_service_started=0
    return 0
}

native_host_rollback() {
    # Exactly one attempt, made while the verified wheel is still unpacked. A
    # failure keeps the durable journal and before-images and says where they are;
    # it never claims the previous host is back.
    if [ "$native_host_pending" -eq 0 ] || [ "$native_host_rollback_tried" -ne 0 ]; then
        return 0
    fi
    native_host_rollback_tried=1
    if ! native_host_quiesce; then
        echo "Ciaobot engine installer: the signed Ciaobot Server is still running, so it was NOT replaced by the previous host. The previous host and its ownership record are kept: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
        return 1
    fi
    native_host_rollback_rc=0
    native_host_run rollback-upgrade --operation "$native_host_operation" \
        --owner-pid "$$" >/dev/null || native_host_rollback_rc=$?
    case "$native_host_rollback_rc" in
        0)
            native_host_pending=0
            native_host_service_started=0
            native_host_install_started=0
            echo "Ciaobot engine installer: the previous Ciaobot Server host was restored." >&2
            return 0
            ;;
        5)
            # The pair is back; only the retained material is left. The operation is
            # settled, so nothing may roll it back a second time - but the installer
            # result is still a failure, because the transaction left material behind
            # and the next host transaction is blocked until it is removed.
            native_host_pending=0
            native_host_service_started=0
            native_host_install_started=0
            echo "Ciaobot engine installer: the previous Ciaobot Server host was restored, but its retained before-images could not be removed. They are kept for a later run: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
            return 5
            ;;
    esac
    echo "Ciaobot engine installer: the previous Ciaobot Server host could NOT be fully restored. Its recoverable material is kept: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
    native_host_service_started=0
    return 1
}

native_host_commit() {
    # Only ever after positive evidence: the exact new engine answered, and every
    # enclosing step that could still roll back has succeeded. A commit that fails
    # rolls the host back through `fail`, because this run is not complete.
    if [ "$native_host_pending" -eq 0 ] || [ "$native_host_committed" -ne 0 ]; then
        return 0
    fi
    native_host_commit_rc=0
    native_host_run commit-upgrade --operation "$native_host_operation" \
        --owner-pid "$$" >/dev/null || native_host_commit_rc=$?
    if [ "$native_host_commit_rc" -ne 0 ] && [ "$native_host_commit_rc" -ne 5 ]; then
        fail "the signed Ciaobot Server host could not be committed, so this run is not complete"
    fi
    native_host_pending=0
    native_host_committed=1
    native_host_service_started=0
    native_host_install_started=0
    if [ "$native_host_commit_rc" -eq 5 ]; then
        # Committed, with before-images the module could not remove. The settled
        # state is kept (nothing rolls a committed host back), but the installer
        # result is a failure: the leftover blocks the next host transaction until a
        # later run removes it.
        fail "the signed Ciaobot Server host was committed, but its retained before-images could not be removed; they are kept for a later run: see $HOME/.local/state/ciaobot/server-host-upgrade.json"
    fi
}

native_host_recover() {
    # A journal a previous installer left. Its owner still running is a refusal -
    # this run must not roll back a live operation between another shell's
    # subprocess calls. An abandoned one is rolled back from its own material
    # before this run touches the host. The previous run's flags are unknown, so
    # any loaded engine job is conservatively quiesced first: the disk may already
    # be the candidate.
    if ! native_host_status > "$tmp/native-host-status"; then
        fail "the Ciaobot Server host upgrade state cannot be read safely; the Ciaobot engine and its service have not been changed"
    fi
    if ! read_native_host_status "$tmp/native-host-status"; then
        fail "the Ciaobot Server host upgrade state is malformed; the Ciaobot engine and its service have not been changed"
    fi
    if [ "$native_status_pending" -eq 0 ]; then
        return 0
    fi
    if [ "$native_status_owner_alive" -eq 1 ] &&
        [ "$native_status_owner_pid" != "$$" ]; then
        fail "installer process $native_status_owner_pid is still upgrading the Ciaobot Server host; wait for it to finish and re-run"
    fi
    echo "Ciaobot engine installer: a previous Ciaobot Server host upgrade did not finish; restoring the previous host first." >&2
    native_host_pending=1
    native_host_operation=$native_status_operation
    native_host_service_started=1
    if ! native_host_rollback; then
        fail "the previous Ciaobot Server host upgrade could not be recovered; nothing new was installed"
    fi
    # That recovery used this run's one rollback attempt. The operation this run
    # is about to start gets its own.
    native_host_rollback_tried=0
    # The conservative quiesce may have unloaded the job. Reload what is on disk;
    # a later step of this run registers the definition it needs anyway, and a
    # label launchd still holds is an error `bootstrap` reports, which is ignored.
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" 2>/dev/null || true
    fi
    native_host_service_started=0
}

native_host_begin() {
    # The journal this very run just created. A status query that cannot be read
    # here is fatal: continuing would install an engine over an operation whose
    # state this run does not know.
    if ! native_host_status > "$tmp/native-host-status"; then
        fail "the Ciaobot Server host upgrade state cannot be read safely after installing the host"
    fi
    if ! read_native_host_status "$tmp/native-host-status"; then
        fail "the Ciaobot Server host upgrade state is malformed after installing the host"
    fi
    if [ "$native_status_pending" -eq 0 ]; then
        return 0
    fi
    if [ "$native_status_owner_pid" != "$$" ]; then
        fail "another installer process ($native_status_owner_pid) owns the Ciaobot Server host upgrade this run started"
    fi
    native_host_pending=1
    native_host_operation=$native_status_operation
    # A fresh operation, so the one rollback attempt applies to it, and the
    # bookkeeping is now complete: the EXIT handler no longer has to discover it.
    native_host_rollback_tried=0
    native_host_install_started=0
}

native_host_job_state() {
    # What launchd says about the engine job:
    #   0 = it holds the job (running or not),
    #   1 = it confirms it has no such job,
    #   2 = the query itself failed - a permission or operational error, a missing
    #       tool, or an answer this installer cannot read as "absent".
    # Only 1 is proof the job is gone; everything else is uncertainty.
    if [ -z "${LAUNCHCTL:-}" ]; then
        return 2
    fi
    : > "$tmp/native-host-print-err"
    native_host_print_rc=0
    "$LAUNCHCTL" print "gui/$(id -u)/$SERVER_LABEL" \
        > "$tmp/native-host-print" 2> "$tmp/native-host-print-err" \
        || native_host_print_rc=$?
    if [ "$native_host_print_rc" -eq 0 ]; then
        return 0
    fi
    # launchd's documented "no such service" answer, by status or by its own text.
    if [ "$native_host_print_rc" -eq 113 ] ||
        grep -qF "Could not find service" "$tmp/native-host-print-err" 2>/dev/null; then
        return 1
    fi
    return 2
}

native_host_job_pid() {
    # The pid launchd reports for the engine job, or nothing at all. The caller
    # checks `native_host_job_state` first, so an absent pid is "no process yet".
    sed -n 's/^[[:space:]]*pid = \([0-9][0-9]*\)$/\1/p' "$tmp/native-host-print" \
        | head -1
}

native_host_listener_pid() {
    # <port>: the one pid holding the listening socket, or nothing. A lookup that
    # cannot answer, no listener, and more than one listener are all "not proven".
    if [ -z "${LSOF:-}" ]; then
        return 1
    fi
    native_host_listener_rc=0
    "$LSOF" -nP -iTCP:"$1" -sTCP:LISTEN -t \
        > "$tmp/native-host-listeners" 2>/dev/null || native_host_listener_rc=$?
    if [ "$native_host_listener_rc" -ne 0 ]; then
        return 1
    fi
    if [ "$(wc -l < "$tmp/native-host-listeners" | tr -d ' ')" != 1 ]; then
        return 1
    fi
    native_host_listener=$(tr -d ' \n' < "$tmp/native-host-listeners")
    case "$native_host_listener" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$native_host_listener"
}

native_host_read_lock() {
    # The engine's own identity for this install's runtime root: pid and the
    # startup_id it writes fresh for every run. Read with the authenticated tool
    # interpreter, through a descriptor rather than the name: the lock must be a
    # regular file this user owns with mode 0600, opened without following a link,
    # and the opened inode must be the one the directory listed - an unsafe or
    # swapped lock is rejected, because readable metadata is not proof of anything.
    # Prints `startup_id=...` and `pid=...`. Exit codes say *why* on failure:
    # 3 = present but unsafe (link, owner, mode, swapped inode), 2 = absent,
    # unreadable or naming another workspace/port. The caller turns that into the
    # step it reports.
    [ -x "${tool_python:-}" ] || return 2
    [ -n "${workspace:-}" ] || return 2
    native_host_lock_out="$tmp/native-host-lock"
    native_host_lock_rc=0
    "$tool_python" -I -c '
import json
import os
import stat
import sys

lock_path, workspace, runtime_root, port = sys.argv[1:5]
try:
    checked = os.lstat(lock_path)
except OSError:
    raise SystemExit(2)
if stat.S_ISLNK(checked.st_mode) or not stat.S_ISREG(checked.st_mode):
    raise SystemExit(3)
if hasattr(os, "getuid") and checked.st_uid != os.getuid():
    raise SystemExit(3)
if stat.S_IMODE(checked.st_mode) != 0o600:
    raise SystemExit(3)
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
try:
    descriptor = os.open(lock_path, flags)
except OSError:
    raise SystemExit(2)
try:
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (checked.st_dev, checked.st_ino):
        raise SystemExit(3)
    with os.fdopen(descriptor, "rb") as handle:
        descriptor = -1
        raw = handle.read()
finally:
    if descriptor >= 0:
        os.close(descriptor)
try:
    owner = json.loads(raw.decode("utf-8"))
except (ValueError, UnicodeDecodeError):
    raise SystemExit(2)
if not isinstance(owner, dict):
    raise SystemExit(2)
if owner.get("status") != "running":
    raise SystemExit(2)
startup_id = owner.get("startup_id")
pid = owner.get("pid")
if not isinstance(startup_id, str) or not startup_id:
    raise SystemExit(2)
if type(pid) is not int or pid <= 0:
    raise SystemExit(2)
if owner.get("port") is not None and owner.get("port") != int(port):
    raise SystemExit(2)
if owner.get("workspace") is not None and owner.get("workspace") != workspace:
    raise SystemExit(2)
if owner.get("runtime") is not None and owner.get("runtime") != runtime_root:
    raise SystemExit(2)
print("startup_id=" + startup_id)
print("pid=%d" % pid)
' "$workspace/.runtime/server.lock" "$workspace" "$workspace/.runtime" "$port" \
        > "$native_host_lock_out" 2>/dev/null || native_host_lock_rc=$?
    case "$native_host_lock_rc" in
        0) ;;
        3) return 3 ;;
        *) return 2 ;;
    esac
    cat "$native_host_lock_out"
}

native_host_listener_pid_wait() {
    # <port>: the one pid holding the listening socket, waiting briefly for it to
    # appear. Exactly one listener on every attempt; none, several, or a lookup that
    # cannot answer is not a listener this install can name.
    native_host_wait_attempt=0
    while [ "$native_host_wait_attempt" -lt 3 ]; do
        native_host_wait_rc=0
        native_host_wait_pid=$(native_host_listener_pid "$1") || native_host_wait_rc=$?
        if [ "$native_host_wait_rc" -eq 0 ] && [ -n "$native_host_wait_pid" ]; then
            printf '%s\n' "$native_host_wait_pid"
            return 0
        fi
        native_host_wait_attempt=$((native_host_wait_attempt + 1))
        [ "$native_host_wait_attempt" -ge 3 ] || "$SLEEP" 1
    done
    return 1
}

native_host_capture_pre_responder() {
    # The responder on the configured port before this run starts an engine, kept
    # only as evidence: a successful start has to be a *different* engine run, so an
    # answer that was already there is recorded and must not be the one that passes
    # later. Best effort; an absent answer records nothing.
    if [ -z "${port:-}" ]; then
        return 0
    fi
    : > "$tmp/pre-identity"
    # The marker tells the probe apart from the port availability check that also
    # asks this endpoint before anything is started; both are single-shot.
    : > "$tmp/pre-responder-probe"
    "$CURL" -fsS "http://localhost:$port/api/startup-status" 2>/dev/null \
        | sed -n 's/.*"startup_id":[[:space:]]*"\([^"]*\)".*/\1/p' \
        | head -1 > "$tmp/pre-identity" || true
    rm -f "$tmp/pre-responder-probe"
}

native_host_ancestor_pids() {
    # <pid>: the pid and up to four ancestors, one per line. Any unreadable answer
    # stops the walk with a failure: an unknown chain is not a proven chain.
    native_host_walk_pid=$1
    native_host_walk_hops=0
    case "$native_host_walk_pid" in
        ''|*[!0-9]*) return 1 ;;
    esac
    printf '%s\n' "$native_host_walk_pid"
    while [ "$native_host_walk_hops" -lt 4 ]; do
        native_host_walk_rc=0
        native_host_walk_ppid=$(
            "$PS" -o ppid= -p "$native_host_walk_pid" 2>/dev/null
        ) || native_host_walk_rc=$?
        if [ "$native_host_walk_rc" -ne 0 ]; then
            return 1
        fi
        native_host_walk_ppid=$(printf '%s' "$native_host_walk_ppid" | tr -d ' ')
        case "$native_host_walk_ppid" in
            ''|*[!0-9]*) return 1 ;;
        esac
        if [ "$native_host_walk_ppid" -le 1 ]; then
            # launchd (or init) is the top of the chain: it is complete.
            return 0
        fi
        printf '%s\n' "$native_host_walk_ppid"
        native_host_walk_pid=$native_host_walk_ppid
        native_host_walk_hops=$((native_host_walk_hops + 1))
    done
    return 0
}

native_host_probe_candidate() {
    # <port> <expected version>: 0 only when the engine that answered is the process
    # instance this install started. Every step is bound to an object, never to a
    # readable string:
    #   1. exactly one listener on the port before the request, captured by pid;
    #   2. the request itself, carrying the exact expected version;
    #   3. exactly one listener after the request, and it is the same pid - so an
    #      unrelated listener cannot answer and exit between the two;
    #   4. that pid is the pid in this runtime root's lock, and the reported
    #      startup_id is the lock's - and the lock itself is a private regular file,
    #      opened no-follow, whose opened inode is the one listed;
    #   5. the listener's bounded ancestor chain (four hops) reaches the pid launchd
    #      reports for the job, and the kernel says that job process' executable is
    #      the installed `Ciaobot Server` - launchd runs the host, which
    #      posix_spawns Python, so the job pid is never the socket holder. The
    #      answer comes from libproc `proc_pidpath` in the verified wheel's module,
    #      not from argv[0], `ps`'s comm, or an `lsof -d txt` record: those name a
    #      string the process chose or every mapped executable segment, and on
    #      macOS `lsof` reports many such segments for one signed host.
    # `native_host_unproven` names the step that failed, so a timeout gives the
    # operator a direction instead of "it did not come up".
    native_host_probe_port=$1
    native_host_probe_version=$2
    native_host_unproven=""
    native_host_listener_before=$(native_host_listener_pid_wait "$native_host_probe_port") || {
        native_host_unproven="exactly one listener on port $native_host_probe_port before the request"
        return 1
    }
    native_host_probe_body=$(
        "$CURL" -fsS "http://localhost:$native_host_probe_port/api/startup-status" \
            2>/dev/null || true
    )
    if [ -z "$native_host_probe_body" ]; then
        native_host_unproven="a health response from port $native_host_probe_port"
        return 1
    fi
    native_host_probe_reported=$(
        printf '%s\n' "$native_host_probe_body" \
            | sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
    )
    if [ "$native_host_probe_reported" != "$native_host_probe_version" ]; then
        native_host_unproven="version $native_host_probe_version from the responder (saw ${native_host_probe_reported:-nothing})"
        return 1
    fi
    native_host_probe_startup_id=$(
        printf '%s\n' "$native_host_probe_body" \
            | sed -n 's/.*"startup_id":[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
    )
    if [ -z "$native_host_probe_startup_id" ]; then
        native_host_unproven="a startup identity in the health response"
        return 1
    fi
    native_host_listener_after=$(native_host_listener_pid_wait "$native_host_probe_port") || {
        native_host_unproven="exactly one listener on port $native_host_probe_port after the request"
        return 1
    }
    if [ "$native_host_listener_after" != "$native_host_listener_before" ]; then
        native_host_unproven="one listener across the request (it was replaced: $native_host_listener_before then $native_host_listener_after)"
        return 1
    fi
    native_host_lock_rc=0
    native_host_lock=$(native_host_read_lock) || native_host_lock_rc=$?
    if [ "$native_host_lock_rc" -ne 0 ]; then
        if [ "$native_host_lock_rc" -eq 3 ]; then
            native_host_unproven="a private 0600 runtime lock owned by this user"
        else
            native_host_unproven="a readable runtime lock for this runtime root"
        fi
        return 1
    fi
    native_host_lock_startup_id=$(
        printf '%s\n' "$native_host_lock" | sed -n 's/^startup_id=//p' | head -1
    )
    native_host_lock_pid=$(
        printf '%s\n' "$native_host_lock" | sed -n 's/^pid=//p' | head -1
    )
    if [ -z "$native_host_lock_startup_id" ] || [ -z "$native_host_lock_pid" ]; then
        native_host_unproven="a runtime lock naming the engine's pid and startup id"
        return 1
    fi
    if [ "$native_host_lock_pid" != "$native_host_listener_after" ]; then
        native_host_unproven="the lock's pid ($native_host_lock_pid) owning the listener ($native_host_listener_after)"
        return 1
    fi
    if [ "$native_host_lock_startup_id" != "$native_host_probe_startup_id" ]; then
        native_host_unproven="the lock's startup id matching the responder"
        return 1
    fi
    native_host_pre=$(cat "$tmp/pre-identity" 2>/dev/null || true)
    if [ -n "$native_host_pre" ] &&
        [ "$native_host_pre" = "$native_host_lock_startup_id" ]; then
        native_host_unproven="an engine run newer than the one that answered before this run started it"
        return 1
    fi
    native_host_job_rc=0
    native_host_job_state || native_host_job_rc=$?
    if [ "$native_host_job_rc" -ne 0 ]; then
        native_host_unproven="launchd reporting the $SERVER_LABEL job"
        return 1
    fi
    native_host_launchd_pid=$(native_host_job_pid)
    if [ -z "$native_host_launchd_pid" ]; then
        native_host_unproven="a running pid in launchd's report for $SERVER_LABEL"
        return 1
    fi
    if [ "$native_host_launchd_pid" = "$native_host_listener_after" ]; then
        native_host_unproven="a listener that is the launched host's child, not the job itself"
        return 1
    fi
    native_host_chain=$(native_host_ancestor_pids "$native_host_listener_after") || {
        native_host_unproven="a readable process ancestry from the listener"
        return 1
    }
    if ! printf '%s\n' "$native_host_chain" | grep -qx "$native_host_launchd_pid"; then
        native_host_unproven="an ancestry from the listener reaching launchd's pid $native_host_launchd_pid within four hops"
        return 1
    fi
    native_host_executable="$HOME/Applications/Ciaobot Server.app/Contents/MacOS/Ciaobot Server"
    native_host_identity_rc=0
    native_host_run executable --pid "$native_host_launchd_pid" \
        --expect "$native_host_executable" \
        >/dev/null 2>/dev/null || native_host_identity_rc=$?
    if [ "$native_host_identity_rc" -ne 0 ]; then
        native_host_unproven="the kernel executable identity of launchd's pid $native_host_launchd_pid (it does not prove the installed Ciaobot Server)"
        return 1
    fi
    return 0
}

native_host_adopt() {
    # Bind the operation this very shell owns, from the journal itself. The child
    # can finish the durable install before `native_host_begin` records anything, so
    # an exit in that window would otherwise leave the operation unrecovered. The
    # module's status is the only authority: a pending operation whose owner pid is
    # this shell, bound by its nonce. A status that cannot be read, or that belongs
    # to another process, leaves the journal untouched and says so - an unreadable
    # state is never "nothing to do".
    if ! native_host_status > "$tmp/native-host-adopt"; then
        echo "Ciaobot engine installer: this run's Ciaobot Server host upgrade state could not be read, so it was NOT rolled back. Its recoverable material is kept: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
        return 1
    fi
    if ! read_native_host_status "$tmp/native-host-adopt"; then
        echo "Ciaobot engine installer: this run's Ciaobot Server host upgrade state is malformed, so it was NOT rolled back. Its recoverable material is kept: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
        return 1
    fi
    if [ "$native_status_pending" -eq 0 ]; then
        return 0
    fi
    if [ "$native_status_owner_pid" != "$$" ]; then
        return 0
    fi
    native_host_pending=1
    native_host_operation=$native_status_operation
    native_host_rollback_tried=0
    # The previous run's flags are unknown, so quiesce conservatively: booting out a
    # job that was never started is a no-op, and skipping it when it was started is
    # how a candidate gets replaced under a running process.
    native_host_service_started=1
    return 0
}

cleanup_on_exit() {
    # The one place a failed or interrupted run gives the old host back, whatever
    # ended it: an explicit `fail`, an unexpected `set -e` exit, a signal (which
    # exits and so runs this trap) or the end of the script. The rollback happens
    # before `$tmp` and the wheel are deleted, because the module that performs it
    # is run out of that wheel. The original status is preserved.
    cleanup_status=$?
    if [ "$native_host_pending" -eq 0 ] && [ "$native_host_install_started" -ne 0 ]; then
        # The child can finish the durable install before `native_host_begin` has
        # recorded the operation - its status query, its own failure, or a signal
        # can land in that window. So bind the operation this shell owns from the
        # journal itself: the owner pid and the nonce are the proof, and nothing is
        # rolled back unless this process is the owner.
        # `set -e` would abort the trap on a failed discovery and lose both the
        # saved status and the temporary cleanup, so the failure is handled here.
        native_host_adopt || true
    fi
    if [ "$native_host_pending" -ne 0 ]; then
        native_host_rollback || true
    fi
    rm -rf "$tmp"
    exit "$cleanup_status"
}

fail() {
    echo "Ciaobot engine installer: $*" >&2
    # The host first: the engine rollback below reloads launchd, and reverting the
    # host identity is only safe once its candidate service is gone.
    native_host_rollback || true
    exit 1
}

quote_shell_path() {
    quoted_path=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
    printf "'%s'" "$quoted_path"
}

configure_path() {
    path_shell=${SHELL##*/}
    path_profile=
    path_manual="export PATH=$(quote_shell_path "$bin_dir"):\"\$PATH\""
    case "$path_shell" in
        zsh)
            path_profile=${ZDOTDIR:-$HOME}/.zshrc
            path_line='case ":${PATH:-}:" in *:'"$(quote_shell_path "$bin_dir")"':*) ;; *) export PATH='"$(quote_shell_path "$bin_dir")"':"$PATH" ;; esac'
            ;;
        bash)
            path_profile=$HOME/.bash_profile
            for path_candidate in "$HOME/.bash_profile" "$HOME/.bash_login" "$HOME/.profile"; do
                if [ -e "$path_candidate" ]; then path_profile=$path_candidate; break; fi
            done
            path_line='case ":${PATH:-}:" in *:'"$(quote_shell_path "$bin_dir")"':*) ;; *) export PATH='"$(quote_shell_path "$bin_dir")"':"$PATH" ;; esac'
            ;;
        fish)
            path_profile=${XDG_CONFIG_HOME:-$HOME/.config}/fish/config.fish
            path_line="fish_add_path $(quote_shell_path "$bin_dir")"
            path_manual=$path_line
            ;;
        *) path_profile= ;;
    esac
    if [ "$no_modify_path" -ne 0 ]; then
        echo "PATH was not modified. For this terminal run: $path_manual"
        return
    fi
    if [ -z "$path_profile" ]; then
        echo "Ciaobot installer could not update PATH automatically (unknown shell). For this terminal run: $path_manual"
        return
    fi
    case ":${PATH:-}:" in
        *:"$bin_dir":*)
            echo "The ciao executable directory is already on PATH; no shell startup file was changed."
            return
            ;;
    esac
    if ! mkdir -p "$(dirname "$path_profile")" 2>/dev/null; then
        echo "Ciaobot installer could not update PATH automatically. For this terminal run: $path_manual"
        return
    fi
    if [ -L "$path_profile" ] ||
        { [ -e "$path_profile" ] && { [ ! -f "$path_profile" ] || [ ! -w "$path_profile" ]; }; } ||
        { [ ! -e "$path_profile" ] && [ ! -w "$(dirname "$path_profile")" ]; }; then
        echo "Ciaobot installer could not update PATH automatically because $path_profile is not a safe writable file. For this terminal run: $path_manual"
        return
    fi
    if grep -qF '# >>> Ciaobot installer PATH >>>' "$path_profile" 2>/dev/null; then
        echo "Ciaobot installer PATH entry is already present in $path_profile."
        return
    fi
    if ! printf '\n# >>> Ciaobot installer PATH >>>\n%s\n# <<< Ciaobot installer PATH <<<\n' "$path_line" >> "$path_profile" 2>/dev/null; then
        echo "Ciaobot installer could not update PATH automatically. For this terminal run: $path_manual"
        return
    fi
    echo "Added ciao to future terminal PATHs in $path_profile. To undo, remove the Ciaobot installer PATH block from that file."
    echo "For this terminal run: $path_manual"
}

while [ "$#" -gt 0 ]; do
    case $1 in
        --version)
            [ "$#" -ge 2 ] || fail "--version requires a value"
            version=$2
            shift 2
            ;;
        --workspace)
            [ "$#" -ge 2 ] || fail "--workspace requires a value"
            workspace=$2
            shift 2
            ;;
        --no-start) no_start=1; shift ;;
        --no-modify-path) no_modify_path=1; shift ;;
        --migrate) migrate=1; shift ;;
        --as-host) as_host=1; shift ;;
        --as-client)
            [ "$#" -ge 2 ] || fail "--as-client requires a URL"
            as_client=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage; fail "unknown option: $1" ;;
    esac
done

# --as-host and --as-client answer a question only the migration asks, and
# they answer it in opposite directions: a URL is not a host, and a host is not
# a URL. Either without --migrate is a plain typo, not a silent no-op. The
# scheme check here is a fast answer for an obvious typo, before anything is
# downloaded; whether the value is an *address* is asked of the verified wheel
# later, in validate_client_url, before anything on this Mac is touched.
case "$as_client" in
    http://*|https://*) ;;
    '') ;;
    *) fail "--as-client takes an http:// or https:// URL" ;;
esac
if [ "$as_host" -ne 0 ] && [ -n "$as_client" ]; then
    fail "--as-host and --as-client are mutually exclusive"
fi
if [ "$migrate" -eq 0 ] && { [ "$as_host" -ne 0 ] || [ -n "$as_client" ]; }; then
    fail "--as-host and --as-client require --migrate"
fi

for command in curl mktemp uname awk sw_vers; do
    command -v "$command" >/dev/null 2>&1 || fail "required command not found: $command"
done
[ -x "$TAR" ] || fail "required command not found: $TAR"
[ -x "$STAT" ] || fail "required command not found: $STAT"
[ -x "$SHASUM" ] || fail "required command not found: $SHASUM"

[ "$(uname -s)" = "Darwin" ] || fail "this installer supports macOS; Linux servers use docs/LINUX.md"

major=$(sw_vers -productVersion | awk -F. '{print $1}')
case "$major" in
    ''|*[!0-9]*) fail "could not determine the macOS version" ;;
esac
[ "$major" -ge 13 ] || fail "the Ciaobot engine requires macOS 13 or newer"

case "$version" in
    '')
        [ -z "${CIAO_RELEASE_BASE_URL:-}" ] || fail "--version is required with CIAO_RELEASE_BASE_URL"
        latest_url=$("$CURL" -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$repo/releases/latest")
        version=${latest_url##*/}
        version=${version#v}
        case "$version" in
            ''|*[!0-9A-Za-z.-]*) fail "could not determine the latest release version" ;;
        esac
        ;;
    *) version=${version#v} ;;
esac
# The version reaches both a download URL and the manifest verifier, so reject
# anything that is not a plain release tag - in the v-prefixed spelling the
# release uses and in the plain one the rest of this script carries.
case "v$version" in
    *[!0-9A-Za-z.-]*) fail "invalid version: $version" ;;
esac
case "$version" in
    *[!0-9A-Za-z.-]*) fail "invalid version: $version" ;;
esac
base="$release_base/v$version"

tmp=$(mktemp -d "${TMPDIR:-/tmp}/ciaobot-engine.XXXXXX")
chmod 700 "$tmp"
# One EXIT handler for every ending: it gives the previous server host back if
# this run left a native-host upgrade uncommitted, and only then deletes the
# wheel it needs to do that. A signal handler that only cleaned up would fall
# through into the next step with the temp dir already deleted; `on_signal` exits
# and so runs this trap.
trap cleanup_on_exit EXIT
trap on_signal HUP INT TERM

download() {
    "$CURL" -fsSL --retry 3 --connect-timeout 15 "$1" -o "$2"
}

# The pinned uv release a missing uv is bootstrapped from. These are the six
# published sha256 values for that release's assets, reviewed by a maintainer
# and committed here: together with TLS to GitHub they are the whole trust root
# for the uv archive, which carries no minisign or Apple signature. Change
# UV_VERSION and this whole table as one review unit, and never fetch a
# checksum at install time - the reviewed literals are what authenticate it.
# At review time the two Darwin archives were downloaded from
# https://github.com/astral-sh/uv/releases/download/0.12.17/ and matched both the
# release's sha256.sum and the GitHub asset digests; each lists
# uv-<triple>/, uv-<triple>/uv and uv-<triple>/uvx.
uv_archive_sha256() {
    case "$1" in
        uv-aarch64-apple-darwin.tar.gz) printf '%s\n' 85f00cbdc6dd3e97eba4c31b4d014375a9fdfe8f570023b84e5102fc3456896b ;;
        uv-x86_64-apple-darwin.tar.gz) printf '%s\n' 8dcf05a8c809bb3c471d2b614788ba27a6e41298fc8c31ac84b5f4339fd468e5 ;;
        uv-x86_64-unknown-linux-gnu.tar.gz) printf '%s\n' fa82fd8dde8e8eefdecada6aa0889666556cfceb690d06e0c3bca49eb3070a63 ;;
        uv-aarch64-unknown-linux-gnu.tar.gz) printf '%s\n' d636d1b678e9e7f367ecb22b46bd1cabbed234d6bc3b4d96365d2b507f72f86c ;;
        uv-x86_64-unknown-linux-musl.tar.gz) printf '%s\n' 6401c4665d8fa2a9893e087c91f585430738e3170f5398a1141483efb4a93310 ;;
        uv-aarch64-unknown-linux-musl.tar.gz) printf '%s\n' a6096da273d548cb9f277d237a01ac7344a39ef0f455c0e148e4dc9737c1596b ;;
        *) fail "no reviewed sha256 for uv archive: $1" ;;
    esac
}

uv_archive_asset() {
    # The macOS guard above has already run, so only the two Darwin archives
    # are selectable here; an unknown machine refuses rather than guessing.
    case "$(uname -m)" in
        arm64|aarch64) printf '%s\n' uv-aarch64-apple-darwin.tar.gz ;;
        x86_64) printf '%s\n' uv-x86_64-apple-darwin.tar.gz ;;
        *) fail "no reviewed uv archive for this architecture: $(uname -m)" ;;
    esac
}

bootstrap_verified_uv() {
    # Download the pinned archive, authenticate it against the reviewed table,
    # then inspect it and extract only its exact `uv` member. Nothing from the
    # archive runs, and tar never creates a path the archive chose: that one
    # member is written to stdout into a file this function made, in a private
    # staging directory. It returns that private staged executable in
    # $verified_uv (and points $uv at it), and never touches ~/.local/bin: only
    # the legacy missing-uv path publishes it, and a native-mode install takes
    # $verified_uv instead of replacing a uv this Mac already has.
    uv_asset=$(uv_archive_asset)
    uv_expected=$(uv_archive_sha256 "$uv_asset")
    uv_archive="$tmp/$uv_asset"
    "$CURL" --proto '=https' --proto-redir '=https' -fsSL --retry 3 --connect-timeout 15 \
        "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$uv_asset" \
        -o "$uv_archive" || fail "could not download the uv archive"
    [ -f "$uv_archive" ] || fail "the downloaded uv archive is not a regular file"
    uv_digest=$("$SHASUM" -a 256 "$uv_archive") || fail "could not hash the uv archive"
    uv_actual=${uv_digest%% *}
    [ "$uv_actual" = "$uv_expected" ] \
        || fail "the uv archive does not match its reviewed sha256"

    # The archive's expected directory (optional: a tar producer may list only
    # the files), its `uv`, and optionally `uvx`. Names are a finite ASCII
    # allowlist, so an unusual name, a duplicate, an absolute path or a `..`
    # component is a refusal, not a parsing problem. At most one directory
    # entry is allowed; its absence is not evidence of anything, because the
    # exact `uv` member below is what is extracted.
    uv_dir=${uv_asset%.tar.gz}
    uv_member="$uv_dir/uv"
    uv_listing="$tmp/uv-archive.members"
    LC_ALL=C "$TAR" -P -tf "$uv_archive" > "$uv_listing" 2>/dev/null \
        || fail "could not list the uv archive"
    [ -s "$uv_listing" ] || fail "the uv archive has no members"
    uv_dirs=0
    uv_files=0
    uv_uvx=0
    while IFS= read -r uv_name; do
        case "$uv_name" in
            *[!A-Za-z0-9_./-]*) fail "the uv archive has an unsafe member name" ;;
        esac
        case "$uv_name" in
            /*) fail "the uv archive has an absolute member" ;;
        esac
        case "/$uv_name/" in
            */../*) fail "the uv archive has a traversing member" ;;
        esac
        case "$uv_name" in
            "$uv_dir/") uv_dirs=$((uv_dirs + 1)) ;;
            "$uv_member") uv_files=$((uv_files + 1)) ;;
            "$uv_dir/uvx") uv_uvx=$((uv_uvx + 1)) ;;
            *) fail "the uv archive has an unexpected member: $uv_name" ;;
        esac
    done < "$uv_listing"
    [ "$uv_dirs" -le 1 ] || fail "the uv archive must not contain more than one $uv_dir/ directory"
    [ "$uv_files" -eq 1 ] || fail "the uv archive must contain exactly one $uv_member"
    [ "$uv_uvx" -le 1 ] || fail "the uv archive has more than one uvx member"

    # A name listing does not say what an entry is - a symlink and a regular
    # file print the same name - so the type comes from the verbose listing and
    # is checked against the name it belongs to. Both listings must agree on
    # the member count, or the listing is ambiguous and nothing is extracted.
    uv_types="$tmp/uv-archive.types"
    LC_ALL=C "$TAR" -P -tvf "$uv_archive" > "$uv_types" 2>/dev/null \
        || fail "could not inspect the uv archive"
    [ "$(wc -l < "$uv_types")" -eq "$(wc -l < "$uv_listing")" ] \
        || fail "the uv archive listing is ambiguous"
    while IFS= read -r uv_line; do
        uv_rest=${uv_line#?}
        uv_type=${uv_line%"$uv_rest"}
        case "$uv_type" in
            -|d) ;;
            *) fail "the uv archive has a non-regular member" ;;
        esac
        case "$uv_line" in
            *" -> "*|*" link to "*) fail "the uv archive has a non-regular member" ;;
        esac
        uv_line_name=${uv_line##* }
        case "$uv_line_name" in
            "$uv_dir/")
                [ "$uv_type" = d ] || fail "the uv archive has an unexpected member type for $uv_dir/"
                ;;
            "$uv_member")
                [ "$uv_type" = - ] || fail "the uv archive has an unexpected member type for $uv_member"
                ;;
            "$uv_dir/uvx")
                [ "$uv_type" = - ] || fail "the uv archive has an unexpected member type for $uv_dir/uvx"
                ;;
            *) fail "the uv archive has an unexpected member: $uv_line_name" ;;
        esac
    done < "$uv_types"

    uv_stage="$tmp/uv-bootstrap"
    (umask 077 && mkdir "$uv_stage") || fail "could not create a private uv staging directory"
    chmod 700 "$uv_stage" || fail "could not protect the uv staging directory"
    uv_staged="$uv_stage/uv"
    if ! (set -C; LC_ALL=C "$TAR" -xOf "$uv_archive" "$uv_member" > "$uv_staged" 2>/dev/null); then
        fail "could not extract $uv_member from the uv archive"
    fi
    [ -f "$uv_staged" ] && [ ! -L "$uv_staged" ] \
        || fail "the extracted uv is not a regular file"
    [ -s "$uv_staged" ] || fail "the extracted uv is empty"
    chmod 700 "$uv_staged" || fail "could not protect the extracted uv"
    uv="$uv_staged"
    verified_uv="$uv_staged"
}

path_inode() {
    # The inode the path itself names, read with lstat so a final symlink is not
    # followed: a link planted at the destination reports its own inode and can
    # never pass as the file this run created.
    #
    # BSD `stat` takes a format with -f, GNU `stat` with -c, and trying one and
    # falling back to the other is not safe: GNU reads `-f` as filesystem status,
    # treats `%i` as one more operand, prints that status to stdout and only then
    # fails on the missing `%i`. A `||` fallback would run with that output
    # already on stdout, and a caller reading it would compare that status as if
    # it were an inode. The kernel name picks the spelling explicitly, before
    # anything is printed. This installer refuses non-Darwin hosts; the GNU
    # branch is the one the Linux test runner exercises.
    if [ "$(uname -s)" = Darwin ]; then
        "$STAT" -f '%i' "$1" 2>/dev/null
    else
        "$STAT" -c '%i' "$1" 2>/dev/null
    fi
}

descriptor_inode() {
    # The inode of the file an open descriptor names, not of the descriptor.
    # macOS resolves /dev/fd/N to the open file; on GNU, /dev/fd/N is a symlink
    # into /proc/self/fd, so -L is required or this would return the symlink's
    # own inode. The installer's own platform is macOS, where the fdesc
    # filesystem reports its own device, so callers compare inodes only and
    # never a device. That is sound because the descriptor is still open while
    # the comparison runs, which keeps the inode allocated and stops another
    # file from being created under the same number.
    if [ "$(uname -s)" = Darwin ]; then
        "$STAT" -f '%i' "$1" 2>/dev/null
    else
        "$STAT" -L -c '%i' "$1" 2>/dev/null
    fi
}

publish_verified_uv() {
    # Put the verified staged uv where the installer has always left it. Only
    # the legacy missing-uv path calls this; the bootstrap helper itself never
    # writes into ~/.local/bin, so it cannot replace an existing uv.
    #
    # The publish is one exclusive create straight at the destination: no
    # temporary and no rename. `set -C` has the shell open the path with O_EXCL,
    # so a destination that already exists - a regular file, a directory, a
    # symlink, a dangling symlink, a hardlink to a file elsewhere - fails the
    # open, which is what refuses it; the check above only gives that refusal a
    # clearer message for the shapes that are not a regular file, and a regular
    # file is left to the create, because a check would leave a window where the
    # create's own EEXIST does not. `mv` is not usable here: on BSD it follows a
    # destination that is a directory or a symlink to one and moves the file
    # inside, overwriting whatever already carries that name there. This branch
    # runs only when this Mac has no uv at all, so the destination does not
    # normally exist. It can still be a leftover from an interrupted earlier run:
    # the create leaves an empty 0600 file if the process dies before the verified
    # bytes are written. That file is never removed automatically.
    #
    # The subshell below opens that create and keeps the descriptor for the whole
    # publish: the bytes are written and the executable bit is set through it,
    # and the path is not named again until the last check. A symlink substituted
    # at the path is therefore never followed by the write or the chmod - it is
    # only ever detected, and the run then refuses and leaves it where it is.
    # Keeping the create in this subshell also confines a redirection error to
    # it, which matters because `exec` with a failed redirection is fatal to a
    # POSIX shell. The status codes 11-14 are what write_published_uv reports;
    # any other failure is the exclusive create refusing the path.
    #
    # Ownership of the destination is claimed only after the create has
    # returned and only once the descriptor's own inode has been read: a create
    # refused by somebody else's file leaves that file alone, and a failed write
    # takes back a file only while the path still lstat's to that descriptor's
    # inode. That check-then-remove is not atomic, so a same-user writer who
    # replaces the path in that window can still have their file unlinked; the
    # ordinary case, and every substitution that happens before the check, does
    # not touch their file.
    #
    # The residual this cannot close is the same-user writer: a process that can
    # already write ~/.local/bin can replace the path after the final check and
    # before the next run, and none of this is per-user isolation. The descriptor
    # makes the write and the chmod safe against substitution; it cannot make the
    # directory owner trustworthy.
    [ -n "$verified_uv" ] || fail "no verified uv to publish"
    uv_bin_dir="$HOME/.local/bin"
    mkdir -p "$uv_bin_dir" || fail "could not create $uv_bin_dir"
    uv_dest="$uv_bin_dir/uv"
    # Read once before the write, only to refuse the shapes that are not a
    # regular file with a message naming them. Anything regular is the create's
    # decision, not this check's.
    if [ -L "$uv_dest" ] || [ -d "$uv_dest" ] || { [ -e "$uv_dest" ] && [ ! -f "$uv_dest" ]; }; then
        fail "refusing to replace $uv_dest: it is not a regular file"
    fi
    uv_created=0
    uv_inode=
    uv_publish_status=0
    ( umask 077; set -C; exec 3> "$uv_dest" || exit 1; write_published_uv ) ||
        uv_publish_status=$?
    case "$uv_publish_status" in
        0) ;;
        11) fail "could not identify the published uv at $uv_dest" ;;
        12) fail "could not publish the verified uv at $uv_dest" ;;
        13) fail "could not protect $uv_dest" ;;
        14) fail "the published uv at $uv_dest changed while it was being published" ;;
        *)
            # The exclusive create did not return: the path was already taken,
            # or could not be opened at all. Either way nothing here wrote to it.
            if [ -e "$uv_dest" ] || [ -L "$uv_dest" ]; then
                fail "could not publish the verified uv at $uv_dest: it already exists. This installer never removes a file it did not create in this run. Inspect it first: ls -l \"$uv_dest\"; \"$SHASUM\" -a 256 \"$uv_dest\". Only if you are sure it is an empty or partial leftover from an interrupted uv bootstrap - for example mode 0600 and no bytes - remove it and re-run the installer. If it is a working uv, leave it where it is."
            fi
            fail "could not publish the verified uv at $uv_dest"
            ;;
    esac
    # Publishing is for the next run: $uv is deliberately left on $verified_uv,
    # the private staged copy. Reopening the published path here would let a
    # replacement of ~/.local/bin/uv after these checks decide which uv the rest
    # of this install executes.
}

write_published_uv() {
    # Runs only inside publish_verified_uv's subshell, with fd 3 the exclusive
    # create of the destination and close on exec not set. Everything it does
    # names either that descriptor or nothing, so the identity it records is the
    # file the create made, whatever later occupies the path.
    #
    # Returns 0 on success, and otherwise: 11 when the descriptor's inode cannot
    # be read, 12 when the write fails, 13 when the mode cannot be set, and 14
    # when the path no longer refers to the created file. Only 12 and 13 attempt
    # a cleanup, and discard_published_uv re-checks the path against the inode
    # before removing anything.
    uv_created=0
    uv_inode=
    uv_inode=$(descriptor_inode /dev/fd/3) || uv_inode=
    if [ -z "$uv_inode" ]; then
        # Without the inode there is no way to prove this file is ours later, so
        # the path is left alone. `stat` is a required tool, so this is a
        # last-resort branch, not a normal failure.
        return 11
    fi
    # From here the file is this run's: the open descriptor keeps its inode
    # allocated, so no other file can appear at the path under the same number.
    uv_created=1
    if ! cat "$verified_uv" >&3; then
        # A short file is not a usable uv and would block every later install;
        # take it back while the descriptor still proves it is this run's.
        discard_published_uv
        return 12
    fi
    if ! chmod 700 /dev/fd/3; then
        discard_published_uv
        return 13
    fi
    uv_inode_now=$(path_inode "$uv_dest") || uv_inode_now=
    if [ -L "$uv_dest" ] || [ -z "$uv_inode_now" ] || [ "$uv_inode_now" != "$uv_inode" ]; then
        # Refused, not repaired: the path is somebody else's now, and both the
        # write and the chmod already went to the descriptor, so that file was
        # neither overwritten nor made executable.
        return 14
    fi
    uv_created=0
}

discard_published_uv() {
    # Take back only the file this run's exclusive create made: called while the
    # publish still holds its descriptor, so uv_inode is that file's inode and
    # cannot have been reused. The path is checked with lstat and has to be a
    # regular file with that inode; a symlink, a foreign regular file or a
    # directory substituted in between is left where the writer put it.
    #
    # That check and the `rm` are two syscalls, not one: a writer running as this
    # same user can replace the path in between, and then `rm` unlinks their file.
    # This narrows the window; it does not close it, and no shell loop can.
    if [ "$uv_created" -eq 1 ] && [ -n "$uv_inode" ] && [ -f "$uv_dest" ] && [ ! -L "$uv_dest" ] &&
        [ "$(path_inode "$uv_dest")" = "$uv_inode" ]; then
        rm -f "$uv_dest" 2>/dev/null || true
    fi
}

find_uv() {
    # No `local` anywhere in this script: every function writes globals on
    # purpose, so the value found here is what the steps below use.
    if command -v uv >/dev/null 2>&1; then
        uv=$(command -v uv)
    elif [ -x "$HOME/.local/bin/uv" ]; then
        uv="$HOME/.local/bin/uv"
    else
        # No uv at all: bootstrap the pinned, digest-verified archive and put
        # it where this installer has always left uv. The caller's shell
        # profile is left alone until the verified engine install completes.
        # `$uv` stays on the private staged copy the bootstrap made; publishing
        # it is for the next run and does not hand this one a writable path.
        bootstrap_verified_uv
        publish_verified_uv
    fi
    [ -x "$uv" ] || fail "uv is not available at $uv"
}

refuse_desktop_engine() {
    # Ciaobot.app owns the engine through a plist whose program arguments
    # point inside the bundle. Two launch agents must never fight over
    # com.ciao.server, so a live desktop engine is refused - unless --migrate
    # was asked for, which is the one path that takes the hand-over on (#576).
    plist="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    if [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
        program=$("$PLISTBUDDY" -c 'Print :ProgramArguments:0' "$plist" 2>/dev/null || true)
        case "$program" in
            *.app/*)
                # The native host this installer installs is itself an .app
                # (`Ciaobot Server.app`). The retired desktop app is the only
                # bundle two agents must not share `com.ciao.server` over.
                # Matching every `.app` refuses the update that re-runs this
                # script after the host is already the LaunchAgent (#1141).
                bundle=${program%%.app/*}.app
                bundle_name=${bundle##*/}
                if [ "$bundle_name" = "Ciaobot Server.app" ]; then
                    :
                elif [ -e "$program" ]; then
                    desktop_live=1
                    if [ "$migrate" -eq 0 ]; then
                        fail "Ciaobot.app manages the engine on this Mac (#576); to move it to the terminal engine, re-run with --migrate"
                    fi
                fi
                ;;
        esac
    fi
    # The desktop installer writes that shim before onboarding creates the
    # plist, so in that window the plist check above sees nothing while a live
    # Ciaobot.app still owns the engine. A shim whose target is gone (the app
    # was deleted) is stale and this script may replace it.
    shim="$HOME/.local/bin/ciao"
    if [ -f "$shim" ] && grep -qF "$SHIM_MARKER" "$shim" 2>/dev/null; then
        shim_target=$(awk -F'"' '/^exec "/ {print $2; exit}' "$shim" 2>/dev/null || true)
        if [ -n "$shim_target" ] && [ -x "$shim_target" ]; then
            desktop_live=1
            if [ "$migrate" -eq 0 ]; then
                fail "Ciaobot.app manages the engine on this Mac (#576); to move it to the terminal engine, re-run with --migrate"
            fi
        fi
    fi
}

read_manifest() {
    # Verifies the manifest signature with the embedded key, then the schema,
    # the requested version and the single wheel entry, and prints that entry
    # as "<filename> <sha256> <size>". Nothing outside this heredoc is
    # trusted, and nothing from the manifest is ever executed.
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$CRYPTOGRAPHY_PIN" \
        python - "$tmp/ciaobot-engine-manifest.json" "$tmp/ciaobot-engine-manifest.json.sig" "$version" "$RELEASE_PUBLIC_KEY" <<'PY'
# --- embedded verifier (keep in sync with ciao/release_manifest.py) ---
import base64, hashlib, json, sys
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

manifest_path, sig_path, expected_version, public_key = sys.argv[1:5]
def die(msg):
    print(msg, file=sys.stderr); sys.exit(1)
raw_key = base64.b64decode(public_key.strip().splitlines()[-1], validate=True)
if len(raw_key) != 42 or raw_key[:2] != b"Ed": die("bad embedded public key")
key_id, key = raw_key[2:10], raw_key[10:]
text = open(sig_path, encoding="utf-8").read().strip()
if not text.startswith("untrusted comment:"):
    text = base64.b64decode(text, validate=True).decode("utf-8")
lines = [l.rstrip("\r") for l in text.splitlines() if l.strip()]
if len(lines) != 4 or not lines[0].startswith("untrusted comment:") or not lines[2].startswith("trusted comment: "):
    die("malformed manifest signature")
blob = base64.b64decode(lines[1], validate=True)
if len(blob) != 74: die("malformed manifest signature")
alg, sig_key_id, signature = blob[:2], blob[2:10], blob[10:]
if sig_key_id != key_id: die("manifest signed by a different key")
data = open(manifest_path, "rb").read()
if alg == b"ED": message = hashlib.blake2b(data, digest_size=64).digest()
elif alg == b"Ed": message = data
else: die("unsupported signature algorithm")
trusted = lines[2][len("trusted comment: "):]
pk = Ed25519PublicKey.from_public_bytes(key)
try:
    pk.verify(signature, message)
    pk.verify(base64.b64decode(lines[3], validate=True), signature + trusted.encode("utf-8"))
except InvalidSignature:
    die("manifest signature does not match")
manifest = json.loads(data)
if manifest.get("schema") != 1 or manifest.get("version") != expected_version:
    die("manifest is not for version " + expected_version)
wheels = [a for a in manifest.get("artifacts", []) if a.get("kind") == "wheel"]
if len(wheels) != 1: die("manifest must list exactly one wheel")
w = wheels[0]
name, digest, size = str(w.get("filename", "")), str(w.get("sha256", "")), w.get("size")
if "/" in name or "\\" in name or not name.endswith(".whl") or len(digest) != 64 or not isinstance(size, int):
    die("manifest wheel entry is malformed")
print(name, digest, size)
PY
}

copy_file() {
    # `cp -p` where it exists, plain `cp` where it does not: the before-images
    # want their mode and timestamps kept, but a copy that preserves nothing is
    # still better than no backup at all.
    #
    # `cp -p src link` follows `link` and overwrites its target, so restoring a
    # before-image over a destination a migration replaced with a symlink (the
    # uv tool entry point at ~/.local/bin/ciao) would write the app shim
    # through the link and then delete the tool env it points at, leaving a
    # dangling `ciao` and no engine. The destination must become the regular
    # file the before-image holds, so a symlink is removed first.
    if [ -L "$2" ]; then
        rm -f "$2" || return 1
    fi
    cp -p "$1" "$2" 2>/dev/null || cp "$1" "$2"
}

snapshot_file() {
    # <source> <name>: copy `source` into the immutable before-image directory
    # and print the path recorded for it - or print nothing, meaning "this was
    # not here". Absence is written down rather than left out, so a rollback
    # knows the difference between "restore this" and "take away what the
    # migration created", and never invents a file the user never had.
    [ -f "$1" ] || return 0
    copy_file "$1" "$migration_dir/before/$2" || return 1
    printf '%s\n' "$migration_dir/before/$2"
}

snapshot_tool_env() {
    # The uv tool environment a forced reinstall is about to replace, or
    # nothing at all when there is none - which is the common hand-over from
    # Ciaobot.app, since the app does not install the engine as a uv tool. A
    # rollback then removes the tool environment this transaction created
    # instead of restoring one. Copying a real engine environment is expensive,
    # so it is only copied when one is already there.
    [ -n "$tool_dir" ] || return 0
    [ -d "$tool_dir/ciaobot" ] || return 0
    rm -rf "$migration_dir/before/tool-env"
    cp -pR "$tool_dir/ciaobot" "$migration_dir/before/tool-env" || return 1
    printf '%s\n' "$migration_dir/before/tool-env"
}

backup_before() {
    # Everything this migration replaces, copied aside before the first of them
    # is written, and kept for the whole transaction: the originals are the only
    # way back to the engine that is running now. Nothing under the workspace or
    # the runtime root is ever copied or touched: that data is what the
    # migration is preserving in place.
    mkdir -p "$migration_dir/before"
    chmod 700 "$migration_dir" "$migration_dir/before"
    if [ "$receipt_valid" -ne 0 ]; then
        # A receipt that parsed, whose schema is this script's and whose
        # before-images are all still on disk describes a transaction already in
        # progress. Its snapshots *are* the originals, so they are reused as
        # they are: re-taking them now would copy the uv entry point and the
        # rewritten plist over the desktop shim, and after that a rollback
        # could not bring the app's engine back.
        before_server_plist=$receipt_before_server_plist
        before_desktop_plist=$receipt_before_desktop_plist
        before_shim=$receipt_before_shim
        before_install_receipt=$receipt_before_install_receipt
        before_tool_env=$receipt_before_tool_env
        migration_started_at=$receipt_started_at
        return 0
    fi
    before_server_plist=$(snapshot_file \
        "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" "$SERVER_LABEL.plist") \
        || fail "could not copy $migration_dir/before/$SERVER_LABEL.plist aside; the migration has not started and the Ciaobot engine is unchanged"
    before_desktop_plist=$(snapshot_file \
        "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" "$DESKTOP_LABEL.plist") \
        || fail "could not copy $migration_dir/before/$DESKTOP_LABEL.plist aside; the migration has not started and the Ciaobot engine is unchanged"
    before_shim=$(snapshot_file "$HOME/.local/bin/ciao" ciao) \
        || fail "could not copy $migration_dir/before/ciao aside; the migration has not started and the Ciaobot engine is unchanged"
    before_install_receipt=$(snapshot_file "$install_receipt" install-receipt.json) \
        || fail "could not copy $migration_dir/before/install-receipt.json aside; the migration has not started and the Ciaobot engine is unchanged"
    before_tool_env=$(snapshot_tool_env) \
        || fail "could not copy $migration_dir/before/tool-env aside; the migration has not started and the Ciaobot engine is unchanged"
    migration_started_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
}

migration_receipt() {
    # Rewritten at every phase, so a migration that dies mid-flight says how far
    # it got. 0600 in a 0700 directory, written through a temp file so a reader
    # never sees half a receipt. Returns non-zero instead of exiting: a receipt
    # written after the tool is installed is a post-install failure like any
    # other, and the caller has to roll back rather than call `fail` from here.
    migration_phase=$1
    migration_error=${2:-}
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# migration-receipt-write: writes ~/.local/state/ciaobot/migration/receipt.json
import json, os, pathlib, sys, tempfile
phase, kind, workspace, host_url, error, started_at, path, version = sys.argv[1:9]
server_plist, desktop_plist, shim, install_receipt, tool_env = sys.argv[9:14]
retiring_desktop = sys.argv[14]
app_bundle = sys.argv[15]
payload = {
    "schema": 1,
    "kind": kind,
    "phase": phase,
    "workspace": workspace,
    "host_url": host_url,
    "version": version,
    # The bundle this hand-over is retiring, recorded for the same reason kind
    # and workspace are: a run that resumes this transaction can no longer learn
    # it from the classifier, because the plist it would read now points at the
    # tool this transaction installed. Empty for a receipt written before this
    # field existed, which a resumed run reads as "unknown" rather than as an
    # absence.
    "app_bundle": app_bundle,
    # Whether the app agent has been, or is being, taken out of launchd. A
    # rollback that stopped caring about this leaves a Mac whose desktop engine
    # never starts again while it reports the engine restored.
    "retiring_desktop": retiring_desktop == "1",
    "before": {
        "server_plist": server_plist,
        "desktop_plist": desktop_plist,
        "shim": shim,
        "install_receipt": install_receipt,
        "tool_env": tool_env,
    },
    "started_at": started_at,
}
if error:
    payload["error"] = error
target = pathlib.Path(path)
target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(target.parent, 0o700)
handle, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".receipt.")
with os.fdopen(handle, "w", encoding="utf-8") as stream:
    json.dump(payload, stream, indent=2, sort_keys=True)
    stream.write("\n")
os.chmod(tmp, 0o600)
os.replace(tmp, target)
' "$migration_phase" "$migrate_kind" "$migrate_workspace" "$migrate_host_url" \
        "$migration_error" "$migration_started_at" "$migration_dir/receipt.json" \
        "$version" \
        "${before_server_plist:-}" "${before_desktop_plist:-}" "${before_shim:-}" \
        "${before_install_receipt:-}" "${before_tool_env:-}" "$retiring_desktop" \
        "$migrate_app_bundle"
}

load_migration_receipt() {
    # The existing migration receipt, read as a whole and checked field by
    # field. `receipt_valid` is 1 only when the file parsed, the schema is this
    # script's, the phase is one this script writes, and all five before-images
    # it has to carry are recorded, are the ones this installer writes and are
    # still on disk: anything less is a state to refuse rather than a state to
    # resume from. The old `grep '"phase":' ` answer is not good enough here - it
    # is satisfied by a truncated file, and a truncated file on an unmigrated Mac
    # reads as "already done".
    receipt_valid=0
    receipt_phase=
    receipt_reason=none
    receipt_kind=
    receipt_installed_version=
    receipt_started_at=
    receipt_workspace=
    receipt_host_url=
    receipt_app_bundle=
    receipt_retiring_desktop=0
    receipt_before_server_plist=
    receipt_before_desktop_plist=
    receipt_before_shim=
    receipt_before_install_receipt=
    receipt_before_tool_env=
    resume_retiring=0
    resume_unfinished_host=0
    [ -f "$migration_dir/receipt.json" ] || return 0
    if ! "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# migration-receipt-read: validates an existing receipt.json, one field per line
import json
import pathlib
import sys

# Every phase this script writes. A receipt naming anything else was written by
# something that is not this installer, and its before-images mean nothing here.
PHASES = (
    "started",
    "installed_no_start",
    "retiring",
    "migrated",
    "migrated_client",
    "rolled_back",
    "interrupted",
)
IMAGES = {
    "server_plist": "com.ciao.server.plist",
    "desktop_plist": "Ciaobot.plist",
    "shim": "ciao",
    "install_receipt": "install-receipt.json",
    "tool_env": "tool-env",
}

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        data = json.load(handle)
except Exception:
    print("reason=unreadable")
    raise SystemExit(0)
if not isinstance(data, dict) or data.get("schema") != 1:
    print("reason=schema")
    raise SystemExit(0)
phase, kind = data.get("phase"), data.get("kind")
if not isinstance(phase, str) or phase not in PHASES or not isinstance(kind, str) or not kind:
    print("reason=phase")
    raise SystemExit(0)
before = data.get("before")
if not isinstance(before, dict):
    print("reason=before")
    raise SystemExit(0)


def image(name):
    # A key that is not there at all is not a record of "this Mac had no such
    # file": it is a receipt that does not say what it replaced, and a rollback
    # that read the omission as absence deletes a file the user had before the
    # migration started. So all five keys have to be present, they have to be
    # strings, and a path has to be the before-image this installer writes under
    # that name - and still be on disk, since a snapshot that is gone cannot roll
    # anything back and must not be reported as one that can.
    if name not in before or not isinstance(before[name], str):
        raise KeyError(name)
    value = before[name]
    if value == "":
        return ""
    path = pathlib.Path(value)
    if path.parent != pathlib.Path(sys.argv[2]) or path.name != IMAGES[name]:
        raise ValueError(name)
    # A tool environment is a directory and the rest are files, so this is the
    # one shape both of them pass.
    if not path.exists():
        raise ValueError(name)
    return value


try:
    images = {name: image(name) for name in IMAGES}
except KeyError:
    # An incomplete receipt is not a state to resume from and not one to read
    # as an absence either: there is nothing here to roll back *to*.
    print("reason=incomplete-image")
    raise SystemExit(0)
except ValueError:
    print("reason=missing-image")
    raise SystemExit(0)
print("valid=1")
print("phase=" + phase)
print("kind=" + kind)
print("version=" + str(data.get("version") or ""))
print("started_at=" + str(data.get("started_at") or ""))
print("workspace=" + str(data.get("workspace") or ""))
print("host_url=" + str(data.get("host_url") or ""))
# Absent in a receipt written before the field existed, which is a transaction
# whose bundle this installer was never told, not one that had no bundle.
print("app_bundle=" + str(data.get("app_bundle") or ""))
print("retiring_desktop=" + ("1" if data.get("retiring_desktop") is True else "0"))
for name in IMAGES:
    print("before_" + name + "=" + images[name])
' "$migration_dir/receipt.json" "$migration_dir/before" > "$tmp/receipt-fields.txt" 2>/dev/null; then
        printf '%s\n' "reason=unreadable" > "$tmp/receipt-fields.txt"
    fi
    # Read with `case` rather than `IFS=`, so a before-image path with a space
    # in it survives as one value.
    while IFS= read -r field || [ -n "$field" ]; do
        case "$field" in
            valid=1) receipt_valid=1 ;;
            phase=*) receipt_phase=${field#phase=} ;;
            kind=*) receipt_kind=${field#kind=} ;;
            version=*) receipt_installed_version=${field#version=} ;;
            started_at=*) receipt_started_at=${field#started_at=} ;;
            workspace=*) receipt_workspace=${field#workspace=} ;;
            host_url=*) receipt_host_url=${field#host_url=} ;;
            app_bundle=*) receipt_app_bundle=${field#app_bundle=} ;;
            retiring_desktop=1) receipt_retiring_desktop=1 ;;
            before_server_plist=*) receipt_before_server_plist=${field#before_server_plist=} ;;
            before_desktop_plist=*) receipt_before_desktop_plist=${field#before_desktop_plist=} ;;
            before_shim=*) receipt_before_shim=${field#before_shim=} ;;
            before_install_receipt=*) receipt_before_install_receipt=${field#before_install_receipt=} ;;
            before_tool_env=*) receipt_before_tool_env=${field#before_tool_env=} ;;
            reason=*) receipt_reason=${field#reason=} ;;
        esac
    done < "$tmp/receipt-fields.txt"
    if [ "$receipt_valid" -eq 0 ]; then
        receipt_phase=
    elif [ "$receipt_phase" = retiring ] && [ "$receipt_retiring_desktop" -ne 0 ]; then
        # The transaction this receipt came from had already started taking the
        # app's own agent out of launchd, so launchd may already have dropped the
        # job on this Mac. Two things follow, and both are about not lying about
        # that agent: this run's rollback has to load it again, and this run is
        # finishing a retirement rather than installing over the top of it.
        desktop_bootout_done=1
        resume_retiring=1
    else
        # A hand-over that never settled. The classifier cannot see it: by the
        # time the receipt is in one of these phases, `ciao setup` has usually
        # already repointed the engine plist at the tool this script installed,
        # and an engine outside a `.app` is an ordinary install as far as the
        # classifier is concerned. Treating that as "nothing to migrate" drops
        # the hand-over on the floor - the app's own agent stays loaded next to
        # the engine that was supposed to replace it, and the receipt never
        # advances. So the receipt, which still holds the originals, the kind and
        # the workspace the transaction was started with, is what the next run
        # finishes from. A `desktop_client` receipt is not in this: a client
        # never runs `ciao setup`, so the classifier still sees the desktop
        # engine and dispatches the client path on its own.
        case "$receipt_phase" in
            started|installed_no_start|interrupted)
                case "$receipt_kind" in
                    desktop_host)
                        resume_unfinished_host=1
                        ;;
                    desktop_invalid)
                        # The receipt is the record of the decision the user has
                        # already made, and a host hand-over records no URL. A
                        # `--as-client` run does record one, and it never runs
                        # `ciao setup`, so the classifier still sees the desktop
                        # engine and dispatches the client path on its own.
                        # Requiring `--as-host` to be passed again would not be
                        # cautious, just silent about the common case: the
                        # transition release and the re-run one-liner both pass
                        # exactly `--migrate`, and on this branch a plain retry
                        # would answer `skip` and install over the transaction the
                        # receipt still holds.
                        if [ -z "$receipt_host_url" ]; then
                            resume_unfinished_host=1
                        fi
                        ;;
                esac
                ;;
        esac
    fi
}

refuse_unusable_receipt() {
    # A receipt this script cannot trust is not a state to carry on from: the
    # next step would be to replace an engine whose before-images are unknown.
    # Every answer here says what to do about the file, because deleting the one
    # record of a rollback by hand is the user's decision, not this script's.
    case "$receipt_reason" in
        none) return 0 ;;
        unreadable)
            fail "the migration receipt at $migration_dir/receipt.json cannot be read, and it is the only record of the before-images this migration keeps. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        schema)
            fail "the migration receipt at $migration_dir/receipt.json was written by a different version of this installer, so its before-images cannot be trusted. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        phase)
            fail "the migration receipt at $migration_dir/receipt.json does not name a phase this installer wrote. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        before)
            fail "the migration receipt at $migration_dir/receipt.json has no before-images recorded, so this migration cannot be rolled back from it. Move it aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) and re-run with --migrate"
            ;;
        missing-image)
            fail "the migration receipt at $migration_dir/receipt.json refers to before-images that are no longer in $migration_dir/before, so a failed migration could no longer be rolled back. Finish the install by hand: ciao setup --workspace <workspace> --python <ciao> && ciao service start"
            ;;
        incomplete-image)
            fail "the migration receipt at $migration_dir/receipt.json does not record every before-image this migration keeps, so this run cannot tell a file this Mac never had from one it was not told about, and refuses to replace anything. Its snapshots are still in $migration_dir/before: put back what you need from there and finish by hand (ciao setup --workspace <workspace> --python <ciao> && ciao service start), or move the receipt aside (mv $migration_dir/receipt.json $migration_dir/receipt.json.bak) once this Mac is in the state you want and re-run with --migrate"
            ;;
    esac
}

server_plist_program() {
    # ProgramArguments:0 out of the engine LaunchAgent, read raw by the same
    # interpreter that reads the migration receipt: the plist is what a
    # migration is judged on, and a program read out of a shell word would be a
    # different answer on a file this script did not write. Prints nothing when
    # the file is missing or is not a plist, which is a fact the caller has to
    # hear about rather than a program it can compare.
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" python -c '
# engine-plist-program: ProgramArguments:0 out of a LaunchAgent plist
import plistlib
import sys

try:
    with open(sys.argv[1], "rb") as handle:
        payload = plistlib.load(handle)
except Exception:
    raise SystemExit(0)
arguments = payload.get("ProgramArguments") if isinstance(payload, dict) else None
if isinstance(arguments, (list, tuple)) and arguments:
    print(str(arguments[0] or ""))
' "$1" 2>/dev/null
}

migration_matches_install() {
    # "Already migrated" is only true when the receipt and what is actually
    # installed agree. A receipt is a file on disk, and a Mac that was restored
    # from a backup, or that had a later install written over it, still carries
    # one - and a stale success that exits 0 is a Mac with no engine and a
    # message saying it has one. So the version, the service role, the retired
    # desktop agent, the tool the install receipt names and - for a host - the
    # engine LaunchAgent itself all have to line up with the receipt.
    settled_mismatch=
    case "$receipt_phase" in
        migrated) expected_backend=launchd; expected_label=$SERVER_LABEL ;;
        migrated_client) expected_backend=none; expected_label= ;;
        *) return 1 ;;
    esac
    [ -n "$receipt_installed_version" ] || return 1
    [ -f "$install_receipt" ] || return 1
    installed_version=$(awk -F'"' '/^[[:space:]]*"version":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_version" = "$receipt_installed_version" ] || return 1
    installed_backend=$(awk -F'"' '/^[[:space:]]*"service_backend":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_backend" = "$expected_backend" ] || return 1
    installed_label=$(awk -F'"' '/^[[:space:]]*"service_label":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    [ "$installed_label" = "$expected_label" ] || return 1
    # The app's own agent is retired by both migrating paths, so a plist still
    # sitting in LaunchAgents means the retirement never happened.
    [ ! -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" ] || return 1
    # A receipt outlives the install it recorded: a `uv tool uninstall`, a wiped
    # state directory or a half-removed tool environment all leave the file
    # behind, and a "migrated" Mac whose `ciao` cannot run is a Mac with no
    # engine whatever the receipt says.
    expected_executable=$(awk -F'"' '/^[[:space:]]*"executable":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    expected_interpreter=$(awk -F'"' '/^[[:space:]]*"python":/ {print $4; exit}' \
        "$install_receipt" 2>/dev/null || true)
    if [ -z "$expected_executable" ] || [ ! -x "$expected_executable" ]; then
        settled_mismatch="the ciao entry point it names (${expected_executable:-none}) is not there"
        return 1
    fi
    if [ -z "$expected_interpreter" ] || [ ! -x "$expected_interpreter" ]; then
        settled_mismatch="the engine it was installed into (${expected_interpreter:-none}) is not there"
        return 1
    fi
    if [ "$expected_backend" != launchd ]; then
        # A client runs no engine of its own, and its plist is left in place
        # pointing inside Ciaobot.app on purpose: that is the agent that was
        # disabled, and the host is where that engine went. There is nothing here
        # to compare a program against.
        return 0
    fi
    # A host's engine is the com.ciao.server agent, so the plist has to be the
    # one this install wrote: a program inside Ciaobot.app is the desktop's
    # engine, which is exactly what a settled migration replaced, and a missing
    # plist is a Mac with nothing running.
    server_plist="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    installed_program=$(server_plist_program "$server_plist")
    if [ -z "$installed_program" ]; then
        settled_mismatch="there is no $SERVER_LABEL.plist in $HOME/Library/LaunchAgents"
        return 1
    fi
    if [ "$installed_program" != "$expected_executable" ]; then
        settled_mismatch="$SERVER_LABEL.plist still points at $installed_program"
        return 1
    fi
    return 0
}

warn_settled_mismatch() {
    # Not fatal, and not silent: the originals are still in before/, and
    # re-running the migration from them is what puts this Mac back into the
    # state the receipt claims. What is not acceptable is a settled receipt
    # quietly re-migrating a Mac, or one that was believed and was not true.
    echo "Ciaobot engine installer: the migration receipt says $receipt_phase, but $settled_mismatch, so it does not describe this Mac and is not believed. Re-running the migration from the before-images in $migration_dir/before" >&2
}

quit_desktop_app() {
    # The app owns the workspace's files, so it is asked to quit before
    # anything is replaced. The wait is a hard deadline, not a formality: an app
    # that is still there when it expires is a live writer, and replacing its
    # engine underneath it is the one outcome this script exists to prevent. So
    # the answer is "stop, with the old engine exactly as it was", which is why
    # this runs before the first thing is disabled, booted out or overwritten.
    # A missing app, or one that ignores the request, is not an error here.
    "$OSASCRIPT" -e 'tell application id "local.ciaobot.app" to quit' >/dev/null 2>&1 || true
    waited=0
    while [ "$waited" -lt 20 ] && "$PGREP" -x ciaobot-desktop >/dev/null 2>&1; do
        waited=$((waited + 1))
        "$SLEEP" 1
    done
    if "$PGREP" -x ciaobot-desktop >/dev/null 2>&1; then
        return 1
    fi
}

retire_desktop_agent() {
    # Only after the new engine has answered with its own version: the app's
    # agent is bootout *and* disabled, so a relaunch cannot bring it back
    # alongside the agent that replaced it. Every one of the three is checked.
    # A relaunch that came back anyway, or a plist removed while its agent is
    # still loaded, is a desktop agent running next to the engine that was
    # supposed to replace it - which is not a migration that worked, so the
    # caller rolls back instead of reporting success.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    # Recorded the moment launchd has dropped the job, which is a different fact
    # from having removed the plist: from here the app's agent is unloaded, and a
    # rollback that only re-enables its label leaves a desktop engine that never
    # starts again.
    desktop_bootout_done=1
    "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    return 0
}

disable_local_engine() {
    # What a client path does before it installs anything: this Mac stops being
    # a writer. The server plist is left on disk (it is backed up) but bootout
    # *and* disable, so nothing restarts it and no second engine ever comes up
    # beside the host. A launchctl that refuses is not shrugged off: the whole
    # point of this path is that the local engine stops.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || return 1
    "$LAUNCHCTL" disable "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || return 1
    # The app's own agent is held to the same standard as the engine's, and the
    # order matters. A client that stopped the engine but left the app's agent
    # loaded comes back on the next relaunch with a desktop engine running next
    # to the host it was just handed to, so neither bootout nor disable is
    # ignored - and the refusal happens before the plist is removed, which is
    # what the rollback restores it from.
    "$LAUNCHCTL" bootout "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    desktop_bootout_done=1
    "$LAUNCHCTL" disable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    return 0
}

restore_before_state() {
    # Every file this transaction replaced goes back to its before-image, and
    # everything it created for itself is taken away again. An empty before-image
    # means "this was not here", so the file the migration made is removed
    # instead - and nothing this transaction did not create is touched.
    if [ -n "$before_server_plist" ]; then
        copy_file "$before_server_plist" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" || return 1
    else
        rm -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" || return 1
    fi
    if [ -n "$before_desktop_plist" ]; then
        copy_file "$before_desktop_plist" \
            "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    else
        rm -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" || return 1
    fi
    if [ -n "$before_shim" ]; then
        mkdir -p "$HOME/.local/bin"
        copy_file "$before_shim" "$HOME/.local/bin/ciao" || return 1
    else
        rm -f "$HOME/.local/bin/ciao" || return 1
    fi
    # The install receipt is what a later `ciao setup` or a runtime swap reads
    # to answer "which release installed me", so a rollback that left the new
    # engine's receipt in place would report the migration as successful after
    # putting the old engine back.
    if [ -n "$before_install_receipt" ]; then
        mkdir -p "$HOME/.local/state/ciaobot"
        copy_file "$before_install_receipt" "$install_receipt" || return 1
    else
        rm -f "$install_receipt" || return 1
    fi
    if [ -n "$tool_dir" ]; then
        if [ -n "$before_tool_env" ]; then
            rm -rf "$tool_dir/ciaobot" || return 1
            cp -pR "$before_tool_env" "$tool_dir/ciaobot" || return 1
        else
            rm -rf "$tool_dir/ciaobot" || return 1
        fi
    fi
    return 0
}

restore_desktop_agent() {
    # The app's own LaunchAgent, if this transaction took it out of launchd. Its
    # plist comes back from the before-image - restore_before_state has already
    # put it back, or taken it away again if this Mac never had one - and the
    # agent is loaded and started, because a host handed back to Ciaobot.app
    # with its agent still unloaded is a Mac whose engine never comes back on its
    # own. bootstrap and kickstart are checked, so a rollback that could not put
    # the app's engine back says so instead of claiming it did. When this
    # transaction never booted the agent out, or the Mac had no app agent to
    # begin with, there is nothing to load: launchd already holds the job, and
    # bootstrapping a label it holds is an error that would report a complete
    # rollback as incomplete.
    "$LAUNCHCTL" enable "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    if [ "$desktop_bootout_done" -ne 0 ] &&
        [ -f "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$DESKTOP_LABEL.plist" 2>/dev/null || return 1
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$DESKTOP_LABEL" 2>/dev/null || return 1
    fi
    # The app's agent is enabled and loaded again, so this transaction is no
    # longer one that took it out of launchd. The receipt the rollback writes
    # says so, or the run after it would reload a job launchd already holds.
    desktop_bootout_done=0
    retiring_desktop=0
    return 0
}

rollback_host() {
    # The Mac gets its engine back: the app's own plist and shim, the install
    # receipt and tool environment the previous install left, and its agent
    # loaded and started. bootstrap and kickstart are checked, because a
    # rollback that reports success while launchd silently kept a job
    # definition for an engine that is no longer on disk is worse than one that
    # says what it could not put back.
    failure=
    restore_before_state || failure=yes
    "$LAUNCHCTL" bootout "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || true
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" 2>/dev/null || failure=yes
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    fi
    restore_desktop_agent || failure=yes
    if [ -n "$failure" ]; then
        migration_receipt rolled_back "$1 (rollback incomplete: $failure)" || true
        fail "migration failed and Ciaobot.app's engine could NOT be fully restored: $1. See $migration_dir/receipt.json, and check: $(quote_shell_path "$bin_dir/ciao") service status"
    fi
    # `|| true` on the write that records the rollback: this runs because
    # something already went wrong, and `set -e` taking the shell down here
    # would leave the user with no message at all.
    migration_receipt rolled_back "$1" || true
    fail "migration failed and Ciaobot.app's engine was restored: $1"
}

rollback_client() {
    # A client had no engine of its own to replace, so what goes back is the
    # state it was in before: both launchd labels enabled and loaded again, the
    # desktop plist that was removed, the shim, the install receipt and the tool
    # environment. The message never claims an engine was restored - there was
    # no engine here - and it says which parts could not be put back.
    failure=
    restore_before_state || failure=yes
    "$LAUNCHCTL" enable "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    if [ -f "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" ]; then
        "$LAUNCHCTL" bootstrap "gui/$(id -u)" \
            "$HOME/Library/LaunchAgents/$SERVER_LABEL.plist" 2>/dev/null || failure=yes
        "$LAUNCHCTL" kickstart -k "gui/$(id -u)/$SERVER_LABEL" 2>/dev/null || failure=yes
    fi
    restore_desktop_agent || failure=yes
    if [ -n "$failure" ]; then
        migration_receipt rolled_back "$1 (rollback incomplete: $failure)" || true
        fail "migration failed and this Mac's local engine could NOT be fully restored: $1. See $migration_dir/receipt.json, and check: $(quote_shell_path "$bin_dir/ciao") service status"
    fi
    migration_receipt rolled_back "$1" || true
    fail "migration failed and this Mac's local engine was put back as it was: $1"
}

on_signal() {
    # An interrupted install must not leave a receipt claiming more than
    # happened, and it must not try to roll back from inside a signal handler:
    # half an undo driven by a signal is not an undo. What it can do truthfully
    # is record that the transaction never finished, so the next run resumes
    # from the originals instead of guessing what state this Mac is in.
    if [ "$migration_active" -ne 0 ]; then
        migration_receipt interrupted "the installer was interrupted before the migration finished" \
            || true
    fi
    exit 130
}

install_step() {
    # One step of the install, with the failure message it reports. In a
    # migration the app's engine is already stopped by the time the first of
    # these runs, so a failure there puts it back before the script exits.
    step_error=$1
    shift
    "$@" || abort_install "$step_error"
}

abort_install() {
    # Every post-install failure has to undo what this transaction changed, and
    # the undo that fits is the one for the path it took: a client has nothing to
    # restore but its own previous state, a host has an engine to give back. The
    # native server host goes back first: `rollback_host` reloads launchd, and
    # reverting the host identity is only safe once its candidate service is gone.
    # A native rollback that did not finish stops the rest: reloading the previous
    # engine definition while the candidate may still be running, over before-images
    # that are the only way back, is worse than reporting an incomplete rollback.
    native_host_rollback_rc=0
    native_host_rollback || native_host_rollback_rc=$?
    case "$native_host_rollback_rc" in
        0) ;;
        5) ;;
        *)
            echo "Ciaobot engine installer: the previous Ciaobot Server host could not be restored, so the previous engine definition was NOT reloaded and nothing else was changed. The recoverable material is kept: see $HOME/.local/state/ciaobot/server-host-upgrade.json" >&2
            fail "$1 (incomplete rollback: the previous Ciaobot Server host was not restored)"
            ;;
    esac
    case "$migrate_path" in
        host) rollback_host "$1" ;;
        client) rollback_client "$1" ;;
    esac
    fail "$1"
}

validate_client_url() {
    # `--as-client` is the one input this migration never read from a state
    # file: it is the user's answer to a question this Mac could not answer, and
    # it is printed back at them as the address to open. A prefix check accepts
    # `https://`, which opens nothing - taking the client path with it would
    # disable this Mac's own engine and then hand it over to no host at all. So
    # the override goes through the same parsed check the classifier applies to
    # a host_url, asked of the verified wheel, and the answer replaces the value
    # the rest of this run uses.
    [ -n "$as_client" ] || return 0
    if ! client_url=$("$uv" run --quiet --no-project --python "$PYTHON_VERSION" \
        --with "$wheel" python -I -m ciao.engine_migration check-client-url \
        "$as_client" 2>/dev/null); then
        fail "--as-client takes an address this Mac can be handed over to, and \"$as_client\" is not one: an http:// or https:// URL with a host name - a prefix, a bare path, credentials or whitespace in it is not an address. Re-run with --migrate --as-client https://your-host"
    fi
    as_client=$client_url
}

is_set_up_workspace() {
    # Mirrors `ciao.setup_marker.is_set_up_workspace`: every `ciao setup` writes
    # `.runtime/workspaces.json`.
    [ -d "$1" ] || return 1
    [ -f "$1/.runtime/workspaces.json" ] && return 0
    # Pre-1.0 path: an install configured only through CIAO_WORKSPACES in its
    # `.env` has no registry until its first start on a release that imports
    # the variable. Delete together with the other pre-1.0 imports.
    [ -f "$1/.env" ] && grep -Eq "^[[:space:]]*CIAO_WORKSPACES[[:space:]]*=[[:space:]]*[\"']?[^[:space:]\"']" "$1/.env"
}

port_owner() {
    # The programs listening on <port>, one name per line; nothing when the
    # port is free or lsof is not installed. `lsof` is the only lister macOS
    # and Linux both ship, so its absence costs the owner, never the port.
    #
    # The name is what the holding process chose for itself, so it is
    # sanitised before it is ever printed: a carriage return or an ANSI
    # escape in it would otherwise reach the operator's terminal verbatim and
    # could rewrite the line it appears on. Only printable bytes survive.
    command -v lsof >/dev/null 2>&1 || return 0
    lsof -nP -iTCP:"$1" -sTCP:LISTEN 2>/dev/null \
        | awk 'NR > 1 { print $1 }' \
        | tr -c '[:print:]' ' ' \
        | tr '\n' ' ' \
        | sed 's/  */ /g; s/^ //; s/ $//'
}

fresh_startup_state() {
    # HTTP 200 alone can be an unrelated listener. Only the installed engine's
    # startup contract and completed bind phase can prove fresh setup is ready.
    "$tool_python" -I -c '
import json
import sys
import uuid

def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate startup field")
        result[key] = value
    return result

def reject_constant(value):
    raise ValueError("invalid JSON constant")

try:
    with open(sys.argv[2], "rb") as response:
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("oversized startup response")
    result = json.loads(raw, object_pairs_hook=unique_fields, parse_constant=reject_constant)
    if not isinstance(result, dict) or result.get("version") != sys.argv[1]:
        raise ValueError("not the installed engine")
    if type(result.get("desktop_api_version")) is not int or result["desktop_api_version"] != 1:
        raise ValueError("unknown startup API")
    for key in ("overall_ready", "port_in_use", "update_available"):
        if type(result.get(key)) is not bool:
            raise ValueError("invalid startup flag")
    if "latest_version" not in result or (result["latest_version"] is not None and not isinstance(result["latest_version"], str)):
        raise ValueError("invalid latest version")
    startup_id = result.get("startup_id")
    if not isinstance(startup_id, str) or uuid.UUID(startup_id).hex != startup_id:
        raise ValueError("invalid live startup id")
    if type(result.get("pid")) is not int or result["pid"] <= 0:
        raise ValueError("invalid live engine pid")
    phases = result.get("phases")
    if not isinstance(phases, list):
        raise ValueError("invalid startup phases")
    by_name = {}
    for phase in phases:
        if not isinstance(phase, dict) or not isinstance(phase.get("name"), str) or not phase["name"] or phase["name"] in by_name:
            raise ValueError("invalid or duplicate startup phase")
        if phase.get("status") not in ("pending", "in_progress", "done", "failed") or not isinstance(phase.get("message"), str):
            raise ValueError("invalid startup phase state")
        for key in ("started_at", "finished_at"):
            if key not in phase or (phase[key] is not None and not isinstance(phase[key], str)):
                raise ValueError("invalid startup phase time")
        by_name[phase["name"]] = phase
    bind = by_name.get("server_starting", {})
    if result["port_in_use"]:
        print("port_in_use")
    elif result["overall_ready"] and bind.get("status") == "done":
        print("ready")
except (ValueError, TypeError, KeyError, RecursionError, OSError):
    sys.exit(1)
' "$version" "$1"
}

python_probe_port() {
    # The verified wheel's own probe: `ciao.port_in_use.port_is_available`
    # binds the port the way the engine does. It answers 0 (the port is
    # free), 1 (the port is taken) and 2 (the probe could not run at all).
    #
    # The three are told apart on purpose: a probe that could not run - a
    # wheel that would not install, an interpreter that would not start -
    # says nothing about the port, and reading it as "taken" would refuse
    # an install on a guess.
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
        python -I -c 'import sys
from ciao.port_in_use import port_is_available
try:
    free = port_is_available(int(sys.argv[1]))
except Exception:
    sys.exit(2)
sys.exit(0 if free else 1)' \
        "$1" 2>/dev/null
}

check_port_available() {
    # Refuse an install whose port is already held by something that is not
    # Ciaobot (#1654). The engine would die on the bind and the service
    # manager would respawn it forever, and the operator was told the engine
    # was "still starting" with a dead sign-in link.
    #
    # Ciaobot's own engine already serving the port is legitimate: that is a
    # re-install of a running engine, and the port is bound by the very
    # thing being replaced.
    port=$1
    workspace=$2
    # `set -e` is on, so the probe's own status is captured rather than
    # allowed to end the script.
    probe_rc=0
    python_probe_port "$port" || probe_rc=$?
    case "$probe_rc" in
        0) return 0 ;;
        2) return 0 ;;
    esac
    if [ -n "$CURL" ]; then
        # Something is answering on that port. It has to *look* like
        # Ciaobot's own engine, not merely answer: a hostile process on the
        # port could answer anything, and the exemption is what lets the
        # install continue. The engine's startup status carries a "version"
        # field, so a response that does not is not the engine.
        status=$("$CURL" -fsS -m 2 "http://localhost:$port/api/startup-status" 2>/dev/null || true)
        case "$status" in
            *'"version"'*) return 0 ;;
        esac
    fi
    owner=$(port_owner "$port")
    if [ -n "$owner" ]; then
        fail "port $port is already in use by $owner. Free port $port, or set PWA_PORT in $workspace/.env to serve on a different port"
    fi
    fail "port $port is already in use by another program. Free port $port, or set PWA_PORT in $workspace/.env to serve on a different port"
}

check_host_workspace() {
    # A host hand-over moves an engine that is *already running*, and the
    # workspace it runs in is the one that engine's own plist names: the `.env`
    # with the password and the provider credentials, the runtime root with the
    # chats, the schedules, the VAPID keys and the push subscriptions. That
    # directory is what the new engine has to keep, and it is the only thing
    # this path is allowed to name. So three answers are refused here, while
    # nothing on this Mac has been touched yet:
    #   - a `--workspace` that is a different directory, which would create a
    #     second empty workspace, repoint the service at it, retire the app's
    #     agent and leave the original - and everything in it - behind, while the
    #     receipt still named the original;
    #   - a `--workspace` that is not there, because a hand-over creates no
    #     workspace: there would be nothing to hand over;
    #   - a classified workspace that is not an existing set-up one (see
    #     `is_set_up_workspace`), which is the same second workspace with an
    #     extra `mkdir` in it.
    # And when the classifier could not recover a workspace at all - an
    # `--as-host` override on state nobody can read - the one that will be used
    # has to be named explicitly and has to exist, because a directory this
    # script invented would be a guess about where a host's data lives.
    if [ -n "$workspace" ]; then
        if [ ! -d "$workspace" ]; then
            fail "--migrate hands the engine that is already running over to this installer, and it never creates a workspace: $workspace does not exist. Re-run without --workspace, or with the workspace Ciaobot.app was using${migrate_workspace:+ ($migrate_workspace)}. Nothing on this Mac has been changed"
        fi
        # Resolved, so a path that only looks different - a trailing slash, a
        # symlink, `..` - is not mistaken for a different directory.
        workspace=$(CDPATH= cd -- "$workspace" && pwd -P)
    fi
    if [ -z "$migrate_workspace" ]; then
        if [ -z "$workspace" ]; then
            fail "Ciaobot could not read a workspace for this Mac, so --migrate --as-host has to be told which one to keep: re-run with --migrate --as-host --workspace <the workspace Ciaobot.app was using>. Nothing on this Mac has been changed"
        fi
        migrate_workspace=$workspace
    fi
    if ! is_set_up_workspace "$migrate_workspace"; then
        fail "the workspace the engine being migrated runs in is not a Ciaobot workspace: $migrate_workspace has no .runtime/workspaces.json, so a host hand-over to it would start a second engine with a fresh password and a fresh runtime root next to the real ones. Point --workspace at the workspace Ciaobot.app was using, or re-run without --migrate on a Mac that has never run Ciaobot.app. Nothing on this Mac has been changed"
    fi
    migrate_workspace=$(CDPATH= cd -- "$migrate_workspace" && pwd -P)
    if [ -n "$workspace" ] && [ "$workspace" != "$migrate_workspace" ]; then
        fail "--migrate hands the engine Ciaobot.app is running over to this installer; it does not move that engine's workspace, and --workspace $workspace is not the workspace it runs in ($migrate_workspace), whose .env, runtime root and chats stay exactly where they are. Re-run without --workspace, or with --workspace $migrate_workspace. Nothing on this Mac has been changed"
    fi
    return 0
}

classify_install() {
    # Reads the state through the *verified* wheel - the only code that has been
    # checked against the signed manifest at this point - and prints one JSON
    # object. It writes nothing: it runs before the first change this script
    # makes, and it has to be safe to run twice.
    classification=$("$uv" run --quiet --no-project --python "$PYTHON_VERSION" \
        --with "$wheel" python -I -m ciao.engine_migration classify --json \
        --launch-agents-dir "$HOME/Library/LaunchAgents") \
        || fail "could not classify this install"
    printf '%s' "$classification" | "$uv" run --quiet --no-project \
        --python "$PYTHON_VERSION" python -c '
import json, sys
state = json.load(sys.stdin)
print(state.get("kind", ""))
print(state.get("workspace", ""))
print(state.get("host_url", ""))
print(state.get("app_bundle", ""))
' > "$tmp/classification.txt" || fail "could not classify this install"
    # Read line by line rather than with `set --`: a workspace path with a space
    # in it is one value, not two words.
    field_number=0
    while IFS= read -r field || [ -n "$field" ]; do
        field_number=$((field_number + 1))
        case "$field_number" in
            1) migrate_kind=$field ;;
            2) migrate_workspace=$field ;;
            3) migrate_host_url=$field ;;
            4) migrate_app_bundle=$field ;;
        esac
    done < "$tmp/classification.txt"
    if [ "$resume_unfinished_host" -ne 0 ]; then
        # A hand-over the receipt says never finished, on a Mac whose plist now
        # looks like an ordinary install. The kind and the workspace come from
        # the transaction, not from what this Mac looks like now, and the
        # dispatch below never gets to answer `skip` for a Mac that is mid
        # hand-over.
        migrate_kind=$receipt_kind
        [ -z "$receipt_workspace" ] || migrate_workspace=$receipt_workspace
        # The bundle comes from the transaction for the same reason, and this is
        # the path that needs it: `ciao setup` has already repointed the engine
        # plist, so the classification above read this Mac as an ordinary
        # installer-managed engine and named no bundle. A receipt written before
        # the field existed leaves it empty, which the completed hand-over
        # reports rather than acting on.
        migrate_app_bundle=${migrate_app_bundle:-$receipt_app_bundle}
        migrate_path=host
    else
        case "$migrate_kind" in
            desktop_host)
                migrate_path=host
                ;;
            desktop_client)
                migrate_path=client
                ;;
            desktop_invalid)
                # Nobody can say what this Mac writes, so nothing is decided for
                # the user: the override is the decision, and it is recorded.
                if [ "$as_host" -ne 0 ]; then
                    migrate_path=host
                elif [ -n "$as_client" ]; then
                    migrate_path=client
                    migrate_host_url=$as_client
                else
                    fail "Ciaobot could not tell whether this Mac is the host or a client (node state unreadable). Re-run with --migrate --as-host, or --migrate --as-client https://your-host"
                fi
                ;;
            desktop_stale|engine|none)
                # Nothing live to migrate: a deleted app, an engine this script
                # already placed, or a Mac that never ran Ciaobot.app. --migrate is
                # a no-op and the ordinary install runs.
                migrate_path=skip
                ;;
            *)
                fail "could not classify this install: unknown kind: $migrate_kind"
                ;;
        esac
        if [ "$resume_retiring" -ne 0 ]; then
            # The last run stopped while it was retiring the app's own agent, which
            # means it had already installed the new engine and repointed the engine
            # plist at it. The classifier reads that as an installer-managed engine -
            # correctly, on its own - and the ordinary install would leave the app's
            # agent behind forever, in a receipt that never settles. So this run
            # finishes the transaction the receipt describes, with the kind and the
            # workspace that transaction was started with.
            migrate_kind=$receipt_kind
            [ -z "$receipt_workspace" ] || migrate_workspace=$receipt_workspace
            # And with the bundle it was started with, for the reason the
            # classifier cannot name one here: the plist it would read is the one
            # `ciao setup` already repointed. This run is the one that finishes
            # the retirement, so it is also the one that has to retire the bundle
            # with it.
            migrate_app_bundle=${migrate_app_bundle:-$receipt_app_bundle}
            migrate_path=host
        elif [ "$desktop_live" -ne 0 ] && [ "$migrate_path" = skip ]; then
            # A live Ciaobot.app was found before verification, and the state it
            # lives in says there is nothing to take over. Installing anyway would
            # be the refusal this script exists to avoid, so it stops here with
            # nothing changed and asks again.
            fail "Ciaobot.app is running but its engine state could not be read; re-run with --migrate --as-host, or --migrate --as-client https://your-host"
        fi
    fi
    if [ "$receipt_valid" -ne 0 ] && [ "$migrate_path" = skip ]; then
        case "$receipt_phase" in
            started|installed_no_start|interrupted)
                # The dispatch above found nothing to migrate, and a validated
                # receipt says a hand-over on this Mac has not settled. Those two
                # cannot both be acted on by installing: `skip` is the ordinary
                # install, and it would run to completion and exit 0 with the
                # app's own agent still loaded next to the engine that was meant
                # to replace it - the receipt never advances and nothing says so.
                # So an open transaction is never installed over, whatever future
                # kind or branch produces this combination; it is answered again,
                # with the same question and nothing changed.
                fail "Ciaobot could not tell whether this Mac is the host or a client (node state unreadable). Re-run with --migrate --as-host, or --migrate --as-client https://your-host"
                ;;
        esac
    fi
    # A host hand-over is only ever a hand-over of one particular workspace, so
    # the workspace it would use is settled here - before the before-images are
    # taken and before the app is asked to quit - rather than discovered
    # halfway through, where a `--workspace` that names somewhere else is
    # already an install that repointed the service.
    if [ "$migrate_path" = host ]; then
        check_host_workspace
    fi
    return 0
}

refuse_desktop_engine
find_uv

download "$base/ciaobot-engine-manifest.json" "$tmp/ciaobot-engine-manifest.json" \
    || fail "could not download the release manifest"
download "$base/ciaobot-engine-manifest.json.sig" "$tmp/ciaobot-engine-manifest.json.sig" \
    || fail "could not download the release manifest signature"

manifest_info=$(read_manifest) || fail "release manifest verification failed"
set -- $manifest_info
wheel_name=${1:-}
wheel_sha=${2:-}
wheel_size=${3:-}
if [ -z "$wheel_name" ] || [ -z "$wheel_sha" ] || [ -z "$wheel_size" ]; then
    fail "release manifest verification failed"
fi

wheel="$tmp/$wheel_name"
download "$base/$wheel_name" "$wheel" || fail "could not download $wheel_name"
actual_sha=$("$SHASUM" -a 256 "$wheel") || fail "could not hash $wheel_name"
actual_sha=${actual_sha%% *}
[ "$actual_sha" = "$wheel_sha" ] || fail "downloaded wheel does not match the signed manifest"
actual_size=$(wc -c < "$wheel" | tr -d ' ')
[ "$actual_size" = "$wheel_size" ] || fail "downloaded wheel does not match the signed manifest"

# Past this point the release bytes are known to be the ones the maintainer
# signed, and not one byte of this Mac has been touched yet. Everything below
# may change the install; everything above may not.
if [ "$migrate" -ne 0 ]; then
    # An override the user typed is checked before the receipt is read and before
    # anything is touched: a client path taken with an address that opens nothing
    # stops this Mac's engine and then hands the user nowhere to sign in.
    validate_client_url
    # Read before anything else, because a migration in progress is resumed
    # from its receipt rather than started again from whatever this Mac looks
    # like now - and a receipt that cannot be trusted stops the run outright.
    load_migration_receipt
    refuse_unusable_receipt
    if migration_matches_install; then
        echo "This Mac was already migrated ($receipt_phase); no migration action is needed."
        exit 0
    fi
    # A settled receipt that does not describe this Mac is re-run from the
    # before-images it kept, and says so, rather than either being believed or
    # quietly starting a second migration.
    [ -z "$settled_mismatch" ] || warn_settled_mismatch
    classify_install
fi

# --- preflight: everything that can be decided without changing anything ----
#
# These checks read. They run before the migration touches a single label or
# file, because a collision discovered after `com.ciao.server` has been booted
# out and `Ciaobot.plist` has been deleted is a collision discovered too late to
# be a refusal: the user would be told to move a file away while their Mac has
# no engine and no agent.
bin_dir=$("$uv" tool dir --bin)
tool_dir=$("$uv" tool dir)
target="$bin_dir/ciao"
if [ -e "$target" ] || [ -L "$target" ]; then
    # `ciao` is a common enough name to collide (Ciao Prolog ships one), and
    # clobbering someone else's program is not this script's call. Ours is
    # either a uv tool entry point or the shim the desktop installer wrote.
    # The listing is indented output (`- ciaobot v1.2.3` under a heading), so
    # the name is matched where uv prints it: a re-run of this script - which is
    # what a retry after a failed or interrupted migration is - has to
    # recognise the entry point its own previous run left behind.
    if ! grep -qF "$SHIM_MARKER" "$target" 2>/dev/null &&
        ! "$uv" tool list 2>/dev/null | grep -qE '^[[:space:]]*-[[:space:]]+ciaobot([[:space:]]|$)'; then
        fail "$target exists and was not installed by Ciaobot; move it away and re-run"
    fi
fi

# Best effort: keep whatever the last install put there, so the engine can
# still answer "which release replaced me" after a runtime swap.
previous_version=
previous_executable=
if [ -f "$install_receipt" ]; then
    previous_version=$(awk -F'"' '/^[[:space:]]*"version":/ {print $4; exit}' "$install_receipt" 2>/dev/null || true)
    previous_executable=$(awk -F'"' '/^[[:space:]]*"executable":/ {print $4; exit}' "$install_receipt" 2>/dev/null || true)
fi

# --- the optional universal server host ------------------------------------
#
# A release may authenticate one prebuilt universal macOS server host beside
# the wheel (#1050, child E1 of #1008). The signed entry is read through the
# verified wheel's own module - the same "run the wheel's code" pattern
# classify_install uses - so the selector and the manifest checks that guard it
# are the canonical ones, not a second copy in shell.
#
# The module only exists on an engine that shipped with the host work. An older
# wheel (an explicit `--version`, or a release cut between the manifest change
# and this one) has no host entry to honor, and importing it here would turn a
# wheel-only install into "No module named". So the module is probed first: an
# engine that predates the host installs the engine exactly as it did before the
# host existed, and a `server-host` entry cannot arrive on a wheel that has no
# way to read it.
#
# Acquiring the host runs the B1 inspector's native codesign probes, so it only
# happens once the signed archive's digest and size are known to be the manifest
# key it already trusts. It runs after the read-only preflight, so a refusal
# there still leaves the Ciaobot engine untouched, and never on a client, which
# runs no engine to host. Installation is inert: it writes no launchd job itself;
# `ciao setup` is what registers the plist when a verified host is installed.
host_entry=
if [ "$migrate_path" != client ] &&
    "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
    python -I -c 'import ciao.server_host_install' >/dev/null 2>&1; then
    # `--describe` adds the selected host revision and signing policy after the
    # three fields this script already reads: a revision-5 host is Developer ID
    # signed by the team the release authenticated, a revision-4 one is the
    # production ad-hoc host. Nothing here decides policy; it is only what lets
    # the migration warning be printed before anything is replaced.
    host_entry=$("$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
        python -I -m ciao.server_host_install select --describe \
        "$tmp/ciaobot-engine-manifest.json" "$tmp/ciaobot-engine-manifest.json.sig" \
        --public-key "$RELEASE_PUBLIC_KEY") \
        || fail "the signed manifest's server host entry could not be read"
fi
if [ -n "$host_entry" ]; then
    set -- $host_entry
    host_name=${1:-}
    host_sha=${2:-}
    host_size=${3:-}
    host_selected_revision=${4:-}
    host_selected_signing=${5:-}
    if [ -z "$host_name" ] || [ -z "$host_sha" ] || [ -z "$host_size" ] ||
        [ -z "$host_selected_revision" ] || [ -z "$host_selected_signing" ]; then
        fail "the signed manifest's server host entry is malformed"
    fi
    host="$tmp/$host_name"
    download "$base/$host_name" "$host" || fail "could not download $host_name"
    actual_sha=$("$SHASUM" -a 256 "$host") || fail "could not hash $host_name"
    actual_sha=${actual_sha%% *}
    [ "$actual_sha" = "$host_sha" ] || fail "downloaded server host does not match the signed manifest"
    actual_size=$(wc -c < "$host" | tr -d ' ')
    [ "$actual_size" = "$host_size" ] || fail "downloaded server host does not match the signed manifest"
    # The module re-checks the digest and size, inspects the extracted bundle,
    # installs it atomically under ~/Applications and writes the owner-only
    # record. A refusal leaves nothing installed. Exit 3 is everything that is
    # not about the release: something already at the target the module cannot
    # prove is ours, a disk full or a chmod EPERM, or a codesign probe that timed
    # out or parsed differently on this macOS release. The manifest signature and
    # the archive's digest were already checked above, so those are local or
    # signed-build facts, never attacker input - and the host is optional and
    # inert, so it must not block every later engine install until the user
    # deletes it by hand. Exit 1 is reserved for a release that did not verify
    # (a manifest/selection error, an archive that does not match its signed
    # digest, or a signed identity the local record contradicts), and stops the
    # run. Exit 4 is its own answer: the previous host can only become the signed
    # one through the recoverable transaction a normal (start-enabled) run takes,
    # so `--no-start` says the migration completes later instead of replacing
    # what it cannot prove healthy.
    #
    # A superseded host is replaced by this one (#1659). Its presence before the
    # install is what makes a successful install a replacement, and a
    # replacement changes the bytes macOS granted Accessibility/Automation to,
    # so the user is told once.
    #
    # The revision that is superseded is read from the wheel itself
    # (`ciao.server_host.SUPERSEDED_HOST_PROTOCOLS`, a set), never written here:
    # a copy of the numbers in this script would silently stop matching on the next
    # revision bump, and the warning is exactly what a user needs then. The
    # installed revision is read from the sealed plist with `plutil`, the one
    # tool macOS has for exactly this. A plist it cannot read is not proof of
    # anything, so it only ever means "not a superseded host": the warning is a
    # courtesy, and the replacement itself is the installer's decision, made
    # from the ownership record, not from this.
    host_superseded_plist="$HOME/Applications/Ciaobot Server.app/Contents/Info.plist"
    host_replacing=0
    host_migrating=0
    superseded_revisions=$(
        "$uv" run --quiet --no-project --python "$PYTHON_VERSION" --with "$wheel" \
            python -I -c 'from ciao.server_host import SUPERSEDED_HOST_PROTOCOLS; print(*SUPERSEDED_HOST_PROTOCOLS)' \
            2>/dev/null || true
    )
    host_revision=
    if [ -f "$host_superseded_plist" ]; then
        # `plutil` is looked up on PATH, not by its absolute path: the
        # installer is exercised on Linux CI too, where macOS's own plutil is
        # not there. A missing plutil only means "not a superseded host".
        host_revision=$(
            plutil -extract CFBundleVersion raw -o - \
                "$host_superseded_plist" 2>/dev/null || true
        )
    fi
    if [ -n "$superseded_revisions" ] && [ -n "$host_revision" ]; then
        for superseded_revision in $superseded_revisions; do
            if [ "$host_revision" = "$superseded_revision" ]; then
                host_replacing=1
            fi
        done
    fi
    # A signed candidate over a host of another revision is the one replacement
    # that is a *migration*, and macOS evaluates the new bytes' identity: the
    # user may have to grant Accessibility, Automation and the folder accesses
    # again. Said once, before anything is replaced, and without promising that
    # any existing grant carries over.
    if [ "$host_selected_signing" != legacy ] && [ -n "$host_revision" ] &&
        [ "$host_revision" != "$host_selected_revision" ]; then
        host_migrating=1
    fi

    # A journal a previous installer left is settled before this run mutates the
    # host, so a second installer never starts from a half-replaced pair.
    native_host_recover

    if [ "$host_migrating" -eq 1 ] && [ "$no_start" -eq 0 ]; then
        echo "warning: Ciaobot Server will be replaced by the Developer ID signed build. macOS may ask again for Accessibility and Automation, and for access to Desktop, Documents and Downloads; approve them in System Settings > Privacy & Security. Existing permissions are not transferred automatically." >&2
    fi

    host_rc=0
    if [ "$no_start" -eq 0 ]; then
        # `--defer-commit` is what makes a legacy-to-signed replacement a durable
        # transaction: the old host and its exact record stay recoverable until
        # `commit-upgrade`, which only runs after this run has proven the new
        # engine healthy.
        native_host_install_started=1
        native_host_run install \
            --manifest "$tmp/ciaobot-engine-manifest.json" \
            --signature "$tmp/ciaobot-engine-manifest.json.sig" \
            --archive "$host" \
            --public-key "$RELEASE_PUBLIC_KEY" \
            --defer-commit --owner-pid "$$" >/dev/null || host_rc=$?
    else
        native_host_install_started=1
        native_host_run install \
            --manifest "$tmp/ciaobot-engine-manifest.json" \
            --signature "$tmp/ciaobot-engine-manifest.json.sig" \
            --archive "$host" \
            --public-key "$RELEASE_PUBLIC_KEY" >/dev/null || host_rc=$?
    fi
    case "$host_rc" in
        0)
            if [ "$no_start" -eq 0 ]; then
                # Whether this run owns an uncommitted host upgrade is answered
                # by the module's journal, not by guessing from the manifest.
                native_host_begin
            fi
            echo "Ciaobot Server host installed and recorded."
            if [ "$host_replacing" -eq 1 ] && [ "$host_migrating" -eq 0 ]; then
                echo "warning: Ciaobot Server was replaced by a new build, so macOS will ask again for its Accessibility and Automation permissions; approve them in System Settings > Privacy & Security" >&2
            fi
            ;;
        3) echo "warning: the Ciaobot Server host was not installed (see above); the engine install continues" >&2 ;;
        4) echo "Ciaobot Server: this Mac still runs the previous server host. The upgrade to the signed host happens automatically on a later installer run without --no-start; nothing was replaced." >&2 ;;
        *) fail "the signed server host could not be installed" ;;
    esac
fi

# --- the migration --------------------------------------------------------
#
# From `migration_active=1` on, this run may have changed the install, so every
# failure after this point has to undo it and a signal has to say so in the
# receipt. Before it, nothing of the engine has been touched and a refusal is
# free; the one thing that may already be written is the inert host above.
if [ "$migrate" -ne 0 ]; then
    case "$migrate_path" in
        host|client)
            # Stop the app and keep a way back before the first change: from
            # here on the engine this Mac is running is the one being replaced.
            backup_before
            migration_receipt started \
                || fail "could not record the migration state; the Ciaobot engine and its service have not been changed"
            # Still before anything is disabled, booted out or overwritten: a
            # desktop that will not quit is a live writer, and the honest
            # answer is to stop with its engine exactly as it was.
            if ! quit_desktop_app; then
                migration_receipt started "Ciaobot.app was still running 20s after it was asked to quit; the Ciaobot engine and its service were unchanged" \
                    || true
                fail "Ciaobot.app is still running 20s after it was asked to quit, so its engine and its service are unchanged. Quit it (or run: osascript -e 'tell application id \"local.ciaobot.app\" to quit') and re-run with --migrate"
            fi
            migration_active=1
            ;;
    esac
    if [ "$migrate_path" = client ]; then
        disable_local_engine || abort_install "the local engine could not be stopped, so this Mac would keep writing"
    fi
fi

install_step "uv tool install failed" \
    "$uv" tool install --force --python "$PYTHON_VERSION" "$wheel" >/dev/null
ciao="$bin_dir/ciao"
tool_python="$tool_dir/ciaobot/bin/python"
[ -x "$ciao" ] || abort_install "the installed ciao entry point is missing: $ciao"
[ -x "$tool_python" ] || abort_install "the installed engine interpreter is missing: $tool_python"
# `uv tool install --force` can exit 0 while leaving an environment this
# interpreter cannot import from (a half-populated tool dir, or a managed
# Python that does not see the package). Probe it here, before the receipt is
# written: a failure now rolls back cleanly, whereas failing at the receipt
# step is the same failure one step later with the app's engine already gone.
install_step "the installed engine is not importable" \
    "$tool_python" -c 'import ciao.install_receipt'

# Absolute paths only: the receipt is read by a process that has no idea which
# directory the installer ran from. A client is recorded as owning no service,
# because that is the whole point of its path: this Mac runs no engine.
receipt_backend=launchd
receipt_label=$SERVER_LABEL
if [ "$migrate_path" = client ]; then
    receipt_backend=none
    receipt_label=
fi
install_step "could not write the install receipt" \
    "$tool_python" -m ciao.install_receipt write \
    --version "$version" \
    --executable "$ciao" \
    --python "$tool_python" \
    --service-backend "$receipt_backend" \
    --service-label "$receipt_label" \
    --previous-version "$previous_version" \
    --previous-executable "$previous_executable" \
    >/dev/null

# A client stops here on purpose. It gets no workspace resolved, no setup, no
# service, no agent: the one thing it must not become is a second writer for a
# runtime root a host owns.

# A Mac with no engine definition and no configured workspace is onboarded in
# the browser. This is decided before anything is created: the foreground
# bootstrap provisions the destination the user confirms, and only a matching
# handoff result activates the service. Migration and existing installs keep the
# CLI setup path below.
fresh_macos=0
fresh_completed=0
# Whether this run's `ciao setup` generated the password. The welcome-pending
# marker alone cannot answer that: an update keeps the marker left by the
# install that generated the password, so the final summary must not claim the
# password was created now (#2162).
password_created=0
if [ "$migrate_path" = skip ] && [ "$(uname -s)" = Darwin ]; then
    fresh_candidate=$workspace
    [ -n "$fresh_candidate" ] || fresh_candidate="$HOME/Ciaobot"
    case "$fresh_candidate" in
        "~") fresh_candidate=$HOME ;;
        "~/"*) fresh_candidate="$HOME/${fresh_candidate#"~/"}" ;;
    esac
    # The child's validator takes an absolute suggestion; a relative one the
    # user typed must not make the wizard unreachable.
    case "$fresh_candidate" in
        /*) ;;
        *) fresh_candidate="$(pwd -P)/$fresh_candidate" ;;
    esac
    fresh_suggestion=
    [ -z "$workspace" ] || fresh_suggestion=$fresh_candidate
    fresh_definition="$HOME/Library/LaunchAgents/$SERVER_LABEL.plist"
    if [ ! -e "$fresh_definition" ] &&
        ! is_set_up_workspace "$fresh_candidate" &&
        ! is_set_up_workspace "$HOME/Ciaobot"; then
        fresh_macos=1
    fi
fi

if [ "$migrate_path" != client ]; then
    if [ "$fresh_macos" -eq 1 ]; then
        # Nothing is created here. `ciao run --installer-bootstrap` refuses to
        # start on a machine that is not fresh, starts the wizard, and writes
        # one handoff result only after setup succeeded; the parent activates
        # the service from that result. A cancel or refusal leaves a complete
        # binary install whose setup can be resumed.
        if [ "$no_start" -eq 0 ]; then
            # The bootstrap serves loopback only, so it can start on a port
            # that the activated engine, which may listen on every interface,
            # cannot bind. Warn, but let Advanced setup select another port.
            port=$FRESH_PORT
            probe_rc=0
            python_probe_port "$port" || probe_rc=$?
            if [ "$probe_rc" -eq 1 ]; then
                owner=$(port_owner "$port")
                echo "Warning: port $port is already in use${owner:+ by $owner}. Choose another port in Advanced setup, or free port $port before completing setup." >&2
            fi
            run_id=$(uuidgen 2>/dev/null | tr '[:upper:]' '[:lower:]' || true)
            [ -n "$run_id" ] || fail "could not generate an installer run id"
            set -- run --installer-bootstrap --installer-run-id "$run_id"
            # `--flag=value` so a suggestion that begins with a dash still
            # reaches the wizard as a path.
            [ -z "$fresh_suggestion" ] || set -- "$@" "--installer-suggest-workspace=$fresh_suggestion"
            # The bootstrap's activation starts the engine, which runs out of the
            # signed host this run just installed. From here a failed run has to
            # quiesce that job before the previous host goes back.
            if [ "$native_host_pending" -ne 0 ]; then
                native_host_capture_pre_responder
                native_host_service_started=1
            fi
            bootstrap_rc=0
            "$ciao" "$@" || bootstrap_rc=$?
            if [ "$bootstrap_rc" -ne 0 ]; then
                echo "Ciaobot is installed, but setup did not finish. Nothing in a workspace was created or changed by this installer." >&2
                echo "Resume setup with: $(quote_shell_path "$ciao") run --installer-bootstrap" >&2
                exit "$bootstrap_rc"
            fi
            if ! activation_output=$("$ciao" installer activate --run-id "$run_id" --json); then
                [ -z "$activation_output" ] || printf '%s\n' "$activation_output" >&2
                echo "Ciaobot is installed, but the background service did not start. Nothing in the chosen folder was deleted or moved." >&2
                echo "Resume with: $(quote_shell_path "$ciao") installer activate --run-id $run_id" >&2
                exit 1
            fi
            # Only the verified activation result knows the wizard's chosen
            # port. Parse data, never shell text or private workspace state, and
            # refuse an ambiguous result rather than guessing the default port.
            if ! activated_fields=$("$tool_python" -I -c '
import json
import os
import sys

def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate activation field")
        result[key] = value
    return result

try:
    result = json.loads(sys.argv[1], object_pairs_hook=unique_fields)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("activation was not successful")
    port = result.get("port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("invalid activation port")
    workspace = result.get("workspace")
    definition = result.get("definition")
    for path in (workspace, definition):
        if not isinstance(path, str) or not os.path.isabs(path) or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise ValueError("invalid activation path")
    if result.get("service_backend") != "macos":
        raise ValueError("invalid activation backend")
except (ValueError, TypeError):
    sys.exit(1)
print(workspace)
print(port)
' "$activation_output"); then
                echo "Ciaobot is installed, but could not verify the background service activation result. Nothing in the chosen folder was deleted or moved." >&2
                echo "Resume with: $(quote_shell_path "$ciao") installer activate --run-id $run_id" >&2
                exit 1
            fi
            workspace=$(printf '%s\n' "$activated_fields" | head -1)
            port=$(printf '%s\n' "$activated_fields" | tail -1)
            fresh_url="http://localhost:$port"
            fresh_completed=1
        else
            workspace=
        fi
    else
    # --yes turns off every guard in `ciao setup` (a health check that never
    # comes up, a service silently re-pointed at a different workspace), so it
    # is passed only for the workspace this script detected the engine already
    # running in - the same rule install.sh applies.
    setup_yes=
    if [ -z "$workspace" ]; then
        workspace=
        if [ "$migrate_path" = host ] && [ -n "$migrate_workspace" ]; then
            # Read out of the plist by the verified wheel, or out of the receipt
            # of an unfinished hand-over: either way this is the workspace the
            # engine being replaced runs in, so it is the one the new engine
            # keeps, and check_host_workspace has already settled that it is
            # this one. Nothing inside it is written.
            workspace=$migrate_workspace
            setup_yes=1
        fi
        if [ -z "$workspace" ] && [ -f "$plist" ] && [ -x "$PLISTBUDDY" ]; then
            existing=$("$PLISTBUDDY" -c 'Print :WorkingDirectory' "$plist" 2>/dev/null || true)
            if [ -n "$existing" ] && is_set_up_workspace "$existing"; then
                workspace=$existing
                setup_yes=1
            fi
        fi
        [ -n "$workspace" ] || workspace="$HOME/Ciaobot"
    fi
    if [ "$migrate_path" = host ]; then
        # No `mkdir` on a host hand-over: check_host_workspace has already proved
        # the workspace is there, and a directory created here would be a second
        # one - with a fresh .env and a fresh runtime root - rather than the
        # workspace whose data this hand-over exists to keep.
        [ -d "$workspace" ] || fail "the workspace to hand over to is not a directory: $workspace"
    else
        mkdir -p "$workspace"
    fi
    # CDPATH would send `cd` looking for a matching directory elsewhere and print
    # the path it found, so it is cleared for this one command.
    workspace=$(CDPATH= cd -- "$workspace" && pwd -P)

    # The port is checked before the service is registered: an engine that
    # cannot bind dies and is respawned forever, and the operator was told
    # the engine was "still starting" with a dead sign-in link (#1654).
    # Ciaobot's own engine already serving the port is legitimate - that is
    # a re-install of a running engine - so the check skips it.
    #
    # The port is the one the workspace names, and a workspace that has none
    # yet gets the default: setup writes the same value, so the check and
    # the engine agree on which port is at stake.
    if [ "$no_start" -eq 0 ]; then
        port=$(awk -F= '/^PWA_PORT=/{print $2}' "$workspace/.env" 2>/dev/null | tr -d '"' | tail -1)
        [ -n "$port" ] || port=8443
        check_port_available "$port" "$workspace"
    fi

    # Idempotent, and it preserves an existing .env and its password. stderr is
    # left visible so setup's own guard message reaches the user.
    set -- --workspace "$workspace" --python "$ciao"
    [ -z "$setup_yes" ] || set -- "$@" --yes
    # Reload the agent so launchd drops any job definition it already holds
    # (another engine's), otherwise `service start` restarts the old one.
    [ "$no_start" -ne 0 ] || set -- "$@" --load-launchd
    # From here a job may be running out of the signed host this run just
    # installed: `--load-launchd` reloads the agent and starts it. A later
    # failure has to boot that job out and wait for it before the previous host is
    # put back, so the fact is recorded before the call, not after.
    if [ "$native_host_pending" -ne 0 ] && [ "$no_start" -eq 0 ]; then
        native_host_capture_pre_responder
        native_host_service_started=1
    fi
    # `ciao setup` reports a workspace whose memory regions could not be set
    # up by exiting 3, which is not a failed install: everything else it
    # scaffolds is there, and a rollback would throw all of that away. Only
    # that one code is tolerated.
    #
    # Its stdout is kept out of the terminal but captured, because setup names
    # every path it wrote, one per line. The welcome-pending marker appears in
    # that list only when setup generated the password itself, which is the
    # per-run evidence the final summary needs; the marker's mere existence is
    # not (an update leaves the old one in place). The file can carry a sign-in
    # credential, so it is never printed.
    setup_rc=0
    "$ciao" setup "$@" >"$tmp/setup-output" || setup_rc=$?
    case "$setup_rc" in
        0) ;;
        3) echo "warning: ciao setup could not set up memory regions (see the warning above); continuing install" >&2 ;;
        *) abort_install "ciao setup failed" ;;
    esac
    # Literal, full-line match: a workspace path with spaces or metacharacters
    # must not turn into a pattern, and a path that merely contains the marker's
    # spelling is not the marker setup wrote.
    if grep -Fxq -- "$workspace/.runtime/welcome-pending" "$tmp/setup-output"; then
        password_created=1
    fi
    fi
fi

if [ "$no_start" -eq 0 ] && [ "$migrate_path" != client ] &&
    { [ "$fresh_macos" -eq 0 ] || [ "$fresh_completed" -eq 1 ]; }; then
    # Fresh setup uses the validated activation port; existing installs keep
    # their workspace configuration as the authority.
    if [ "$fresh_macos" -eq 0 ]; then
        port=$(awk -F= '/^PWA_PORT=/{print $2}' "$workspace/.env" 2>/dev/null | tr -d '"' | tail -1)
        [ -n "$port" ] || port=8443
    fi
    # A fresh install's engine was already started by `ciao installer
    # activate`; it still has to answer before the installer says so.
    if [ "$fresh_macos" -eq 0 ]; then
        install_step "could not start the engine; try: $(quote_shell_path "$ciao") service status" \
            "$ciao" service start --workspace "$workspace" --json >/dev/null
    fi
    # A slow first boot is not a failed install: the plist is written, so say
    # so instead of failing an install that did everything it promised. A
    # migration is the exception - the app's own agent is about to be retired,
    # and only an engine answering with the version just installed proves the
    # hand-over worked.
    health_attempts=60
    if [ "$migrate_path" = host ]; then
        health_attempts=90
    fi
    # Only a record written after this point describes this run.
    : > "$tmp/health-wait-started"
    attempt=0
    healthy=0
    port_in_use=0
    while [ "$attempt" -lt "$health_attempts" ]; do
        # An engine that could not bind its port cannot answer this, so the
        # record it left outside the workspace is read too (#1654). Its path
        # is ciao.port_in_use.port_in_use_path() (PORT_IN_USE_FILENAME):
        # keep both in sync.
        port_record="$HOME/.local/state/ciaobot/port-in-use.txt"
        if [ -f "$port_record" ] && [ "$port_record" -nt "$tmp/health-wait-started" ]; then
            port_in_use=1
            break
        fi
        if [ "$fresh_macos" -eq 1 ]; then
            # Bound both time and response size; discard even a partial body
            # when curl fails (including its size/time limit).
            if "$CURL" -fsS --max-time 5 --max-filesize 65536 \
                "http://localhost:$port/api/startup-status" \
                -o "$tmp/startup-status.json" 2>/dev/null; then
                status=fresh
            else
                status=
            fi
        else
            status=$("$CURL" -fsS "http://localhost:$port/api/startup-status" 2>/dev/null || true)
        fi
        if [ -n "$status" ]; then
            if [ "$fresh_macos" -eq 1 ]; then
                startup_state=$(fresh_startup_state "$tmp/startup-status.json" 2>/dev/null || true)
                case "$startup_state" in
                    ready)
                        if [ "$native_host_pending" -eq 0 ]; then
                            healthy=1
                            break
                        fi
                        # A bare `ready` is not proof of the candidate either: the
                        # same process-instance probe applies here.
                        if native_host_probe_candidate "$port" "$version"; then
                            healthy=1
                            break
                        fi
                        ;;
                    port_in_use) port_in_use=1; break ;;
                esac
            else
                # Preserve established-install and migration readiness rules.
                # The engine reports a bind failure as an explicit flag (#1654).
                if printf '%s' "$status" | grep -q '"port_in_use":[[:space:]]*true'; then
                    port_in_use=1
                    break
                fi
                # Established installs accept any answer from Ciaobot's engine on
                # the configured port... unless this run started a native host
                # upgrade. Then `healthy=1` from an unrelated old or wrong-version
                # server is not evidence that the engine using the new signed host
                # came up, so the strict version match below is required.
                if [ "$migrate_path" != host ] && [ "$native_host_pending" -eq 0 ]; then
                    healthy=1
                    break
                fi
                # Match the engine version, not a substring of latest_version.
                reported=$(printf '%s\n' "$status" \
                    | sed -n 's/.*"version":[[:space:]]*"\([^"]*\)".*/\1/p' \
                    | head -1)
                if [ "$reported" = "$version" ]; then
                    if [ "$native_host_pending" -eq 0 ]; then
                        healthy=1
                        break
                    fi
                    # Positive evidence that the responder is the process instance
                    # this install started: one listener before and the same one
                    # after the request, that pid in the runtime lock, the reported
                    # startup id in the same lock, and that listener's ancestry
                    # reaching launchd's job - whose kernel-named executable is
                    # the installed host. A version string says nothing about any
                    # of that.
                    if native_host_probe_candidate "$port" "$version"; then
                        healthy=1
                        break
                    fi
                fi
            fi
        fi
        attempt=$((attempt + 1))
        "$SLEEP" 1
    done
    # Written by the supervisor when macOS denies the workspace; the path is
    # ciao.workspace_access.denial_path() (DENIAL_FILE_NAME): keep both in sync.
    access_denied=
    denial_record="$HOME/.local/state/ciaobot/workspace-access-denied.txt"
    if [ "$denial_record" -nt "$tmp/health-wait-started" ]; then
        access_denied=$(cat "$denial_record" 2>/dev/null || true)
    fi
    if [ "$port_in_use" -ne 0 ]; then
        # The engine could not bind its port (#1654). The install itself
        # succeeded - the wheel is installed and the workspace is set up - but
        # the engine is not serving, so no sign-in link is printed: a link
        # that cannot work is worse than no link.
        owner=$(port_owner "$port")
        if [ -n "$owner" ]; then
            echo "Ciaobot engine installer: port $port is already in use by $owner. Free port $port, or set PWA_PORT in $workspace/.env to serve on a different port" >&2
        else
            echo "Ciaobot engine installer: port $port is already in use by another program. Free port $port, or set PWA_PORT in $workspace/.env to serve on a different port" >&2
        fi
        engine_ready=0
        # A pending host upgrade was only justified by an engine that would use
        # it. One that could not bind is not that engine, so the previous host
        # comes back instead of leaving the signed one installed unproven.
        if [ "$native_host_pending" -ne 0 ]; then
            abort_install "the new engine could not bind port $port, so the previous Ciaobot Server host was restored"
        fi
    elif [ "$healthy" -eq 0 ]; then
        engine_ready=0
        if [ "$migrate_path" = host ] || [ "$native_host_pending" -ne 0 ]; then
            if [ -n "$access_denied" ]; then
                abort_install "$access_denied"
            fi
            if [ "$native_host_pending" -ne 0 ] && [ -n "${native_host_unproven:-}" ]; then
                abort_install "the new engine this run started was not proven within ${health_attempts}s: no ${native_host_unproven}. Check: $(quote_shell_path "$ciao") service status"
            fi
            abort_install "the new engine this run started did not answer on http://localhost:$port with version $version within ${health_attempts}s; check: $(quote_shell_path "$ciao") service status"
        fi
        if [ -n "$access_denied" ]; then
            echo "Ciaobot engine installer: $access_denied" >&2
        else
            echo "Ciaobot engine installer: the engine is still starting; check: $(quote_shell_path "$ciao") service status" >&2
        fi
    else
        engine_ready=1
    fi
else
    engine_ready=1
fi

# An ordinary update that replaced the server host commits here: the exact newly
# installed version answered on the configured port. A desktop hand-over commits
# further down, after the retirement steps that can still roll the engine back.
if [ "$migrate_path" != host ] && [ "$native_host_pending" -ne 0 ]; then
    native_host_commit
fi

if [ "$migrate_path" = client ]; then
    install_step "could not write the migration receipt" migration_receipt migrated_client
    migration_active=0
    echo "Ciaobot engine $version installed. This Mac runs no engine."
    echo "  ciao: $ciao"
    echo "This Mac was a client of $migrate_host_url. Open that address in your browser and sign in there; this Mac no longer runs its own engine."
    if [ -t 1 ] && command -v open >/dev/null 2>&1; then
        open "$migrate_host_url" >/dev/null 2>&1 || true
    fi
    exit 0
fi

if [ "$migrate_path" = host ]; then
    if [ "$no_start" -eq 0 ]; then
        # A durable phase a crash can resume from: the new engine is proven, and
        # the app's own agent is not retired yet. `retiring_desktop` goes in with
        # it because from this line on launchd may be about to lose that job, and
        # a run that stops in the middle of the retirement has to be resumed as
        # though it did - otherwise its rollback re-enables a label with no job
        # behind it and reports the engine restored. Only once the retirement
        # itself has succeeded does this become `migrated`.
        retiring_desktop=1
        install_step "could not write the migration receipt" migration_receipt retiring
        migration_active=0
        if retire_desktop_agent; then
            retiring_desktop=0
            install_step "could not write the migration receipt" migration_receipt migrated
            # The engine is proven and the app's own agent is retired: this is
            # the last step that could still roll the hand-over back, so a pending
            # native host upgrade is committed here and not before.
            native_host_commit
        else
            # The new engine is up, but the app's agent is still loaded: a
            # desktop relaunch would bring it back next to the engine that was
            # supposed to replace it. That is not a migration that worked, so
            # the app's engine is put back and the failure is reported.
            migration_active=1
            abort_install "Ciaobot.app's own agent could not be retired: launchctl refused to boot it out or disable it"
        fi
        # Only now, with the receipt at `migrated` and the app's own agent gone,
        # is the bundle inert: nothing runs out of it and no launchd job points
        # at it, so leaving it only invites a double-click that brings the old
        # PWA back against an engine this Mac no longer has. The `.app` guard
        # stays - this is an irreversible delete of a path read out of a plist,
        # not something the user typed. A removal that fails (a /Applications
        # install the user cannot write) is reported rather than fatal: the
        # migration itself has already succeeded and must not be rolled back
        # over a leftover directory.
        if [ -z "$migrate_app_bundle" ]; then
            # A hand-over this run cannot name a bundle for - a receipt written
            # before the field existed, so the transaction that is being
            # finished never recorded one. Silence is the one answer that is
            # wrong here: the app is still on disk and still does what it did
            # before, and a run that says nothing about it is the dead-but-
            # misleading state this step exists to end.
            echo "Ciaobot.app could not be removed automatically; remove it with: $(quote_shell_path "$ciao") desktop uninstall"
        elif [ -d "$migrate_app_bundle" ]; then
            case "$migrate_app_bundle" in
                *.app)
                    if rm -rf "$migrate_app_bundle"; then
                        echo "Removed the retired Ciaobot.app."
                    else
                        echo "Ciaobot.app could not be removed automatically; remove it with: $(quote_shell_path "$ciao") desktop uninstall"
                    fi
                    ;;
                *)
                    # Not a bundle: nothing this step is for, and not a path
                    # this installer is willing to delete either.
                    ;;
            esac
        fi
    else
        # --no-start promised no service, and an engine that was never started
        # cannot retire anything. The receipt says so; re-running without
        # --no-start is what completes the hand-over.
        install_step "could not write the migration receipt" migration_receipt installed_no_start
        migration_active=0
    fi
fi

echo "Ciaobot engine $version installed."
echo "  ciao:      $ciao"
[ -z "$workspace" ] || echo "  workspace: $workspace"
configure_path
url=
if [ "$fresh_macos" -eq 1 ]; then
    if [ "$fresh_completed" -eq 1 ] && [ "$engine_ready" -eq 1 ]; then
        echo "Ciaobot is running from the folder you chose. Return to the browser tab to finish onboarding."
        echo "If you closed it, open $fresh_url and log in with the password you set."
        if [ -t 1 ] && command -v open >/dev/null 2>&1; then
            open "$fresh_url" >/dev/null 2>&1 || true
        fi
    elif [ "$fresh_completed" -eq 1 ]; then
        # The health wait has already said why; setup itself is saved.
        echo "Setup is saved in the folder you chose, but Ciaobot is not answering yet. Once it is, open $fresh_url and log in with the password you set."
        exit 1
    else
        echo "Setup was not started. Resume with: $(quote_shell_path "$ciao") run --installer-bootstrap"
    fi
elif [ "$no_start" -eq 0 ] && [ "$engine_ready" -eq 1 ]; then
    # Printed to the terminal and nowhere else: the URL is a one-time
    # credential that signs the person at this keyboard in. A failure must not
    # reach the user as "Open Ciaobot: " with nothing after it.
    url_output=$("$ciao" setup-url --workspace "$workspace") \
        || fail "could not create the sign-in link; run: $(quote_shell_path "$ciao") setup-url --workspace $workspace"
    url=$(printf '%s\n' "$url_output" | tail -1)
    case "$url" in
        http://*) ;;
        *) fail "could not create the sign-in link; run: $(quote_shell_path "$ciao") setup-url --workspace $workspace" ;;
    esac
    echo "Open Ciaobot: $url"
    echo "This link signs you in once. Do not share it."
fi
# `ciao setup` generates the password only for a workspace that has none yet,
# so the final summary must key on this run's own written-files output rather
# than on a welcome-pending marker an update inherited (#2162).
if [ "$migrate_path" != client ] && [ "$password_created" -eq 1 ]; then
    echo "Your Ciaobot password was created for you. Ciaobot shows it once on Home on this computer: save it in your password manager."
fi
if [ -t 1 ] && [ "$fresh_macos" -eq 0 ] && [ "$no_start" -eq 0 ] && [ "$engine_ready" -eq 1 ] && command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
fi
