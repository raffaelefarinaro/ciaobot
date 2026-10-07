"""Reading a past conversation into one provider-neutral shape.

A conversation importer needs the same three facts whichever agent wrote the
session: which conversation this is, what was said in it, and what was left out.
This package is where those three are defined and filled in —
:mod:`ciao.import_sources.contract` holds the shape, and the adapters beside it
read one provider's own storage into it (``claude_code`` and ``opencode`` today).

Everything downstream of here is provider-blind: the tool-less extractor (C4),
the consent UI (C5) and the batch store (C6) consume a
:class:`~ciao.import_sources.contract.NormalizedSession` and never learn whether
it came from Claude Code or OpenCode. That is the point — a decision about what
to drop from someone's history is made once, in one place, and recorded in the
session's omissions rather than made again per adapter.

No model, no network, no engine. The adapters read one provider's own storage
and nothing else — the Claude Code one from a transcript file, the OpenCode one
through the supported V2 CLI's ``session export``/``session list``, each refusing
to follow a link, read past
:data:`~ciao.import_sources.contract.MAX_SESSION_BYTES`, or return a session it
could not decide the contents of.
"""

from ciao.import_sources.contract import (
    KNOWN_PROVIDERS,
    MAX_SESSION_BYTES,
    OMISSION_KINDS,
    PROVIDER_CLAUDE_ACCOUNT,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_OPENCODE,
    ROLE_ASSISTANT,
    ROLE_OTHER,
    ROLE_USER,
    ROLES,
    SUPPORTED_PROVIDERS,
    ContractError,
    NormalizedMessage,
    NormalizedSession,
    Omission,
    SourceRef,
)
from ciao.import_sources.opencode import (
    DISCOVERY_MAX_COUNT,
    SourceError,
    discover_opencode_sessions,
    read_opencode_session,
)

__all__ = [
    "DISCOVERY_MAX_COUNT",
    "KNOWN_PROVIDERS",
    "MAX_SESSION_BYTES",
    "OMISSION_KINDS",
    "SUPPORTED_PROVIDERS",
    "PROVIDER_CLAUDE_ACCOUNT",
    "PROVIDER_CLAUDE_CODE",
    "PROVIDER_OPENCODE",
    "ROLES",
    "ROLE_ASSISTANT",
    "ROLE_OTHER",
    "ROLE_USER",
    "ContractError",
    "NormalizedMessage",
    "NormalizedSession",
    "Omission",
    "SourceError",
    "SourceRef",
    "discover_opencode_sessions",
    "read_opencode_session",
]