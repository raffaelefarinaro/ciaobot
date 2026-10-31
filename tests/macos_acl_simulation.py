"""Model macOS's extended ACL only on a host that has no macOS ACL API.

The host installer is a macOS path, so its tests set ``sys.platform = "darwin"``
in a child interpreter to reach the macOS branches. That simulation must not
make the strict private-file check call ``acl_get_fd_np`` in a Linux libc: the
extended ACL question is a native macOS dependency, and a simulated Mac supplies
a hermetic answer to it instead. Production code never imports this module.

The substitution is deliberately narrow:

* the decision probes libc for the macOS ACL calls, not the spoofed
  ``sys.platform``;
* an API that is present is never replaced, so a real Mac keeps exercising the
  real ACL;
* a real Mac with a missing API is never given a stand-in either — that case
  must stay the fail-closed ``OSError`` the production reader raises.

Only a host that is not macOS *and* lacks the API gets the fixed answer.
"""

from __future__ import annotations

import ctypes
import os

_MACOS_ACL_SYMBOLS = (
    "acl_get_fd_np",
    "acl_get_entry",
    "acl_get_tag_type",
    "acl_get_qualifier",
    "acl_free",
    "mbr_uid_to_uuid",
)


def _host_is_macos() -> bool:
    """Whether this process is really on macOS, ignoring ``sys.platform``."""
    uname = getattr(os, "uname", None)
    return uname is not None and uname().sysname == "Darwin"


def _libc_has_macos_acl() -> bool:
    """Whether this host's libc really exports the macOS extended-ACL calls."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except (AttributeError, OSError):
        return False
    try:
        return all(hasattr(libc, name) for name in _MACOS_ACL_SYMBOLS)
    except (AttributeError, OSError):
        return False


def install_hermetic_macos_acl(*, grants_another_account: bool = False) -> None:
    """Stand in for the native macOS ACL reader where the host has none.

    ``False`` is what a freshly created ``0o600`` file on macOS reports: no
    extended ACL entry allows another principal. ``True`` models the hostile
    case where a mode-private file still carries such a grant.

    On a host whose libc exports the macOS calls this is a no-op, so a real Mac
    subprocess keeps using the real ACL. A real Mac whose API is missing is
    also left alone: that must fail closed in the production reader rather than
    be masked by a test harness.

    Call this only from a harness that models the macOS path without intending
    to exercise the native ACL itself. The real macOS ACL tests live in
    ``tests/test_os_support_private.py`` and must not use this helper.
    """
    if _libc_has_macos_acl() or _host_is_macos():
        return
    from ciao.os_support import private

    private._acl_grants_another_account = (  # type: ignore[assignment]
        lambda fd: grants_another_account
    )
