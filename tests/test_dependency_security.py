"""Regression guards for the Python runtime security pins (#953, #1081).

PyJWT 2.13.0 mutated the caller's ``options`` dict in place when a token was
decoded without signature verification (GHSA-gvp8-978c-rx2q, alert #61):
``_merge_options`` never copied the dict, so it inserted its own defaults —
``verify_exp``, ``verify_aud`` and the other claim checks — directly into the
caller's options. An application that reuses one options dict across calls
(the documented "peek at the payload, then verify" pattern) therefore found
those claim checks still disabled on the second call: setting
``verify_signature: True`` still verified the signature, but the token's
expiry was no longer checked. Signature verification itself is unaffected.
PyJWT 2.15.1 copies the options before applying defaults, so the dict handed
in comes back byte-equivalent.

Ciaobot itself never calls ``jwt.decode``: it mints opaque session tokens and
delegates JWT to MCP, so this test guards the pinned version rather than a
Ciaobot code path. It uses only the installed package, a fixed in-process key
and a token whose ``exp`` is already in the past — no secret, no network and no
wall-clock wait.

``multidict`` 6.7.1 carries CVE-2026-104874 (a reference leak in the
``CIMultiDict``/``MultiDict`` items-view union and subtraction paths,
availability-only), fixed in 6.9.1. It is reached at runtime through ``mcp``
1.29.0 → ``aiohttp`` 3.14.3 → ``multidict``, and ``aiohttp`` allows
``multidict>=4.5,<7.0``, so the patched release is permitted. Unlike the PyJWT
pin, this one guards no reproduced Ciaobot behaviour: it is pinned for
reachability, and the test asserts the installed floor rather than an app
invariant.
"""

from __future__ import annotations

import copy
import datetime as dt
from importlib import metadata

import jwt
import pytest


def _as_version(raw: str) -> tuple[int, ...]:
    """Compare release numbers without pulling in a version parser."""
    parts: list[int] = []
    for chunk in raw.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def test_pyjwt_unverified_peek_does_not_mutate_reused_options() -> None:
    key = b"ciaobot-test-key-0123456789abcdef"  # 33 bytes, HS256 minimum
    now = dt.datetime.now(tz=dt.timezone.utc)
    token = jwt.encode(
        {
            "sub": "regression",
            "iat": now - dt.timedelta(seconds=120),
            "exp": now - dt.timedelta(seconds=60),
        },
        key,
        algorithm="HS256",
    )

    options = {"verify_signature": False}
    before = copy.deepcopy(options)

    payload = jwt.decode(token, options=options, algorithms=["HS256"])
    assert payload["sub"] == "regression"
    assert options == before, (
        "jwt.decode mutated the caller's options dict; pinned PyJWT must copy "
        "options before applying its defaults (#953)"
    )

    # The reused dict still means what the caller wrote: flipping signature
    # verification back on and reusing the same dict now has to check the
    # signature and the expiry, and the already-expired token must be refused.
    options["verify_signature"] = True
    with pytest.raises(jwt.ExpiredSignatureError):
        jwt.decode(token, key=key, options=options, algorithms=["HS256"])


def test_multidict_floor_covers_cve_2026_104874() -> None:
    version = metadata.version("multidict")
    assert _as_version(version) >= _as_version("6.9.1"), (
        f"multidict {version} predates the CVE-2026-104874 fix in 6.9.1; the "
        "runtime pin exists because a wheel install resolves the declared "
        "dependency, not uv.lock (#1081)"
    )
