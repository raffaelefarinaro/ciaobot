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

from pathlib import Path
from typing import Any

from ciao.vault_index import (
    DEFAULT_PROMOTION_THRESHOLD,
    Entry,
    promotion_threshold,
    scan_targets,
    vocabulary_report,
)


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


def is_near_duplicate(a: str, b: str, *, known_tags: set[str] | None = None) -> bool:
    """Whether two distinct tags look like near-duplicates of each other.

    Heuristics, in order of cheap to expensive:

    * Identical tags are not duplicates of themselves; a case-only variant
      (``ai`` vs ``AI``) IS a duplicate — the same tag spelled differently.
    * One tag is the other plus a separator (``ai`` vs ``ai-analysis``): the
      namespace/value convention groups a value under a bare parent, so this is
      the common case the plan calls out.
    * Two tags sharing the same namespace prefix (``ai-analysis`` vs
      ``ai-adoption``) are duplicates only when the bare stem is itself an
      ESTABLISHED tag in the vault (``ai`` exists and is used more than once).
      Otherwise ``project/draft`` vs ``project/active`` would merge distinct
      values that are meant to coexist.
    * Edit distance, scaled to the shorter tag's length: a two-character
      difference only counts as a near-miss once the tags are long enough to
      carry that many edits without coincidence (``ai`` vs ``hr`` is distance
      2 but must NOT merge), while a single-character difference still counts
      for longer tags — but not for tags under three characters, where a
      single edit is nearly guaranteed by chance (``jo`` vs ``mo``, ``ai`` vs
      ``aj``) rather than evidence of a typo.
    * Normalized forms (separators removed) matching exactly or within the
      same length-scaled edit distance. Deliberately no bare-prefix match:
      ``ai`` vs ``airline`` share a normalized prefix but are unrelated.
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
    # distinct short tags is within distance 2 (`ai` vs `hr`, `data` vs `java`),
    # so an unconditional distance-2 test would propose merging unrelated tags.
    # Scale the allowance so a single-character difference matters for short
    # tags and two edits only count once the tags are materially longer — a
    # four-character pair like data/java is distance 2 but must NOT merge.
    shorter = min(len(la), len(lb))
    max_edit = 1 if shorter < 6 else 2
    # A length floor for the edit-distance checks below (raw and normalized
    # form alike). At two characters there are only 26*26 = 676 possible
    # tags, so almost any real two-letter tag is one edit from another real
    # one (`jo` vs `mo`, `ai` vs `aj`) — a distance-1 "near-miss" there is
    # coincidence, not a typo. Three characters is already an order of
    # magnitude roomier (26**3 = 17,576 combinations), so a distance-1 match
    # goes back to being meaningful signal — `data` vs `dato` (4 chars) must
    # still merge. This floor applies ONLY to the edit-distance branches
    # below; it must NOT gate the separator/namespace-prefix branch just
    # below, which is how a genuinely short established tag like `ai` still
    # merges with `ai-analysis` (the plan's canonical example).
    min_edit_len = 3
    # Shared namespace/value: the plan's canonical example is ai-analysis /
    # ai-adoption / ai-practice alongside the bare established ai.
    for sep in ("/", "-", "_"):
        # One tag is the other plus a separator: ai vs ai-analysis. This is a
        # value under a bare parent — always a near-duplicate.
        if lb.startswith(la + sep) or la.startswith(lb + sep):
            return True
        # Both share the same prefix before the separator (ai-analysis vs
        # ai-adoption, project/draft vs project/active). Distinct values in a
        # namespace are meant to coexist (project/active vs project/draft), so
        # this only merges when the bare stem is itself an ESTABLISHED tag —
        # ai exists, so ai-analysis and ai-adoption both alias to it.
        if sep in la and sep in lb:
            pa = la.split(sep, 1)[0]
            pb = lb.split(sep, 1)[0]
            if pa == pb and known_tags and pa in known_tags:
                return True
    # Edit distance on raw forms.
    if shorter >= min_edit_len and _edit_distance(la, lb, max_dist=max_edit) <= max_edit:
        return True
    # Normalized forms (separators removed): handles ai-analysis vs aianalysis.
    # Deliberately NO bare-prefix match here — `ai` vs `airline` share a
    # normalized prefix but are unrelated, and the separator-delimited stem
    # cases are already handled above. Only an exact normalized equality (or a
    # length-scaled edit, subject to the same length floor) counts.
    na, nb = _normalized_tag(a), _normalized_tag(b)
    if na == nb:
        return True
    if shorter >= min_edit_len and _edit_distance(na, nb, max_dist=max_edit) <= max_edit:
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
    #
    # Case-equivalent unknown types are aggregated before thresholding: since
    # canonical_type() treats casing as insignificant for known types, `type:
    # brainstorm` and `type: Brainstorm` are the same semantic type, and their
    # combined usage must reach the threshold together rather than each being
    # counted separately and never promoted.
    type_promotions: list[dict[str, Any]] = []
    casefolded_drift: dict[str, dict[str, Any]] = {}
    for raw_type, record in drift.items():
        suggested = record.get("suggested", "")
        if suggested:
            continue  # Has a rename target — low-risk fix, not a promotion.
        key = raw_type.casefold()
        bucket = casefolded_drift.setdefault(
            key, {"spellings": [], "paths": [], "suggested": ""}
        )
        bucket["spellings"].append(raw_type)
        bucket["paths"].extend(record.get("paths", []))
    for key, bucket in sorted(casefolded_drift.items()):
        paths = sorted(set(bucket["paths"]))
        count = len(paths)
        if count < threshold:
            continue
        # Pick the most common spelling as the canonical proposal name; ties
        # resolve to the lexicographically first.
        spelling_counts = {s: 0 for s in bucket["spellings"]}
        for e in entries:
            raw = (e.type or "").strip()
            if raw.casefold() == key and raw in spelling_counts:
                spelling_counts[raw] += 1
        dominant = max(
            bucket["spellings"],
            key=lambda s: (spelling_counts[s], -bucket["spellings"].index(s)),
        )
        workspaces = sorted(
            {
                e.workspace
                for e in entries
                if (e.type or "").strip().casefold() == key
            }
        )
        type_promotions.append(
            {
                "type": dominant,
                "count": count,
                "suggested": "",
                "paths": paths,
                "workspaces": workspaces,
            }
        )

    # Tag merges: singleton tags with a near-duplicate among any other tag,
    # plus repeated tags that are case-only variants of a dominant spelling
    # (their combined usage reaches the threshold but the spellings stay
    # fragmented). A singleton with no near-duplicate is just a one-off (the
    # Candidates tier), not a merge proposal.
    all_tags = sorted(tags.keys())

    # Case-equivalent spellings (ai / AI) are the same tag spelled differently.
    # The dominant spelling is what gets promoted; every other spelling is a
    # merge candidate into it. Compute the grouping up front so tag promotions
    # do not simultaneously propose establishing a variant that the merge pass
    # eliminates.
    by_casefold: dict[str, list[str]] = {}
    for t in all_tags:
        by_casefold.setdefault(t.casefold(), []).append(t)
    case_dominant: dict[str, str] = {}
    non_dominant_case_variants: set[str] = set()
    repeated_case_variants: set[str] = set()
    for key, spellings in by_casefold.items():
        if len(spellings) < 2:
            continue
        dominant = max(spellings, key=lambda s: tags[s])
        case_dominant[key] = dominant
        # Every spelling in a multi-spelling group is handled by the case-fold
        # pass (dominant is promoted, the rest merge into it), so none of them
        # should also run through the singleton/merge loop.
        repeated_case_variants.update(spellings)
        for variant in spellings:
            if variant == dominant:
                continue
            non_dominant_case_variants.add(variant)

    # Tag promotions: tags that have crossed the threshold into established
    # territory. These are informational — the tag is already in use at scale,
    # and the question is whether it should be treated as a convention.
    # Only the dominant spelling of a case group is promoted; a non-dominant
    # variant is proposed for merging instead.
    tag_promotions: list[dict[str, Any]] = []
    for tag, count in sorted(tags.items()):
        if count < threshold:
            continue
        if tag in non_dominant_case_variants:
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

    # A namespace/value stem only merges when the bare stem is itself an
    # ESTABLISHED tag (at or above the promotion threshold), so pass that set
    # in — a bare tag with only two uses must not resurrect the false namespace
    # merge when the threshold is 5.
    known_tags = {t for t, count in tags.items() if count >= threshold}
    tag_merges: list[dict[str, Any]] = []

    # Fold every non-dominant case variant into the dominant spelling (ai 3x +
    # AI 2x: AI merges into ai), regardless of singleton status.
    for key, dominant in case_dominant.items():
        for variant in sorted(s for s in by_casefold[key] if s != dominant):
            tag_merges.append(
                {
                    "tag": variant,
                    "count": tags[variant],
                    "kind": "case_variant",
                    "workspaces": sorted(tag_workspaces.get(variant, [])),
                    "near_duplicates": [dominant],
                }
            )

    singletons = [t for t in all_tags if tags[t] == 1 and t not in repeated_case_variants]
    for tag in sorted(singletons):
        neighbors: list[str] = []
        for other in all_tags:
            if other == tag:
                continue
            if not is_near_duplicate(tag, other, known_tags=known_tags):
                continue
            # Do not propose reciprocal aliases between two singleton tags
            # (analysis vs analysys): both would recommend aliasing the other,
            # which cannot both be applied and gives no evidence for a
            # convention. Point the lexicographically-smaller singleton at the
            # larger one, so only one direction is emitted.
            if tags[other] == 1 and tag > other:
                continue
            neighbors.append(other)
        if neighbors:
            tag_merges.append(
                {
                    "tag": tag,
                    "count": tags[tag],
                    "kind": "singleton",
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
        # Preserve each target's workspace stamp exactly as the config supplied
        # it: on a pre-re-rooting install vault_scan_targets() returns an EMPTY
        # stamp so scan_vault() infers each note's workspace from its first path
        # segment. Coercing that empty stamp to "personal" would mislabel every
        # note in a shared vault that spans workspaces. Only the no-config
        # fallback above stamps the caller's own root.
        entries, _abs = scan_targets(targets)
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
