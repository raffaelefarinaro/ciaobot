"""The migration notices, each probed once and stated once.

Why this module exists
----------------------
Two surfaces answer "what has an upgrade left for this install?": the Home strip
(``operator_actions.detect_actions``, polled every 60s and on window focus) and
the ``upgrade_notices`` section of the OS audit (a diagnostic report, never red).
They used to hold their own copy of two conditions each, and the copies had
drifted in ways only one of them could see:

* **vault location.** One predicate, two implementations. The audit's remedy also
  taught the manual path — "identify which files are vault content, make a
  backup, move the approved content, atomically update the active workspace
  registry" — which is the path the engine refuses, that
  ``docs/VAULT_MIGRATION_PROMPT.md`` no longer teaches since #815, and that
  ``ciao vault-relocate`` exists to replace. Two surfaces, two wordings, and one
  of them wrong.
* **unmigrated links.** Two different *applicability* rules. Home asked for an
  adopted vault (``vault_mode == "existing"``) with no completed link-migration
  receipt; the audit asked only for the receipt, so on a scratch install the two
  surfaces answered different questions about the same vault. Applicability a
  surface decides for itself is a disagreement waiting to be filed as a bug, and
  the cheap fix for it — letting the surface that cannot afford the work declare
  the condition out of scope — would delete a true finding from a diagnostic.

So the condition, the applicability rule and the wording live here once, and both
surfaces reference them. What stays different is *cost*, and it is a named
argument rather than a private copy of the rules: :func:`unmigrated_links`
establishes the offending note only for a caller that can afford the walk.

The cost rule
-------------
Home detection runs on every app open and every window focus, so what Home may
call is the cheap half: read a receipt, compare two registry-resolved paths, and
``is_dir()`` each result. Nothing here opens a file under a vault. The one
expensive fact — which note still holds a wikilink — is the walk behind
``establish=True``, and the only caller that pays for it is the audit, which is a
full-install diagnostic that already reads every note. The preview a chat runs on
the button press costs the same walk, and it is the one that changes the vault.
``tests/test_migration_notices.py`` pins the Home bound by counting filesystem
accesses under the vault across a poll, not by timing it.

Audit truth, Home honesty
-------------------------
Sharing a probe must not turn the audit into a mirror of the Home strip. Home's
card is a pointer: it can say the retired dialect *may* be in use, because the
walk is not available to it, and it never claims a wikilink exists on the strength
of a receipt's absence. The audit gets the same finding with the example
established, so it keeps reporting a first offending note after any Home-side
decision — including a future dismissal once #800's catalog step makes this
optional and dismissible. No Home-side suppression is an input to these
functions, which is what keeps that true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: The audit's notice type for a workspace vault kept outside its standard folder.
VAULT_LOCATION_NOTICE = "vault_outside_vault_root"

#: The audit's notice type for a vault still written in the retired wikilink dialect.
UNMIGRATED_LINKS_NOTICE = "unmigrated_vault_links"


# -- vault location ----------------------------------------------------------


@dataclass(frozen=True)
class VaultLocationFinding:
    """One workspace whose registered vault is not where the layout says it is.

    The registry *is* the layout, so this is a comparison of two resolved paths
    rather than a scan, and the paths themselves are the whole of the evidence.
    Every sentence a surface shows about the condition is derived here, so the
    two cannot describe the same mismatch differently.
    """

    workspace: str
    actual: Path
    standard: Path

    @property
    def title(self) -> str:
        return f"The {self.workspace} vault is not in its standard folder"

    @property
    def detail(self) -> str:
        return (
            f"Workspace '{self.workspace}' keeps its vault at the nonstandard "
            f"location {self.actual}; its standard location is {self.standard}."
        )

    @property
    def remedy(self) -> str:
        """The managed command, and only the managed command.

        `vault-relocate` has an apply/undo cycle and updates the registry itself,
        so a remedy that describes moving the folder and hand-editing the
        registry teaches a path the engine refuses and the operator cannot
        reproduce. `--undo` restores the exact previous location and a restart is
        needed either way, so the way back and the restart belong to the same
        sentence as the way forward.
        """
        name = self.workspace
        return (
            f"Preview with `ciao vault-relocate {name}`, then apply with "
            f"`ciao vault-relocate {name} --apply`. It moves this workspace's own "
            "content into the standard folder, updates the registry, and can be "
            f"reversed exactly with `ciao vault-relocate {name} --undo`. "
            "Ciaobot needs a restart (Settings -> Restart) before the new location "
            "takes effect everywhere, including in an open chat."
        )


def vault_location_findings(config: Any) -> list[VaultLocationFinding]:
    """Every registered workspace whose vault sits outside its standard folder.

    A config without a workspace registry exposes none of the three accessors
    this needs, and there is nothing for it to report. A registry entry that
    cannot be resolved is skipped rather than raised: this is a notice, and a
    broken registry must not take an audit or a Home render down with it.
    """
    resolver = getattr(config, "workspace_vault_root", None)
    standardizer = getattr(config, "canonical_workspace_vault_root", None)
    lister = getattr(config, "workspace_names", None)
    if not callable(resolver) or not callable(standardizer) or not callable(lister):
        return []
    findings: list[VaultLocationFinding] = []
    for name in lister():
        try:
            actual = Path(resolver(name)).resolve()
            standard = Path(standardizer(name)).resolve()
        except Exception:  # noqa: BLE001 — advisory; a bad registry must not fail
            continue
        # A root that does not exist is a different notice (`workspace-root-missing`
        # on Home, and a setup finding in the audit): there is no misplaced vault
        # to relocate, and offering a move for a path that is not there is the
        # unactionable tile operators learn to ignore.
        if actual == standard or not actual.is_dir():
            continue
        findings.append(
            VaultLocationFinding(workspace=str(name), actual=actual, standard=standard)
        )
    return findings


# -- unmigrated links --------------------------------------------------------


@dataclass(frozen=True)
class LinksFinding:
    """An adopted vault with no completed link migration recorded against it.

    ``example`` is the first note still holding a wikilink, relative to the
    vault root, or ``""`` when the walk was not run and nothing is established.
    An empty example is not a clean vault — it is an unanswered question, and the
    wording below says exactly that.
    """

    vault_root: Path
    example: str = ""

    @property
    def established(self) -> bool:
        """Whether the walk actually ran, rather than whether it found nothing.

        Only a caller that asked for the walk may say the dialect is in use. This
        is what keeps the receipt's absence from becoming a claim: an adopted
        vault written in markdown links from the start satisfies every
        applicability condition and has nothing to convert.
        """
        return bool(self.example)

    @property
    def title(self) -> str:
        if self.established:
            return "The vault still uses the retired wikilink dialect"
        return "The vault may still use the retired wikilink dialect"

    @property
    def detail(self) -> str:
        if self.established:
            return (
                "The vault still uses `[[wikilinks]]`, which nothing reads any "
                "more: they are not graph edges, not backlinks, and not clickable "
                f"in the file viewer. First example: {self.example}."
            )
        return (
            "This vault was adopted and no link migration has been recorded, so it "
            "may still contain `[[wikilinks]]`, which nothing reads as graph edges, "
            "backlinks, or clickable links. The preview reports exactly what would "
            "change, and finds nothing if the vault is already clean."
        )

    @property
    def remedy(self) -> str:
        return (
            "Preview with `ciao vault-migrate-links` (dry-run by default), then "
            "apply with `ciao vault-migrate-links --apply`. Every rewrite is "
            "recorded, so `ciao vault-unmigrate-links --apply` restores the notes "
            "byte for byte."
        )


def unmigrated_links(
    config: Any, runtime_dir: Path | None, *, establish: bool = False
) -> LinksFinding | None:
    """Whether the retired wikilink dialect is still in scope for this install.

    Applicability, identical for every caller:

    * a runtime root to read the receipt from — a caller with none cannot know
      whether the migration ran, and guessing "unmigrated" would nag installs
      that did the work;
    * an **adopted** vault. A ``scratch`` vault is created conformant by this
      engine, so it is out of scope for a notice about a dialect it never had,
      and that is also what lets the Home card reach zero. A config that does not
      declare its mode is treated as a vault Ciaobot created, and a real
      ``CiaoConfig`` always declares it;
    * no **completed** migration receipt. ``read_receipt`` reports only a
      ``status == "migrated"`` run, so a receipt left by a run that could not
      write every note does not silence the notice — the vault is half-converted
      and the half is the finding.

    ``establish=False`` (Home) returns the finding with no example and reads no
    note: this function is on the poll path, and a walk there is the cost the
    whole Home contract is written against. ``establish=True`` (the audit)
    additionally runs the one walk, which stops at the first hit.
    """
    if runtime_dir is None:
        return None
    if str(getattr(config, "vault_mode", "") or "") != "existing":
        return None
    vault_root = getattr(config, "vault_root", None)
    if vault_root is None:
        return None
    try:
        from ciao.vault_migrate_links import has_unmigrated_links, read_receipt

        if read_receipt(runtime_dir) is not None:
            return None
        example = has_unmigrated_links(Path(vault_root)) if establish else ""
    except Exception:  # noqa: BLE001 — advisory; a broken receipt is not a notice
        logger.exception("migration notices: link-dialect check failed")
        return None
    return LinksFinding(vault_root=Path(vault_root), example=example)
