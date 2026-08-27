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

Types are a single global canonical set, so usage is counted across EVERY
workspace vault via ``config.vault_scan_targets()`` when a registry is
available: a non-canonical type split across two workspaces reaches the
threshold only when summed, and the workspace attribution on each proposal
reflects all the roots that use it.

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

    * Identical tags are not duplicates of themselves; a case-only variant
      (``ai`` vs ``AI``) IS a duplicate — the same tag spelled differently.
    * Shared prefix before a namespace/prefix separator (``/``, ``-``, ``_``)
      — e.g. ``ai`` vs ``ai-analysis``, ``ai-adoption`` vs ``ai-practice``
      all share the ``ai`` stem. This is the common case the plan calls out.
    * Edit distance, scaled to the shorter tag's length: a two-character
      difference only counts as a near-miss once the tags are long enough to
      carry that many edits without coincidence (``ai`` vs ``hr`` is distance
      2 but must NOT merge), while a single-character difference still counts
      for longer tags.
    * Normalized forms (separators removed) matching exactly or within the
      same length-scaled edit distance. Deliberately no bare-prefix match:
      ``ai`` vs ``airline`` share a normalized prefix but are unrelated, and
      the separator-delimited stem cases are already handled above.
    """
    if not a or not b:
        return False
    if a == b:
        return False
    la, lb = a.lower(), b.lower()
    if la == lb:
        # Case-only variant (ai vs AI): the same tag spelled differently, so
        # the singleton is a merge candidate, not a distinct tag.
        return True
    # Edit distance is only meaningful relative to length: nearly every pair of
    # distinct short tags is within distance 2 (`ai` vs `hr`), so an
    # unconditional distance-2 test would propose merging unrelated tags. Scale
    # the allowance so a single-character difference matters more for short
    # tags — two letters apart only counts as near-duplicate once the tags have
    # enough characters to carry that many edits without being coincidence.
    shorter = min(len(la), len(lb))
    max_edit = 1 if shorter < 4 else 2
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
    if _edit_distance(la, lb, max_dist=max_edit) <= max_edit:
        return True
    # Normalized forms (separators removed): handles ai-analysis vs aianalysis.
    # Deliberately NO bare-prefix match here — `ai` vs `airline` share a
    # normalized prefix but are unrelated, and the separator-delimited stem
    # cases are already handled above. Only an exact normalized equality (or a
    # length-scaled edit) counts.
    na, nb = _normalized_tag(a), _normalized_tag(b)
    if na == nb:
        return True
    if _edit_distance(na, nb, max_dist=max_edit) <= max_edit:
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
    *,
    config: Any | None = None,
) -> dict[str, Any]:
    """Scan the install's vaults and return their vocabulary proposals.

    ``vault_root`` is the fallback/active vault root. When ``config`` exposes a
    workspace registry, ``config.vault_scan_targets()`` supplies EVERY vault in
    the install so a type or established tag that crosses the threshold only
    when summed across workspaces still reaches it — types are a single global
    canonical set, so usage must be counted globally, not per workspace. The
    workspace attribution on each proposal reflects all the roots that use it.
    Without a registry the caller's own vault root is scanned, stamped
    ``workspace_name``.

    Failures degrade to an empty proposal set with a scan error, never to
    "checked and clean".
    """
    from ciao.vault_index import scan_targets

    empty = {
        "threshold": promotion_threshold(),
        "type_promotions": [],
        "tag_promotions": [],
        "tag_merges": [],
        "errors": [],
    }
    targets: list[tuple[Path, str, Path]]
    if config is not None and callable(getattr(config, "vault_scan_targets", None)):
        try:
            targets = config.vault_scan_targets()
        except Exception as exc:  # noqa: BLE001 — advisory section
            return {
                **empty,
                "errors": [
                    {
                        "type": "vocabulary_proposal_scan_failed",
                        "path": str(vault_root),
                        "message": f"vault discovery failed: {exc}",
                    }
                ],
            }
    elif vault_root.is_dir():
        targets = [(vault_root, workspace_name or "personal", Path("memory-vault"))]
    else:
        return empty
    try:
        entries, _abs = scan_targets(
            [
                (Path(root), workspace or "personal", Path(prefix))
                for root, workspace, prefix in targets
            ]
        )
    except Exception as exc:  # noqa: BLE001 — advisory section
        return {
            **empty,
            "errors": [
                {
                    "type": "vocabulary_proposal_scan_failed",
                    "path": str(vault_root),
                    "message": f"vocabulary proposal scan failed: {exc}",
                }
            ],
        }
    # scan_targets silently skips a configured vault whose root is missing or
    # unmounted. These counts are presented as aggregated across every
    # workspace, so an omitted root can suppress a promotion that should cross
    # the threshold and let an incomplete scan read as clean. Surface each
    # skipped root as a scan error rather than claiming the aggregation is
    # complete.
    skipped = [
        (root, workspace)
        for root, workspace, _prefix in targets
        if not Path(root).is_dir()
    ]
    errors: list[dict[str, str]] = [
        {
            "type": "vocabulary_proposal_scan_failed",
            "path": str(root),
            "message": (
                f"configured vault for workspace {workspace or 'personal'} "
                "is missing or not a directory; its vocabulary usage was not "
                "counted"
            ),
        }
        for root, workspace in skipped
    ]
    result = generate_vocabulary_proposals(entries)
    result["errors"] = errors
    return result
