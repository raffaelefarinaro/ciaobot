"""One accurate, provider-neutral memory and unattended-execution policy.

The memory system has more than one way to write durable memory, and they do
not all have the same approval rule. Before this module the copies drifted:
the architecture said new memory needs review while archive extraction called
``auto_promote_memory=True``; the memory agent said the typed path enforces the
cap while ``update_region`` documents and implements an advisory one; and the
unattended capsule said "do not ask" without saying what to do with work that
*requires* approval.

This module is the single machine-readable statement of that policy. The prose
lives in the stock assets (``ciao/stock/agents/memory.md``,
``ciao/stock/commands/remember.md``, ``ciao/stock/skills/memory-curation``) and
in ``docs/ARCHITECTURE.md``; tests pin every copy here so they cannot drift
apart again. It is deliberately behavior-free: the pipeline in
``ciao/memory_proposals.py`` and the curation skill remain the implementation.

Two rules the matrix encodes and the whole surface must respect:

* **Cap semantics are ADVISORY everywhere.** Nothing refuses a write at edit
  time. ``ciao memory update`` writes over the cap and reports ``over_cap``
  with ``used_chars`` and ``char_limit``; ``os_audit`` and nightly curation
  report and consolidate when over cap. No shipped instruction may claim a hard
  cap.
* **Unattended runs defer approval-requiring work.** They do not ask (nobody
  is watching) and they must not seek an alternate route around the absent
  reviewer; the run reports the deferred item in its final output instead.
"""

from __future__ import annotations

from dataclasses import dataclass

CAP_SEMANTICS_ADVISORY = "advisory"
"""The only cap semantics in the codebase; see ``ciao/memory_tool.update_region``."""

MEMORY_DESTINATIONS: tuple[str, ...] = (
    "memory",
    "profile",
    "project",
    "people",
    "learnings",
    "review",
)
"""Destination vocabulary shared with the extraction prompts and the queue.

``memory`` and ``profile`` are the bounded regions; ``review`` is the honest
"unsure" bucket and always waits in ``Workspace/Memory-Proposals.md``.
"""


# How a context treats NEW region facts. ``archive`` auto-applies only
# confident, state-shaped facts; ``reviewed`` queues them.
PROMOTE_AUTO = "auto"
PROMOTE_REVIEWED = "reviewed"
PROMOTE_ATTENDED = "attended"


@dataclass(frozen=True, slots=True)
class MemoryWritePolicy:
    """What one memory-writing context may do, in one row of the matrix.

    ``writes_regions`` covers the two bounded regions only. ``writes_vault``
    covers the durable-markdown destinations (project docs, people notes,
    learnings). ``promotes_new_region_facts`` is ``auto`` (confident,
    state-shaped facts at archive time), ``reviewed`` (queued for a human or
    curator), or ``attended`` (a human action in the current turn).
    """

    key: str
    summary: str
    writes_regions: bool
    writes_vault: bool
    promotes_new_region_facts: str
    queues_uncertain: bool
    consolidates_regions: str  # "never" | "at_threshold"
    cap_semantics: str
    undo_log_required: bool
    approval: str  # "attended" | "unattended" | "archive"


CONTEXT_POLICIES: tuple[MemoryWritePolicy, ...] = (
    MemoryWritePolicy(
        key="attended_explicit_remember",
        summary=(
            "The user asks a chat to remember or forget a fact. It is queued for "
            "review by default; a live region write is allowed only when the user "
            "explicitly asks for it in the current turn."
        ),
        writes_regions=True,
        writes_vault=True,
        promotes_new_region_facts=PROMOTE_ATTENDED,
        queues_uncertain=True,
        consolidates_regions="at_threshold",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=False,
        approval="attended",
    ),
    MemoryWritePolicy(
        key="archive_extraction",
        summary=(
            "A chat is archived and its Session insights are routed. Confident, "
            "state-shaped facts are auto-applied (regions, people stubs, "
            "learnings); project facts are owned by the doc fold; event-shaped, "
            "unsure, and failed facts wait in the proposals queue. Unattended "
            "turns in the transcript are never lifted as facts."
        ),
        writes_regions=True,
        writes_vault=True,
        promotes_new_region_facts=PROMOTE_AUTO,
        queues_uncertain=True,
        consolidates_regions="never",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=True,
        approval="archive",
    ),
    MemoryWritePolicy(
        key="unattended_curation",
        summary=(
            "The nightly Workspace care run may consolidate a region at/above "
            "~85% of its cap (merge duplicates, drop expired, move project-scoped "
            "facts out) under the undo log. It never promotes a NEW region fact, "
            "never trashes or permanently deletes a note, and defers anything "
            "that needs a reviewer."
        ),
        writes_regions=True,
        writes_vault=True,
        promotes_new_region_facts=PROMOTE_REVIEWED,
        queues_uncertain=True,
        consolidates_regions="at_threshold",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=True,
        approval="unattended",
    ),
    MemoryWritePolicy(
        key="direct_edit",
        summary=(
            "A human edits the workspace guide with Edit, or a provider issues a "
            "ciao memory update. This is human-controlled maintenance; the cap is "
            "advisory and an over-cap write reports over_cap."
        ),
        writes_regions=True,
        writes_vault=False,
        promotes_new_region_facts=PROMOTE_ATTENDED,
        queues_uncertain=False,
        consolidates_regions="at_threshold",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=False,
        approval="attended",
    ),
    MemoryWritePolicy(
        key="proposal_acceptance",
        summary=(
            "A reviewer accepts or dismisses a queued proposal from the PWA or the "
            "CLI. Region facts go through the same guarded write as archive time "
            "(event-shape check, stamp-stripped dedupe, learned-at stamp, undo log "
            "on replacement); people notes merge rather than overwrite."
        ),
        writes_regions=True,
        writes_vault=True,
        promotes_new_region_facts=PROMOTE_ATTENDED,
        queues_uncertain=False,
        consolidates_regions="never",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=True,
        approval="attended",
    ),
)

CONTEXT_KEYS: tuple[str, ...] = tuple(policy.key for policy in CONTEXT_POLICIES)


@dataclass(frozen=True, slots=True)
class DeferredAction:
    """One approval-requiring action an unattended run must defer and report."""

    action: str
    reason: str


UNATTENDED_DEFERRED_ACTIONS: tuple[DeferredAction, ...] = (
    DeferredAction(
        "Promote a new fact into the always-loaded ciao:memory / ciao:profile regions",
        "New region facts need a reviewer; queue them in Workspace/Memory-Proposals.md.",
    ),
    DeferredAction(
        "Trash, restore, or permanently delete a vault note",
        "ciao vault review mutations require an attended turn (unattended_forbidden).",
    ),
    DeferredAction(
        "Write memory or a project doc in another workspace",
        "Each run is scoped to its own workspace guide and vault; never cross workspaces.",
    ),
    DeferredAction(
        "Create or move an automation into another workspace",
        "Schedules are auto-approved and run unattended in bypass; only the caller's "
        "workspace is allowed.",
    ),
    DeferredAction(
        "Open or comment on a public GitHub issue, or run a destructive git operation",
        "Public and destructive actions need the operator's approval.",
    ),
)
"""Dangerous unattended examples, pinned across providers.

The unattended capsule (``ciao/context/capsule.py``) tells the model not to ask
and to defer; the curation skill encodes the memory-specific half. Tests assert
every one of these resolves to "defer" for both supported providers, so a new
provider cannot introduce a different unattended rule.
"""


UNATTENDED_MARKER = "unattended=true; this turn was fired automatically"
"""The stable prefix that flags an automation turn in the session JSONL.

``ciao/insights.py`` matches this to mark a user message unattended during
extraction, so it is part of the archive format and must not change. The
capsule guidance opens with it.
"""

UNATTENDED_CAPSULE_GUIDANCE = (
    UNATTENDED_MARKER + ". Do not ask "
    "questions or wait for approval, and do not route around the absent "
    "reviewer. Defer and report in your final output any action that needs "
    "approval: promoting a NEW fact into the always-loaded memory regions, "
    "trashing or permanently deleting a vault note, writing another "
    "workspace, creating or moving an automation into another workspace, and "
    "public or destructive git actions."
)
"""The unattended marker the context capsule injects, in one place.

``ciao/insights.py`` matches the ``unattended=true; this turn was fired
automatically`` prefix to flag automation turns during extraction, so that
prefix is part of the archive format and must not change. Tests pin both the
prefix and the deferral language here.
"""


def context_policy(key: str) -> MemoryWritePolicy:
    """Return the matrix row for ``key``; raise ``KeyError`` for an unknown one."""
    for policy in CONTEXT_POLICIES:
        if policy.key == key:
            return policy
    raise KeyError(key)


def unattended_policy() -> MemoryWritePolicy:
    """The one unattended policy every provider shares."""
    return context_policy("unattended_curation")


def unattended_deferrals() -> tuple[DeferredAction, ...]:
    """The approval-requiring actions an unattended run defers and reports."""
    return UNATTENDED_DEFERRED_ACTIONS
