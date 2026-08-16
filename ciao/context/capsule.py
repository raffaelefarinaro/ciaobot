"""One small, provider-neutral context capsule for normal chat turns.

The capsule is deliberately separate from provider system prompts. It carries
only request-scoped routing facts and entity hints; native ``CLAUDE.md`` /
``AGENTS.md`` loaders remain the source of instructions and memory.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from ciao.context.entity_tagger import find_entities, format_entities

_RETRIEVAL_RE = re.compile(
    r"\b(?:remember|recall|what did we|what have we|previously|last time|"
    r"find|search|look up|decision|decided|note about|notes about)\b",
    re.IGNORECASE,
)


def _field(value: str, *, limit: int = 1200) -> str:
    """Keep user-controlled routing metadata single-line and bounded."""
    return " ".join(str(value or "").split())[:limit]


def needs_retrieval_hint(prompt: str) -> bool:
    """Return whether the prompt likely needs a vault search before answering."""
    return bool(_RETRIEVAL_RE.search(prompt or ""))


def build_context_capsule(
    *,
    prompt: str,
    workspace: str = "",
    gws_profile: str = "",
    project_name: str = "",
    project_context: str = "",
    canonical_doc: str = "",
    vault_root: Path | None = None,
    legacy_entity_workspace: str = "",
    unattended: bool = False,
    handover: str = "",
    include_stable: bool = True,
) -> str:
    """Render the compact context visible to every provider.

    Stable project facts can be omitted after the first turn of a provider
    session. Date, entity hints, retrieval routing, and handover data remain
    dynamic and are intentionally calculated from the current prompt.
    """
    stable: list[str] = []
    if workspace:
        stable.append(f"workspace={_field(workspace, limit=120)}")
    if gws_profile:
        stable.append(f"gws_profile={_field(gws_profile, limit=120)}")
    if project_name and project_name != "General":
        stable.append(f'project="{_field(project_name, limit=180)}"')
    if project_context:
        stable.append(f"project_context={_field(project_context)}")
    if canonical_doc:
        stable.append(f"canonical_doc={_field(canonical_doc, limit=300)}")

    dynamic: list[str] = [f"today={datetime.now(UTC).date().isoformat()}"]
    if vault_root is not None:
        try:
            entities = find_entities(
                prompt,
                vault_root,
                workspace=workspace,
                legacy_workspace=legacy_entity_workspace,
            )
        except Exception:  # noqa: BLE001 - context enrichment is fail-open
            entities = []
        formatted = format_entities(entities)
        if formatted:
            dynamic.append(formatted)
    if needs_retrieval_hint(prompt):
        dynamic.append(
            "retrieval_hint=Use vault_search snippets as private evidence; do "
            "not open full vault notes for pure recall."
        )
    if unattended:
        dynamic.append(
            "unattended=true; this turn was fired automatically. Do not ask "
            "questions or wait for approval."
        )

    parts: list[str] = []
    if include_stable:
        parts.extend(stable)
    parts.extend(dynamic)
    if handover:
        parts.append(handover)
    if not parts:
        return ""
    return "<ciao-context>\n" + "\n".join(parts) + "\n</ciao-context>"


def context_digest(
    *,
    workspace: str,
    gws_profile: str,
    project_name: str,
    project_context: str,
    canonical_doc: str,
) -> str:
    """Return a stable digest for deciding whether routing facts changed."""
    raw = "\0".join(
        (workspace, gws_profile, project_name, project_context, canonical_doc)
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
