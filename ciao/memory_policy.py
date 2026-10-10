"""One accurate, provider-neutral memory and unattended-execution policy.

The memory system has more than one way to write durable memory, and they do
not all have the same approval rule. Before this module the copies drifted:
the architecture said new memory needs review while archive-time extraction
called ``auto_promote_memory=True``; the retired memory agent said the typed path
enforces the cap while ``update_region`` documents and implements an advisory
one; and the unattended capsule said "do not ask" without saying what to do with
work that *requires* approval.

That archive-time auto-apply is itself gone: it was the one-shot extraction
pipeline, deleted in #627, and the memory pass (an attended chat, row
``memory_pass`` below) is the post-archive writer now. No context in this matrix
promotes a new region fact with no reviewer present; the rows that write one
live are attended turns a person can steer.

This module is the single machine-readable statement of that policy. The prose
lives in the stock assets (``ciao/stock/skills/ciao-memory/SKILL.md``,
``ciao/stock/commands/remember.md``, ``ciao/stock/schedules.json``) and
in ``docs/ARCHITECTURE.md``; tests pin every copy here so they cannot drift
apart again. It is deliberately behavior-free: the accept path in
``ciao/memory_proposals.py``, the memory-pass chat in
``ciao/web/memory_pass.py`` and the Workspace care schedule prompt remain the implementation.

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
"""Destination vocabulary shared with the queue and the stock memory assets.

``memory`` and ``profile`` are the bounded regions; ``review`` is the honest
"unsure" bucket and always waits in ``Workspace/Memory-Proposals.md``.
"""


# How a context treats NEW region facts. ``reviewed`` means the fact is filed as
# a proposal and only a human or a curator settles it; ``attended`` means the
# write happens in a turn a person is present for and can steer — an attended
# chat, which includes the post-archive memory pass. There is deliberately no
# ``auto``: the archive-time auto-apply was deleted with the one-shot pipeline
# (#627), and no unattended run promotes a new region fact.
PROMOTE_REVIEWED = "reviewed"
PROMOTE_ATTENDED = "attended"


@dataclass(frozen=True, slots=True)
class MemoryWritePolicy:
    """What one memory-writing context may do, in one row of the matrix.

    ``writes_regions`` covers the two bounded regions only. ``writes_vault``
    covers the durable-markdown destinations (project docs, people notes,
    learnings). ``promotes_new_region_facts`` is ``reviewed`` (filed as a
    proposal for a human or a curator) or ``attended`` (written inside a turn a
    person is present for); nothing here promotes one with no reviewer.
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
    approval: str  # "attended" | "unattended"


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
        key="memory_pass",
        summary=(
            "A chat is archived and an attended memory-pass chat is enqueued for "
            "it. The pass reads the archived transcript and the vault, then does "
            "the vault work with its own tools: it updates the note an entity "
            "already has, promotes a confident state-shaped fact to a region "
            "directly, folds Decisions into the project doc, creates a note only "
            "for a genuinely new entity, and queues whatever it is unsure of — "
            "including anything with no decided destination — in "
            "Workspace/Memory-Proposals.md. A turn the transcript marks as "
            "automated is not the user's, and is never lifted as a fact."
        ),
        writes_regions=True,
        writes_vault=True,
        promotes_new_region_facts=PROMOTE_ATTENDED,
        queues_uncertain=True,
        consolidates_regions="never",
        cap_semantics=CAP_SEMANTICS_ADVISORY,
        undo_log_required=True,
        approval="attended",
    ),
    MemoryWritePolicy(
        key="unattended_curation",
        summary=(
            "The nightly Workspace care run may consolidate a region at/above "
            "~85% of its cap (merge duplicates, drop expired, move project-scoped "
            "facts out) under the undo log, and may VERIFY a stale note: "
            "re-stamp one it found still true, or apply a cited whole-note "
            "replacement, both through the undo log. It never promotes a NEW "
            "region fact, never retires/trashes or permanently deletes a note, "
            "and defers anything that needs a reviewer — including a retirement "
            "verdict, which it files as a note_edit proposal instead of applying."
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
            "CLI. Region facts go through the guarded write every promotion uses "
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
    """One approval-requiring action an unattended run must defer and report.

    ``action`` names the *judgement*, never the mechanism. An entry may also say
    what the run is allowed to do on the same subject — retiring a learning whose
    findings are already settled is not a judgement, and saying so is what keeps
    the row from reading as a blanket prohibition the engine then breaks every
    night. What must not appear is an action whose own name is something the run
    may do: a list that defers what the code then performs teaches a model to
    route around the deferral.
    """

    action: str
    reason: str


UNATTENDED_DEFERRED_ACTIONS: tuple[DeferredAction, ...] = (
    DeferredAction(
        "Promote a new fact into the always-loaded ciao:memory / ciao:profile regions",
        "New region facts need a reviewer; queue them in Workspace/Memory-Proposals.md.",
    ),
    DeferredAction(
        "Edit a skill, or promote a lesson into a skill or the AGENTS.md guide body",
        "A recurring lesson is a routing priority, not permission: file a proposal "
        "or a [review] draft for a person instead (ciao skill-proposal-add, "
        "ciao skill-draft-add). Recurrence is how often, never who decided.",
    ),
    DeferredAction(
        "Settle a skill proposal or a skill draft",
        "Settlement follows verified application or an explicit rejection, and an "
        "unattended run can verify neither; a run that decided something would "
        "archive an unanswered question as an answer. ciao.upstream_drafts reads "
        "the run's own curation lease and refuses approve, create and reject "
        "alike.",
    ),
    DeferredAction(
        "Trash, restore, or permanently delete a vault note",
        "ciao vault review mutations require an attended turn (unattended_forbidden).",
    ),
    DeferredAction(
        "Retire a note a verification found wrong",
        "A verification may re-stamp a note it read in full, or apply a cited "
        "whole-note replacement, but retirement is a human decision: note_verification "
        "imports no delete primitive at all and returns the verdict as a note_edit "
        "proposal, which only an attended accept or dismiss settles. While that "
        "proposal waits, the review queue links to it instead of asking the same "
        "question again.",
    ),
    DeferredAction(
        "Retire one fact inside a note, or retire the note a fact lives in",
        "The same rule one level in. An entry verification may re-stamp that "
        "entry's own `[verified:]` date or apply a cited replacement for its exact "
        "span, and entry_verification imports no delete primitive either; a "
        "retired entry comes back as a retire_entry proposal whose accept removes "
        "that one span through the same whole-note receipt, so it is reversible and "
        "the note's other facts survive. A whole-note retirement stays Vault "
        "Review's trash and is never reachable from either verification path.",
    ),
    DeferredAction(
        "Judge a learning obsolete, or retire an Active entry the reconciliation "
        "did not propose",
        "The judgement is a person's, never a flag's. The nightly run *may* remove "
        "an Active entry whose every finding is already applied-with-a-verification "
        "or dismissed — that is settlement, already recorded by a person, and the "
        "removal is reversible from a receipt — through `ciao learnings-cleanup "
        "--apply-settled`, which retires only the rows the reconciliation itself "
        "proposed, never reapproves one, and is capped at "
        "LEARNINGS_CLEANUP_MAX_ITEMS. What it may not do is remove an entry "
        "nothing has ever proposed, one whose finding is still open, or one whose "
        "only destination was an upstream issue: those are judgements, not "
        "settlements, and they are `ciao learnings-cleanup --apply --approval-file`, "
        "which refuses without a stated reason and its evidence per row. An "
        "unattended run never sets `reapprove`.",
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
        "Public and destructive actions need the operator's approval. An upstream "
        "skill lesson is prepared as a [review] draft and waits for that approval; "
        "ciao.upstream_drafts refuses approve_draft (and create_new_skill and "
        "reject_draft) whenever the run holds the vault's curation lease, and "
        "`ciao skill-draft-approve` / `skill-draft-reject` exit 4 for the same "
        "reason.",
    ),
)
"""Dangerous unattended examples, pinned across providers.

The unattended capsule (``ciao/context/capsule.py``) tells the model not to ask
and to defer; the Workspace care schedule prompt encodes the memory-specific half. Tests assert
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
    "editing a skill or the AGENTS.md guide body, settling a skill proposal or "
    "draft, trashing or permanently deleting a vault note, writing another "
    "workspace, creating or moving an automation into another workspace, and "
    "public or destructive git actions."
)
"""The unattended marker the context capsule injects, in one place.

``ciao/insights.py`` matches the ``unattended=true; this turn was fired
automatically`` prefix to flag automation turns during extraction, so that
prefix is part of the archive format and must not change. Tests pin both the
prefix and the deferral language here.
"""


def is_unattended_turn(prompt: str) -> bool:
    """True when a stored user turn was fired automatically.

    The marker sits inside the ``[CIAO_CONTEXT_BEGIN]`` envelope that prefixes
    the turn's stored prompt. It is matched only there, so a person who pastes
    the marker text into their own message is still a human turn.
    """
    if not prompt.startswith("[CIAO_CONTEXT_BEGIN]"):
        return False
    envelope = prompt.partition("[CIAO_CONTEXT_END]")[0]
    return UNATTENDED_MARKER in envelope


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
