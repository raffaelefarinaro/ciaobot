"""Vocabulary promotion and merge proposals for the vault hygiene audit.

Step 5 of the vault vocabulary plan: the hygiene routine should *propose*,
not auto-apply, two kinds of vocabulary changes that need a human decision,
mirroring the Memory-Proposals.md promote/dismiss pattern:

* **Promotion.** A non-canonical ``type:`` or an emerging tag that crosses a
  usage threshold (default 5 — the established-tag tier boundary) is a
  candidate for the canonical set. ``skill-proposal`` at 49 uses earned its
  place; a 1-use type would not. Auto-applying would rewrite frontmatter
  across the vault on a judgement call, so the audit surfaces it instead.

* **Merge.** A singleton tag (used once) that looks like a near-duplicate of
  an established/emerging tag (e.g. ``ai-analysis`` alongside ``ai``) is a
  candidate for aliasing. A singleton with no near-duplicate is just a
  one-off, not a merge proposal.

Threshold is deliberately a constant rather than a registry entry: ``5`` matched
the tier boundary in the original sweep and is the point where a tag moves
from ``Tags (emerging)`` to ``Tags (established)`` in ``VOCABULARY.md``. It
is configurable via ``VOCAB_PROMOTION_THRESHOLD`` env for tests/installs
that want a different bar, but the default is the deliberate number from the
plan's open question.

Where proposals surface is the plan's second open question. This module
chooses **inline in the hygiene audit output** over a new
``Vocabulary-Proposals.md`` file: the proposals are derived from the current
snapshot on every audit (no persistent queue to reconcile), they are
informational pending actions (like ``upgrade_notices``), and they do not
raise the audit status. A persistent queue with dismiss tracking can be added
later without changing the generation logic, but it is not needed to satisfy
the plan's tests or to keep the routine from auto-applying.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ciao.vault_index import Entry, vocabulary_report

# Promotion threshold: a non-canonical type or an emerging tag that reaches
# this many uses becomes a candidate for the canonical/background set.
DEFAULT_PROMOTION_THRESHOLD = 5


def promotion_threshold() -> int:
    raw = os.environ.get("VOCAB_PROMOTION_THRESHOLD", "").strip()
    if not raw:
        return DEFAULT_PROMOTION_THRESHOLD
    try:
        value = int(raw)
        return value if value > 0 else DEFAULT_PROMOTION_THRESHOLD
    except ValueError:
        return DEFAULT_PROMOTION_THRESHOLD


def _edit_distance(a: str, b: str, max_dist: int = 2) -> int:
    """Levenshtein distance capped at ``max_dist+1`` for early exit."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > max_dist:
        return max_dist + 1
    # Ensure a is the shorter.
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        min_cur = cur[0]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if cur[j] < min_cur:
                min_cur = cur[j]
        if min_cur > max_dist:
            return max_dist + 1
        prev = cur
    return prev[-1]


def _normalized_tag(tag: str) -> str:
    """Lowercased tag with separators stripped for fuzzy comparison."""
    return tag.lower().replace("-", "").replace("_", "").replace("/", "")


def is_near_duplicate(a: str, b: str) -> bool:
    """Whether two distinct tags look like near-duplicates of each other.

    Heuristics, in order of cheap to expensive:

    * Identical tags are not duplicates of themselves.
    * Shared prefix before a namespace/prefix separator (``/``, ``-``, ``_``)
      — e.g. ``ai`` vs ``ai-analysis``, ``ai-adoption`` vs ``ai-practice``
      all share the ``ai`` stem. This is the common case the plan calls out.
    * Edit distance ≤2 on the raw lowercased forms (typo/near-miss).
    * Normalized forms (separators removed) matching within edit distance 1
      or one being a prefix of the other.
    """
    if not a or not b:
        return False
    la, lb = a.lower(), b.lower()
    if la == lb:
        return False
    # Shared prefix before a separator: the plan's canonical example is
    # ai-analysis / ai-adoption / ai-practice alongside ai.
    for sep in ("/", "-", "_"):
        # Case 1: one tag is a prefix of the other plus separator.
        if lb.startswith(la + sep) or la.startswith(lb + sep):
            return True
        # Case 2: both share the same prefix before the separator.
        if sep in la and sep in lb:
            pa = la.split(sep, 1)[0]
            pb = lb.split(sep, 1)[0]
            if pa == pb and len(pa) >= 2:
                # Same stem (e.g. ai-analysis vs ai-adoption, project/active
                # vs project/draft). Guard on stem length to avoid single-char
                # coincidence like "a-b" vs "a-c".
                return True
    # Edit distance on raw forms.
    if _edit_distance(la, lb, max_dist=2) <= 2:
        return True
    # Normalized forms: handles ai-analysis vs aianalysis, etc.
    na, nb = _normalized_tag(a), _normalized_tag(b)
    if na == nb:
        return True
    if na.startswith(nb) or nb.startswith(na):
        # One normalized tag is a prefix of the other (e.g. ai vs aianalysis)
        # but only when the shorter is at least 2 chars and the longer is not
        # trivially longer (avoids "a" matching everything).
        short, long = (na, nb) if len(na) < len(nb) else (nb, na)
        if len(short) >= 2 and len(long) - len(short) <= 6:
            return True
    if _edit_distance(na, nb, max_dist=1) <= 1:
        return True
    return False


def generate_vocabulary_proposals(
    entries: list[Entry],
    *,
    threshold: int | None = None,
) -> dict[str, Any]:
    """Build promotion and merge proposals from a vault snapshot.

    Returns a dict with:

    * ``type_promotions`` — non-canonical types with ``count >= threshold``
      and no alias target (alias-target types have a safe rename, not a
      judgement call). Each carries ``type``, ``count``, ``suggested`` (empty),
      ``paths`` and ``workspaces``.
    * ``tag_promotions`` — tags with ``count >= threshold`` (emerging → established)
      as candidates for the canonical/background set. Each carries ``tag``,
      ``count`` and ``workspaces``.
    * ``tag_merges`` — singleton tags (count == 1) that have a near-duplicate
      among any other tag. Each carries ``tag``, ``workspaces`` and
      ``near_duplicates`` (list of the tags it resembles).

    ``threshold`` overrides the env-driven default; used by tests to pin the
    bar without touching the environment.
    """
    if threshold is None:
        threshold = promotion_threshold()
    report = vocabulary_report(entries)
    tags: dict[str, int] = report["tags"]
    tag_workspaces: dict[str, list[str]] = report["tag_workspaces"]
    drift: dict[str, dict[str, Any]] = report["type_drift"]

    # Type promotions: non-canonical types without alias target that have
    # reached the threshold. Aliased types are handled by the existing safe
    # rename path (vault_migration / hygiene low-risk fix), not here.
    type_promotions: list[dict[str, Any]] = []
    for raw_type, record in sorted(drift.items()):
        suggested = record.get("suggested", "")
        if suggested:
            continue  # Has a rename target — low-risk fix, not a promotion.
        paths: list[str] = record.get("paths", [])
        count = len(paths)
        if count < threshold:
            continue
        workspaces = sorted(
            {e.workspace for e in entries if (e.type or "").strip() == raw_type}
        )
        type_promotions.append(
            {
                "type": raw_type,
                "count": count,
                "suggested": suggested,
                "paths": sorted(paths),
                "workspaces": workspaces,
            }
        )

    # Tag promotions: tags that have crossed the threshold into established
    # territory. These are informational — the tag is already in use at scale,
    # and the question is whether it should be treated as a convention.
    tag_promotions: list[dict[str, Any]] = []
    for tag, count in sorted(tags.items()):
        if count < threshold:
            continue
        # Only propose tags that are at the boundary or newly established?
        # The current snapshot cannot tell "just crossed" from "always there",
        # so every tag at or above threshold is a candidate. The hygiene
        # routine surfaces them without raising the audit status, so a stable
        # vault with long-established tags will list them each time — which is
        # fine because they are not defects. Tests pin threshold to verify the
        # boundary behavior.
        tag_promotions.append(
            {
                "tag": tag,
                "count": count,
                "workspaces": sorted(tag_workspaces.get(tag, [])),
            }
        )

    # Tag merges: singleton tags with a near-duplicate among any other tag.
    # A singleton with no near-duplicate is just a one-off (the Candidates
    # tier), not a merge proposal.
    all_tags = sorted(tags.keys())
    singletons = [t for t in all_tags if tags[t] == 1]
    tag_merges: list[dict[str, Any]] = []
    for tag in sorted(singletons):
        neighbors: list[str] = []
        for other in all_tags:
            if other == tag:
                continue
            if is_near_duplicate(tag, other):
                neighbors.append(other)
        if neighbors:
            tag_merges.append(
                {
                    "tag": tag,
                    "workspaces": sorted(tag_workspaces.get(tag, [])),
                    "near_duplicates": sorted(neighbors),
                }
            )

    return {
        "threshold": threshold,
        "type_promotions": type_promotions,
        "tag_promotions": tag_promotions,
        "tag_merges": tag_merges,
    }


def audit_vocabulary_proposals(
    vault_root: Path,
    workspace_name: str = "",
) -> dict[str, Any]:
    """Scan one vault root and return its vocabulary proposals.

    ``workspace_name`` is accepted for API parity with the other per-workspace
    audit helpers but is currently unused: types are a single global set and
    tags carry their own workspace attribution per proposal. The vault root is
    the workspace's own vault, so scoping already happened at the caller.

    Failures degrade to an empty proposal set with a scan error, never to
    "checked and clean".
    """
    from ciao.vault_index import scan_vault

    if not vault_root.is_dir():
        return {
            "threshold": promotion_threshold(),
            "type_promotions": [],
            "tag_promotions": [],
            "tag_merges": [],
            "errors": [],
        }
    try:
        entries = scan_vault(vault_root, workspace=workspace_name or "personal")
    except Exception as exc:  # noqa: BLE001 — advisory section
        return {
            "threshold": promotion_threshold(),
            "type_promotions": [],
            "tag_promotions": [],
            "tag_merges": [],
            "errors": [
                {
                    "type": "vocabulary_proposal_scan_failed",
                    "path": str(vault_root),
                    "message": f"vocabulary proposal scan failed: {exc}",
                }
            ],
        }
    result = generate_vocabulary_proposals(entries)
    result["errors"] = []
    return result
