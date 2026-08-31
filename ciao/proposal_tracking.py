"""Stable proposal identities and cheap pending-queue checks."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciao import proposal_kinds


_PROPOSALS_REL = ("Workspace", "Memory-Proposals.md")
_SKILL_PROPOSALS_REL = ("Workspace", "Skill-Proposals")


def stable_proposal_id(
    workspace: str,
    path: str,
    kind: str,
    text: str,
    source: str,
    dup: int,
) -> str:
    digest = hashlib.sha256(
        f"{workspace}\x00{path}\x00{kind}\x00{text}\x00{source}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{digest}{f':{dup}' if dup else ''}"


@dataclass(frozen=True)
class QueuedProposal:
    """One parsed bullet from a workspace's proposal queue, with its id."""

    line: int
    raw: str
    bullet: Any
    dup: int
    proposal_id: str


def walk_proposal_queue(
    workspace: str, rel_path: str, text: str
) -> Iterator[QueuedProposal]:
    """Yield every proposal bullet in *text*, in file order, with its stable id.

    The single walk behind both the review-tab listing and the pending-id set
    that decides whether a resolution helper chat can be archived. The two used
    to keep private copies of this loop, and the ``dup`` counter in particular
    had to stay byte-identical between them: if it drifted, the ids stopped
    matching and helper chats simply never archived — no error, no log line.
    """
    seen: dict[tuple[str, str, str], int] = {}
    for line_index, raw in enumerate(text.splitlines()):
        bullet = proposal_kinds.parse_bullet(raw)
        if bullet is None:
            continue
        key = (bullet.kind, bullet.text, bullet.source)
        dup = seen.get(key, 0)
        seen[key] = dup + 1
        yield QueuedProposal(
            line=line_index,
            raw=raw,
            bullet=bullet,
            dup=dup,
            proposal_id=stable_proposal_id(
                workspace, rel_path, bullet.kind, bullet.text, bullet.source, dup
            ),
        )


def pending_proposal_ids(config: Any) -> set[str]:
    """Return proposal IDs without the expensive re-home evidence scan."""
    pending: set[str] = set()
    for workspace in config.workspace_names():
        root = Path(config.workspace_vault_root(workspace))
        queue = root.joinpath(*_PROPOSALS_REL)
        rel_path = Path(workspace).joinpath(*_PROPOSALS_REL).as_posix()
        if queue.is_file():
            for entry in walk_proposal_queue(
                workspace, rel_path, queue.read_text(encoding="utf-8")
            ):
                pending.add(entry.proposal_id)
                # The ordinal suffix is only a rendering detail. Keep the
                # unsuffixed identity as an alias so removing an earlier
                # duplicate cannot make the surviving proposal look settled.
                if entry.dup:
                    bullet = entry.bullet
                    pending.add(
                        stable_proposal_id(
                            workspace, rel_path, bullet.kind, bullet.text, bullet.source, 0
                        )
                    )
        skill_dir = root.joinpath(*_SKILL_PROPOSALS_REL)
        if skill_dir.is_dir():
            for proposal in skill_dir.glob("*.md"):
                pending.add(
                    stable_proposal_id(
                        workspace, rel_path, "skill", proposal.name, "", 0
                    )
                )
    return pending
