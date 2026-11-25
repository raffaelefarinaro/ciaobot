"""Operator-reviewed guide mappings; these do not authorize publication."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao import shared_brain_guide as guide


@dataclass(frozen=True, slots=True)
class GuideMapping:
    identity: str
    lifetime_id: str
    generation: int
    pin: guide.GuidePin
    grants: tuple[guide.ConsentGrant, ...]


def approve(link: Any, load: guide.GuideLoad, names: list[str], *, expected_pin: guide.GuidePin) -> GuideMapping:
    """Record only explicitly selected entries from the exact previewed guide."""
    if not load.ok or load.guide is None or load.guide.pin != expected_pin:
        raise ValueError("the guide changed; review it again")
    if expected_pin.brain_identity != link.identity or not names or len(names) != len(set(names)):
        raise ValueError("select distinct types for this shared brain")
    if any(not isinstance(name, str) or name not in load.guide.types for name in names):
        raise ValueError("unknown note type")
    grants = tuple(
        guide.ConsentGrant(name, note.destination.kind, note.destination.path,
                           frozenset({note.destination.operation}), note.destination.section)
        for name in sorted(names) for note in [load.guide.types[name]]
    )
    current: GuideMapping | None = link.guide_mapping
    if current and current.identity == link.identity and current.lifetime_id == link.lifetime_id and current.pin == expected_pin and current.grants == grants:
        return current
    return GuideMapping(link.identity, link.lifetime_id, link.guide_mapping_generation + 1, expected_pin, grants)


def row(mapping: GuideMapping) -> dict[str, Any]:
    return {"identity": mapping.identity, "lifetime_id": mapping.lifetime_id,
            "generation": mapping.generation,
            "pin": {"identity": mapping.pin.brain_identity, "version": mapping.pin.version, "digest": mapping.pin.digest},
            "grants": [{"note_type": g.note_type, "kind": g.kind, "path": g.path,
                        "section": g.section, "operations": sorted(g.operations)} for g in mapping.grants]}


def from_row(raw: object, *, identity: str, lifetime_id: str, generation: int) -> GuideMapping | None:
    try:
        if not isinstance(raw, dict) or raw.get("identity") != identity or raw.get("lifetime_id") != lifetime_id or raw.get("generation") != generation or generation < 1:
            return None
        pin = raw["pin"]
        if not isinstance(pin, dict) or pin.get("identity") != identity or type(pin.get("version")) is not int or pin["version"] != guide.GUIDE_VERSION or not isinstance(pin.get("digest"), str) or not re.fullmatch(r"[0-9a-f]{64}", pin["digest"]):
            return None
        entries = raw["grants"]
        if not isinstance(entries, list) or not 0 < len(entries) <= guide.MAX_TYPES:
            return None
        grants = tuple(guide.ConsentGrant(g["note_type"], g["kind"], g["path"], frozenset(g["operations"]), g["section"]) for g in entries)
        guide.ConsentMapping(identity, grants)
        return GuideMapping(identity, lifetime_id, generation, guide.GuidePin(identity, pin["version"], pin["digest"]), grants)
    except (KeyError, TypeError, ValueError):
        return None


def resolve(link: Any, load: guide.GuideLoad, *, note_type: str, relative_path: str = "", operation: str | None = None) -> guide.Resolution:
    """Resolve a reviewed type; the result alone is never effect admission."""
    mapping = link.guide_mapping
    if mapping is None or mapping.identity != link.identity or mapping.lifetime_id != link.lifetime_id or mapping.generation != link.guide_mapping_generation:
        return guide.Resolution("not_consented", reason="no mapping for this link lifetime")
    return guide.resolve_for_publication(load, guide.ConsentMapping(mapping.identity, mapping.grants), note_type=note_type, pinned=mapping.pin, relative_path=relative_path, operation=operation)


@dataclass(frozen=True, slots=True)
class MappingSnapshot:
    workspace: str
    brain: str
    identity: str
    lifetime_id: str
    generation: int
    sharing_generation: int
    destination: guide.ResolvedDestination


def resolve_workspace_destination(config: Any, workspace: str, brain: str, *, note_type: str, relative_path: str = "", operation: str | None = None, owner: Any) -> MappingSnapshot | None:
    """Resolve under the caller's registry and checkout claims, with fresh guide bytes.

    Inputs can only narrow a stored mapping. This snapshot is review data, not
    disclosure authorization, admission, or a fence for an external effect.
    """
    from ciao import shared_brain
    from ciao.config import shared_brain_row
    from ciao.workspaces import persisted_shared_links, workspace_registry_lock

    if owner.closed or owner.key != str(Path(config.state_path).resolve().parent) or owner.current is None or owner.current != getattr(config, "_workspace_registry_bytes", None):
        return None
    with workspace_registry_lock(config):
        ws = config.workspace(workspace)
        link = next((item for item in ws.shared_brains if item.name == brain), None) if ws else None
        if link is None:
            return None
        rows = persisted_shared_links(owner.current, link.path)
        if not any(name == workspace and item == shared_brain_row(link) for name, item in rows):
            return None
        if link.kind == "git":
            if shared_brain.probe_under_fence(config, link, owner).get("problem"):
                return None
        elif shared_brain.link_problem(config, link):
            return None
        result = resolve(link, guide.read_guide(link.path, brain_identity=link.identity), note_type=note_type, relative_path=relative_path, operation=operation)
        if not result.ok or result.destination is None:
            return None
        return MappingSnapshot(workspace, brain, link.identity, link.lifetime_id, link.guide_mapping_generation, link.sharing_generation, result.destination)


def revalidate_snapshot(config: Any, snapshot: MappingSnapshot, *, owner: Any) -> bool:
    """Re-read the mapping; callers still need a separate effect admission fence."""
    destination = snapshot.destination.destination
    current = resolve_workspace_destination(config, snapshot.workspace, snapshot.brain, note_type=snapshot.destination.note_type, relative_path=destination.path, operation=destination.operation, owner=owner)
    return current == snapshot
