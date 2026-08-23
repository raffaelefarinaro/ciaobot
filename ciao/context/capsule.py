"""One small, provider-neutral context capsule for normal chat turns.

The capsule is deliberately separate from provider system prompts. It carries
only request-scoped routing facts and entity hints; native ``CLAUDE.md`` /
``AGENTS.md`` loaders remain the source of instructions and memory.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ciao.context.entity_tagger import find_entities, format_entities


def _field(value: str, *, limit: int = 1200) -> str:
    """Keep user-controlled routing metadata single-line and bounded."""
    return " ".join(str(value or "").split())[:limit]


def build_context_capsule(
    *,
    prompt: str,
    workspace: str = "",
    gws_profile: str = "",
    project_name: str = "",
    project_context: str = "",
    canonical_doc: str = "",
    vault_root: Path | None = None,
    workspace_vault_root: str = "",
    legacy_entity_workspace: str = "",
    entity_index_owns_workspace: bool = False,
    unattended: bool = False,
    handover: str = "",
    include_stable: bool = True,
) -> str:
    """Render the compact context visible to every provider.

    ``workspace_vault_root`` is the active workspace's vault, relative to the
    provider's cwd, so a path the model writes is usable verbatim.

    Stable project facts can be omitted after the first turn of a provider
    session. Date, entity hints, and handover data remain dynamic and are
    intentionally calculated from the current prompt.
    """
    stable: list[str] = []
    if workspace:
        stable.append(f"workspace={_field(workspace, limit=120)}")
    if workspace_vault_root:
        # The workspace *name* is not a location. Without this the model knew it
        # was in "work" but had to guess where that workspace's vault lived, and
        # guessed from precedent — which on a vault whose People/ folder had been
        # filled by the old single-workspace curator meant writing every new
        # contact back into the wrong workspace. Naming the path is what stops
        # the misfiling recurring at the write step.
        stable.append(f"vault={_field(workspace_vault_root, limit=300)}")
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
                index_owns_workspace=entity_index_owns_workspace,
            )
        except Exception:  # noqa: BLE001 - context enrichment is fail-open
            entities = []
        formatted = format_entities(entities)
        if formatted:
            dynamic.append(formatted)
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
