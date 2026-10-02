"""Regression guards for the Python runtime security pins (#953).

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
"""

from __future__ import annotations

import copy
import datetime as dt

import jwt
import pytest


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
