"""Anonymous bug reporting via a public Google Form.

When the startup-triage agent (or a user) hits a genuine Ciaobot app bug and
the ``gh`` CLI is not available/authenticated, it can file the report to a
central inbox instead of asking the user to open GitHub by hand. The report is
POSTed to a public Google Form; a scheduled sync on the maintainer's instance
turns new form responses into GitHub issues.

The form endpoint and field entry IDs are **public** (a Google Form accepts
anonymous submissions by design), not secrets, so they ship as defaults. Each
is overridable via environment for forks, testing, or a relocated form:

- ``CIAO_BUG_REPORT_FORM_URL``    — the form's ``…/formResponse`` endpoint
- ``CIAO_BUG_REPORT_ENTRY_TITLE`` — entry id for the Title field
- ``CIAO_BUG_REPORT_ENTRY_DETAILS`` — entry id for the Details field
- ``CIAO_BUG_REPORT_ENTRY_SYSTEM`` — entry id for the System Info field

If the form is not configured, :func:`submit_bug_report` is a no-op that
returns ``False`` so callers can fall back to a paste-ready report.
"""
from __future__ import annotations

import os
import platform
import sys
import urllib.parse
import urllib.request

# Public Google Form submission endpoint + field entry ids. Filled in once the
# maintainer's "Ciaobot Bug Reports" form exists; overridable via env.
_DEFAULT_FORM_URL = ""
_DEFAULT_ENTRY_TITLE = ""
_DEFAULT_ENTRY_DETAILS = ""
_DEFAULT_ENTRY_SYSTEM = ""

_TIMEOUT_SECONDS = 15


def _form_config() -> tuple[str, str, str, str]:
    """Return ``(form_url, entry_title, entry_details, entry_system)`` from the
    environment, falling back to the baked-in public defaults."""
    return (
        os.environ.get("CIAO_BUG_REPORT_FORM_URL", _DEFAULT_FORM_URL).strip(),
        os.environ.get("CIAO_BUG_REPORT_ENTRY_TITLE", _DEFAULT_ENTRY_TITLE).strip(),
        os.environ.get("CIAO_BUG_REPORT_ENTRY_DETAILS", _DEFAULT_ENTRY_DETAILS).strip(),
        os.environ.get("CIAO_BUG_REPORT_ENTRY_SYSTEM", _DEFAULT_ENTRY_SYSTEM).strip(),
    )


def is_configured() -> bool:
    """True when the form URL and all three field ids are set."""
    return all(_form_config())


def gather_system_info() -> str:
    """A compact one-line system fingerprint for a bug report."""
    try:
        from ciao import __version__ as ciao_version
    except Exception:  # noqa: BLE001 — version import must never break reporting
        ciao_version = "unknown"
    py = ".".join(str(p) for p in sys.version_info[:3])
    return (
        f"Ciaobot {ciao_version}; Python {py}; "
        f"{platform.system()} {platform.release()} ({platform.machine()})"
    )


def submit_bug_report(title: str, details: str, system_info: str | None = None) -> bool:
    """POST a bug report to the public Google Form.

    Returns ``True`` on a successful submission, ``False`` if the form is not
    configured or the POST fails. Never raises — the caller can always fall
    back to printing a paste-ready report.
    """
    form_url, entry_title, entry_details, entry_system = _form_config()
    if not (form_url and entry_title and entry_details and entry_system):
        print(
            "Bug-report form is not configured "
            "(set CIAO_BUG_REPORT_FORM_URL and the entry ids); skipping submission.",
            file=sys.stderr,
        )
        return False

    payload = {
        entry_title: title,
        entry_details: details,
        entry_system: system_info or gather_system_info(),
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(
        form_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            # Google Forms returns 200 on a successful submission.
            return 200 <= response.status < 300
    except Exception as exc:  # noqa: BLE001 — reporting must degrade gracefully
        print(f"Failed to submit bug report to Google Form: {exc}", file=sys.stderr)
        return False
